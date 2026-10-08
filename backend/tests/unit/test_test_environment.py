# Creado por Aldo Garcia.
"""Regresiones de la barrera que protege datos frente a fixtures destructivos."""

from types import SimpleNamespace

import pytest

from app.config import Settings
from scripts import test_environment
from tests import conftest


@pytest.fixture()
def disposable(monkeypatch):
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://test:synthetic@127.0.0.1/matrix_rh_test",  # secrets-scan: allow (fixture sintetico de base desechable)
        qdrant_path="var/tests/qdrant",
        upload_storage_root="var/tests/uploads",
    )
    monkeypatch.setattr(test_environment, "get_settings", lambda: settings)
    monkeypatch.setenv("MATRIX_TEST_ALLOW_DESTRUCTIVE", "true")
    return settings


def test_accepts_explicit_disposable_storage(disposable):
    test_environment.require_disposable_database()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("app_env", "development", "APP_ENV"),
        ("database_url", "mysql+pymysql://test:synthetic@localhost/matrix_rh", "DATABASE_URL"),  # secrets-scan: allow (fixture sintetico de base desechable)
        ("database_url", "mysql+pymysql://test:synthetic@localhost/another_test", "DATABASE_URL"),  # secrets-scan: allow (fixture sintetico de base desechable)
        ("qdrant_path", "var/qdrant", "QDRANT_PATH"),
        ("upload_storage_root", "var/uploads", "UPLOAD_STORAGE_ROOT"),
        ("qdrant_path", "var/tests/../qdrant", "QDRANT_PATH"),
    ],
)
def test_blocks_operational_environment_even_with_opt_in(disposable, monkeypatch, field, value, reason):
    changed = Settings(_env_file=None, **(disposable.model_dump() | {field: value}))
    monkeypatch.setattr(test_environment, "get_settings", lambda: changed)
    with pytest.raises(ValueError, match=reason):
        test_environment.require_disposable_database()


def test_requires_explicit_opt_in(disposable, monkeypatch):
    monkeypatch.delenv("MATRIX_TEST_ALLOW_DESTRUCTIVE")
    with pytest.raises(ValueError, match="MATRIX_TEST_ALLOW_DESTRUCTIVE"):
        test_environment.require_disposable_database()


def test_server_cannot_use_operational_collections(disposable, monkeypatch):
    changed = Settings(_env_file=None, **(disposable.model_dump() | {"qdrant_mode": "server"}))
    monkeypatch.setattr(test_environment, "get_settings", lambda: changed)
    with pytest.raises(ValueError, match="colecciones"):
        test_environment.require_disposable_database()
    changed.rag_collection_corporate = "matrix_rh_test_corporate"
    changed.rag_collection_private = "matrix_rh_test_private"
    test_environment.require_disposable_database()


@pytest.mark.parametrize("marked", [True, False])
def test_collection_blocks_before_database_fixtures(disposable, monkeypatch, marked):
    monkeypatch.delenv("MATRIX_TEST_ALLOW_DESTRUCTIVE")
    monkeypatch.setattr("app.database.engine.check_database", lambda: (True, "available"))
    item = SimpleNamespace(
        get_closest_marker=lambda _name: object() if marked else None,
        fixturenames=[] if marked else ["require_database"],
    )
    with pytest.raises(pytest.UsageError, match="bloqueada antes de modificar datos"):
        conftest.pytest_collection_modifyitems([item])


def test_absent_database_still_allows_isolated_integration_and_explicit_skips(monkeypatch):
    monkeypatch.setattr("app.database.engine.check_database", lambda: (False, "unavailable"))
    monkeypatch.setattr(test_environment, "require_disposable_database", lambda: pytest.fail("guard called"))
    item = SimpleNamespace(get_closest_marker=lambda _name: object(), fixturenames=[])
    conftest.pytest_collection_modifyitems([item])
