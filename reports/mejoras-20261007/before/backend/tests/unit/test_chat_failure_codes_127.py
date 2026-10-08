# Creado por Aldo Garcia.
"""Estado HTTP de fallos y compatibilidad SQL 1.2.7, con sesiones sinteticas."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.agents import chat_queue, chat_service
from app.agents.orchestrator import Orchestrator
from app.common.chat_failures import chat_failure_code
from app.common.errors import ForbiddenError, OllamaUnavailableError
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.database.migrator import discover_migrations, split_statements
from app.database.models import ChatOperation, Conversation, ConversationMessage
from app.llm.model_policy import Intent, ModelPolicy
from app.memory.service import MemoryService
from app.rag.retriever import RetrievalResult
from tests import test_chat_queue as queue_testing
from tests.conftest import make_context
from tests.unit import test_implementation_v2

pytestmark = pytest.mark.unit
queue_db = queue_testing.queue_db
controlled_queue = queue_testing.controlled_queue
sql = test_implementation_v2.sql


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (InferenceFailureError(InferenceFailureKind.TIMEOUT), "timeout"),
        (InferenceFailureError(InferenceFailureKind.DEADLINE), "timeout"),
        (InferenceFailureError(InferenceFailureKind.INCOMPLETE), "model_incomplete"),
        (InferenceFailureError(InferenceFailureKind.EMPTY), "model_incomplete"),
        (InferenceFailureError(InferenceFailureKind.CONTEXT_LIMIT), "model_incomplete"),
        (InferenceFailureError(InferenceFailureKind.FORMAT), "model_incomplete"),
        (InferenceFailureError(InferenceFailureKind.REASONING), "model_incomplete"),
        (InferenceFailureError(InferenceFailureKind.SCHEMA), "model_incomplete"),
        (InferenceFailureError(InferenceFailureKind.TRANSPORT), "inference_unavailable"),
        (InferenceFailureError(InferenceFailureKind.HTTP, http_status=404), "inference_unavailable"),
        (OllamaUnavailableError(detail="ReadTimeout"), "timeout"),
        (OllamaUnavailableError(detail="SENSITIVE_PROVIDER_DETAIL"), "inference_unavailable"),
        (ForbiddenError(), "forbidden"),
        (RuntimeError("SENSITIVE_PROMPT_AND_SQL"), "generation_failed"),
    ),
)
def test_accepted_operation_failure_is_persisted_and_pollable(
    queue_db, controlled_queue, monkeypatch, caplog, error, expected,
):
    request_id = "request-failure-code-123456"
    calls = []

    def failure(db, *, ctx, conversation, message):
        db.commit()
        calls.append((ctx.user_id, conversation.id))
        raise error

    monkeypatch.setattr(chat_service, "get_orchestrator", lambda: SimpleNamespace(handle_chat=failure))
    with queue_testing.authenticated_client(queue_db) as owner, queue_testing.authenticated_client(queue_db) as other:
        accepted = queue_testing.submit(owner, request_id, message="SENSITIVE_PENDING_PROMPT")
        assert accepted.status_code == 202
        before = owner.operation(request_id)
        assert before.status == "running"
        assert before.error_code is None
        assert accepted.json()["operation_id"] == before.id
        assert accepted.json()["error_code"] is None
        assert before.message == "SENSITIVE_PENDING_PROMPT"
        assert before.session_id == owner.session_id

        controlled_queue.manager._run(before.id, controlled_queue.launches[before.id])
        result = queue_testing.status(owner, request_id)
        assert result.status_code == 200
        payload = result.json()
        assert payload["status"] == "failed"
        assert payload["error_code"] == expected
        assert payload["operation_id"] == before.id
        assert payload["response"] is None
        assert payload["notice"] is None
        assert "SENSITIVE" not in result.text
        persisted = owner.operation(request_id)
        assert persisted.error_code == expected
        assert persisted.message is None
        assert persisted.session_id is None
        assert persisted.response is None
        # Un fallo conserva la idempotencia y no se vuelve a ejecutar al reenviar.
        duplicate = queue_testing.submit(owner, request_id, message="SENSITIVE_PENDING_PROMPT")
        assert duplicate.status_code == 202
        assert duplicate.json()["status"] == "failed"
        assert duplicate.json()["error_code"] == expected
        assert duplicate.json()["operation_id"] == before.id
        assert queue_testing.status(other, request_id).status_code == 404
        assert len(calls) == 1
    assert "SENSITIVE" not in caplog.text
    assert "SENSITIVE" not in str([record.__dict__ for record in caplog.records])


def test_unknown_sql_exception_never_exposes_statement_parameters_or_driver_message(
    queue_db, controlled_queue, monkeypatch, caplog,
):
    original = RuntimeError("SENSITIVE_SQL_DRIVER_MESSAGE")
    error = OperationalError("SELECT SENSITIVE_SQL_STATEMENT", {"value": "SENSITIVE_PARAMETER"}, original)

    def failure(db, **_kwargs):
        db.commit()
        raise error

    monkeypatch.setattr(chat_service, "get_orchestrator", lambda: SimpleNamespace(handle_chat=failure))
    with queue_testing.authenticated_client(queue_db) as owner:
        assert queue_testing.submit(owner, "request-sql-error-123456").status_code == 202
        operation = owner.operation("request-sql-error-123456")
        controlled_queue.manager._run(operation.id, controlled_queue.launches[operation.id])
        response = queue_testing.status(owner, "request-sql-error-123456")
        assert response.json()["error_code"] == "generation_failed"
        assert "SENSITIVE" not in response.text
    assert "SENSITIVE" not in str([record.__dict__ for record in caplog.records])


@pytest.mark.parametrize("basis", ("documented", "general", "mixed", "insufficient"))
def test_completed_operation_preserves_declared_answer_basis(queue_db, controlled_queue, monkeypatch, basis):
    request_id = "request-answer-basis-123456"

    def generation(db, *, conversation, **_kwargs):
        db.commit()
        result = queue_testing.outcome(conversation)
        result.answer_basis = basis
        return result

    monkeypatch.setattr(chat_service, "get_orchestrator", lambda: SimpleNamespace(handle_chat=generation))
    with queue_testing.authenticated_client(queue_db) as owner:
        accepted = queue_testing.submit(owner, request_id)
        assert accepted.status_code == 202
        operation = owner.operation(request_id)
        controlled_queue.manager._run(operation.id, controlled_queue.launches[operation.id])
        result = queue_testing.status(owner, request_id).json()
        assert result["status"] == "completed"
        assert result["error_code"] is None
        assert result["operation_id"] == operation.id
        assert result["response"]["answer_basis"] == basis
        stored = owner.operation(request_id)
        assert stored.response["answer_basis"] == basis
        assert stored.error_code is None
        assert stored.message is None
        assert stored.session_id is None


def test_failure_cause_dictionary_has_closed_safe_fields():
    typed = InferenceFailureError(InferenceFailureKind.TIMEOUT)
    assert chat_queue._failure_fields(typed) == {
        "error_type": "InferenceFailureError", "error_code": "ollama_unavailable", "failure_kind": "timeout",
    }
    assert chat_queue._failure_fields(RuntimeError("SENSITIVE_PROMPT")) == {"error_type": "RuntimeError"}
    assert chat_failure_code(OllamaUnavailableError(detail="SENSITIVE_DETAIL")) == "inference_unavailable"


def test_invalid_answer_basis_is_not_published_as_completed(queue_db, controlled_queue, monkeypatch, caplog):
    request_id = "request-invalid-basis-123456"

    def generation(db, *, conversation, **_kwargs):
        db.commit()
        result = queue_testing.outcome(conversation)
        result.answer_basis = "SENSITIVE_INVALID_METADATA"
        return result

    monkeypatch.setattr(chat_service, "get_orchestrator", lambda: SimpleNamespace(handle_chat=generation))
    with queue_testing.authenticated_client(queue_db) as owner:
        assert queue_testing.submit(owner, request_id).status_code == 202
        operation = owner.operation(request_id)
        controlled_queue.manager._run(operation.id, controlled_queue.launches[operation.id])
        result = queue_testing.status(owner, request_id)
        assert result.json()["status"] == "failed"
        assert result.json()["response"] is None
        assert result.json()["error_code"] == "generation_failed"
        assert owner.operation(request_id).message is None
        assert owner.operation(request_id).session_id is None
        assert "SENSITIVE" not in result.text
    assert "SENSITIVE" not in str([record.__dict__ for record in caplog.records])


def test_unknown_failure_kind_attribute_is_never_logged():
    error = RuntimeError("SENSITIVE_RUNTIME_MESSAGE")
    error.failure_kind = "SENSITIVE_UNTRUSTED_ATTRIBUTE"
    fields = chat_queue._failure_fields(error)
    assert "failure_kind" not in fields
    assert "SENSITIVE" not in str(fields)


@pytest.mark.parametrize(
    "question",
    (
        "Explica los datos privados de nómina",
        "Explica el salario de Juan Pérez",
        "Define la información confidencial de los empleados",
        "Explica los registros médicos de Ana",
        "Explica los datos personales de los colaboradores",
        "Hola, explica los datos privados de nómina",
        "Por favor, explica el expediente de Juan Pérez",
        "Explica los tabuladores de nómina",
        "Explica la contraseña del usuario Matrix",
        "¿Qué es el código de empleado de Ana?",
        "Explica quién cobra la cifra exacta del sueldo de Juan",
    ),
)
def test_private_data_requests_cannot_bypass_retrieval_as_general_concepts(question):
    assert ModelPolicy().classify_intent(question) is Intent.DOCUMENTAL
    assert ModelPolicy.requires_internal_evidence(question) is True


@pytest.mark.parametrize(
    "question",
    (
        "Explica los datos privados de nómina", "Explica el salario de Juan Pérez",
        "Define la información confidencial de los empleados",
        "Explica los registros médicos de Ana", "Explica la contraseña del usuario Matrix",
    ),
)
def test_private_request_with_empty_authorized_rag_cannot_enable_general_fallback(sql, question):
    db, _, _ = sql
    context = make_context(
        permissions=frozenset(), categories=frozenset({"prestaciones"}), wildcard=False, sources=frozenset(),
    )
    memory = MemoryService()
    conversation = memory.create_conversation(db, context)
    policies = MagicMock()
    policies.effective_categories.return_value = context.allowed_categories
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult()

    class NeverGeneratesPrivateData:
        def chat(self, **_kwargs):
            from app.llm.ollama_client import ChatResult
            assert "Redacta solamente una pregunta" in _kwargs["messages"][0]["content"]
            return ChatResult(content="¿Qué documento desea consultar?", model=_kwargs["model"], latency_ms=1)

    result = Orchestrator(
        llm=NeverGeneratesPrivateData(), policy_engine=policies, retriever=retriever,
        structured_tool=MagicMock(), memory=memory, audit=MagicMock(),
    ).handle_chat(db, ctx=context, conversation=conversation, message=question)
    assert result.answer == "¿Qué documento desea consultar?"
    assert result.public_sources() == []
    assert result.answer_basis == "insufficient"
    assert retriever.retrieve.call_count == 1
    assert retriever.retrieve.call_args.kwargs["authorized_categories"] == context.allowed_categories


@pytest.mark.parametrize(
    "question",
    (
        "¿Qué es la nómina?", "¿Qué tipos de prestaciones existen?",
        "Define un empleado", "¿Qué es un salario?", "Define un expediente laboral",
        "¿Qué es un tabulador salarial?", "¿Qué es una contraseña?",
        "¿Qué es el salario de un empleado?", "Define el expediente de una persona",
    ),
)
def test_general_rh_concepts_are_still_available(question):
    assert ModelPolicy().classify_intent(question) is Intent.GENERAL


def test_migration_0009_adds_nullable_metadata_and_preserves_legacy_rows(queue_db):
    """Ejecuta exactamente sus dos ALTER en SQLite; no acredita runner MySQL."""
    memory = MemoryService()
    with queue_testing.authenticated_client(queue_db) as owner:
        with queue_db() as db:
            context = make_context(user_id=owner.user_id)
            conversation = memory.create_conversation(db, context)
            message = ConversationMessage(
                id="legacy-message", seq=1, conversation_id=conversation.id, user_id=owner.user_id,
                role="assistant", content="Respuesta anterior que debe conservarse.",
                model="gemma4:latest", intent="documental", source_ids=["prestaciones/reglas.md#0"],
                authorized_categories=["prestaciones"],
            )
            operation = ChatOperation(
                id="a" * 64, user_id=owner.user_id, conversation_id=conversation.id,
                request_hash="b" * 64, authorization_scope="c" * 64, status="completed",
                response={"answer": message.content, "grounded": True},
            )
            db.add_all((message, operation))
            db.commit()
        engine = queue_db.kw["bind"]
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE conversation_messages DROP COLUMN answer_basis"))
            connection.execute(text("ALTER TABLE chat_operations DROP COLUMN error_code"))
            tables = ("users", "sessions", "conversations", "conversation_messages", "chat_operations")
            before = {
                name: [dict(row._mapping) for row in connection.execute(text(f"SELECT * FROM {name}"))]
                for name in tables
            }
            migration = next(item for item in discover_migrations() if item.version == "0009")
            for statement in split_statements(migration.sql):
                connection.execute(text(statement))
            after = {
                name: [dict(row._mapping) for row in connection.execute(text(f"SELECT * FROM {name}"))]
                for name in tables
            }
        assert after["conversation_messages"][0].pop("answer_basis") is None
        assert after["chat_operations"][0].pop("error_code") is None
        assert before == after
        with queue_db() as db:
            restored = db.get(ChatOperation, "a" * 64)
            assert restored.response == {"answer": "Respuesta anterior que debe conservarse.", "grounded": True}
            assert restored.error_code is None
            assistant = db.scalar(select(ConversationMessage).where(ConversationMessage.id == "legacy-message"))
            assert assistant.answer_basis is None
            assert assistant.content == "Respuesta anterior que debe conservarse."
            assert db.get(Conversation, restored.conversation_id).user_id == owner.user_id
