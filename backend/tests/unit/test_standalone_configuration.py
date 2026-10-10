# Creado por Aldo Garcia.
"""Contratos de independencia local y exposicion de credenciales reales."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import settings as configuration
from app.config.settings import AuthProvider, Settings


def standalone(**overrides):
    values = {
        "app_env": "standalone", "auth_provider": "local", "local_test_auth_enabled": False,
        "local_test_seed_users_enabled": False, "app_secret_key": "synthetic-isolated-test-key-not-a-real-secret",
        # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
        "database_url": "mysql+pymysql://matrixrh:synthetic@127.0.0.1:3308/matrix_rh_test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_standalone_uses_real_local_identity_without_claiming_storage_encryption():
    settings = standalone()
    assert settings.auth_provider is AuthProvider.LOCAL and settings.is_local_auth_allowed
    assert not settings.is_production
    assert not settings.storage_encryption_attested
    assert not settings.database_encryption_attested
    assert not settings.backup_encryption_attested


@pytest.mark.parametrize("changes", [
    {"app_host": "0.0.0.0"}, {"app_base_url": "http://192.0.2.10"},
    {"auth_provider": "local_test"}, {"local_test_auth_enabled": True},
    {"local_test_seed_users_enabled": True}, {"app_secret_key": ""},
    {"llm_local_only": False}, {"llm_provider": "vertex"},
    {"qdrant_mode": "server"},
    # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
    {"database_url": "mysql+pymysql://matrixrh:synthetic@192.0.2.20:3306/test"},
    # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
    {"database_url": "mysql+pymysql://root:synthetic@127.0.0.1:3308/test"},
    {"database_url": "mysql+pymysql://matrixrh:@127.0.0.1:3308/test"},
    {"ollama_base_url": "http://192.0.2.30:11434"},
    # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
    {"ollama_base_url": "https://user:private@localhost:11434"},
    {"ollama_base_url": "http://localhost:11434/#fragment"},
    {"app_base_url": "ftp://localhost:8085"},
    {"llm_embedding_provider": "openai_compatible", "llm_api_base_url": "http://192.0.2.40:8080/v1"},
])
def test_standalone_rejects_external_dependencies_or_test_identity(changes):
    with pytest.raises(ValidationError):
        standalone(**changes)


@pytest.mark.parametrize("field", [
    "rag_knowledge_root", "upload_storage_root", "qdrant_path", "authorization_policy_file",
    "structured_sources_config", "database_datadir_path",
])
def test_standalone_canonicalizes_absolute_parent_escape(tmp_path, monkeypatch, field):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(configuration, "PROJECT_ROOT", project)
    with pytest.raises(ValidationError):
        standalone(**{field: project / ".." / "outside"})


def test_standalone_rejects_symlink_to_external_storage(tmp_path, monkeypatch):
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(configuration, "PROJECT_ROOT", project)
    with pytest.raises(ValidationError):
        standalone(upload_storage_root=project / "linked")


@pytest.mark.parametrize("changes", [
    {"app_host": "0.0.0.0"}, {"app_host": "::"}, {"app_host": "192.0.2.10"},
    {"app_base_url": "http://matrix.example.test"},
])
def test_local_real_account_requires_https_when_either_bind_or_url_exposes_network(changes):
    with pytest.raises(ValidationError, match="HTTPS"):
        standalone(app_env="development", **changes)


def test_explicit_local_https_proxy_configuration_is_allowed():
    settings = standalone(app_env="development", app_host="0.0.0.0",
                          app_base_url="https://127.0.0.1:8443", session_cookie_secure=True)
    assert settings.is_local_auth_allowed


def test_local_production_still_requires_actual_encryption_attestation():
    with pytest.raises(ValidationError, match="ENCRYPTION"):
        standalone(app_env="production", app_base_url="https://localhost", session_cookie_secure=True,
                   llm_fast_digest="a" * 64, llm_deep_digest="a" * 64)


def test_settings_never_resolves_project_paths_from_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert standalone().knowledge_root_path == configuration.PROJECT_ROOT / "knowledge-base/documents"
    assert Settings.model_config["env_file"] == configuration.PROJECT_ROOT / "backend/config/.env"


def test_all_runtime_paths_in_default_template_are_project_relative():
    root = Path(__file__).resolve().parents[3]
    entries = dict(line.split("=", 1) for line in (root / "backend/config/env.example").read_text().splitlines()
                   if line and not line.startswith("#") and "=" in line)
    assert entries["AUTH_PROVIDER"] == "local"
    assert entries["LOCAL_TEST_AUTH_ENABLED"] == entries["LOCAL_TEST_SEED_USERS_ENABLED"] == "false"
    assert entries["APP_ENV"] == "standalone"
    for field in ("RAG_KNOWLEDGE_ROOT", "QDRANT_PATH", "UPLOAD_STORAGE_ROOT",
                  "AUTHORIZATION_POLICY_FILE", "STRUCTURED_SOURCES_CONFIG"):
        assert entries[field].startswith(("./knowledge-base/", "./backend/config/"))
    assert entries["APP_SECRET_KEY"] == ""  # Se genera una vez, no se distribuye.
