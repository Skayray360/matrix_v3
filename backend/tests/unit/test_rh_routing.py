# Creado por Aldo Garcia.
"""Preguntas RH ambiguas: evidencia autorizada o abstencion, sin inferencia libre.

SQLite y Qdrant en memoria son descartables. Los vectores iguales solo fuerzan
coincidencias para ejercitar ACL; no miden calidad semantica ni un modelo real.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agents import chat_service
from app.agents.orchestrator import Orchestrator
from app.agents.prompts import answer_system_policy
from app.api.routes import conversations
from app.common.answers import UNVERIFIED_ANSWER_NOTICE, safe_nonfactual_text
from app.common.errors import ForbiddenError
from app.config import get_settings
from app.database.models import ConversationMessage
from app.llm.model_policy import Intent, ModelPolicy
from app.llm.ollama_client import ChatResult
from app.memory.service import MemoryService, authorization_fingerprint
from app.rag.retriever import RetrievalResult, Retriever
from app.rag.schemas import SCOPE_CONVERSATION, Evidence
from app.rag.vector_store import VectorStore
from tests.conftest import make_context
from tests.unit import test_implementation_v2
from tests.unit.test_rag_pipeline_isolated import DIMENSION, make_chunk

pytestmark = pytest.mark.unit
sql = test_implementation_v2.sql


@pytest.fixture(autouse=True)
def markdown_transport(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_structured_output", False)

RH_QUESTIONS = (
    "¿Cuántos días me dan si me caso?",
    "¿Qué pasa si tengo 3 retardos?",
    "¿Cómo registro a mi pareja en el IMSS?",
    "¿Cuál es el importe del bono?",
    "Hola, ¿cómo entrego la incapacidad del IMSS?",
    "¿Cuánto se entrega por el apoyo de casamiento?",
    "Hola, ¿cuánto me dan cuando nazca mi bebé?",
    "Gracias, necesito tiempo para atender a mi mamá",
    "Explícame Python y cuánto me dan si me caso",
    "Explica qué es el aprendizaje automático en nuestra empresa",
    "Conocimiento general: ¿qué pasa si tengo 3 retardos?",
)


@pytest.mark.parametrize("question", RH_QUESTIONS)
def test_ambiguous_rh_never_routes_to_general_or_greeting(question):
    assert ModelPolicy().classify_intent(question) is Intent.DOCUMENTAL


@pytest.mark.parametrize(
    "question",
    (
        "¿Qué es la fotosíntesis?",
        "Explica el aprendizaje automático",
        "Explícame Python para principiantes",
        "Define el teorema de Pitágoras",
        "Explica la fotosíntesis en profundidad.",
    ),
)
def test_only_declared_complete_conceptual_requests_are_general(question):
    assert ModelPolicy().classify_intent(question) is Intent.GENERAL


@pytest.mark.parametrize("question", ("¡Hola!", "Buenos días.", "Muchas gracias", "Hasta luego"))
def test_complete_greetings_remain_conversational(question):
    assert ModelPolicy().classify_intent(question) is Intent.CONVERSATIONAL


class ControlledLlm:
    def __init__(self, answer="¿Qué documento desea consultar?"):
        self.answer = answer
        self.calls = []

    def embed_one(self, _text):
        return [1.0] + [0.0] * (DIMENSION - 1)

    def chat(self, *, model, messages, **kwargs):
        self.calls.append(messages)
        assert self.answer, "No debe inferir una politica sin evidencia."
        return ChatResult(content=self.answer, model=model, latency_ms=1)


def services(db, context, *, categories, retriever, llm):
    memory = MemoryService()
    conversation = memory.create_conversation(db, context)
    policies = MagicMock()
    policies.effective_categories.return_value = categories
    orchestrator = Orchestrator(
        llm=llm, policy_engine=policies, retriever=retriever,
        structured_tool=MagicMock(), memory=memory, audit=MagicMock(),
    )
    return orchestrator, conversation, memory, policies


@pytest.mark.parametrize("question", RH_QUESTIONS)
def test_rh_without_evidence_generates_only_clarification(sql, question):
    db, _, _ = sql
    context = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    retrieval = MagicMock()
    retrieval.retrieve.return_value = RetrievalResult()
    llm = ControlledLlm()
    orchestrator, conversation, _, _ = services(
        db, context, categories=context.allowed_categories, retriever=retrieval, llm=llm,
    )
    outcome = orchestrator.handle_chat(db, ctx=context, conversation=conversation, message=question)
    assert outcome.intent == "documental"
    assert outcome.answer == llm.answer
    assert outcome.public_sources() == []
    assert len(llm.calls) == 1
    assert "Redacta solamente una pregunta" in llm.calls[0][0]["content"]
    assert retrieval.retrieve.call_count == 1
    assert retrieval.retrieve.call_args.kwargs["authorized_categories"] == context.allowed_categories



@pytest.mark.parametrize("allowed", (False, True))
def test_unknown_rh_uses_real_acl_before_model(sql, manifest_db, allowed):
    db, _, _ = sql
    client = QdrantClient(location=":memory:")
    store = VectorStore(client=client)
    llm = ControlledLlm()
    text = "Tres retardos en un mes equivalen a una falta para el bono de puntualidad."
    public_chunk = make_chunk(text, category="relaciones_laborales", document_id="retardos")
    private_chunk = make_chunk(
        "OTRO_USUARIO_SECRETO: concedemos 99 dias.", category="private",
        document_id="private-other", scope=SCOPE_CONVERSATION,
        owner="other-user", conversation="other-conversation",
    )
    vector = llm.embed_one("")
    store.upsert_chunks([public_chunk, private_chunk], [vector, vector])
    categories = frozenset({"relaciones_laborales"}) if allowed else frozenset()
    context = make_context(categories=categories, wildcard=False)
    llm.answer = f"{text} [[{public_chunk.metadata.source_id}]]"
    orchestrator, conversation, _, _ = services(
        db, context, categories=categories, retriever=Retriever(store=store, llm=llm), llm=llm,
    )
    try:
        if not allowed:
            with pytest.raises(ForbiddenError):
                orchestrator.handle_chat(db, ctx=context, conversation=conversation, message="¿Qué pasa si tengo 3 retardos?")
            assert llm.calls == []
            return
        outcome = orchestrator.handle_chat(
            db, ctx=context, conversation=conversation, message="¿Qué pasa si tengo 3 retardos?",
        )
        if allowed:
            assert outcome.grounded is True
            assert outcome.intent == "documental"
            assert len(llm.calls) == 1
            assert [source["source_id"] for source in outcome.public_sources()] == [public_chunk.metadata.source_id]
            assert "OTRO_USUARIO_SECRETO" not in str(llm.calls)
        assert "99 dias" not in outcome.answer
    finally:
        client.close()



def test_authorized_followup_retrieves_previous_topic_instead_of_general(sql):
    db, _, _ = sql
    context = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult()
    orchestrator, conversation, memory, _ = services(
        db, context, categories=context.allowed_categories, retriever=retriever, llm=ControlledLlm(),
    )
    db.info["authorization_scope"] = authorization_fingerprint(db, context, context.allowed_categories)
    memory.append_message(db, conversation, role="user", content="¿Cuántos días me dan si me caso?")
    outcome = orchestrator.handle_chat(db, ctx=context, conversation=conversation, message="¿Y en ese caso?")
    assert outcome.answer == "¿Qué documento desea consultar?"
    assert "me caso" in retriever.retrieve.call_args.kwargs["question"]
    assert "Seguimiento:" in retriever.retrieve.call_args.kwargs["question"]



def test_revoked_memory_never_supplies_rh_fact(sql):
    db, _, _ = sql
    original = make_context(categories=frozenset({"relaciones_laborales"}), wildcard=False)
    current = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult()
    orchestrator, conversation, memory, _ = services(
        db, current, categories=current.allowed_categories, retriever=retriever, llm=ControlledLlm(),
    )
    db.info["authorization_scope"] = authorization_fingerprint(db, original, original.allowed_categories)
    memory.append_message(
        db, conversation, role="user", content="¿Qué pasa si tengo 3 retardos?",
        authorized_categories=("relaciones_laborales",),
    )
    memory.append_message(
        db, conversation, role="assistant", content="SECRETO_ANTERIOR: tres retardos equivalen a una falta.",
        source_ids=("relaciones_laborales/retardos.md#0",), authorized_categories=("relaciones_laborales",),
    )
    outcome = orchestrator.handle_chat(db, ctx=current, conversation=conversation, message="¿Y en ese caso?")
    assert outcome.answer == "¿Qué documento desea consultar?"
    assert retriever.retrieve.call_args.kwargs["question"] == "¿Y en ese caso?"
    assert "SECRETO_ANTERIOR" not in outcome.answer



def test_old_general_policy_claim_never_becomes_rh_evidence(sql):
    db, _, _ = sql
    context = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult()
    llm = ControlledLlm()
    orchestrator, conversation, memory, _ = services(
        db, context, categories=context.allowed_categories, retriever=retriever, llm=llm,
    )
    db.info["authorization_scope"] = authorization_fingerprint(db, context, context.allowed_categories)
    memory.append_message(
        db, conversation, role="assistant", content="INVENTADO: concedemos 99 dias por casamiento.", intent="general",
    )
    outcome = orchestrator.handle_chat(
        db, ctx=context, conversation=conversation, message="¿Cuántos días me dan si me caso?",
    )
    assert outcome.answer == "¿Qué documento desea consultar?"
    assert "99" not in outcome.answer
    assert len(llm.calls) == 1
    assert "INVENTADO" not in str(llm.calls)



def test_weak_retrieval_never_enables_unverified_general_fallback(sql):
    db, _, _ = sql
    context = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    retriever = MagicMock()
    source = Evidence(
        source_id="prestaciones/registro.md#0", text="La solicitud requiere registro previo.",
        score=0.36, category="prestaciones", filename="registro.md", section="",
        page_or_sheet="", document_id="doc", chunk_id="chunk",
    )
    retriever.retrieve.return_value = RetrievalResult(evidences=(source,), best_score=0.36)
    llm = ControlledLlm("INVENTADO: concedemos 99 dias por casamiento.")
    orchestrator, conversation, _, _ = services(
        db, context, categories=context.allowed_categories, retriever=retriever, llm=llm,
    )
    result = orchestrator.handle_chat(
        db, ctx=context, conversation=conversation, message="¿Cuántos días me dan si me caso?",
    )
    assert result.answer.startswith(UNVERIFIED_ANSWER_NOTICE) and safe_nonfactual_text(result.answer)
    assert result.public_sources() == [] and result.answer_basis == "insufficient"
    assert "INVENTADO" not in result.answer and "99" not in result.answer
    assert len(llm.calls) == 2
    assert all(messages[0]["content"] == answer_system_policy(documentary_only=True) for messages in llm.calls)



def test_rh_revocation_during_generation_discards_answer(sql, monkeypatch):
    db, _, engine = sql
    context = make_context(categories=frozenset({"relaciones_laborales"}), wildcard=False)
    source_text = "Tres retardos requieren revision de Relaciones Laborales."
    retriever = MagicMock()
    source = Evidence(
        source_id="relaciones_laborales/retardos.md#0", text=source_text,
        score=0.9, category="relaciones_laborales", filename="retardos.md", section="",
        page_or_sheet="", document_id="doc", chunk_id="chunk",
    )
    retriever.retrieve.return_value = RetrievalResult(evidences=(source,), best_score=0.9)
    llm = ControlledLlm(f"{source_text} [[{source.source_id}]]")
    orchestrator, conversation, _, policies = services(
        db, context, categories=context.allowed_categories, retriever=retriever, llm=llm,
    )
    initial_scope = authorization_fingerprint(db, context, context.allowed_categories)
    original_chat = llm.chat

    def revoke(**kwargs):
        policies.effective_categories.return_value = frozenset()
        return original_chat(**kwargs)

    llm.chat = revoke
    monkeypatch.setattr(chat_service, "get_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(chat_service, "get_policy_engine", lambda: policies)
    # La comprobacion al publicar necesita su propia conexion breve, igual
    # que el servicio real. El pool de la fixture principal tiene una sola.
    check_engine = create_engine(engine.url)
    check_factory = sessionmaker(check_engine, expire_on_commit=False)
    monkeypatch.setattr(chat_service, "get_sessionmaker", lambda: check_factory)
    try:
        with pytest.raises(ForbiddenError, match="permisos cambiaron"):
            chat_service.execute_chat(
                db, ctx=context, conversation=conversation, message="¿Qué pasa si tengo 3 retardos?",
                expected_scope=initial_scope, operation_id=None, reauthorize=lambda _db: context,
            )
    finally:
        check_engine.dispose()
    stored = db.execute(select(ConversationMessage).where(ConversationMessage.role == "assistant")).scalars().all()
    assert stored == []


def test_history_preserves_general_provenance(sql, monkeypatch):
    db, _, _ = sql
    context = make_context(categories=frozenset(), wildcard=False)
    memory = MemoryService()
    conversation = memory.create_conversation(db, context)
    db.info["authorization_scope"] = authorization_fingerprint(db, context, context.allowed_categories)
    memory.append_message(db, conversation, role="assistant", content="La fotosintesis transforma energia.", intent="general")
    policies = MagicMock()
    policies.effective_categories.return_value = context.allowed_categories
    monkeypatch.setattr(conversations, "get_policy_engine", lambda: policies)
    detail = conversations.get_conversation(conversation.id, db=db, ctx=context, before_seq=None)
    assert detail.messages[0].intent == "general"
    assert detail.messages[0].sources == []
