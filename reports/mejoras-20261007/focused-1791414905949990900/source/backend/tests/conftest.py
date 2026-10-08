# Creado por Aldo Garcia.
"""Fixtures compartidas.

Principios:

* las pruebas unitarias **no** requieren MySQL, Qdrant ni Ollama;
* las de integracion se saltan de forma explicita (``skip``) cuando falta la
  dependencia, en lugar de fallar y contaminar el porcentaje de aprobacion;
* ningun dato real: todos los fixtures son sinteticos.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Las pruebas fuerzan el entorno de test antes de que se cargue la configuracion.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_LOG_LEVEL", "WARNING")
if os.environ.get("APP_ENV") == "test":
    # Credencial sintetica del fixture; nunca es un valor por defecto de la app.
    os.environ.setdefault("MATRIX_SEED_PASSWORD", "Matrix RH")

from app.authorization.context import (  # noqa: E402
    PERM_DIAGNOSTICS_READ,
    PERM_KNOWLEDGE_ADMIN,
    PERM_STRUCTURED_QUERY,
    PERM_USERS_ADMIN,
    UserContext,
)
from app.common.ids import new_id  # noqa: E402
from app.config import get_settings  # noqa: E402


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Bloquea antes de fixtures de modulo que pueden confirmar DELETE/migraciones."""
    if not any(
        item.get_closest_marker("integration") is not None
        or {"db_session", "require_database", "database_available"}.intersection(item.fixturenames)
        for item in items
    ):
        return
    from app.database.engine import check_database
    from scripts.test_environment import require_disposable_database

    available, _ = check_database()
    if available:
        try:
            require_disposable_database()
        except ValueError as error:
            raise pytest.UsageError(str(error)) from error


@pytest.fixture(scope="session")
def settings():  # noqa: ANN201
    return get_settings()


@pytest.fixture()
def secret(settings) -> str:  # noqa: ANN001
    return settings.app_secret_key.get_secret_value()


def make_context(
    *,
    username: str = "Matrix",
    roles: frozenset[str] = frozenset({"matrix_admin_test"}),
    permissions: frozenset[str] = frozenset(
        {PERM_KNOWLEDGE_ADMIN, PERM_USERS_ADMIN, PERM_STRUCTURED_QUERY, PERM_DIAGNOSTICS_READ}
    ),
    categories: frozenset[str] = frozenset(),
    wildcard: bool = True,
    sources: frozenset[str] = frozenset({"rh_demo"}),
    user_id: str | None = None,
) -> UserContext:
    """Construye un contexto firmado para pruebas."""
    context = UserContext(
        user_id=user_id or new_id(),
        username=username,
        display_name=username,
        auth_source="local_test",
        session_id=new_id(),
        request_id=new_id(),
        roles=roles,
        groups=frozenset(),
        permissions=permissions,
        allowed_categories=categories,
        category_wildcard=wildcard,
        allowed_sources=sources,
    )
    return context.sign(get_settings().app_secret_key.get_secret_value())


@pytest.fixture()
def admin_context() -> UserContext:
    return make_context()


@pytest.fixture()
def restricted_context() -> UserContext:
    """Equivalente sintetico de ``MatrixR1``: solo prestaciones, sin permisos."""
    return make_context(
        username="MatrixR1",
        roles=frozenset({"prestaciones_reader_test"}),
        permissions=frozenset(),
        categories=frozenset({"prestaciones"}),
        wildcard=False,
        sources=frozenset(),
    )


@pytest.fixture(scope="session")
def database_available() -> bool:
    from app.database.engine import check_database
    from scripts.test_environment import require_disposable_database

    ok, _ = check_database()
    if ok:
        try:
            require_disposable_database()
        except ValueError as error:
            raise pytest.UsageError(str(error)) from error
    return ok


@pytest.fixture()
def require_database(database_available: bool) -> None:
    if not database_available:
        pytest.skip("MySQL/MariaDB no disponible en este entorno")


@pytest.fixture(scope="session")
def ollama_available() -> bool:
    try:
        from app.llm.ollama_client import OllamaClient

        return OllamaClient().ping()
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def require_ollama(ollama_available: bool) -> None:
    if not ollama_available:
        pytest.skip("Ollama no disponible en este entorno")


@pytest.fixture(scope="session")
def qdrant_available() -> bool:
    try:
        from app.rag.vector_store import get_vector_store

        ok, _ = get_vector_store().health()
        return ok
    except Exception:  # noqa: BLE001
        # En modo embedded el almacen esta bloqueado si el backend corre.
        return False


@pytest.fixture()
def require_qdrant(qdrant_available: bool) -> None:
    if not qdrant_available:
        pytest.skip("Qdrant no disponible (o bloqueado por el backend en modo embedded)")


@pytest.fixture()
def db_session(require_database) -> Iterator:  # noqa: ANN201, ARG001
    """Sesion de BD con rollback al final: las pruebas no dejan residuo."""
    from app.database.engine import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
