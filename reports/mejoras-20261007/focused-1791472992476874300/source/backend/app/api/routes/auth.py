# Creado por Aldo Garcia.
"""Rutas de autenticacion.

* ``GET  /auth/login``       -> inicia Entra ID cuando ``AUTH_PROVIDER=entra``.
* ``GET  /auth/callback``    -> callback OIDC.
* ``POST /auth/local/login`` -> **solo** con ``local_test`` en development/test.
* ``POST /auth/logout``      -> revoca la sesion de servidor.
* ``GET  /me``               -> perfil y token CSRF.

La ruta local no existe fuera de development/test: se comprueba en cada peticion
ademas de en la configuracion. Es defensa en profundidad frente a un despliegue
mal configurado.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_user_context, require_csrf
from app.api.schemas import LocalLoginRequest, MeResponse
from app.audit.service import AuditRecord, get_audit_service
from app.auth.provider import NormalizedIdentity, get_identity_provider
from app.auth.sessions import cookie_parameters, create_session, resolve_session, revoke_session
from app.authorization.context import ROLE_HCM_BASE, UserContext
from app.common.errors import ForbiddenError, RateLimitedError, UnauthorizedError
from app.common.ids import new_id, sha256_text, utcnow_naive
from app.common.logging import get_logger
from app.config import AuthProvider, get_settings
from app.database.models import EntraGroupRoleMapping, IdentityLink, Role, User, UserRole
from app.security.rate_limit import get_rate_limiter

logger = get_logger(__name__)
router = APIRouter(tags=["auth"])


def _client_key(request: Request, suffix: str = "") -> str:
    ip = request.client.host if request.client else "unknown"
    return f"login:{sha256_text(ip)}:{suffix}"


def _issue_session_cookie(response: Response, token: str) -> None:
    params = dict(cookie_parameters())
    key = str(params.pop("key"))
    response.set_cookie(key=key, value=token, **params)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Entra ID
# ---------------------------------------------------------------------------
@router.get("/auth/login")
def login(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """Inicia el flujo del proveedor configurado."""
    settings = get_settings()
    if settings.auth_provider not in (AuthProvider.ENTRA, AuthProvider.OIDC):
        # Con el proveedor local no hay redireccion federada: la UI muestra su
        # propio formulario.
        return RedirectResponse(url="/login", status_code=302)
    challenge = get_identity_provider().start_login(db)
    redirect = RedirectResponse(url=challenge.authorization_url, status_code=302)
    redirect.set_cookie(
        "matrixrh_oidc_state",
        challenge.state,
        max_age=600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/api/v1/auth",
    )
    return redirect


@router.get("/auth/callback")
def auth_callback(
    request: Request,
    response: Response,
    code: str = "",
    state: str = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Callback OIDC de Microsoft Entra ID."""
    settings = get_settings()
    if settings.auth_provider not in (AuthProvider.ENTRA, AuthProvider.OIDC):
        raise UnauthorizedError("El proveedor Entra ID no esta activo.")

    correlation = request.cookies.get("matrixrh_oidc_state", "")
    if not state or not correlation or not secrets.compare_digest(state, correlation):
        raise UnauthorizedError("La autenticacion no corresponde a este navegador.")
    provider = get_identity_provider()
    identity = provider.authenticate(db, code=code, state=state)
    user = _upsert_user_from_identity(db, identity)
    issued = create_session(db, user=user, auth_source=identity.auth_source)

    redirect = RedirectResponse(url="/", status_code=302)
    _issue_session_cookie(redirect, issued.session_token)
    redirect.delete_cookie("matrixrh_oidc_state", path="/api/v1/auth")
    get_audit_service().record(
        db,
        AuditRecord(
            request_id=getattr(request.state, "request_id", ""),
            event_type="auth.login",
            user_opaque_id=user.id,
            authorization_decision="ALLOW",
            status="ok",
        ),
    )
    return redirect


# ---------------------------------------------------------------------------
# Proveedor local de pruebas
# ---------------------------------------------------------------------------
@router.post("/auth/local/login", response_model=MeResponse)
def local_login(
    payload: LocalLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> MeResponse:
    """Login del proveedor local. Solo development/test."""
    settings = get_settings()
    if not settings.is_local_auth_allowed or settings.auth_provider is not AuthProvider.LOCAL_TEST:
        logger.warning("auth.local_login_blocked", extra={"app_env": str(settings.app_env)})
        raise ForbiddenError("El inicio de sesion local no esta habilitado en este entorno.")

    # Rate limiting por IP y por usuario: frena tanto el barrido de cuentas como
    # la fuerza bruta contra una cuenta concreta.
    limiter = get_rate_limiter()
    for key in (_client_key(request), _client_key(request, sha256_text(payload.username)[:16])):
        if not limiter.check(key, limit=settings.rate_limit_login_per_minute).allowed:
            raise RateLimitedError()

    provider = get_identity_provider()
    identity = provider.authenticate(db, username=payload.username, password=payload.password)
    user = _upsert_user_from_identity(db, identity)
    issued = create_session(db, user=user, auth_source=identity.auth_source)
    _issue_session_cookie(response, issued.session_token)

    from app.authorization.policy import get_policy_engine

    ctx = get_policy_engine().build_context(
        db, user=user, session_id=issued.session_id, request_id=getattr(request.state, "request_id", "")
    )
    get_audit_service().record(
        db,
        AuditRecord(
            request_id=ctx.request_id,
            event_type="auth.login",
            user_opaque_id=user.id,
            role_set_hash=ctx.role_set_hash,
            authorization_decision="ALLOW",
            status="ok",
        ),
    )
    return _me_payload(ctx, issued.csrf_token)


# ---------------------------------------------------------------------------
# Sesion
# ---------------------------------------------------------------------------
@router.post("/auth/logout", dependencies=[Depends(require_csrf)])
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, bool]:
    """Revoca la sesion y borra la cookie."""
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    try:
        record, user = resolve_session(db, token)
        revoke_session(db, record.id)
        get_audit_service().record(
            db,
            AuditRecord(
                request_id=getattr(request.state, "request_id", ""),
                event_type="auth.logout",
                user_opaque_id=user.id,
                status="ok",
            ),
        )
    except UnauthorizedError:
        # Cerrar una sesion ya invalida no es un error para el cliente.
        pass
    response.delete_cookie(key=settings.session_cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
def me(request: Request, ctx: UserContext = Depends(get_user_context)) -> MeResponse:
    """Perfil del usuario autenticado y token CSRF para peticiones mutantes."""
    csrf = getattr(request.state, "csrf_token", "")
    return _me_payload(ctx, csrf)


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------
def _me_payload(ctx: UserContext, csrf_token: str) -> MeResponse:
    profile = ctx.public_profile()

    def string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    return MeResponse(
        user_id=str(profile["user_id"]),
        username=str(profile["username"]),
        display_name=str(profile["display_name"]),
        auth_source=str(profile["auth_source"]),
        roles=string_list(profile["roles"]),
        permissions=string_list(profile["permissions"]),
        allowed_categories=string_list(profile["allowed_categories"]),
        category_wildcard=bool(profile["category_wildcard"]),
        csrf_token=csrf_token,
    )


def _upsert_user_from_identity(db: Session, identity: NormalizedIdentity) -> User:
    """Vincula la identidad del proveedor con el usuario logico de Matrix RH.

    Con ``local_test`` el usuario ya existe (lo creo el seed). Con Entra ID se
    crea en el primer login y se le asignan los roles que resultan del mapeo de
    grupos/app roles. Si ningun grupo mapea a un rol, el usuario queda **sin**
    permisos: deny-by-default tambien en el alta.
    """
    link = db.execute(
        select(IdentityLink).where(
            IdentityLink.provider == identity.auth_source,
            IdentityLink.subject_id == identity.subject_id,
        )
    ).scalar_one_or_none()

    if link is not None:
        user = db.get(User, link.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError()
        if identity.auth_source in ("entra", "oidc"):
            _sync_entra_roles(db, user, identity)
            # Un cambio/revocacion del correo en el IdP debe alcanzar los
            # filtros SQL del siguiente contexto firmado, sin reutilizar el
            # atributo de la primera vinculacion. No se toma del navegador.
            user.email = identity.email
            db.flush()
        return user

    user = db.execute(select(User).where(User.username == identity.username)).scalar_one_or_none()
    if user is not None and (not user.is_active or identity.auth_source != "local_test"):
        # No vincular automaticamente por nombre/correo una identidad federada
        # a una cuenta previa. TI debe migrar explicitamente la identidad.
        raise UnauthorizedError("La identidad requiere vinculacion por TI.")
    if user is None:
        now = utcnow_naive()
        user = User(
            id=new_id(),
            username=identity.username,
            display_name=identity.display_name,
            email=identity.email,
            auth_source=identity.auth_source,
            is_active=True,
            is_synthetic_test=False,
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        db.flush()

    db.add(
        IdentityLink(
            id=new_id(),
            user_id=user.id,
            provider=identity.auth_source,
            subject_id=identity.subject_id,
            tenant_id=(get_settings().entra_tenant_id if identity.auth_source == "entra" else None),
            created_at=utcnow_naive(),
        )
    )
    db.flush()
    if identity.auth_source in ("entra", "oidc"):
        _sync_entra_roles(db, user, identity)
    return user


def _sync_entra_roles(db: Session, user: User, identity: NormalizedIdentity) -> None:
    """Sincroniza, no solo agrega, los roles gobernados por Entra ID.

    Los perfiles de la matriz RH son acumulativos mientras sus grupos sigan
    presentes. Si AD retira un grupo, el siguiente login debe retirar tambien
    el rol interno correspondiente; conservarlo seria una escalada de
    privilegios por autorizacion obsoleta. Los roles que no estan gobernados por
    ningun mapeo Entra quedan intactos para no mezclar este flujo con cuentas o
    concesiones de otros proveedores.
    """
    keys = frozenset((*identity.groups, *identity.roles))
    managed_role_ids = set(
        db.execute(select(EntraGroupRoleMapping.role_id).where(EntraGroupRoleMapping.provider == identity.auth_source))
        .scalars()
        .all()
    )
    mapped: Sequence[EntraGroupRoleMapping] = ()
    if keys:
        mapped = (
            db.execute(
                select(EntraGroupRoleMapping).where(
                    EntraGroupRoleMapping.provider == identity.auth_source,
                    or_(
                        and_(
                            EntraGroupRoleMapping.external_kind == "group",
                            EntraGroupRoleMapping.external_key.in_(identity.groups),
                        ),
                        and_(
                            EntraGroupRoleMapping.external_kind == "app_role",
                            EntraGroupRoleMapping.external_key.in_(identity.roles),
                        ),
                    ),
                )
            )
            .scalars()
            .all()
        )
    desired_role_ids = {mapping.role_id for mapping in mapped}
    base_role = db.execute(select(Role).where(Role.name == ROLE_HCM_BASE)).scalar_one_or_none()
    base_role_id = base_role.id if base_role is not None else None
    specialized_role_ids = desired_role_ids - ({base_role_id} if base_role_id else set())
    if specialized_role_ids and (base_role_id is None or base_role_id not in desired_role_ids):
        logger.warning("auth.entra.base_profile_missing")
        desired_role_ids = set()
    existing = set(db.execute(select(UserRole.role_id).where(UserRole.user_id == user.id)).scalars().all())

    revoked = (existing & managed_role_ids) - desired_role_ids
    if revoked:
        db.execute(
            delete(UserRole).where(
                UserRole.user_id == user.id,
                UserRole.role_id.in_(revoked),
            )
        )

    for role_id in sorted(desired_role_ids):
        if role_id not in existing:
            db.add(UserRole(user_id=user.id, role_id=role_id, granted_at=utcnow_naive()))
    db.flush()
