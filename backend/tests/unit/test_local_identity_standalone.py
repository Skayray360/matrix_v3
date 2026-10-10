# Creado por Aldo Garcia.
"""Identidad real con SQLite aislado: HTTP, transacciones y concurrencia."""

from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.dml import Update

from app.api import deps
from app.api.middleware import matrix_error_handler
from app.api.routes import auth
from app.auth import local_accounts, local_provider, provider, sessions
from app.auth.local_accounts import (
    LOCAL_ADMIN_ROLE,
    LOCAL_READER_ROLE,
    bootstrap_administrator,
    create_local_account,
    replace_local_password,
)
from app.auth.passwords import verify_password
from app.authorization import policy
from app.authorization.categories import CategoryPolicy, CategoryRegistry
from app.common.errors import ConfigurationError, MatrixError, UnauthorizedError, ValidationFailedError
from app.config import Settings
from app.database.models import AuditEvent, Base, IdentityLink, LocalCredential, Role, SessionRecord, User
from app.security.rate_limit import get_rate_limiter
from scripts import local_identity


@pytest.fixture
def local_system(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, app_env="test", auth_provider="local", local_test_auth_enabled=False,
        local_test_seed_users_enabled=False, app_secret_key=secrets.token_hex(32), rate_limit_login_per_minute=100,
    )
    for module in (local_accounts, local_provider, provider, sessions, auth, deps, policy):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    engine = create_engine("sqlite:///" + str(tmp_path / "identity.sqlite"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(deps, "get_sessionmaker", lambda: factory)
    registry = CategoryRegistry(
        policies={"prestaciones": CategoryPolicy(name="prestaciones"), "general": CategoryPolicy(name="general")},
        default_wildcard_eligible=False, default_sensitivity="internal",
    )
    engine_policy = policy.PolicyEngine(registry)
    monkeypatch.setattr(policy, "get_policy_engine", lambda: engine_policy)
    monkeypatch.setattr(deps, "get_policy_engine", lambda: engine_policy)
    password = secrets.token_urlsafe(24)
    with factory.begin() as db:
        user, _ = bootstrap_administrator(db, username="Matrix", password=password)
        user_id = user.id
    application = FastAPI()
    application.include_router(auth.router, prefix="/api/v1")
    application.add_exception_handler(MatrixError, matrix_error_handler)
    get_rate_limiter().reset()
    with TestClient(application) as client:
        yield SimpleNamespace(client=client, app=application, factory=factory, password=password,
                              user_id=user_id, settings=settings, root=tmp_path)
    get_rate_limiter().reset()
    engine.dispose()


def login(system, *, password=None, username="Matrix", client=None):
    return (client or system.client).post("/api/v1/auth/local/login", json={
        "username": username, "password": password or system.password,
    })


@pytest.mark.parametrize("damage", [None, "identity", "hash", "inactive"])
def test_preflight_verifies_real_admin_without_requiring_synthetic_reader(local_system, monkeypatch, damage):
    from app.database import engine
    from scripts import preflight

    system = local_system
    with system.factory.begin() as db:
        if damage == "identity":
            db.delete(db.execute(select(IdentityLink).where(IdentityLink.user_id == system.user_id)).scalar_one())
        elif damage == "hash":
            db.get(LocalCredential, system.user_id).password_hash_argon2id = "not-a-password-hash"
        elif damage == "inactive":
            db.get(User, system.user_id).is_active = False
    monkeypatch.setattr(engine, "session_scope", system.factory.begin)
    report = preflight.PreflightReport()
    preflight.check_seed_users(report, system.settings)
    assert report.ok is (damage is None)
    assert report.checks[0].name == "usuarios_locales"
    assert "MatrixR1" not in preflight.render_text(report)
    with system.factory() as db:
        assert db.execute(select(User.username)).scalars().all() == ["Matrix"]


def test_bootstrap_creates_only_real_admin_and_does_not_reset_changed_password(local_system):
    system = local_system
    updated = secrets.token_urlsafe(24)
    with system.factory.begin() as db:
        user = db.get(User, system.user_id)
        replace_local_password(db, user=user, password=updated)
        before = db.get(LocalCredential, user.id).password_hash_argon2id
    with system.factory.begin() as db:
        user, created = bootstrap_administrator(db, username="Matrix", password=system.password)
        assert not created
        assert db.get(LocalCredential, user.id).password_hash_argon2id == before
        assert db.execute(select(User)).scalars().all() == [user]
        assert user.auth_source == "local" and not user.is_synthetic_test
        assert all(not role.is_test_role for role in db.execute(select(Role)).scalars())
    assert login(system, password=updated).status_code == 200
    assert login(system).status_code == 401


def test_bootstrap_does_not_adopt_federated_identity_or_create_second_initial_admin(local_system):
    with local_system.factory.begin() as db:
        db.add(User(username="Federated", display_name="Federated", auth_source="oidc"))
    with local_system.factory() as db, pytest.raises(ConfigurationError):
        bootstrap_administrator(db, username="Federated", password=local_system.password)
    with local_system.factory() as db, pytest.raises(ConfigurationError):
        bootstrap_administrator(db, username="OtherAdmin", password=local_system.password)


def test_login_denial_persists_lock_across_http_rollbacks(local_system):
    for _ in range(5):
        # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
        assert login(local_system, password="wrong-value").status_code == 401
    with local_system.factory() as db:
        credential = db.get(LocalCredential, local_system.user_id)
        assert credential.locked_until is not None
        assert credential.failed_attempts == 0
        assert len(db.execute(select(AuditEvent).where(AuditEvent.event_type == "auth.login_failed")).all()) == 5
    assert login(local_system).status_code == 429


def test_concurrent_failed_logins_do_not_lose_account_attempts(local_system, monkeypatch):
    monkeypatch.setattr(local_provider, "verify_password", lambda *_: False)

    def denied(_):
        with TestClient(local_system.app) as client:
            # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
            return login(local_system, password="wrong-value", client=client).status_code

    with ThreadPoolExecutor(max_workers=5) as workers:
        statuses = list(workers.map(denied, range(5)))
    assert all(status in (401, 429) for status in statuses)
    with local_system.factory() as db:
        assert db.get(LocalCredential, local_system.user_id).locked_until is not None


def test_mysql_failure_update_computes_lock_before_resetting_counter(local_system, monkeypatch):
    statements = []
    execute = Session.execute

    def capture(self, statement, *args, **kwargs):
        if isinstance(statement, Update) and statement.table.name == "local_credentials":
            statements.append(str(statement.compile(dialect=mysql.dialect())))
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", capture)
    # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
    assert login(local_system, password="wrong-value").status_code == 401
    assert len(statements) == 1
    assignments = statements[0].split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    # MySQL usa el valor ya asignado en expresiones posteriores del mismo UPDATE.
    assert assignments.index("locked_until=") < assignments.index("failed_attempts=")


def test_login_verified_against_replaced_hash_cannot_issue_session(local_system, monkeypatch):
    verify = local_provider.verify_password
    next_password = secrets.token_urlsafe(24)

    def changed_after_verification(stored_hash, password):
        correct = verify(stored_hash, password)
        with local_system.factory.begin() as db:
            user = db.get(User, local_system.user_id)
            replace_local_password(db, user=user, password=next_password)
        return correct

    monkeypatch.setattr(local_provider, "verify_password", changed_after_verification)
    assert login(local_system).status_code == 401
    with local_system.factory() as db:
        assert db.execute(select(SessionRecord)).first() is None
        assert verify(db.get(LocalCredential, local_system.user_id).password_hash_argon2id, next_password)


def test_local_provider_cannot_authenticate_federated_or_synthetic_account(local_system):
    with local_system.factory.begin() as db:
        user = db.get(User, local_system.user_id)
        user.auth_source = "oidc"
    assert login(local_system).status_code == 401
    with local_system.factory.begin() as db:
        user = db.get(User, local_system.user_id)
        user.auth_source = "local"
        user.is_synthetic_test = True
    assert login(local_system).status_code == 401


def test_missing_or_mismatched_identity_link_fails_closed(local_system):
    with local_system.factory.begin() as db:
        link = db.execute(select(IdentityLink).where(IdentityLink.user_id == local_system.user_id)).scalar_one()
        db.delete(link)
    assert login(local_system).status_code == 401


def test_password_change_rotates_cookie_csrf_and_revokes_every_previous_session(local_system):
    system = local_system
    first = login(system)
    previous_cookie = system.client.cookies.get(system.settings.session_cookie_name)
    with TestClient(system.app) as other:
        assert login(system, client=other).status_code == 200
        next_password = secrets.token_urlsafe(24)
        response = system.client.post("/api/v1/auth/local/change-password", json={
            "current_password": system.password, "new_password": next_password,
        }, headers={"X-CSRF-Token": first.json()["csrf_token"]})
        assert response.status_code == 200, response.text
        assert response.json()["csrf_token"] != first.json()["csrf_token"]
        assert system.client.cookies.get(system.settings.session_cookie_name) != previous_cookie
        assert other.get("/api/v1/me").status_code == 401
    assert system.client.get("/api/v1/me").status_code == 200
    with system.factory() as db:
        records = db.execute(select(SessionRecord)).scalars().all()
        assert len([record for record in records if record.revoked_at is None]) == 1
        assert verify_password(db.get(LocalCredential, system.user_id).password_hash_argon2id, next_password)
    assert login(system).status_code == 401
    assert login(system, password=next_password).status_code == 200


def test_password_change_wrong_current_preserves_session_and_persists_attempt(local_system):
    profile = login(local_system).json()
    response = local_system.client.post("/api/v1/auth/local/change-password", json={
        "current_password": "wrong-value", "new_password": secrets.token_urlsafe(24),
    }, headers={"X-CSRF-Token": profile["csrf_token"]})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert local_system.client.get("/api/v1/me").status_code == 200
    with local_system.factory() as db:
        assert db.get(LocalCredential, local_system.user_id).failed_attempts == 1


def test_password_change_requires_csrf_and_rejects_old_csrf_after_rotation(local_system):
    first = login(local_system).json()
    payload = {"current_password": local_system.password, "new_password": secrets.token_urlsafe(24)}
    assert local_system.client.post("/api/v1/auth/local/change-password", json=payload).status_code == 403
    assert local_system.client.post("/api/v1/auth/local/change-password", json=payload,
                                   headers={"X-CSRF-Token": first["csrf_token"]}).status_code == 200
    assert local_system.client.post("/api/v1/auth/local/change-password", json=payload,
                                   headers={"X-CSRF-Token": first["csrf_token"]}).status_code == 403


def test_reader_cannot_administer_users_and_admin_can_create_restricted_user(local_system):
    profile = login(local_system).json()
    password = secrets.token_urlsafe(24)
    created = local_system.client.post("/api/v1/admin/users", json={
        "username": "Reader", "display_name": "Reader", "password": password, "role": LOCAL_READER_ROLE,
    }, headers={"X-CSRF-Token": profile["csrf_token"]})
    assert created.status_code == 201
    assert "password" not in created.text
    reader = login(local_system, username="Reader", password=password).json()
    assert reader["permissions"] == []
    assert reader["allowed_categories"] == ["prestaciones"]
    assert local_system.client.get("/api/v1/admin/users").status_code == 403
    assert local_system.client.post("/api/v1/admin/users", json={
        "username": "Intruder", "display_name": "Intruder", "password": password, "role": LOCAL_ADMIN_ROLE,
    }, headers={"X-CSRF-Token": reader["csrf_token"]}).status_code == 403


def test_local_test_cannot_be_instantiated_with_real_local_configuration(local_system):
    with pytest.raises(ConfigurationError):
        local_provider.LocalTestIdentityProvider()


def test_cli_bootstrap_and_reset_never_emit_password_and_preserve_idempotence(local_system, monkeypatch, capsys):
    root = local_system.root
    secret_path = root / "backend/config/secrets/admin.txt"
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text(local_system.password, encoding="utf-8")
    original_reader = local_identity.read_password
    monkeypatch.setattr(local_identity, "read_password", lambda path: original_reader(path, root=root))

    @contextmanager
    def scope():
        with local_system.factory.begin() as db:
            yield db

    monkeypatch.setattr(local_identity, "session_scope", scope)
    assert local_identity.main(["bootstrap", "--username", "Matrix", "--password-file", str(secret_path)]) == 0
    output = capsys.readouterr().out
    assert local_system.password not in output
    assert '"created": false' in output
    login(local_system)
    assert local_identity.main(["change-password", "--username", "Matrix", "--password-file", str(secret_path)]) == 0
    assert local_system.password not in capsys.readouterr().out
    assert local_system.client.get("/api/v1/me").status_code == 401


def test_cli_rejects_password_files_outside_project_and_symlinks(tmp_path):
    outside = tmp_path / "other.txt"
    outside.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    with pytest.raises(ValidationFailedError):
        local_identity.read_password(outside, root=tmp_path)
    secret_root = tmp_path / "backend/config/secrets"
    secret_root.mkdir(parents=True)
    link = secret_root / "linked.txt"
    link.symlink_to(outside)
    with pytest.raises(ValidationFailedError):
        local_identity.read_password(link, root=tmp_path)


@pytest.mark.parametrize("password", ["short", " " * 20, "x" * 20, "a\nbcdefghijklmnopq", " leading-space123456"])
def test_new_local_password_policy_rejects_invalid_values(password):
    with pytest.raises(ValidationFailedError):
        local_accounts.validate_password(password)


def test_invalid_local_bootstrap_rolls_back_role_and_user_creation(local_system):
    with local_system.factory() as db:
        with pytest.raises(ValidationFailedError):
            # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
            create_local_account(db, username="ValidName", display_name="ValidName", password="short",
                                 role_name=LOCAL_READER_ROLE)
        db.rollback()
        assert db.execute(select(User).where(User.username == "ValidName")).first() is None


def test_provider_wrong_password_does_not_commit_unrelated_session_work(local_system):
    with local_system.factory() as db:
        db.add(User(username="Unrelated", display_name="Unrelated", auth_source="oidc"))
        with pytest.raises(UnauthorizedError):
            # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
            local_provider.LocalIdentityProvider().authenticate(db, username="Matrix", password="wrong-value")
        db.rollback()
    with local_system.factory() as db:
        assert db.execute(select(User).where(User.username == "Unrelated")).first() is None
