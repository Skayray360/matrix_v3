# Creado por Aldo Garcia.
"""Dependencias de FastAPI: sesion de BD, contexto de usuario y CSRF.

``get_user_context`` es el punto por el que pasa **toda** ruta autenticada. El
contexto se construye desde la cookie de sesion y las politicas de la base de
datos; ningun dato de identidad o permisos proviene del cuerpo o de las cabeceras
que envia el navegador.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.sessions import resolve_session
from app.authorization.context import UserContext
from app.authorization.policy import get_policy_engine
from app.common.errors import ForbiddenError, UnauthorizedError
from app.common.ids import sha256_text
from app.common.logging import get_logger
from app.config import get_settings
from app.database.engine import get_sessionmaker
from app.database.models import SessionRecord

logger = get_logger(__name__)

CSRF_HEADER = "X-CSRF-Token"
#: Metodos que no mutan estado y por tanto no exigen token CSRF.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_db(request: Request) -> Iterator[Session]:
    """Sesion de base de datos por request, con commit/rollback automatico."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _client_fingerprint(request: Request) -> str:
    """Huella debil del cliente para auditoria.

    Se guarda **hasheada**: sirve para detectar un salto brusco de cliente en una
    sesion, pero no almacena la IP ni el user agent en claro.
    """
    ip = request.client.host if request.client else ""
    agent = request.headers.get("user-agent", "")
    return sha256_text(f"{ip}|{agent}")


def get_session_record(request: Request, db: Session = Depends(get_db)) -> tuple[SessionRecord, object]:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    return resolve_session(db, token)


def get_user_context(
    request: Request,
    db: Session = Depends(get_db),
) -> UserContext:
    """Resuelve la sesion y construye el ``UserContext`` firmado."""
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    record, user = resolve_session(db, token)

    request_id = getattr(request.state, "request_id", "")
    context = get_policy_engine().build_context(
        db, user=user, session_id=record.id, request_id=request_id
    )
    # Verificacion inmediata de la firma: cualquier construccion fuera del motor
    # de politicas queda descartada aqui.
    context.require_valid(settings.app_secret_key.get_secret_value())

    request.state.user_context = context
    request.state.csrf_token = record.csrf_token
    return context


def require_csrf(request: Request, db: Session = Depends(get_db)) -> None:
    """Valida el token CSRF en metodos mutantes.

    Se compara contra el token almacenado en la sesion del servidor: un atacante
    que provoque una peticion cross-site no puede leer ese valor porque la cookie
    es ``HttpOnly`` y la respuesta de ``/me`` esta sujeta a la politica de origen.
    """
    if request.method in SAFE_METHODS:
        return
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    record, _ = resolve_session(db, token)
    provided = request.headers.get(CSRF_HEADER, "")
    # Bytes evita TypeError si una cabecera contiene caracteres no ASCII.
    if not provided or not secrets.compare_digest(
        provided.encode("utf-8"), record.csrf_token.encode("utf-8")
    ):
        logger.warning("security.csrf_rejected", extra={"http_method": request.method})
        raise ForbiddenError("Token CSRF invalido o ausente.")


def require_permission(permission: str):
    """Fabrica de dependencias que exige un permiso concreto."""

    def _checker(ctx: UserContext = Depends(get_user_context)) -> UserContext:
        if not ctx.has_permission(permission):
            logger.warning(
                "authorization.permission_denied",
                extra={
                    "user_opaque_id": ctx.user_id,
                    "required_permission": permission,
                    "authorization_decision": "DENY",
                },
            )
            raise ForbiddenError()
        return ctx

    return _checker


def get_optional_context(request: Request, db: Session = Depends(get_db)) -> UserContext | None:
    """Contexto si hay sesion valida, ``None`` si no. Usado por ``/me`` publico."""
    try:
        return get_user_context(request, db)
    except UnauthorizedError:
        return None
