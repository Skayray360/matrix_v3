# Creado por Aldo Garcia.
"""Publicacion transaccional comun a HTTP y cola, con SQL y memoria reales."""

from __future__ import annotations

from dataclasses import replace
from itertools import count
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker

from app.agents import chat_service
from app.agents.orchestrator import ChatOutcome
from app.common.errors import ForbiddenError, NotFoundError, OllamaUnavailableError, ValidationFailedError
from app.database.models import Base, ChatOperation, Conversation, ConversationMessage
from app.memory.service import MemoryService
from tests.conftest import make_context

pytestmark = pytest.mark.unit


@pytest.fixture
def publication_db(tmp_path, monkeypatch):
    """Una segunda conexion puede observar cambios confirmados durante la IA."""
    engine = create_engine(f"sqlite:///{tmp_path / 'chat-publication.db'}")
    Base.metadata.create_all(engine)
    # SQLite no provee AUTO_INCREMENT para seq, que no es la PK de MySQL.
    sequence = count(1)

    def assign_seq(mapper, connection, target):
        if target.seq is None:
            target.seq = next(sequence)

    event.listen(ConversationMessage, "before_insert", assign_seq)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(chat_service, "get_sessionmaker", lambda: factory)
    context = make_context(permissions=frozenset(), sources=frozenset(), wildcard=False)
    memory = MemoryService()
    with factory() as db:
        conversation = memory.create_conversation(db, context)
        scope = chat_service.authorization_scope(db, context)
        operation = ChatOperation(
            id="e" * 64, user_id=context.user_id, conversation_id=conversation.id,
            request_hash="f" * 64, authorization_scope=scope, status="running",
            message="Consulta sintetica.", session_id=context.session_id,
        )
        db.add(operation)
        db.commit()
    yield SimpleNamespace(
        factory=factory, context=context, conversation_id=conversation.id,
        operation_id=operation.id, scope=scope, memory=memory,
    )
    event.remove(ConversationMessage, "before_insert", assign_seq)
    engine.dispose()


def install_generation(monkeypatch, state, *, during_inference=None, error=None, invalid_result=False):
    """Mantiene el commit previo a IA y el mensaje asistente pendiente de publicacion."""
    def handle_chat(db, *, ctx, conversation, message):
        state.memory.append_message(db, conversation, role="user", content=message)
        db.commit()
        if during_inference is not None:
            with state.factory() as concurrent_db:
                during_inference(concurrent_db)
                concurrent_db.commit()
        assistant = state.memory.append_message(
            db, conversation, role="assistant", content="Resultado sintetico.", intent="general"
        )
        if error is not None:
            raise error
        return ChatOutcome(
            answer=None if invalid_result else assistant.content,
            conversation_id=conversation.id, message_id=assistant.id, model="synthetic",
            intent="general", grounded=False, latency_ms=5,
        )

    monkeypatch.setattr(chat_service, "get_orchestrator", lambda: SimpleNamespace(handle_chat=handle_chat))


def execute(state, *, reauthorize=None, track_operation=True):
    with state.factory() as db:
        return chat_service.execute_chat(
            db, ctx=state.context, conversation=db.get(Conversation, state.conversation_id),
            message="Consulta sintetica.", expected_scope=state.scope,
            operation_id=state.operation_id if track_operation else None,
            reauthorize=reauthorize or (lambda check_db: state.context),
        )


def persisted(state):
    with state.factory() as db:
        operation = db.get(ChatOperation, state.operation_id)
        messages = db.scalars(select(ConversationMessage).order_by(ConversationMessage.seq)).all()
        return operation, messages


def assert_discarded(state, *, status="failed"):
    operation, messages = persisted(state)
    assert operation.status == status
    assert operation.response is None
    assert operation.message is None
    assert operation.session_id is None
    assert [(message.role, message.content) for message in messages] == [("user", "Consulta sintetica.")]


def test_publication_commits_response_and_assistant_after_fresh_reauthorization(publication_db, monkeypatch):
    state = publication_db
    install_generation(monkeypatch, state)
    observations = []

    def reauthorize(check_db):
        # La conexion ve el mensaje de usuario confirmado antes de la IA,
        # pero no el asistente pendiente en la transaccion de ejecucion.
        observations.append(check_db.scalars(select(ConversationMessage.role)).all())
        assert check_db.get(ChatOperation, state.operation_id).status == "running"
        return state.context

    result = execute(state, reauthorize=reauthorize)
    operation, messages = persisted(state)
    assert observations == [["user"]]
    assert result.answer == "Resultado sintetico."
    assert result.sources == []
    assert operation.status == "completed"
    assert operation.response == result.model_dump()
    assert operation.message is None
    assert operation.session_id is None
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[-1].id == result.message_id


def test_provider_failure_rolls_back_pending_assistant_and_purges_operation(publication_db, monkeypatch):
    state = publication_db
    install_generation(monkeypatch, state, error=OllamaUnavailableError("Fallo sintetico del proveedor."))
    with pytest.raises(OllamaUnavailableError, match="Fallo sintetico"):
        execute(state)
    assert_discarded(state)


def test_revoked_scope_discards_generated_answer(publication_db, monkeypatch):
    state = publication_db
    install_generation(monkeypatch, state)
    current = replace(state.context, roles=frozenset({"changed-role"}))
    with pytest.raises(ForbiddenError, match="permisos cambiaron"):
        execute(state, reauthorize=lambda check_db: current)
    assert_discarded(state)


def test_conversation_deleted_during_inference_is_not_published(publication_db, monkeypatch):
    from app.common.ids import utcnow_naive

    state = publication_db

    def delete_conversation(concurrent_db):
        concurrent_db.execute(
            update(Conversation).where(Conversation.id == state.conversation_id).values(deleted_at=utcnow_naive())
        )

    install_generation(monkeypatch, state, during_inference=delete_conversation)
    with pytest.raises(NotFoundError, match="Conversacion no encontrada"):
        execute(state)
    assert_discarded(state)


@pytest.mark.parametrize("terminal_status", ["cancelled", "expired"])
def test_terminal_operation_discards_answer_without_overwriting_cancellation(
    publication_db, monkeypatch, terminal_status,
):
    state = publication_db

    def terminate(concurrent_db):
        concurrent_db.execute(
            update(ChatOperation).where(ChatOperation.id == state.operation_id)
            .values(status=terminal_status, message=None, session_id=None)
        )

    install_generation(monkeypatch, state, during_inference=terminate)
    with pytest.raises(ValidationFailedError, match="cancelada o expirada"):
        execute(state)
    assert_discarded(state, status=terminal_status)


def test_invalid_outcome_is_rolled_back_before_operation_completion(publication_db, monkeypatch):
    state = publication_db
    install_generation(monkeypatch, state, invalid_result=True)
    with pytest.raises(ValidationError):
        execute(state)
    assert_discarded(state)


def test_compatible_chat_without_idempotency_key_still_reauthorizes_and_commits(publication_db, monkeypatch):
    state = publication_db
    install_generation(monkeypatch, state)
    with state.factory() as db:
        operation = db.get(ChatOperation, state.operation_id)
        db.delete(operation)
        db.commit()
    result = execute(state, track_operation=False)
    operation, messages = persisted(state)
    assert operation is None
    assert result.conversation_id == state.conversation_id
    assert result.message_id == messages[-1].id
    assert [message.role for message in messages] == ["user", "assistant"]
