# Creado por Aldo Garcia.
"""Diagnostico standalone: SELECT-only, alcance autorizado y sin datos privados."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app.database.models import (
    AuditEvent,
    Base,
    CategoryPermission,
    Document,
    JobLock,
    Permission,
    Role,
    RolePermission,
    StructuredSourcePermission,
    User,
    UserRole,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
DIAGNOSTIC_PATH = ROOT / "backend" / "scripts" / "diagnosticar_rag.py"
SPEC = importlib.util.spec_from_file_location("diagnosticar_rag_fix01", DIAGNOSTIC_PATH)
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


@pytest.fixture
def diagnostic_database():
    engine = create_engine("sqlite://")
    tables = [User, Role, Permission, RolePermission, UserRole, CategoryPermission,
              StructuredSourcePermission, Document, AuditEvent, JobLock]
    Base.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    with Session(engine) as db:
        db.add(User(id="owner", username="Matrix", display_name="Synthetic", auth_source="local_test"))
        db.add(Role(id="reader", name="prestaciones_reader_test"))
        db.add(UserRole(user_id="owner", role_id="reader"))
        db.add(CategoryPermission(role_id="reader", category="prestaciones"))
        for name, category, scope, chunks, fingerprint in (
            ("visible", "prestaciones", "corporate", 6, "current"),
            ("old", "prestaciones", "corporate", 3, "previous"),
            ("restricted", "nomina", "corporate", 100, "current"),
            ("private", "prestaciones", "conversation", 200, "current"),
        ):
            db.add(Document(
                id=name, category=category, scope=scope, chunk_count=chunks,
                filename="SECRET_FILENAME", mime_type="text/plain", sha256="SECRET_HASH",
                active_generation="SECRET_GENERATION", index_fingerprint=fingerprint,
                status="indexed",
            ))
        for who, kind, status in (
            ("owner", "chat.insufficient_evidence", "insufficient_evidence"),
            ("owner", "chat.answer", "degraded"),
            ("other", "chat.answer", "PRIVATE_OTHER_USER_EVENT"),
        ):
            db.add(AuditEvent(
                user_opaque_id=who, event_type=kind, status=status, selected_model="gemma4:latest",
                selected_tools=["rag", "SECRET_TOOL"], request_id="SECRET_REQUEST",
                conversation_id="SECRET_CONVERSATION", resource="SECRET_CONTENT",
                source_ids=["SECRET_SOURCE"], role_set_hash="SECRET_ROLE_HASH",
            ))
        db.commit()
    yield engine
    engine.dispose()


def test_snapshot_reads_authorized_metadata_without_writes(diagnostic_database):
    engine = diagnostic_database
    statements = []
    with engine.connect() as connection:
        before = list(connection.connection.driver_connection.iterdump())
        event.listen(connection, "before_cursor_execute", diagnostic.select_only_guard)
        event.listen(connection, "before_cursor_execute", lambda _c, _u, sql, *_: statements.append(sql))
        with Session(bind=connection, autoflush=False) as db:
            report = diagnostic.database_snapshot(db, username="Matrix", fingerprint="current")
        after = list(connection.connection.driver_connection.iterdump())
    assert before == after
    assert statements and all(sql.lstrip().startswith("SELECT ") for sql in statements)
    assert report["effective_categories"] == ["prestaciones"]
    assert report["authorized_document_count"] == 2
    assert report["authorized_chunk_count"] == 9
    assert report["compatible_indexed_chunk_count"] == 6
    assert {row["event_type"] for row in report["recent_chat_events"]} == {
        "chat.insufficient_evidence", "chat.answer",
    }
    serialized = json.dumps(report)
    assert "SECRET" not in serialized and "PRIVATE_OTHER_USER_EVENT" not in serialized


def test_guard_blocks_accidental_write(diagnostic_database):
    with diagnostic_database.connect() as connection:
        event.listen(connection, "before_cursor_execute", diagnostic.select_only_guard)
        with pytest.raises(RuntimeError, match="SELECT"):
            connection.execute(text("DELETE FROM documents"))
        assert connection.execute(text("SELECT COUNT(*) FROM documents")).scalar_one() == 4


def test_unknown_user_and_unverified_fingerprint(diagnostic_database):
    with Session(diagnostic_database, autoflush=False) as db:
        absent = diagnostic.database_snapshot(db, username="absent", fingerprint="current")
        present = diagnostic.database_snapshot(db, username="Matrix", fingerprint=None)
    assert absent == {"user_found": False, "documents": [], "recent_chat_events": []}
    assert present["compatible_indexed_chunk_count"] is None
    assert all(row["fingerprint_compatible"] is None for row in present["documents"])


def test_collect_errors_never_expose_exception_message(monkeypatch, diagnostic_database):
    monkeypatch.chdir(ROOT)

    def inventory_failure():
        raise RuntimeError("SECRET_PASSWORD mysql://user:password@server/private")

    monkeypatch.setattr("app.llm.ollama_client.get_ollama_client", inventory_failure)
    monkeypatch.setattr("app.database.engine.get_engine", lambda: diagnostic_database)
    report = diagnostic.collect(ROOT)
    assert report["database"]["authorized_chunk_count"] == 9
    assert report["errors"] == [{"component": "embedding_inventory", "error_type": "RuntimeError"}]
    assert report["qdrant_opened"] is False and report["inference_executed"] is False
    assert "SECRET" not in json.dumps(report) and "mysql://" not in json.dumps(report)


def test_collect_only_requests_ollama_inventory(monkeypatch, diagnostic_database):
    import httpx

    from app.config import get_settings
    from app.llm.provider import ModelClient

    monkeypatch.chdir(ROOT)
    requests = []

    def transport(request):
        requests.append((request.method, request.url.path))
        assert request.method == "GET" and request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{
            "name": get_settings().ollama_embedding_model, "digest": "a" * 64,
        }]})

    def forbid_qdrant(*args, **kwargs):
        raise AssertionError("Qdrant must remain owned by the API")

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = ModelClient(client=http)
        monkeypatch.setattr("app.llm.ollama_client.get_ollama_client", lambda: client)
        monkeypatch.setattr("app.database.engine.get_engine", lambda: diagnostic_database)
        monkeypatch.setattr("app.rag.vector_store.VectorStore.__init__", forbid_qdrant)
        report = diagnostic.collect(ROOT)
    assert report["errors"] == []
    assert report["embedding_revision_verified"] is True
    assert requests == [("GET", "/api/tags")]


@pytest.mark.parametrize("state", ["alive", "dead", "unknown"])
def test_dispatcher_snapshot_is_read_only_and_never_reveals_owner(diagnostic_database, monkeypatch, state):
    from datetime import timedelta

    from app.agents.chat_queue import LEASE_NAME
    from app.common.ids import utcnow_naive

    now = utcnow_naive()
    with Session(diagnostic_database) as db:
        db.add(JobLock(lock_name=LEASE_NAME, locked_by="SECRET_OWNER", locked_at=now,
                       expires_at=now + timedelta(seconds=90)))
        db.commit()
    monkeypatch.setattr("app.agents.process_owner.owner_state", lambda _: state)
    with diagnostic_database.connect() as connection:
        before = list(connection.connection.driver_connection.iterdump())
        event.listen(connection, "before_cursor_execute", diagnostic.select_only_guard)
        with Session(bind=connection, autoflush=False) as db:
            report = diagnostic.dispatcher_snapshot(db)
        after = list(connection.connection.driver_connection.iterdump())
    assert before == after
    assert report["active"] and 0 < report["remaining_seconds"] <= 90
    assert report["owner_state"] == state
    assert report["reclaim_on_start"] is (state == "dead")
    assert "SECRET_OWNER" not in json.dumps(report)


def test_dispatcher_absent_is_not_created(diagnostic_database):
    with Session(diagnostic_database) as db:
        assert diagnostic.dispatcher_snapshot(db) == {
            "present": False, "active": False, "remaining_seconds": 0, "owner_state": "none",
        }
