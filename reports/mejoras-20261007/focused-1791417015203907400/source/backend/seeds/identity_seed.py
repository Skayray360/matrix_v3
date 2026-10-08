# Creado por Aldo Garcia.
"""Seed idempotente de identidad de prueba (seccion 27.1).

Crea roles, permisos, politicas de categoria y las dos cuentas sinteticas:

* ``Matrix``   -> ``matrix_admin_test`` (wildcard de negocio)
* ``MatrixR1`` -> ``prestaciones_reader_test`` (solo prestaciones)

El instalador genera MATRIX_SEED_PASSWORD al crear la configuracion. Las cuentas
ya creadas conservan sus hashes y nunca se reinician por ejecutar este seed.
Su identidad solo puede utilizarse en development/test y con escucha loopback.

La contrasena **nunca** se almacena en claro: se guarda exclusivamente un hash
Argon2id.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password
from app.authorization.categories import SEED_CATEGORIES
from app.authorization.context import (
    PERM_DIAGNOSTICS_READ,
    PERM_KNOWLEDGE_ADMIN,
    PERM_STRUCTURED_QUERY,
    PERM_USERS_ADMIN,
    ROLE_BUSINESS_ADMIN,
    ROLE_PRESTACIONES_READER,
)
from app.common.errors import ConfigurationError
from app.common.ids import new_id, utcnow_naive
from app.common.logging import get_logger
from app.config import AppEnv, get_settings
from app.database.models import (
    CategoryPermission,
    IdentityLink,
    LocalCredential,
    Permission,
    Role,
    RolePermission,
    StructuredSourcePermission,
    User,
    UserRole,
)

logger = get_logger(__name__)

#: Fixture de pruebas automatizadas. El seed de instalacion no la usa por defecto.
SYNTHETIC_TEST_PASSWORD = "Matrix RH"  # noqa: S105 - credencial de prueba declarada

PERMISSIONS: tuple[tuple[str, str], ...] = (
    (PERM_KNOWLEDGE_ADMIN, "Publicar y reconciliar conocimiento corporativo"),
    (PERM_USERS_ADMIN, "Administrar usuarios y roles"),
    (PERM_STRUCTURED_QUERY, "Consultar fuentes de datos estructuradas autorizadas"),
    (PERM_DIAGNOSTICS_READ, "Consultar el diagnostico administrativo (sin secretos)"),
)


@dataclass(frozen=True, slots=True)
class SeedUser:
    username: str
    display_name: str
    role_name: str


SEED_USERS: tuple[SeedUser, ...] = (
    SeedUser("Matrix", "Matrix (administrador de prueba)", ROLE_BUSINESS_ADMIN),
    SeedUser("MatrixR1", "MatrixR1 (prestaciones)", ROLE_PRESTACIONES_READER),
)


def _get_or_create_role(db: Session, name: str, description: str) -> Role:
    role = db.execute(select(Role).where(Role.name == name)).scalar_one_or_none()
    if role is None:
        role = Role(
            id=new_id(), name=name, description=description, is_test_role=True, created_at=utcnow_naive()
        )
        db.add(role)
        db.flush()
    return role


def _get_or_create_permission(db: Session, name: str, description: str) -> Permission:
    permission = db.execute(select(Permission).where(Permission.name == name)).scalar_one_or_none()
    if permission is None:
        permission = Permission(id=new_id(), name=name, description=description)
        db.add(permission)
        db.flush()
    return permission


def _grant_permission(db: Session, role: Role, permission: Permission) -> None:
    exists = db.execute(
        select(RolePermission).where(
            RolePermission.role_id == role.id, RolePermission.permission_id == permission.id
        )
    ).scalar_one_or_none()
    if exists is None:
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        db.flush()


def _grant_category(db: Session, role: Role, category: str, *, wildcard: bool = False) -> None:
    exists = db.execute(
        select(CategoryPermission).where(
            CategoryPermission.role_id == role.id,
            CategoryPermission.category == category,
            CategoryPermission.access == "read",
        )
    ).scalar_one_or_none()
    if exists is None:
        db.add(
            CategoryPermission(
                id=new_id(),
                role_id=role.id,
                category=category,
                access="read",
                is_wildcard=wildcard,
                created_at=utcnow_naive(),
            )
        )
        db.flush()


def _grant_source(db: Session, role: Role, source_name: str) -> None:
    exists = db.execute(
        select(StructuredSourcePermission).where(
            StructuredSourcePermission.role_id == role.id,
            StructuredSourcePermission.source_name == source_name,
        )
    ).scalar_one_or_none()
    if exists is None:
        db.add(
            StructuredSourcePermission(
                id=new_id(),
                role_id=role.id,
                source_name=source_name,
                allowed_entities=None,
                created_at=utcnow_naive(),
            )
        )
        db.flush()


def seed_roles_and_permissions(db: Session) -> dict[str, Role]:
    """Crea roles, permisos y politicas de categoria de forma idempotente."""
    permissions = {name: _get_or_create_permission(db, name, desc) for name, desc in PERMISSIONS}

    admin = _get_or_create_role(
        db,
        ROLE_BUSINESS_ADMIN,
        "Administrador de negocio de prueba: wildcard sobre categorias de negocio.",
    )
    reader = _get_or_create_role(
        db,
        ROLE_PRESTACIONES_READER,
        "Lector restringido: unicamente la categoria prestaciones.",
    )

    # Administrador de negocio: conocimiento, usuarios, consultas estructuradas y
    # diagnostico. NO incluye secretos ni SQL arbitrario: esos no existen como
    # permiso en el sistema.
    for name in (PERM_KNOWLEDGE_ADMIN, PERM_USERS_ADMIN, PERM_STRUCTURED_QUERY, PERM_DIAGNOSTICS_READ):
        _grant_permission(db, admin, permissions[name])

    # El lector restringido no recibe ningun permiso administrativo. Tampoco
    # ``structured.query``: por defecto las fuentes estructuradas le estan
    # denegadas (matriz de la seccion 6.1).

    # Wildcard de negocio: se declara como politica explicita, no como ausencia
    # de filtro. El motor la resuelve en una lista enumerada de categorias.
    _grant_category(db, admin, "*", wildcard=True)
    # Concesion nominal ademas del wildcard: deja constancia de las semillas.
    for category in SEED_CATEGORIES:
        _grant_category(db, admin, category)

    _grant_category(db, reader, "prestaciones")
    _grant_source(db, admin, "rh_demo")

    # Los roles funcionales HCM_* viven en el contrato declarativo de Entra.
    # Se crean durante el mismo seed para que el primer login federado nunca
    # encuentre un mapeo apuntando a un rol inexistente.
    from scripts.load_entra_mapping import (
        _sync_declared_roles,
        _sync_declared_structured_sources,
        load_mapping_file,
    )

    hcm_roles = _sync_declared_roles(load_mapping_file(), db)
    # El catalogo tambien declara al administrador sintetico local. Incluirlo
    # aqui mantiene la autorizacion efectiva de BD alineada con sources.yaml;
    # el runtime nunca confia directamente en el YAML ni en el frontend.
    roles_with_structured_sources = {**hcm_roles, ROLE_BUSINESS_ADMIN: admin}
    _sync_declared_structured_sources(roles_with_structured_sources, db)

    return {ROLE_BUSINESS_ADMIN: admin, ROLE_PRESTACIONES_READER: reader}


def seed_test_users(db: Session, *, password: str | None = None) -> list[str]:
    """Crea cuentas con un secreto configurado; nunca cambia credenciales existentes."""
    settings = get_settings()
    if settings.app_env not in (AppEnv.DEVELOPMENT, AppEnv.TEST):
        raise ConfigurationError(
            "El seed de usuarios sinteticos solo se permite en development/test."
        )
    if not settings.local_test_seed_users_enabled:
        logger.info("seed.skipped", extra={"reason": "LOCAL_TEST_SEED_USERS_ENABLED=false"})
        return []

    roles = seed_roles_and_permissions(db)
    created: list[str] = []

    for seed_user in SEED_USERS:
        user = db.execute(
            select(User).where(User.username == seed_user.username)
        ).scalar_one_or_none()
        now = utcnow_naive()
        if user is None:
            user = User(
                id=new_id(),
                username=seed_user.username,
                display_name=seed_user.display_name,
                email=None,
                auth_source="local_test",
                is_active=True,
                is_synthetic_test=True,
                created_at=now,
                updated_at=now,
            )
            db.add(user)
            db.flush()
            created.append(seed_user.username)
        elif user.auth_source != "local_test" or not user.is_synthetic_test:
            raise ConfigurationError("El nombre de una cuenta de seed pertenece a una identidad distinta.")

        credential = db.get(LocalCredential, user.id)
        if credential is None:
            seed_password = password if password is not None else settings.matrix_seed_password.get_secret_value()
            if not seed_password or (
                settings.app_env is not AppEnv.TEST and not 16 <= len(seed_password) <= 128
            ):
                raise ConfigurationError(
                    "Crear credenciales de prueba requiere MATRIX_SEED_PASSWORD aleatoria "
                    "de 16 a 128 caracteres; las credenciales existentes no se modifican."
                )
            db.add(
                LocalCredential(
                    user_id=user.id,
                    password_hash_argon2id=hash_password(seed_password),
                    failed_attempts=0,
                    locked_until=None,
                    updated_at=now,
                )
            )
            db.flush()

        link = db.execute(
            select(IdentityLink).where(
                IdentityLink.provider == "local_test", IdentityLink.subject_id == user.id
            )
        ).scalar_one_or_none()
        if link is None:
            db.add(
                IdentityLink(
                    id=new_id(),
                    user_id=user.id,
                    provider="local_test",
                    subject_id=user.id,
                    tenant_id=None,
                    created_at=now,
                )
            )
            db.flush()

        role = roles[seed_user.role_name]
        assigned = db.execute(
            select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
        ).scalar_one_or_none()
        if assigned is None:
            db.add(UserRole(user_id=user.id, role_id=role.id, granted_at=now))
            db.flush()

    logger.info(
        "seed.completed",
        extra={"created_users": created, "total_seed_users": len(SEED_USERS)},
    )
    return created


def run() -> list[str]:
    """Entrada del seed desde scripts e instalador."""
    from app.database.engine import session_scope

    with session_scope() as db:
        return seed_test_users(db)


if __name__ == "__main__":  # pragma: no cover
    created_users = run()
    print(f"Seed completado. Usuarios creados: {created_users or '(ninguno nuevo)'}")
