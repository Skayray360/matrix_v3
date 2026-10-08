# Creado por Aldo Garcia.
"""Fabrica de engine y sesiones SQLAlchemy.

El engine es unico por proceso y se crea de forma perezosa para que importar el
paquete no abra conexiones (importante para las pruebas unitarias, que no
requieren MySQL).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.common.errors import DatabaseUnavailableError
from app.common.logging import get_logger, safe_error_fields
from app.config import get_settings

logger = get_logger(__name__)

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Engine compartido con pool y ``pool_pre_ping``.

    ``pool_pre_ping`` evita el clasico "MySQL server has gone away" cuando WAMP
    recicla conexiones ociosas.
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_pool_size,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=settings.database_echo,
            hide_parameters=True,
            future=True,
        )
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _sessionmaker


def dispose_engine() -> None:
    """Libera el pool. Lo usan las pruebas y el apagado limpio del proceso."""
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Sesion transaccional: commit al salir bien, rollback ante cualquier error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        logger.error("db.session_error", extra=safe_error_fields(exc))
        raise DatabaseUnavailableError(detail=type(exc).__name__) from exc
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database() -> tuple[bool, str]:
    """Comprobacion usada por ``/ready`` y por el preflight."""
    try:
        with get_engine().connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar_one()
        return True, str(version)
    except Exception as exc:  # noqa: BLE001 - se reporta como estado, no se propaga
        return False, type(exc).__name__
