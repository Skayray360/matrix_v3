# Creado por Aldo Garcia.
"""Regresiones S-01/S-03/S-04/M-01/M-02 sin infraestructura externa."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api import deps
from app.auth import sessions
from app.auth.passwords import verify_password
from app.common.errors import ConfigurationError, ForbiddenError, OllamaUnavailableError, UnauthorizedError
from app.config import Settings
from app.database.models import (
    Base,
    IdentityLink,
    LocalCredential,
    Role,
    SessionRecord,
    User,
    UserRole,
)
from app.llm.provider import ModelClient
from seeds import identity_seed

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


def configured(**updates) -> Settings:
    values = {"_env_file": None, "app_env": "test", "app_secret_key": "0" * 64}
    values.update(updates)
    return Settings(**values)


def corporate(**updates) -> Settings:
    values = {
        "auth_provider": "oidc",
        "local_test_auth_enabled": False,
        "local_test_seed_users_enabled": False,
        "oidc_issuer": "https://identity.example.internal",
        "oidc_authorization_endpoint": "https://identity.example.internal/auth",
        "oidc_token_endpoint": "https://identity.example.internal/token",
        "oidc_jwks_uri": "https://identity.example.internal/jwks",
        "oidc_redirect_uri": "https://matrix.example.internal/callback",
        "oidc_client_id": "synthetic-client",
        "oidc_client_secret": "synthetic-client-secret",
        # secrets-scan: allow (fixture sintetico aislado; sin credenciales de un servicio real)
        "database_url": "mysql+pymysql://matrixrh:synthetic-password@127.0.0.1/matrix_rh",
        "session_cookie_secure": True,
        "llm_fast_digest": "a" * 64,
        "llm_deep_digest": "a" * 64,
        "storage_encryption_attested": True,
        "database_encryption_attested": True,
        "backup_encryption_attested": True,
        "encryption_attestation_reference": "synthetic-infrastructure-evidence.md",
    }
    values.update(updates)
    return configured(**values)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.10.3", "matrix.example.internal"])
def test_local_identity_cannot_be_exposed_even_in_development(host):
    with pytest.raises(ValidationError, match="expone Matrix RH"):
        configured(app_env="development", app_host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1", "::ffff:127.0.0.1"])
def test_local_identity_accepts_literal_loopback_only(host):
    assert configured(app_host=host).is_local_auth_allowed


def test_external_browser_url_does_not_bypass_guard_with_loopback_proxy():
    with pytest.raises(ValidationError, match="APP_HOST/APP_BASE_URL"):
        configured(app_host="127.0.0.1", app_base_url="https://matrix.example.internal")


def test_configuration_failure_diagnostics_never_include_secret_inputs(monkeypatch):
    from app.config import settings as settings_module

    secret_marker = "private-settings-marker-184938"
    with pytest.raises(ValidationError) as validation:
        configured(
            # secrets-scan: allow (fixture sintetico aislado; sin credenciales de un servicio real)
            app_host="0.0.0.0", database_url=f"mysql+pymysql://root:{secret_marker}@127.0.0.1/matrix_rh",
            app_secret_key=secret_marker, matrix_seed_password=secret_marker,
        )
    assert secret_marker not in str(validation.value)
    monkeypatch.setattr(settings_module, "Settings", lambda: (_ for _ in ()).throw(validation.value))
    with pytest.raises(ConfigurationError) as failure:
        settings_module.get_settings.__wrapped__()
    assert "APP_HOST/APP_BASE_URL" in failure.value.detail
    assert secret_marker not in str(failure.value) + failure.value.detail


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_both_corporate_environments_block_test_identity(environment):
    with pytest.raises(ValidationError, match="LOCAL_TEST_AUTH_ENABLED"):
        configured(app_env=environment)


@pytest.mark.parametrize("dsn", [
    "mysql+pymysql://root:password@127.0.0.1/matrix_rh",
    "mysql+pymysql://%72oot:password@127.0.0.1/matrix_rh",
    "mysql+pymysql://matrixrh:@127.0.0.1/matrix_rh",
    # secrets-scan: allow (fixture sintetico aislado; sin credenciales de un servicio real)
    "mysql+pymysql://matrixrh:%20%20@127.0.0.1/matrix_rh",
])
def test_exposed_corporate_configuration_requires_dedicated_database_password(dsn):
    with pytest.raises(ValidationError, match="usuario dedicado"):
        corporate(app_host="0.0.0.0", database_url=dsn)


def test_loopback_test_database_is_preserved_but_not_permitted_for_staging():
    assert configured().database_url.get_secret_value().startswith("mysql+pymysql://root:")
    with pytest.raises(ValidationError, match="usuario dedicado"):
        corporate(app_env="staging", database_url="mysql+pymysql://root:@127.0.0.1/matrix_rh")


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_corporate_local_runtime_requires_generation_pins(environment):
    with pytest.raises(ValidationError, match="LLM_FAST_DIGEST"):
        corporate(app_env=environment, llm_fast_digest="")
    assert corporate(app_env=environment).llm_fast_digest == "a" * 64


@pytest.mark.parametrize("digest", ["latest", "unverified", "sha256:bad", "0" * 63])
def test_model_pin_cannot_be_an_unverifiable_label(digest):
    with pytest.raises(ValidationError, match="SHA-256"):
        configured(llm_fast_digest=digest)


def test_digest_prefix_and_uppercase_are_normalized():
    assert configured(llm_fast_digest="sha256:" + "A" * 64).llm_fast_digest == "a" * 64


@pytest.mark.parametrize("entry", ["*", "0.0.0.0/0", "::/0", "nginx", "127.0.0.1,*"])
def test_proxy_headers_never_trust_global_or_hostname_allowlists(entry):
    with pytest.raises(ValidationError, match="FORWARDED_ALLOW_IPS"):
        configured(forwarded_allow_ips=entry)


def test_proxy_exact_ips_cidr_and_empty_denylist_are_supported():
    assert configured(forwarded_allow_ips="127.0.0.1, 172.30.0.10,172.30.0.10").forwarded_allow_ips == (
        "127.0.0.1,172.30.0.10"
    )
    assert configured(forwarded_allow_ips="172.30.0.0/24").forwarded_allow_ips == "172.30.0.0/24"
    assert configured(forwarded_allow_ips="").forwarded_allow_ips == ""


def test_corporate_requires_explicit_infrastructure_evidence_not_just_local_runtime():
    with pytest.raises(ValidationError, match="ENCRYPTION_ATTESTATION_REFERENCE"):
        corporate(app_env="staging", backup_encryption_attested=False)


def test_retention_does_not_invent_policy_days_or_accept_empty_enabled_policy():
    assert configured(retention_conversation_days="").retention_conversation_days is None
    with pytest.raises(ValidationError, match="plazo acordado"):
        configured(retention_enabled=True)
    assert configured(retention_enabled=True, retention_conversation_days=30).retention_enabled


@pytest.fixture()
def identity_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        User.__table__, LocalCredential.__table__, IdentityLink.__table__, Role.__table__,
        UserRole.__table__, SessionRecord.__table__,
    ])
    with Session(engine) as database:
        yield database
    engine.dispose()


def seed_fixture(database: Session, monkeypatch, *, password: str = "") -> None:
    settings = configured(matrix_seed_password=password)
    monkeypatch.setattr(identity_seed, "get_settings", lambda: settings)
    roles = {}
    for seed_user in identity_seed.SEED_USERS:
        role = Role(name=seed_user.role_name)
        database.add(role)
        database.flush()
        roles[seed_user.role_name] = role
    monkeypatch.setattr(identity_seed, "seed_roles_and_permissions", lambda _: roles)


def test_new_seed_never_falls_back_to_published_password(identity_db, monkeypatch):
    seed_fixture(identity_db, monkeypatch)
    with pytest.raises(ConfigurationError, match="MATRIX_SEED_PASSWORD"):
        identity_seed.seed_test_users(identity_db)
    assert identity_db.execute(select(LocalCredential)).scalars().all() == []


def test_development_seed_rejects_short_password_only_when_creating_credential(identity_db, monkeypatch):
    seed_fixture(identity_db, monkeypatch, password="Matrix RH")
    monkeypatch.setattr(
        identity_seed, "get_settings", lambda: configured(app_env="development", matrix_seed_password="Matrix RH")
    )
    with pytest.raises(ConfigurationError, match="16 a 128"):
        identity_seed.seed_test_users(identity_db)
    assert identity_db.execute(select(LocalCredential)).scalars().all() == []


def test_configured_seed_hashes_new_password_and_does_not_reset_existing_credentials(identity_db, monkeypatch):
    # secrets-scan: allow (fixture sintetico aislado; sin credenciales de un servicio real)
    seed_fixture(identity_db, monkeypatch, password="fixture-random-seed-938174")
    assert identity_seed.seed_test_users(identity_db) == ["Matrix", "MatrixR1"]
    existing_hashes = {
        record.user_id: record.password_hash_argon2id
        for record in identity_db.execute(select(LocalCredential)).scalars()
    }
    assert all(verify_password(value, "fixture-random-seed-938174") for value in existing_hashes.values())
    monkeypatch.setattr(identity_seed, "get_settings", lambda: configured(matrix_seed_password=""))
    assert identity_seed.seed_test_users(identity_db) == []
    assert {
        record.user_id: record.password_hash_argon2id
        for record in identity_db.execute(select(LocalCredential)).scalars()
    } == existing_hashes


def test_seed_cannot_appropriate_existing_non_test_identity(identity_db, monkeypatch):
    # secrets-scan: allow (fixture sintetico aislado; sin credenciales de un servicio real)
    seed_fixture(identity_db, monkeypatch, password="fixture-random-seed-938174")
    identity_db.add(User(username="Matrix", display_name="SSO real identity", auth_source="oidc"))
    identity_db.flush()
    with pytest.raises(ConfigurationError, match="identidad distinta"):
        identity_seed.seed_test_users(identity_db)
    assert identity_db.execute(select(LocalCredential)).scalars().all() == []


@pytest.mark.parametrize("inactive_minutes", [30, 31, 480])
def test_idle_session_is_rejected_before_last_seen_is_updated(inactive_minutes, monkeypatch):
    now = datetime(2026, 10, 2, 15, 0)
    monkeypatch.setattr(sessions, "utcnow_naive", lambda: now)
    monkeypatch.setattr(sessions, "get_settings", lambda: configured(session_idle_minutes=30))
    record = SessionRecord(
        user_id="synthetic-user", last_seen_at=now - timedelta(minutes=inactive_minutes),
        expires_at=now + timedelta(hours=1), revoked_at=None,
    )
    previous = record.last_seen_at
    with pytest.raises(UnauthorizedError):
        sessions.validate_session_record(SimpleNamespace(get=lambda *_: pytest.fail("no user fetch")), record)
    assert record.last_seen_at == previous


def test_active_session_refreshes_idle_clock_without_extending_absolute_deadline(monkeypatch):
    now = datetime(2026, 10, 2, 15, 0)
    monkeypatch.setattr(sessions, "utcnow_naive", lambda: now)
    monkeypatch.setattr(sessions, "get_settings", lambda: configured(session_idle_minutes=30))
    record = SessionRecord(
        user_id="synthetic-user", last_seen_at=now - timedelta(minutes=29),
        expires_at=now + timedelta(hours=1), revoked_at=None,
    )
    deadline = record.expires_at
    user = User(id=record.user_id, is_active=True)
    assert sessions.validate_session_record(SimpleNamespace(get=lambda *_: user), record) == (record, user)
    assert record.last_seen_at == now
    assert record.expires_at == deadline


def test_purge_removes_idle_absolute_and_revoked_sessions_preserving_live_sessions(identity_db, monkeypatch):
    now = datetime(2026, 10, 2, 15, 0)
    monkeypatch.setattr(sessions, "utcnow_naive", lambda: now)
    monkeypatch.setattr(sessions, "get_settings", lambda: configured(session_idle_minutes=30))
    for index in range(4):
        identity_db.add(SessionRecord(
            session_token_hash=str(index) * 64, user_id="synthetic-user", csrf_token="test-csrf",
            auth_source="local_test", created_at=now - timedelta(hours=1),
            last_seen_at=now - timedelta(minutes=30 if index == 0 else 1),
            expires_at=now if index == 1 else now + timedelta(hours=1),
            revoked_at=now if index == 2 else None,
        ))
    identity_db.flush()
    assert sessions.purge_expired_sessions(identity_db, limit=1) == 1
    assert sessions.purge_expired_sessions(identity_db) == 2
    assert identity_db.execute(select(SessionRecord.session_token_hash)).scalars().all() == ["3" * 64]


@pytest.mark.parametrize("provided", ["wrong", "token\u00e9", "valid-token"])
def test_csrf_compares_bytes_in_constant_time_including_non_ascii_headers(provided, monkeypatch):
    record = SimpleNamespace(csrf_token="valid-token")
    monkeypatch.setattr(deps, "resolve_session", lambda *_: (record, object()))
    monkeypatch.setattr(deps, "get_settings", lambda: configured())
    compared = []
    compare_digest = deps.secrets.compare_digest

    def comparison(left, right):
        compared.append((left, right))
        return compare_digest(left, right)

    monkeypatch.setattr(deps.secrets, "compare_digest", comparison)
    request = Request({
        "type": "http", "method": "POST",
        "headers": [(b"x-csrf-token", provided.encode("latin-1"))],
    })
    if provided == "valid-token":
        deps.require_csrf(request, SimpleNamespace())
    else:
        with pytest.raises(ForbiddenError):
            deps.require_csrf(request, SimpleNamespace())
    assert compared == [(provided.encode("utf-8"), b"valid-token")]


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_runtime_digest_is_rechecked_and_changed_tag_never_receives_generation(provider, monkeypatch):
    settings = configured(
        llm_provider=provider, llm_deep_provider=provider, llm_embedding_provider=provider,
        llm_fast_digest="a" * 64,
    )
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)
    digest = ["a" * 64]
    requests = []

    def transport(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": settings.ollama_fast_model, "digest": digest[0]}]})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": settings.ollama_fast_model, "digest": digest[0]}]})
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"model": settings.ollama_fast_model, "message": {"content": "OK"}, "done": True})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = ModelClient(client=http)
        assert client.chat(model=settings.ollama_fast_model, messages=[{"role": "user", "content": "synthetic"}]).content == "OK"
        digest[0] = "b" * 64
        with pytest.raises(ConfigurationError, match="digest fijado"):
            client.chat(model=settings.ollama_fast_model, messages=[{"role": "user", "content": "synthetic"}])
    assert len([request for request in requests if request[0] == "POST"]) == 1
    assert len([request for request in requests if request[0] == "GET"]) == 2


def test_compatible_runtime_without_digest_cannot_claim_a_verified_pin(monkeypatch):
    settings = configured(
        llm_provider="openai_compatible", llm_deep_provider="openai_compatible",
        llm_embedding_provider="openai_compatible", llm_fast_digest="a" * 64,
    )
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)

    def transport(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"data": [{"id": settings.ollama_fast_model}]})

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = ModelClient(client=http)
        with pytest.raises(ConfigurationError, match="acredita el digest"):
            client.list_models()
        with pytest.raises(ConfigurationError, match="acredita el digest"):
            client.chat(model=settings.ollama_fast_model, messages=[{"role": "user", "content": "synthetic"}])


def test_expired_request_budget_does_not_start_pin_inventory_network_call(monkeypatch):
    from app.llm import provider as provider_module

    settings = configured(llm_fast_digest="a" * 64)
    monkeypatch.setattr(provider_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)
    token = provider_module._deadline.set(0.0)
    try:
        with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("budget exhausted"))) as http:
            client = ModelClient(client=http)
            with pytest.raises(OllamaUnavailableError, match="tiempo limite"):
                client.chat(model=settings.ollama_fast_model, messages=[{"role": "user", "content": "synthetic"}])
    finally:
        provider_module._deadline.reset(token)


@pytest.mark.parametrize("endpoint", [
    "http://:secret@127.0.0.1:11434", "http://127.0.0.1:11434?token=secret",
    "http://127.0.0.1:11434#fragment",
])
def test_local_inference_endpoint_rejects_credential_query_and_fragment_before_network(endpoint, monkeypatch):
    settings = configured(ollama_base_url=endpoint)
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    with pytest.raises(ConfigurationError, match="Endpoint de inferencia invalido"):
        ModelClient(client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no network"))))


def test_compose_keeps_inference_internal_and_trusts_only_explicit_proxy_ip():
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    overlay = yaml.safe_load((ROOT / "docker-compose.ollama.yml").read_text())
    backend = base["services"]["backend"]
    assert "http://ollama:11434" in backend["environment"]["OLLAMA_BASE_URL"]
    assert "host.docker.internal" not in backend["environment"]["LLM_LOCAL_HOSTS"]
    assert "${MATRIX_NGINX_IP" in backend["environment"]["FORWARDED_ALLOW_IPS"]
    assert not any(key == "ports" for key in overlay["services"]["ollama"])
    assert overlay["services"]["ollama"]["networks"] == ["matrix_llm"]
    assert overlay["networks"]["matrix_llm"]["internal"] is True
    assert "matrix_proxy" not in base["services"]["mysql"]["networks"]
    assert "matrix_proxy" not in base["services"]["qdrant"]["networks"]
