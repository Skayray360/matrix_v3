# Creado por Aldo Garcia.
"""Administracion de cuentas locales reales sin alterar identidades federadas.

Las funciones participan en la transaccion del llamador. Las credenciales solo
se almacenan como Argon2id; bootstrap conserva cuentas y hashes existentes.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password
from app.auth.sessions import revoke_all_user_sessions
from app.authorization.context import PERM_DIAGNOSTICS_READ, PERM_KNOWLEDGE_ADMIN, PERM_USERS_ADMIN
from app.common.errors import ConfigurationError, ValidationFailedError
from app.common.ids import new_id, utcnow_naive
from app.config import AuthProvider, get_settings
from app.database.models import (
    CategoryPermission,
    IdentityLink,
    LocalCredential,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)

LOCAL_ADMIN_ROLE = "matrix_admin"
LOCAL_READER_ROLE = "prestaciones_reader"
LOCAL_ROLES = frozenset({LOCAL_ADMIN_ROLE, LOCAL_READER_ROLE})


def require_local_provider() -> None:
    if get_settings().auth_provider is not AuthProvider.LOCAL:
        raise ConfigurationError("La administracion de cuentas reales requiere AUTH_PROVIDER=local.")


def validate_password(password: str) -> None:
    """Permite frases largas sin reglas de composicion que reducen su utilidad."""
    if (not isinstance(password, str) or not 16 <= len(password) <= 128
            or password != password.strip() or any(ord(char) < 32 or ord(char) == 127 for char in password)
            or len(set(password)) < 5):
        raise ValidationFailedError(
            "Use una contrasena de 16 a 128 caracteres, sin controles ni espacios al inicio o al final."
        )


def validate_username(username: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", username):
        raise ValidationFailedError("El usuario admite letras, numeros, punto, guion, guion bajo y @.")


def ensure_local_roles(db: Session) -> dict[str, Role]:
    """Crea roles reales: administrador de negocio y lector de prestaciones."""
    roles: dict[str, Role] = {}
    for name, description in (
        (LOCAL_ADMIN_ROLE, "Administrador local de usuarios, conocimiento y diagnostico."),
        (LOCAL_READER_ROLE, "Lector local de documentos de prestaciones."),
    ):
        role = db.execute(select(Role).where(Role.name == name).with_for_update()).scalar_one_or_none()
        if role is None:
            role = Role(id=new_id(), name=name, description=description, is_test_role=False)
            db.add(role)
            db.flush()
        elif role.is_test_role:
            raise ConfigurationError("Un rol local real colisiona con un rol de prueba.")
        roles[name] = role

    admin = roles[LOCAL_ADMIN_ROLE]
    for name, description in (
        (PERM_KNOWLEDGE_ADMIN, "Administrar conocimiento corporativo autorizado"),
        (PERM_USERS_ADMIN, "Administrar usuarios locales"),
        (PERM_DIAGNOSTICS_READ, "Consultar diagnostico sin secretos"),
    ):
        permission = db.execute(select(Permission).where(Permission.name == name)).scalar_one_or_none()
        if permission is None:
            permission = Permission(id=new_id(), name=name, description=description)
            db.add(permission)
            db.flush()
        if db.get(RolePermission, (admin.id, permission.id)) is None:
            db.add(RolePermission(role_id=admin.id, permission_id=permission.id))

    for role, category, wildcard in (
        (admin, "*", True), (roles[LOCAL_READER_ROLE], "prestaciones", False),
    ):
        grant = db.execute(select(CategoryPermission).where(
            CategoryPermission.role_id == role.id, CategoryPermission.category == category,
            CategoryPermission.access == "read",
        )).scalar_one_or_none()
        if grant is None:
            db.add(CategoryPermission(
                id=new_id(), role_id=role.id, category=category, access="read", is_wildcard=wildcard,
            ))
    db.flush()
    return roles


def create_local_account(
    db: Session, *, username: str, display_name: str, password: str, role_name: str,
) -> User:
    require_local_provider()
    validate_username(username)
    validate_password(password)
    if role_name not in LOCAL_ROLES:
        raise ValidationFailedError("El rol local solicitado no esta permitido.")
    if not display_name.strip() or len(display_name) > 256:
        raise ValidationFailedError("Indique un nombre de hasta 256 caracteres.")
    if db.execute(select(User.id).where(User.username == username)).first() is not None:
        raise ValidationFailedError("El nombre de usuario ya esta registrado.")
    roles = ensure_local_roles(db)
    now = utcnow_naive()
    user = User(
        id=new_id(), username=username, display_name=display_name.strip(), auth_source="local",
        is_active=True, is_synthetic_test=False, created_at=now, updated_at=now,
    )
    db.add(user)
    db.flush()
    db.add_all([
        LocalCredential(user_id=user.id, password_hash_argon2id=hash_password(password), updated_at=now),
        IdentityLink(id=new_id(), user_id=user.id, provider="local", subject_id=user.id, created_at=now),
        UserRole(user_id=user.id, role_id=roles[role_name].id, granted_at=now),
    ])
    db.flush()
    return user


def bootstrap_administrator(db: Session, *, username: str, password: str) -> tuple[User, bool]:
    """Crea una sola cuenta inicial; una reinstalacion no eleva ni reactiva cuentas."""
    require_local_provider()
    validate_username(username)
    user = db.execute(select(User).where(User.username == username).with_for_update()).scalar_one_or_none()
    if user is not None:
        role = db.execute(select(Role).join(UserRole, UserRole.role_id == Role.id).where(
            UserRole.user_id == user.id, Role.name == LOCAL_ADMIN_ROLE, Role.is_test_role.is_(False),
        )).scalar_one_or_none()
        link = db.execute(select(IdentityLink).where(
            IdentityLink.provider == "local", IdentityLink.subject_id == user.id, IdentityLink.user_id == user.id,
        )).scalar_one_or_none()
        if (user.auth_source != "local" or user.is_synthetic_test or not user.is_active
                or role is None or link is None or db.get(LocalCredential, user.id) is None):
            raise ConfigurationError("La cuenta inicial existente requiere revision; no se cambia su identidad.")
        return user, False
    if db.execute(select(User.id).join(UserRole, UserRole.user_id == User.id).join(Role).where(
        User.auth_source == "local", Role.name == LOCAL_ADMIN_ROLE,
    )).first() is not None:
        raise ConfigurationError("Ya existe un administrador local; bootstrap no crea una segunda cuenta inicial.")
    return create_local_account(
        db, username=username, display_name=username, password=password, role_name=LOCAL_ADMIN_ROLE,
    ), True


def replace_local_password(db: Session, *, user: User, password: str) -> int:
    """Solo despues de reautenticacion HTTP o de autorizacion local del operador."""
    require_local_provider()
    validate_password(password)
    if user.auth_source != "local" or user.is_synthetic_test or not user.is_active:
        raise ValidationFailedError("La cuenta no es una identidad local activa.")
    credential = db.execute(select(LocalCredential).where(
        LocalCredential.user_id == user.id,
    ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if credential is None:
        raise ConfigurationError("La cuenta local no contiene una credencial valida.")
    credential.password_hash_argon2id = hash_password(password)
    credential.failed_attempts = 0
    credential.locked_until = None
    credential.updated_at = utcnow_naive()
    db.flush()
    return revoke_all_user_sessions(db, user.id)
