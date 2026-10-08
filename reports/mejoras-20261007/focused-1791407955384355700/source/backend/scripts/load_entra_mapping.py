# Creado por Aldo Garcia.
"""Carga el mapeo grupo/app-role de Entra ID a roles internos.

Lee ``config/authorization/entra-role-mapping.yaml`` (datos, validados por
esquema, nunca codigo) y sincroniza la tabla ``entra_group_role_mappings``.

Es idempotente: reejecutarlo no duplica filas. Con ``--prune`` elimina los mapeos
que ya no estan declarados en el archivo, lo que permite revocar un grupo
simplemente quitandolo del YAML.

Uso:
    python -m scripts.load_entra_mapping
    python -m scripts.load_entra_mapping --prune --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.common.errors import ConfigurationError
from app.common.ids import new_id, utcnow_naive
from app.common.logging import get_logger
from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import (
    CategoryPermission,
    EntraGroupRoleMapping,
    Permission,
    Role,
    RolePermission,
    StructuredSourcePermission,
)
from app.structured_data.sources import load_sources

logger = get_logger(__name__)


class MappingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_key: str = Field(min_length=1, max_length=255)
    role: str = Field(min_length=1, max_length=128)
    external_kind: str = "group"
    provider: str = "entra"
    comment: str = ""

    @field_validator("external_kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        if value not in ("group", "app_role"):
            raise ValueError("external_kind debe ser 'group' o 'app_role'")
        return value


class RoleDefinition(BaseModel):
    """Rol funcional declarado junto al mapeo Entra.

    Mantener roles, categorias y permisos en el mismo contrato evita que un
    grupo correctamente configurado apunte a un rol inexistente o vacio.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_]+$")
    description: str = Field(default="", max_length=512)
    categories: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)

    @field_validator("categories", "permissions")
    @classmethod
    def _unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("La lista no admite valores duplicados.")
        return values


class MappingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    roles: list[RoleDefinition] = Field(default_factory=list)
    mappings: list[MappingEntry] = Field(default_factory=list)

    @field_validator("roles")
    @classmethod
    def _unique_roles(cls, roles: list[RoleDefinition]) -> list[RoleDefinition]:
        names = [role.name for role in roles]
        if len(names) != len(set(names)):
            raise ValueError("Hay roles duplicados en el contrato Entra.")
        return roles

    @field_validator("mappings")
    @classmethod
    def _unique_mappings(cls, mappings: list[MappingEntry]) -> list[MappingEntry]:
        keys = [
            (entry.provider, entry.external_kind, entry.external_key)
            for entry in mappings
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Hay identificadores externos duplicados en el contrato Entra.")
        return mappings


#: Valores de ejemplo del archivo que se distribuye. Cargarlos seria peor que no
#: cargar nada: concederian un rol a un identificador inexistente.
PLACEHOLDER_KEYS = frozenset(
    {
        "00000000-0000-0000-0000-000000000000",
        "11111111-1111-1111-1111-111111111111",
    }
)


def load_mapping_file(path: Path | None = None) -> MappingFile:
    target = path or get_settings().entra_role_mapping_path
    if not target.exists():
        raise ConfigurationError(f"No existe el archivo de mapeo: {target}")
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        return MappingFile.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(
            f"El archivo de mapeo es invalido: {target.name}", detail=str(exc)
        ) from exc


def _sync_declared_roles(mapping_file: MappingFile, db: Session) -> dict[str, Role]:
    """Crea y reconcilia el contrato RBAC funcional de manera idempotente."""
    from app.authorization.categories import get_registry
    from app.common.ids import new_id, utcnow_naive

    registry = get_registry()
    known_permissions = {
        str(name)
        for name in db.execute(select(Permission.name)).scalars().all()
    }
    roles: dict[str, Role] = {}

    for definition in mapping_file.roles:
        unknown_categories = set(definition.categories) - set(registry.known())
        if unknown_categories:
            raise ConfigurationError(
                "El rol declara categorias que no existen: " + ", ".join(sorted(unknown_categories))
            )
        unknown_permissions = set(definition.permissions) - known_permissions
        if unknown_permissions:
            raise ConfigurationError(
                "El rol declara permisos que no existen: " + ", ".join(sorted(unknown_permissions))
            )

        role = db.execute(select(Role).where(Role.name == definition.name)).scalar_one_or_none()
        if role is None:
            role = Role(
                id=new_id(),
                name=definition.name,
                description=definition.description,
                is_test_role=False,
                created_at=utcnow_naive(),
            )
            db.add(role)
            db.flush()
        else:
            role.description = definition.description
            role.is_test_role = False

        existing_categories = db.execute(
            select(CategoryPermission).where(CategoryPermission.role_id == role.id)
        ).scalars().all()
        desired_categories = set(definition.categories)
        for existing_category in existing_categories:
            if (
                existing_category.is_wildcard
                or existing_category.access != "read"
                or existing_category.category not in desired_categories
            ):
                db.delete(existing_category)

        present_categories = {
            row.category
            for row in existing_categories
            if row.access == "read" and not row.is_wildcard and row.category in desired_categories
        }
        for category in sorted(desired_categories - present_categories):
            category_grant = db.execute(
                select(CategoryPermission).where(
                    CategoryPermission.role_id == role.id,
                    CategoryPermission.category == category,
                    CategoryPermission.access == "read",
                )
            ).scalar_one_or_none()
            if category_grant is None:
                db.add(
                    CategoryPermission(
                        id=new_id(),
                        role_id=role.id,
                        category=category,
                        access="read",
                        is_wildcard=False,
                        created_at=utcnow_naive(),
                    )
                )

        desired_permissions = set(definition.permissions)
        permission_rows = db.execute(
            select(Permission).where(Permission.name.in_(desired_permissions))
        ).scalars().all() if desired_permissions else []
        permission_by_name = {permission.name: permission for permission in permission_rows}
        desired_permission_ids = {permission.id for permission in permission_rows}
        db.execute(
            delete(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission_id.not_in(desired_permission_ids),
            )
        )
        for permission_name in sorted(desired_permissions):
            permission = permission_by_name[permission_name]
            exists = db.execute(
                select(RolePermission).where(
                    RolePermission.role_id == role.id,
                    RolePermission.permission_id == permission.id,
                )
            ).scalar_one_or_none()
            if exists is None:
                db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        roles[definition.name] = role

    db.flush()
    return roles


def _sync_declared_structured_sources(roles: dict[str, Role], db: Session) -> None:
    """Materializa en RBAC los ``allowed_roles`` declarados por cada conector.

    El YAML es el contrato operativo; la capa runtime sigue autorizando desde
    base de datos y nunca confia directamente en una lista enviada por cliente.
    """
    catalog = load_sources()
    desired_by_role: dict[str, set[str]] = {role_name: set() for role_name in roles}
    for source in catalog.sources.values():
        for role_name in source.allowed_roles:
            if role_name in desired_by_role:
                desired_by_role[role_name].add(source.name)

    for role_name, role in roles.items():
        desired_sources = desired_by_role[role_name]
        existing = db.execute(
            select(StructuredSourcePermission).where(
                StructuredSourcePermission.role_id == role.id
            )
        ).scalars().all()
        for grant in existing:
            if grant.source_name not in desired_sources:
                db.delete(grant)
        present = {grant.source_name for grant in existing if grant.source_name in desired_sources}
        for source_name in sorted(desired_sources - present):
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


def sync(*, prune: bool = False, dry_run: bool = False, allow_placeholders: bool = False) -> dict[str, int]:
    """Sincroniza el YAML con la tabla. Devuelve un resumen de cambios."""
    mapping_file = load_mapping_file()
    stats = {
        "declared": len(mapping_file.mappings),
        "declared_roles": len(mapping_file.roles),
        "created": 0,
        "skipped": 0,
        "pruned": 0,
    }

    with session_scope() as db:
        declared_roles = _sync_declared_roles(mapping_file, db)
        _sync_declared_structured_sources(declared_roles, db)
        declared_keys: set[tuple[str, str, str]] = set()

        for entry in mapping_file.mappings:
            if entry.external_key in PLACEHOLDER_KEYS and not allow_placeholders:
                logger.warning(
                    "entra_mapping.placeholder_skipped",
                    extra={"external_key_kind": entry.external_kind, "role_name": entry.role},
                )
                stats["skipped"] += 1
                continue

            role = declared_roles.get(entry.role)
            if role is None:
                role = db.execute(select(Role).where(Role.name == entry.role)).scalar_one_or_none()
            if role is None:
                raise ConfigurationError(
                    f"El rol '{entry.role}' no existe en la base. Cree el rol antes de mapearlo."
                )

            declared_keys.add((entry.provider, entry.external_kind, entry.external_key))
            exists = db.execute(
                select(EntraGroupRoleMapping).where(
                    EntraGroupRoleMapping.provider == entry.provider,
                    EntraGroupRoleMapping.external_kind == entry.external_kind,
                    EntraGroupRoleMapping.external_key == entry.external_key,
                    EntraGroupRoleMapping.role_id == role.id,
                )
            ).scalar_one_or_none()
            if exists is not None:
                continue
            if not dry_run:
                db.add(
                    EntraGroupRoleMapping(
                        id=new_id(),
                        provider=entry.provider,
                        external_key=entry.external_key,
                        external_kind=entry.external_kind,
                        role_id=role.id,
                        created_at=utcnow_naive(),
                    )
                )
            stats["created"] += 1

        if prune:
            current = db.execute(select(EntraGroupRoleMapping)).scalars().all()
            for row in current:
                key = (row.provider, row.external_kind, row.external_key)
                if key not in declared_keys:
                    if not dry_run:
                        db.execute(
                            delete(EntraGroupRoleMapping).where(EntraGroupRoleMapping.id == row.id)
                        )
                    stats["pruned"] += 1

        if dry_run:
            db.rollback()

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carga el mapeo de grupos de Entra ID")
    parser.add_argument("--prune", action="store_true", help="elimina mapeos no declarados en el YAML")
    parser.add_argument("--dry-run", action="store_true", help="no escribe; sólo informa")
    parser.add_argument(
        "--allow-placeholders",
        action="store_true",
        help="carga tambien los identificadores de ejemplo (solo para pruebas)",
    )
    args = parser.parse_args(argv)

    stats = sync(prune=args.prune, dry_run=args.dry_run, allow_placeholders=args.allow_placeholders)
    print(
        f"Mapeos declarados: {stats['declared']} | creados: {stats['created']} "
        f"| omitidos (marcadores de ejemplo): {stats['skipped']} | eliminados: {stats['pruned']}"
    )
    if stats["skipped"]:
        print(
            "Sustituya los object id de ejemplo en config/authorization/entra-role-mapping.yaml "
            "por los reales del tenant."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
