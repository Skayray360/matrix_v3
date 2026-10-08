# Creado por Aldo Garcia.
"""Runner de migraciones idempotente.

Se eligio un runner propio sobre SQL versionado en lugar de Alembic porque el
instalador Windows debe poder aplicar el esquema en frio, varias veces seguidas,
sin estado previo y sin dependencias adicionales. Cada archivo
``backend/migrations/NNNN_nombre.sql`` se aplica una sola vez y queda registrado
con su checksum en ``schema_migrations``.

Si el contenido de una migracion ya aplicada cambia, el runner falla en lugar de
reaplicarla: una migracion mutada silenciosamente es una fuente clasica de
divergencia entre entornos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.common.errors import ConfigurationError, DatabaseUnavailableError
from app.common.ids import sha256_text, utcnow_naive
from app.common.logging import get_logger
from app.config import get_settings

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_VERSION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    VARCHAR(64) NOT NULL,
    checksum   CHAR(64)    NOT NULL,
    applied_at DATETIME(6) NOT NULL,
    PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return sha256_text(self.sql)


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Lee los archivos de migracion ordenados por version."""
    base = directory or MIGRATIONS_DIR
    if not base.is_dir():
        raise ConfigurationError(f"No existe el directorio de migraciones: {base}")
    found: list[Migration] = []
    for path in sorted(base.glob("*.sql")):
        match = _VERSION_RE.match(path.name)
        if not match:
            raise ConfigurationError(
                f"Nombre de migracion invalido: {path.name}. Formato esperado NNNN_nombre.sql"
            )
        found.append(Migration(version=match.group(1), path=path, sql=path.read_text(encoding="utf-8")))
    return found


def split_statements(sql: str) -> list[str]:
    """Divide un script en sentencias.

    Se eliminan los comentarios de linea ``--`` antes de dividir por ``;`` para
    que un punto y coma dentro de un comentario no rompa el parseo.
    """
    without_comments = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    return [stmt.strip() for stmt in without_comments.split(";") if stmt.strip()]


def ensure_database_exists() -> tuple[str, bool]:
    """Crea la base de datos si no existe.

    Devuelve ``(nombre, la_creamos_nosotros)``. Ese segundo valor importa: una
    base que **ya existia** antes de que Matrix RH la tocara puede pertenecer a
    otra aplicacion aunque en este instante este vacia. Adoptarla en silencio es
    justo lo que no debe ocurrir.

    Caso real que motivo este control: en el equipo destino convivia otro
    proyecto que gestionaba una base llamada tambien ``matrix_rh`` con Alembic.

    Se conecta al servidor sin nombre de base para poder emitir el
    ``CREATE DATABASE``. El nombre se valida con una lista blanca de caracteres
    para que un DATABASE_URL manipulado no pueda inyectar DDL.
    """
    settings = get_settings()
    url = make_url(settings.database_url.get_secret_value())
    db_name = url.database
    if not db_name:
        raise ConfigurationError("DATABASE_URL no especifica el nombre de la base de datos.")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", db_name):
        raise ConfigurationError(f"Nombre de base de datos invalido: {db_name!r}")

    # ``URL.set`` ignora los valores None, asi que para conectarse al servidor
    # sin base de datos hay que pasar una cadena vacia explicita.
    server_url = url.set(database="")
    try:
        engine = create_engine(server_url, pool_pre_ping=True, future=True, hide_parameters=True)
        with engine.connect() as conn:
            ya_existia = bool(
                conn.execute(
                    text("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = :n"),
                    {"n": db_name},
                ).first()
            )
            if not ya_existia:
                conn.execute(
                    text(
                        f"CREATE DATABASE `{db_name}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
                conn.commit()
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailableError(
            "No fue posible crear/verificar la base de datos interna.", detail=type(exc).__name__
        ) from exc
    return db_name, not ya_existia


def list_tables(engine) -> set[str]:  # noqa: ANN001 - Engine de SQLAlchemy
    """Tablas presentes. **Solo lectura**: no crea nada."""
    with engine.connect() as conn:
        return {str(row[0]) for row in conn.execute(text("SHOW TABLES")).all()}


def inspect_ownership(engine) -> tuple[bool, set[str]]:  # noqa: ANN001
    """Determina si la base es de Matrix RH. **No escribe nada.**

    Devuelve ``(es_nuestra, tablas_ajenas)``.

    La tabla de control debe contener la migracion inicial de Matrix y todos
    sus registros deben coincidir con versiones/checksums del proyecto. Una
    tabla homonima de otra aplicacion no demuestra propiedad. Las bases vacias
    se someten ademas a la guardia de adopcion explicita en run_migrations.
    """
    tables = list_tables(engine)
    foreign = tables - {"schema_migrations"}

    if "schema_migrations" not in tables:
        return (not foreign), foreign

    with engine.connect() as conn:
        try:
            registered = dict(conn.execute(text("SELECT version, checksum FROM schema_migrations")).all())
        except SQLAlchemyError:  # Una tabla homonima con otro formato tampoco es Matrix.
            return False, tables
    if registered:
        known = {migration.version: migration.checksum for migration in discover_migrations()}
        if "0001" in registered and all(known.get(version) == checksum for version, checksum in registered.items()):
            return True, set()
        return False, tables
    return (not foreign), foreign


def assert_database_is_ours(engine) -> None:  # noqa: ANN001 - Engine de SQLAlchemy
    """Impide adoptar una base de datos que pertenece a otra aplicacion.

    Escenario real que motivo esta comprobacion: el equipo destino ya tenia una
    base llamada ``matrix_rh`` creada por otro proyecto. Sin esta verificacion,
    ``CREATE TABLE IF NOT EXISTS`` habria convivido con tablas ajenas de esquema
    distinto y la instalacion habria fallado a mitad de camino dejando la base
    del otro proyecto contaminada.

    La comprobacion es de **solo lectura**: una version anterior creaba
    ``schema_migrations`` antes de decidir, con lo que ensuciaba precisamente la
    base que pretendia proteger.
    """
    es_nuestra, foreign = inspect_ownership(engine)
    if es_nuestra:
        return
    raise ConfigurationError(
        "La base configurada contiene tablas ajenas a Matrix RH (de otra aplicacion) "
        "o un registro de migraciones incompatible "
        f"({', '.join(sorted(foreign)[:8])}). Matrix RH no la va a modificar.\n"
        "         Solucion: edite DATABASE_URL en .env y use un nombre libre, "
        "por ejemplo matrix_rh_app. Si es una instalacion de Matrix existente, "
        "verifique la version y los checksums con el respaldo; no edite el registro para forzar la adopcion."
    )


def applied_versions(engine, *, create: bool = False) -> dict[str, str]:  # noqa: ANN001
    """Migraciones ya aplicadas.

    ``create=False`` (por defecto) NO crea ``schema_migrations``: consultar el
    estado no debe modificar la base. Solo ``run_migrations`` la crea, y unicamente
    despues de verificar que la base nos pertenece.
    """
    with engine.connect() as conn:
        if create:
            conn.execute(text(_SCHEMA_MIGRATIONS_DDL))
            conn.commit()
        elif "schema_migrations" not in list_tables(engine):
            return {}
        rows = conn.execute(text("SELECT version, checksum FROM schema_migrations")).all()
    return {row[0]: row[1] for row in rows}


def run_migrations(*, directory: Path | None = None) -> list[str]:
    """Aplica las migraciones pendientes. Devuelve las versiones aplicadas ahora."""
    from app.database.engine import get_engine  # import local: evita ciclo

    db_name, la_creamos = ensure_database_exists()
    engine = get_engine()
    # Orden critico: primero se verifica la propiedad (solo lectura) y solo
    # despues se crea la tabla de control.
    assert_database_is_ours(engine)

    # Una base que ya existia y no tiene migraciones registradas NO se adopta en
    # silencio, aunque ahora mismo este vacia: pudo crearla otra aplicacion y
    # estar vacia por cualquier motivo (por ejemplo, un `downgrade` de Alembic).
    # Adoptarla haria que Matrix RH se apropiara de un nombre que no le pertenece.
    if not la_creamos and not applied_versions(engine):
        settings = get_settings()
        if not settings.matrix_adopt_existing_database:
            raise ConfigurationError(
                f"La base '{db_name}' ya existia y no fue creada por Matrix RH.\n"
                "         Matrix RH no adopta bases preexistentes por si pertenecen a\n"
                "         otra aplicacion, aunque ahora esten vacias.\n"
                "         Opciones: (a) use un nombre nuevo en DATABASE_URL y deje que\n"
                "         Matrix RH la cree, o (b) si esta base es suya y la quiere usar,\n"
                "         ponga MATRIX_ADOPT_EXISTING_DATABASE=true en .env."
            )
        logger.warning("db.adopting_existing_database", extra={"database": db_name})

    already = applied_versions(engine, create=True)
    migrations = discover_migrations(directory)
    newly_applied: list[str] = []

    for migration in migrations:
        recorded = already.get(migration.version)
        if recorded is not None:
            if recorded != migration.checksum:
                raise ConfigurationError(
                    f"La migracion {migration.version} ya aplicada fue modificada "
                    f"({migration.path.name}). Cree una migracion nueva en lugar de editarla."
                )
            continue

        logger.info("db.migration.apply", extra={"migration_version": migration.version})
        with engine.connect() as conn:
            for statement in split_statements(migration.sql):
                conn.execute(text(statement))
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, checksum, applied_at) "
                    "VALUES (:v, :c, :t)"
                ),
                {"v": migration.version, "c": migration.checksum, "t": utcnow_naive()},
            )
            conn.commit()
        newly_applied.append(migration.version)

    return newly_applied


def pending_migrations() -> list[str]:
    """Versiones aun no aplicadas.

    Lo usan ``/ready`` y el preflight, que **no deben modificar la base**: si la
    tabla de control no existe, se devuelven todas las migraciones como
    pendientes en lugar de crearla.
    """
    from app.database.engine import get_engine

    already = applied_versions(get_engine())
    return [m.version for m in discover_migrations() if m.version not in already]


def database_ownership_problem() -> str | None:
    """Devuelve el motivo si la base configurada pertenece a otra aplicacion.

    Pensado para el preflight y el diagnostico: informa sin escribir nada y sin
    lanzar excepcion, para que el operador vea el problema real en lugar de un
    error generico de base de datos.

    Limitacion deliberada: mira lo que hay dentro, no quien creo la base. Una
    base ajena **vacia** es indistinguible de una recien creada por Matrix RH
    desde un proceso que no la creo, asi que aqui se da por buena. Ese caso lo
    resuelve ``run_migrations``, que si sabe si la creo, y se detiene con el
    mensaje sobre ``MATRIX_ADOPT_EXISTING_DATABASE``.
    """
    from app.database.engine import get_engine

    try:
        es_nuestra, foreign = inspect_ownership(get_engine())
    except Exception as exc:  # noqa: BLE001
        return f"no fue posible inspeccionar la base ({type(exc).__name__})"
    if es_nuestra:
        return None
    return "contiene tablas ajenas o un registro de migraciones incompatible: " + ", ".join(sorted(foreign)[:6])
