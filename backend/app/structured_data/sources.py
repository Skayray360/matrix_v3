# Creado por Aldo Garcia.
"""Configuracion declarativa de fuentes estructuradas.

``config/data_sources/sources.yaml`` define cada fuente: motor, variable de
entorno con el DSN (nunca el DSN), entidades permitidas, columnas permitidas,
filtros organizacionales obligatorios, timeout y maximo de filas.

El archivo **no contiene secretos**: ``secret_ref`` es el nombre de la variable de
entorno. Si la variable no existe, la fuente queda ``PREPARED_NOT_CONNECTED`` y el
sistema lo declara honestamente en lugar de simular una conexion.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth.provider import IntegrationStatus
from app.common.errors import ConfigurationError
from app.common.logging import get_logger
from app.config import get_settings
from app.structured_data.schemas import IDENTIFIER_RE

logger = get_logger(__name__)

SUPPORTED_ENGINES = frozenset({"mysql", "mariadb", "postgresql", "sqlserver", "oracle", "sqlite"})


class EntityConfig(BaseModel):
    """Entidad expuesta: se mapea a una tabla o, preferiblemente, a una vista de seguridad."""

    model_config = ConfigDict(extra="forbid")

    name: str
    table: str
    description: str = ""
    allowed_columns: list[str] = Field(default_factory=list)
    #: Filtros que SIEMPRE se anaden a la consulta (alcance organizacional).
    required_filters: dict[str, str] = Field(default_factory=dict)
    #: Igualdades obligatorias resueltas exclusivamente desde UserContext firmado.
    #: No admite expresiones, placeholders ni atributos de un payload del cliente.
    required_user_filters: dict[str, Literal["user.user_id", "user.username", "user.email"]] = Field(
        default_factory=dict
    )
    max_rows: int = Field(default=200, ge=1, le=10000)

    @field_validator("name", "table")
    @classmethod
    def _check_identifier(cls, value: str) -> str:
        if not IDENTIFIER_RE.match(value):
            raise ValueError(f"Identificador invalido: {value!r}")
        return value

    @field_validator("allowed_columns")
    @classmethod
    def _check_columns(cls, value: list[str]) -> list[str]:
        for column in value:
            if not IDENTIFIER_RE.match(column):
                raise ValueError(f"Columna invalida: {column!r}")
        return value

    @field_validator("required_user_filters")
    @classmethod
    def _check_user_filter_columns(
        cls, value: dict[str, Literal["user.user_id", "user.username", "user.email"]]
    ) -> dict[str, Literal["user.user_id", "user.username", "user.email"]]:
        if len(value) > 10 or any(not IDENTIFIER_RE.fullmatch(column) for column in value):
            raise ValueError("Columnas invalidas o demasiados filtros de identidad.")
        return value


class SourceConfig(BaseModel):
    """Definicion de una fuente estructurada."""

    model_config = ConfigDict(extra="forbid")

    name: str
    engine: str
    description: str = ""
    enabled: bool = False
    #: Nombre de la variable de entorno que contiene el DSN read-only.
    secret_ref: str = ""
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    max_rows: int = Field(default=200, ge=1, le=10000)
    #: Roles de Matrix RH autorizados a consultar la fuente.
    allowed_roles: list[str] = Field(default_factory=list)
    #: Alcance por usuario por defecto. Una vista de seguridad por rol requiere
    #: aprobacion nominal de la tabla/vista y no se infiere de un rol amplio.
    row_scope: Literal["user", "role_view"] = "user"
    approved_security_views: list[str] = Field(default_factory=list)
    entities: list[EntityConfig] = Field(default_factory=list)

    @field_validator("approved_security_views")
    @classmethod
    def _check_security_views(cls, value: list[str]) -> list[str]:
        if any(not IDENTIFIER_RE.fullmatch(name) for name in value):
            raise ValueError("Vista de seguridad invalida.")
        return value

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not IDENTIFIER_RE.match(value):
            raise ValueError(f"Nombre de fuente invalido: {value!r}")
        return value

    @field_validator("engine")
    @classmethod
    def _check_engine(cls, value: str) -> str:
        if value not in SUPPORTED_ENGINES:
            raise ValueError(f"Motor no soportado: {value}. Soportados: {sorted(SUPPORTED_ENGINES)}")
        return value

    def entity(self, name: str) -> EntityConfig | None:
        return next((e for e in self.entities if e.name == name), None)

    def dsn(self) -> str | None:
        """Lee el DSN de la variable de entorno referenciada. Nunca se persiste."""
        if not self.secret_ref:
            return None
        return os.environ.get(self.secret_ref) or None

    def status(self) -> IntegrationStatus:
        """Estado declarativo; tener DSN no prueba conectividad ni permisos."""
        if not self.enabled:
            return IntegrationStatus.DISABLED
        # Solo ReadOnlySourceAdapter.health_check ejecuta una comprobacion.
        # Este metodo se usa en diagnosticos de solo lectura de configuracion.
        return IntegrationStatus.PREPARED_NOT_CONNECTED


class SourcesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    sources: list[SourceConfig] = Field(default_factory=list)


@dataclass(frozen=True)
class SourceCatalog:
    """Catalogo consultable de fuentes."""

    sources: dict[str, SourceConfig]

    def get(self, name: str) -> SourceConfig | None:
        return self.sources.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.sources))

    def enabled_names(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, s in self.sources.items() if s.enabled))

    def status_report(self) -> list[dict[str, str]]:
        return [
            {
                "name": source.name,
                "engine": source.engine,
                "status": str(source.status()),
                "secret_ref": source.secret_ref,
            }
            for source in sorted(self.sources.values(), key=lambda s: s.name)
        ]


def load_sources(path: Path | None = None) -> SourceCatalog:
    """Carga y valida el YAML de fuentes."""
    target = path or get_settings().structured_sources_path
    if not target.exists():
        logger.warning("structured.sources_file_missing", extra={"sources_path": str(target)})
        return SourceCatalog(sources={})
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        parsed = SourcesFile.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(
            f"El archivo de fuentes estructuradas es invalido: {target.name}", detail=str(exc)
        ) from exc
    return SourceCatalog(sources={s.name: s for s in parsed.sources})


_catalog_lock = RLock()


@lru_cache(maxsize=1)
def _cached_source_catalog() -> SourceCatalog:
    return load_sources()


def get_source_catalog() -> SourceCatalog:
    with _catalog_lock:
        return _cached_source_catalog()


def refresh_source_catalog() -> SourceCatalog:
    with _catalog_lock:
        _cached_source_catalog.cache_clear()
        return _cached_source_catalog()


def authorization_catalog_fingerprint(source_names: frozenset[str]) -> str:
    """Huella del contrato vigente para invalidar memoria derivada de SQL.

    Incluye deshabilitaciones, cambios de entidad, columnas y filtros. Nunca lee
    DSN ni credenciales: model_dump solo serializa referencias declarativas.
    """
    catalog = get_source_catalog()
    payload = {
        name: source.model_dump(mode="json") if (source := catalog.get(name)) is not None else None
        for name in sorted(source_names)
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
