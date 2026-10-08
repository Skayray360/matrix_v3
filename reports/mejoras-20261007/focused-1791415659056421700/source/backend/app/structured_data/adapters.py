# Creado por Aldo Garcia.
"""Adapters de conexion read-only por motor.

Se entregan adapters **reales** para MySQL/MariaDB, PostgreSQL, SQL Server y
Oracle. Los drivers de motores que no se usan en el entorno local son extras
opcionales del paquete (``pip install .[oracle]`` etc.), pero el adapter, su
validacion de configuracion, su health check y sus pruebas de contrato existen
siempre (seccion 37.1).

Reglas comunes a todos los adapters:

* la cuenta del DSN debe ser de solo lectura -- se documenta y se verifica con la
  prueba de contrato ``SELECT`` + intento de escritura rechazado;
* timeout por consulta;
* limite de filas aplicado tambien en el cliente, no solo en el SQL;
* nunca se acepta SQL libre: solo :class:`CompiledQuery`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.auth.provider import IntegrationStatus
from app.common.errors import ConfigurationError, StructuredQueryRejectedError
from app.common.logging import get_logger
from app.structured_data.compiler import CompiledQuery
from app.structured_data.sources import SourceConfig

logger = get_logger(__name__)

#: Motor -> driver SQLAlchemy por defecto y extra de pip que lo provee.
ENGINE_DRIVERS: dict[str, tuple[str, str]] = {
    "mysql": ("mysql+pymysql", "incluido"),
    "mariadb": ("mysql+pymysql", "incluido"),
    "postgresql": ("postgresql+psycopg", "postgres"),
    "sqlserver": ("mssql+pymssql", "mssql"),
    "oracle": ("oracle+oracledb", "oracle"),
    "sqlite": ("sqlite", "incluido"),
}


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    """Filas devueltas por una consulta autorizada."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    truncated: bool
    source: str
    entity: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "rows": [list(r) for r in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "source": self.source,
            "entity": self.entity,
        }


class ReadOnlySourceAdapter:
    """Adapter generico basado en SQLAlchemy.

    Un unico adapter cubre los cuatro motores porque toda la diferencia relevante
    (dialecto SQL, sintaxis de limite) ya se resolvio en el compilador. Lo que
    cambia por motor es el driver del DSN, que se declara en ``ENGINE_DRIVERS``.
    """

    def __init__(self, source: SourceConfig) -> None:
        self.source = source
        self._engine: Engine | None = None

    # ------------------------------------------------------------- validacion
    def validate_configuration(self) -> list[str]:
        """Errores de configuracion detectables sin conectar."""
        problems: list[str] = []
        if self.source.engine not in ENGINE_DRIVERS:
            problems.append(f"Motor no soportado: {self.source.engine}")
        if self.source.enabled and not self.source.secret_ref:
            problems.append("La fuente esta habilitada pero no declara secret_ref.")
        dsn = self.source.dsn()
        if self.source.enabled and not dsn:
            problems.append(
                f"La variable de entorno {self.source.secret_ref} no esta definida."
            )
        if dsn:
            try:
                make_url(dsn)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"DSN invalido: {type(exc).__name__}")
        if not self.source.entities:
            problems.append("La fuente no declara entidades.")
        if self.source.enabled:
            for entity in self.source.entities:
                if self.source.row_scope == "user" and not entity.required_user_filters:
                    problems.append(f"La entidad {entity.name} no declara filtros por usuario.")
                if self.source.row_scope == "role_view" and entity.table not in self.source.approved_security_views:
                    problems.append(f"La entidad {entity.name} no declara una vista de seguridad aprobada.")
                if set(entity.required_user_filters) - set(entity.allowed_columns):
                    problems.append(f"La entidad {entity.name} filtra identidad en columnas no permitidas.")
        return problems

    def status(self) -> IntegrationStatus:
        return self.source.status()

    # -------------------------------------------------------------- conexion
    def _get_engine(self) -> Engine:
        if self._engine is None:
            dsn = self.source.dsn()
            if not dsn:
                raise ConfigurationError(
                    f"La fuente '{self.source.name}' no tiene DSN configurado "
                    f"(variable {self.source.secret_ref})."
                )
            try:
                options: dict[str, Any] = {
                    "pool_pre_ping": True,
                    "connect_args": self._connect_args(),
                    "future": True,
                }
                # SQLite usa pools que no admiten pool_size/max_overflow; pasarlos
                # aborta la creacion del engine.
                if self.source.engine != "sqlite":
                    options["pool_size"] = 2
                    options["max_overflow"] = 2
                self._engine = create_engine(dsn, **options)
            except Exception as exc:  # noqa: BLE001
                raise ConfigurationError(
                    f"No fue posible crear el engine de '{self.source.name}'.", detail=str(exc)
                ) from exc
        return self._engine

    def _connect_args(self) -> dict[str, Any]:
        """Timeout de conexion por motor."""
        timeout = self.source.timeout_seconds
        if self.source.engine in ("mysql", "mariadb"):
            return {"connect_timeout": timeout, "read_timeout": timeout}
        if self.source.engine == "postgresql":
            return {"connect_timeout": timeout}
        if self.source.engine == "sqlserver":
            return {"timeout": timeout}
        return {}

    # ------------------------------------------------------------- ejecucion
    def execute(self, compiled: CompiledQuery, *, entity_name: str) -> QueryOutcome:
        """Ejecuta una consulta ya compilada y verificada."""
        if not compiled.sql.lstrip().upper().startswith("SELECT"):
            # Ultima barrera antes del motor.
            raise StructuredQueryRejectedError("Solo se permiten sentencias SELECT.")

        engine = self._get_engine()
        try:
            with engine.connect() as connection:
                timeout_ms = int(self.source.timeout_seconds * 1000)
                if self.source.engine == "postgresql":
                    connection.execute(
                        text("SELECT set_config('statement_timeout', :timeout, true)"), {"timeout": str(timeout_ms)}
                    )
                elif self.source.engine == "oracle":
                    driver = connection.connection.driver_connection
                    if driver is None:
                        raise StructuredQueryRejectedError("La conexion a la fuente no esta disponible.")
                    driver.call_timeout = timeout_ms
                result = connection.execute(text(compiled.sql), compiled.parameters)
                columns = tuple(str(c) for c in result.keys())  # noqa: SIM118 - Result.keys() no es un dict
                # El SQL ya pidio `limit + 1` filas: si llegan todas, el
                # resultado esta truncado y se avisa al usuario.
                fetched = result.fetchmany(compiled.sql_limit)
        except StructuredQueryRejectedError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "structured.query_failed",
                extra={"source_name": self.source.name, "error_type": type(exc).__name__},
            )
            raise StructuredQueryRejectedError("La consulta a la fuente estructurada no pudo completarse.") from exc

        truncated = len(fetched) > compiled.limit
        rows = tuple(tuple(row) for row in fetched[: compiled.limit])
        return QueryOutcome(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            source=self.source.name,
            entity=entity_name,
        )

    def health_check(self) -> tuple[bool, str]:
        """Prueba de conexion real. Solo se usa en preflight/diagnostico."""
        if not self.source.enabled:
            return False, str(IntegrationStatus.DISABLED)
        if not self.source.dsn():
            return False, str(IntegrationStatus.PREPARED_NOT_CONNECTED)
        try:
            with self._get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
            return True, str(IntegrationStatus.CONNECTED_AND_VALIDATED)
        except Exception as exc:  # noqa: BLE001
            return False, f"{IntegrationStatus.ERROR}:{type(exc).__name__}"

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
