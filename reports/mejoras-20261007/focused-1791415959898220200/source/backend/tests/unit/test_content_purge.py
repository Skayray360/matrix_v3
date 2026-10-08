# Creado por Aldo Garcia.
"""Borrado real: SQL, filesystem y Qdrant aislados; fallos y workers tardios."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select, text

from app.agents import chat_service
from app.api.routes import conversations
from app.common.errors import ConversationCleanupPendingError, NotFoundError, ValidationFailedError
from app.common.ids import sha256_text, utcnow_naive
from app.database.models import (
    AuditEvent,
    ChatOperation,
    Conversation,
    ConversationMessage,
    ConversationSummary,
    Document,
    DocumentVersion,
    OidcLoginState,
    SessionRecord,
)
from app.jobs import retention
from app.memory.service import MemoryService
from app.rag.schemas import SCOPE_CONVERSATION
from scripts import purge
from tests.conftest import make_context
from tests.unit import test_private_reindex_final as private_fixtures
from tests.unit.test_rag_pipeline_isolated import make_chunk

pytestmark = [pytest.mark.unit, pytest.mark.security]
environment = private_fixtures.environment


@pytest.fixture
def state(environment, monkeypatch):
    env = environment
    env.db.execute(text("PRAGMA foreign_keys=ON"))
    sequence = count(1)

    def assign_seq(mapper, connection, target):
        if target.seq is None:
            target.seq = next(sequence)

    event.listen(ConversationMessage, "before_insert", assign_seq)
    env.context = make_context(user_id="user-a", username="Usuario sintetico", sources=frozenset())
    env.memory = MemoryService()
    monkeypatch.setattr(conversations, "get_ingestion_service", lambda: env.service)
    monkeypatch.setattr(retention, "get_settings", lambda: env.settings)
    monkeypatch.setattr("app.auth.sessions.get_settings", lambda: env.settings)
    monkeypatch.setattr(chat_service, "get_sessionmaker", lambda: env.factory)
    try:
        yield env
    finally:
        event.remove(ConversationMessage, "before_insert", assign_seq)


def add_payload(env, *, data=b"# RH sintetico\n\nContenido de un expediente privado."):
    conversation = env.db.get(Conversation, "chat-a")
    env.memory.append_message(env.db, conversation, role="user", content="Pregunta privada RH")
    env.memory.append_message(env.db, conversation, role="assistant", content="Respuesta privada RH")
    env.memory.store_summary(env.db, conversation.id, summary="Resumen privado RH", message_count=2)
    operation = ChatOperation(
        id="1" * 64, user_id="user-a", conversation_id="chat-a", request_hash="2" * 64,
        authorization_scope="3" * 64, status="completed",
        response={"answer": "Respuesta privada RH", "sources": ["expediente-personal.md"]},
        message="Pregunta privada RH", session_id="sesion-interna", request_id="request-interna",
    )
    env.db.add(operation)
    env.db.add(AuditEvent(
        id="audit-private", request_id="request-interna", event_type="attachment.uploaded",
        conversation_id="chat-a", resource="expediente-personal.md", source_ids=["archivo-restringido.md"],
    ))
    outcome = env.service.ingest_conversation_attachment(
        env.db, data=data, display_name="expediente-personal.md", internal_filename="private.md",
        mime_type="text/markdown", owner_user_id="user-a", conversation_id="chat-a",
    )
    env.db.commit()
    document = env.db.get(Document, outcome.document_id)
    return document, env.settings.upload_storage_path / document.storage_path


def erase(env, *, db=None):
    return conversations.delete_conversation("chat-a", db=db or env.db, ctx=env.context)


def assert_sql_content_removed(env):
    with env.factory() as db:
        assert db.scalar(select(func.count()).select_from(ConversationMessage)) == 0
        assert db.scalar(select(func.count()).select_from(ConversationSummary)) == 0
        operation = db.get(ChatOperation, "1" * 64)
        assert operation is not None and operation.status == "cancelled"
        assert operation.response is None and operation.message is None
        assert operation.session_id is None and operation.request_id is None
        previous_audit = db.get(AuditEvent, "audit-private")
        assert previous_audit.resource is None and previous_audit.source_ids is None
        conversation = db.get(Conversation, "chat-a")
        assert conversation.title == "Conversacion eliminada" and conversation.deleted_at is not None


def test_delete_removes_sql_bytes_vectors_versions_and_is_idempotent(state):
    env = state
    document, path = add_payload(env)
    document_id = document.id
    assert path.exists() and env.store.count(env.store.collection_for(SCOPE_CONVERSATION)) > 0
    assert erase(env) == {"ok": True}
    env.db.commit()
    assert_sql_content_removed(env)
    assert not path.exists()
    assert env.db.get(Document, document_id) is None
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == 0
    assert env.store.count(env.store.collection_for(SCOPE_CONVERSATION)) == 0
    assert env.db.get(Conversation, "chat-a").purged_at is not None
    assert erase(env) == {"ok": True}
    env.db.commit()
    with pytest.raises(NotFoundError):
        env.memory.get_owned_conversation(env.db, env.context, "chat-a")
    with pytest.raises(NotFoundError):
        env.memory.delete_conversation(env.db, make_context(user_id="other-user"), "chat-a")


@pytest.mark.parametrize("failure", ["vector", "filesystem", "client_constructor"])
def test_failed_cleanup_is_durable_private_and_retryable(state, monkeypatch, failure):
    env = state
    document, path = add_payload(env)
    document_id, portable, size = document.id, document.storage_path, document.size_bytes
    with monkeypatch.context() as patch:
        def broken(*_args, **_kwargs):
            raise PermissionError("SECRET INTERNAL PATH")

        if failure == "vector":
            patch.setattr(env.store, "delete_conversation", broken)
        elif failure == "client_constructor":
            patch.setattr(conversations, "get_ingestion_service", broken)
        else:
            original = Path.unlink

            def unlink(target, *args, **kwargs):
                if target == path:
                    broken()
                return original(target, *args, **kwargs)

            patch.setattr(Path, "unlink", unlink)
        with pytest.raises(ConversationCleanupPendingError) as error:
            erase(env)
        assert error.value.code == "conversation_cleanup_pending"
        assert "SECRET" not in error.value.message
        env.db.rollback()  # Como el dependency HTTP al devolver 503.
    assert_sql_content_removed(env)
    with env.factory() as db:
        row = db.get(Document, document_id)
        assert row.deleted_at is not None and row.status == "deleted"
        assert row.storage_path == portable and row.size_bytes == size
        assert row.filename == "" and row.error_message is None
        assert db.get(Conversation, "chat-a").purged_at is None
    assert path.exists()
    assert erase(env) == {"ok": True}
    env.db.commit()
    assert not path.exists() and env.db.get(Document, document_id) is None


def test_rollback_before_cleanup_preserves_original_content_and_file(state):
    env = state
    document, path = add_payload(env)
    env.memory.delete_conversation(env.db, env.context, "chat-a")
    env.service.delete_conversation_documents(env.db, "chat-a")
    env.db.rollback()
    conversation = env.db.get(Conversation, "chat-a")
    assert conversation.deleted_at is None and conversation.title == "Conversacion sintetica"
    assert env.db.scalar(select(func.count()).select_from(ConversationMessage)) == 2
    assert env.db.get(ChatOperation, "1" * 64).response["answer"] == "Respuesta privada RH"
    assert path.exists() and document.status == "indexed"
    assert env.store.count(env.store.collection_for(SCOPE_CONVERSATION)) > 0


def test_namespace_purge_covers_files_and_vectors_without_sql_manifest(state):
    env = state
    path = env.settings.upload_storage_path / "user-a/chat-a/orphan.md"
    path.parent.mkdir(parents=True)
    path.write_text("Contenido privado huerfano.")
    chunk = make_chunk(
        "Chunk huerfano.", category="__private__", scope=SCOPE_CONVERSATION,
        document_id="rolled-back-upload", owner="user-a", conversation="chat-a",
    )
    env.store.upsert_chunks([chunk], [[1.0] + [0.0] * 767])
    assert erase(env) == {"ok": True}
    env.db.commit()
    assert not path.exists() and not path.parent.exists()
    assert env.store.count(env.store.collection_for(SCOPE_CONVERSATION)) == 0


@pytest.mark.parametrize("stored_path", ["../../outside.md", "absolute"])
def test_corrupt_manifest_cannot_delete_outside_private_namespace(state, stored_path):
    env = state
    document, path = add_payload(env)
    safe_path = document.storage_path
    outside = env.root / "outside.md"
    outside.write_text("No eliminar este archivo.")
    document.storage_path = str(outside) if stored_path == "absolute" else stored_path
    env.db.commit()
    with pytest.raises(ConversationCleanupPendingError):
        erase(env)
    env.db.rollback()
    assert outside.read_text() == "No eliminar este archivo."
    assert path.exists()
    document = env.db.get(Document, document.id)
    document.storage_path = safe_path
    env.db.commit()
    erase(env)
    env.db.commit()
    assert not path.exists() and outside.exists()


def test_symlink_namespace_never_follows_external_target(state):
    env = state
    document, path = add_payload(env)
    path.unlink()
    path.parent.rmdir()
    outside = env.root / "external"
    outside.mkdir()
    secret = outside / "private.md"
    secret.write_text("Contenido fuera del root.")
    path.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConversationCleanupPendingError):
        erase(env)
    env.db.rollback()
    assert secret.read_text() == "Contenido fuera del root."
    assert env.db.get(Document, document.id).storage_path is not None


def test_deleted_files_pending_cleanup_cannot_bypass_quota(state, monkeypatch):
    env = state
    env.settings = env.settings.model_copy(update={"upload_max_total_bytes": 1024})
    env.service._settings = env.settings
    document, path = add_payload(env, data=b"# Documento\n\n" + b"a" * 900)
    with monkeypatch.context() as patch:
        patch.setattr(env.store, "delete_conversation", lambda *_: (_ for _ in ()).throw(RuntimeError()))
        with pytest.raises(ConversationCleanupPendingError):
            erase(env)
        env.db.rollback()
    next_chat = env.memory.create_conversation(env.db, env.context, title="Segunda conversacion")
    env.db.commit()
    with pytest.raises(ValidationFailedError, match="cuota de almacenamiento"):
        env.service.ingest_conversation_attachment(
            env.db, data=b"b" * 200, display_name="second.md", internal_filename="second.md",
            mime_type="text/markdown", owner_user_id="user-a", conversation_id=next_chat.id,
        )
    env.db.rollback()
    assert path.exists() and env.db.get(Document, document.id).size_bytes > 900
    erase(env)
    env.db.commit()
    result = env.service.ingest_conversation_attachment(
        env.db, data=b"# Documento\n\n" + b"b" * 200, display_name="second.md", internal_filename="second.md",
        mime_type="text/markdown", owner_user_id="user-a", conversation_id=next_chat.id,
    )
    assert result.status == "indexed"


def test_orphan_bytes_left_by_rollback_still_reserve_quota(state):
    env = state
    env.settings = env.settings.model_copy(update={"upload_max_total_bytes": 1024})
    env.service._settings = env.settings
    result = env.service.ingest_conversation_attachment(
        env.db, data=b"# Documento\n\n" + b"a" * 900, display_name="private.md", internal_filename="private.md",
        mime_type="text/markdown", owner_user_id="user-a", conversation_id="chat-a",
    )
    env.db.rollback()
    assert env.db.get(Document, result.document_id) is None
    next_chat = env.memory.create_conversation(env.db, env.context)
    env.db.commit()
    with pytest.raises(ValidationFailedError, match="cuota de almacenamiento"):
        env.service.ingest_conversation_attachment(
            env.db, data=b"b" * 200, display_name="next.md", internal_filename="next.md",
            mime_type="text/markdown", owner_user_id="user-a", conversation_id=next_chat.id,
        )
    env.db.rollback()
    erase(env)
    env.db.commit()
    assert not (env.settings.upload_storage_path / "user-a/chat-a/private.md").exists()


@pytest.mark.parametrize("tracked_operation", [False, True])
def test_concurrent_delete_prevents_late_publication_with_stale_orm(state, monkeypatch, tracked_operation):
    env = state
    document, path = add_payload(env)
    operation = env.db.get(ChatOperation, "1" * 64)
    operation.status = "running"
    env.db.commit()
    scope = chat_service.authorization_scope(env.db, env.context)
    env.db.commit()

    def handle(db, *, ctx, conversation, message):
        env.memory.append_message(db, conversation, role="user", content=message)
        db.commit()
        assert conversation.deleted_at is None  # Objeto de antes de la IA.
        with env.factory() as deletion_db:
            erase(env, db=deletion_db)
            deletion_db.commit()
        # La consulta actual bajo el lock ve la baja, no el objeto stale.
        env.memory.append_message(db, conversation, role="assistant", content="Respuesta tardia privada")
        raise AssertionError("No se puede publicar una respuesta despues del DELETE.")

    monkeypatch.setattr(chat_service, "get_orchestrator", lambda: SimpleNamespace(handle_chat=handle))
    with env.factory() as worker_db, pytest.raises(NotFoundError):
        chat_service.execute_chat(
            worker_db, ctx=env.context, conversation=worker_db.get(Conversation, "chat-a"),
            message="Pregunta de la operacion en curso", expected_scope=scope,
            operation_id=operation.id if tracked_operation else None, reauthorize=lambda _: env.context,
        )
    assert_sql_content_removed(env)
    assert not path.exists()
    with env.factory() as worker_db, pytest.raises(NotFoundError):
        env.memory.store_summary(worker_db, "chat-a", summary="Resumen tardio", message_count=3)
    # La sesion del fixture conserva su mapa de identidad anterior al DELETE;
    # comprobar persistencia desde otra conexion, como el siguiente request.
    with env.factory() as verification_db:
        assert verification_db.get(Document, document.id) is None


def test_historical_tombstones_include_previously_deleted_documents(state):
    env = state
    document, path = add_payload(env)
    # Simula el contrato anterior: solo marcas, sin purga de contenido.
    env.db.get(Conversation, "chat-a").deleted_at = utcnow_naive() - timedelta(days=90)
    document.deleted_at = utcnow_naive() - timedelta(days=90)
    document.status = "deleted"
    env.db.commit()
    stats = retention.run_maintenance(env.db, ingestion=env.service, deleted_only=True)
    assert stats.conversations_scrubbed == 1 and stats.documents_removed == 1
    assert stats.pending_cleanup == 0 and not path.exists()
    assert_sql_content_removed(env)
    second = retention.run_maintenance(env.db, ingestion=env.service, deleted_only=True)
    assert second.conversations_scrubbed == second.documents_removed == 0


def test_retention_disabled_preserves_live_data_but_purges_expired_sessions(state, monkeypatch):
    env = state
    document, path = add_payload(env)
    before = utcnow_naive() - timedelta(days=90)
    env.db.get(Conversation, "chat-a").updated_at = before
    env.db.get(ChatOperation, "1" * 64).created_at = before
    env.db.get(AuditEvent, "audit-private").created_at = before
    for session_id, expired in (("expired-session", True), ("live-session", False)):
        env.db.add(SessionRecord(
            id=session_id, user_id="user-a", session_token_hash=sha256_text(session_id),
            csrf_token="synthetic", auth_source="local_test", last_seen_at=utcnow_naive(),
            expires_at=utcnow_naive() + timedelta(hours=-1 if expired else 1),
        ))
    env.db.add(OidcLoginState(
        id="expired-login", state="synthetic-state", nonce="synthetic-nonce", code_verifier="synthetic-pkce",
        expires_at=utcnow_naive() - timedelta(minutes=1),
    ))
    env.db.commit()
    monkeypatch.setattr(retention, "IngestionService", lambda: (_ for _ in ()).throw(AssertionError("Sin bajas")))
    stats = retention.run_maintenance(env.db)
    assert stats.conversations_scrubbed == stats.operations_scrubbed == stats.audit_events_removed == 0
    assert stats.expired_sessions_removed == stats.expired_login_states_removed == 1
    assert env.db.get(SessionRecord, "expired-session") is None
    assert env.db.get(SessionRecord, "live-session") is not None
    assert path.exists() and env.db.get(Document, document.id).deleted_at is None
    assert env.db.get(ChatOperation, "1" * 64).response is not None
    assert env.db.get(Conversation, "chat-a").deleted_at is None


def test_explicit_retention_erases_only_due_content_and_keeps_operation_keys(state):
    env = state
    document, path = add_payload(env)
    env.settings = env.settings.model_copy(update={
        "retention_enabled": True, "retention_conversation_days": 30,
        "retention_operation_days": 30, "retention_audit_days": 30,
    })
    before = utcnow_naive() - timedelta(days=31)
    env.db.get(Conversation, "chat-a").updated_at = before
    live = env.memory.create_conversation(env.db, env.context, title="Datos vigentes")
    old_operation = ChatOperation(
        id="old-terminal", user_id="user-a", conversation_id=live.id, request_hash="4" * 64,
        authorization_scope="5" * 64, status="completed", response={"answer": "Vencida"}, created_at=before,
    )
    running = ChatOperation(
        id="old-running", user_id="user-a", conversation_id=live.id, request_hash="6" * 64,
        authorization_scope="7" * 64, status="running", message="En curso", created_at=before,
    )
    env.db.add_all([old_operation, running, AuditEvent(
        id="old-audit", request_id="synthetic", event_type="login", created_at=before,
    )])
    env.db.commit()
    stats = retention.run_maintenance(env.db, ingestion=env.service)
    assert stats.conversations_scrubbed == stats.documents_removed == 1
    assert stats.operations_scrubbed == stats.audit_events_removed == 1
    assert not path.exists() and env.db.get(Document, document.id) is None
    env.db.expire_all()
    assert env.db.get(Conversation, live.id).deleted_at is None
    terminal = env.db.get(ChatOperation, "old-terminal")
    assert terminal is not None and terminal.response is None and terminal.status == "expired"
    assert env.db.get(ChatOperation, "old-running").message == "En curso"
    again = retention.run_maintenance(env.db, ingestion=env.service)
    assert again.operations_scrubbed == again.conversations_scrubbed == again.audit_events_removed == 0


def test_batch_limit_and_cli_preview_do_not_silently_apply_changes(state, monkeypatch, capsys):
    env = state
    document, path = add_payload(env)
    env.db.get(Conversation, "chat-a").deleted_at = utcnow_naive()
    another = env.memory.create_conversation(env.db, env.context, title="Otra baja")
    another.deleted_at = utcnow_naive()
    env.db.commit()

    @contextmanager
    def scope():
        with env.factory() as db:
            yield db
            db.commit()

    monkeypatch.setattr("app.database.engine.session_scope", scope)
    assert purge.main(["--deleted-only", "--batch-size", "1"]) == 0
    assert '"apply": false' in capsys.readouterr().out
    assert path.exists() and env.db.get(Document, document.id).deleted_at is None
    assert env.db.scalar(select(func.count()).select_from(ConversationMessage)) == 2
    # Aplicacion explicita procesa una baja por pasada, no las dos de golpe.
    monkeypatch.setattr(retention, "IngestionService", type("Services", (), {
        "__new__": staticmethod(lambda cls: env.service),
        "delete_conversation_documents": staticmethod(env.service.delete_conversation_documents),
    }))
    assert purge.main(["--deleted-only", "--batch-size", "1", "--apply"]) == 0
    env.db.expire_all()
    completed = env.db.scalar(select(func.count()).select_from(Conversation).where(Conversation.purged_at.is_not(None)))
    assert completed == 1
    assert retention.preview_maintenance(env.db, batch_size=1, deleted_only=True)["conversation_candidates"] == 1
    retention.run_maintenance(env.db, batch_size=1, deleted_only=True, ingestion=env.service)
    assert retention.preview_maintenance(env.db, batch_size=1, deleted_only=True)["conversation_candidates"] == 0


def test_purge_rejects_unbounded_batches_before_accessing_database(monkeypatch):
    monkeypatch.setattr("app.database.engine.session_scope", lambda: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(SystemExit) as error:
        purge.main(["--batch-size", "1001", "--apply"])
    assert error.value.code == 2
