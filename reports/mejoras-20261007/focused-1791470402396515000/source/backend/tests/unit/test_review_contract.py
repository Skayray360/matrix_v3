# Creado por Aldo Garcia.
"""Revision integral: funciones reales, documentos y transporte sinteticos."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.common.answers import safe_nonfactual_text
from app.common.chat_failures import chat_failure_code
from app.common.errors import AnswerValidationError
from app.common.timing import timed_stage
from app.config import get_settings
from app.database.models import ConversationMessage
from app.llm.model_policy import Intent, ModelPolicy
from app.rag.retriever import RetrievalResult
from tests.conftest import make_context
from tests.unit import test_implementation_v2
from tests.unit.test_conversation_policy_stage1 import synthetic_evidence
from tests.unit.test_rh_routing import ControlledLlm, services

sql = test_implementation_v2.sql


@pytest.mark.parametrize("question", ["Explícame sobre la carta de beneficiarios", "Qué es la fotosíntesis"])
def test_conceptual_request_looks_up_authorized_data_before_generation(sql, monkeypatch, question):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    db, _, _ = sql
    ctx = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    source = synthetic_evidence("formulario", "La carta requiere nombres completos y firma.")
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult(evidences=(source,), best_score=.9)
    llm = ControlledLlm(f"{source.text} [[{source.source_id}]]")
    orchestrator, conversation, _, _ = services(db, ctx, categories=ctx.allowed_categories,
                                               retriever=retriever, llm=llm)
    result = orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message=question)
    args = retriever.retrieve.call_args.kwargs
    assert args["question"] == question
    assert args["authorized_categories"] == ctx.allowed_categories
    assert args["conversation_id"] == conversation.id and args["ctx"] is ctx
    assert source.text in str(llm.calls)
    assert result.answer == llm.answer and result.public_sources()


def test_compound_identity_does_not_bypass_document_lookup():
    policy = ModelPolicy()
    assert policy.classify_intent("Quién eres") is Intent.IDENTITY
    assert policy.classify_intent("Quién eres y cómo lleno la carta de beneficiarios") is Intent.DOCUMENTAL


@pytest.mark.parametrize("generated", [
    "¿Qué documento desea consultar?", "¿Puede precisar el nombre del beneficio?",
])
def test_missing_evidence_uses_generated_clarification(sql, generated):
    db, _, _ = sql
    ctx = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult()
    llm = ControlledLlm(generated)
    orchestrator, conversation, _, _ = services(db, ctx, categories=ctx.allowed_categories,
                                               retriever=retriever, llm=llm)
    result = orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message="Qué es el plan flexible")
    assert result.answer == generated and result.answer_basis == "insufficient"
    assert len(llm.calls) == 1 and result.model
    assert result.public_sources() == []
    assert "Redacta solamente una pregunta" in llm.calls[0][0]["content"]


@pytest.mark.parametrize("draft", [
    "Se otorgan 999 pesos.", "¿Qué documento garantiza un vehículo a todos?", "No tengo información, pero todos reciben un coche.",
])
def test_fabrication_without_sources_is_an_error_not_a_published_answer(sql, draft):
    db, _, _ = sql
    ctx = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult()
    llm = ControlledLlm(draft)
    orchestrator, conversation, _, _ = services(db, ctx, categories=ctx.allowed_categories,
                                               retriever=retriever, llm=llm)
    with pytest.raises(AnswerValidationError) as failed:
        orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message="Qué es el plan flexible")
    assert chat_failure_code(failed.value) == "answer_unverified"
    assert not db.scalars(select(ConversationMessage).where(ConversationMessage.role == "assistant")).all()


@pytest.mark.parametrize("text", [
    "No dispongo de evidencia suficiente. ¿Qué documento desea consultar?",
    "No encontré información pertinente para responder la consulta.",
    "No puedo confirmar la vigencia con las fuentes disponibles.",
])
def test_nonfactual_redaction_does_not_require_a_fixed_answer(text):
    assert safe_nonfactual_text(text)
    assert not safe_nonfactual_text(text + " La empresa paga 999 pesos.")


def test_timing_is_bounded_and_does_not_log_content(caplog):
    @timed_stage("synthetic-stage")
    def recurse(depth):
        if depth:
            return recurse(depth - 1)
        return "PRIVATE_TEST_CONTENT"

    with caplog.at_level("INFO"):
        assert recurse(3) == "PRIVATE_TEST_CONTENT"
    events = [r for r in caplog.records if r.message == "chat.stage"]
    assert len(events) == 1 and events[0].status == "ok" and events[0].duration_ms >= 0
    assert "PRIVATE_TEST_CONTENT" not in str([r.__dict__ for r in events])


@pytest.mark.parametrize("intent", [Intent.GENERAL, Intent.DOCUMENTAL])
def test_documentary_generation_cannot_add_unbacked_general_section(monkeypatch, intent):
    from app.agents.knowledge_agent import KnowledgeAgent
    from tests.unit.test_rag_citation_transport import Client

    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    monkeypatch.setattr(get_settings(), "answer_allow_general_knowledge", True)
    source = synthetic_evidence("formulario", "La carta requiere nombres completos y firma.")
    mixed = f"{source.text} [[E1]]\n\n### Orientación general\nLa empresa concede beneficios adicionales."
    client = Client(mixed, f"{source.text} [[E1]]")
    result = KnowledgeAgent(llm=client, policy=ModelPolicy()).synthesize(
        question="Explica el formulario", evidences=(source,), model_name="gemma4:latest", intent=intent,
    )
    assert result.regenerated and result.answer_basis == "documented"
    assert len(client.calls) == 2
    assert all("conocimiento general esta deshabilitada" in call["messages"][0]["content"] for call in client.calls)
    assert all(source.text in call["messages"][1]["content"] for call in client.calls)
    assert "adicionales" not in result.answer


def test_long_generated_text_is_persisted_and_returned_in_full(sql):
    db, _, _ = sql
    ctx = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult()
    answer = "Introducción completa.\n\n" + "Explicación general con acentos y símbolos.\n\n" * 300 + "FIN COMPLETO"
    llm = ControlledLlm(answer)
    orchestrator, conversation, _, _ = services(db, ctx, categories=ctx.allowed_categories,
                                               retriever=retriever, llm=llm)
    result = orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message="Qué es la fotosíntesis")
    stored = db.scalars(select(ConversationMessage).where(ConversationMessage.role == "assistant")).one()
    assert stored.content == result.answer
    assert result.answer.removeprefix("### Orientación general\n\n") == answer
    assert result.answer_basis == "general" and not result.public_sources()


@pytest.mark.parametrize("claim,accepted", [
    ("Si ingresaste en mayo de 2020 y tienes 6 años y 6 meses, el cálculo es de 63%.", True),
    ("Si ingresaste en abril de 2020 y tienes 6 años y 6 meses, el cálculo es de 63%.", False),
    ("Si ingresaste en mayo de 2020 y tienes 6 años y 6 meses, el cálculo es de 81%.", False),
])
def test_conditional_application_uses_facts_without_required_opening(claim, accepted):
    from tests.unit.test_conditional_contract import cite, verify
    assert verify(cite(claim)).grounded is accepted


@pytest.mark.parametrize("question,has_anchor", [("¿Qué requisitos tiene?", True), ("Qué es la fotosíntesis", False)])
def test_documented_concept_remains_context_only_until_topic_changes(sql, monkeypatch, question, has_anchor):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    db, _, _ = sql
    ctx = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    source = synthetic_evidence("formulario", "La carta requiere nombres completos y firma.")
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult(evidences=(source,), best_score=.9)
    llm = ControlledLlm(f"{source.text} [[{source.source_id}]]")
    orchestrator, conversation, memory, _ = services(db, ctx, categories=ctx.allowed_categories,
                                                   retriever=retriever, llm=llm)
    original = "Explícame sobre la carta de beneficiarios"
    first = orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message=original)
    assert first.answer_basis == "documented"
    context = memory.build_context(db, ctx, conversation, authorized_categories=ctx.allowed_categories)
    assert context.turns[-1].documented
    retriever.retrieve.return_value = RetrievalResult()
    llm.answer = "¿Qué documento desea consultar?" if has_anchor else "La fotosíntesis usa energía luminosa."
    result = orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message=question)
    sent = retriever.retrieve.call_args.kwargs["question"]
    assert (original in sent) is has_anchor
    assert source.text not in sent  # La respuesta anterior nunca es evidencia de consulta.
    assert not result.public_sources()
    assert result.answer_basis == ("insufficient" if has_anchor else "general")


def test_documented_history_flag_does_not_resurrect_an_older_topic():
    policy = ModelPolicy()
    assert policy.contextual_reference("¿Qué requisitos tiene?",
        prior_questions=("Explícame la carta de beneficiarios", "Qué es la fotosíntesis"),
        documented_indices=frozenset({0}),
    ) == ""



def test_capability_instructions_survive_memory_removal_on_fallback(monkeypatch):
    from app.agents.knowledge_agent import KnowledgeAgent
    from app.llm.model_policy import ModelChoice
    from app.memory.service import ConversationContext
    from tests.unit.test_rag_citation_transport import Client

    monkeypatch.setattr(get_settings(), "ollama_deep_model", "synthetic-large-model")
    monkeypatch.setattr(get_settings(), "answer_allow_general_knowledge", False)
    policy = ModelPolicy()
    client = Client("Puedo consultar documentos autorizados.")
    result = KnowledgeAgent(llm=client, policy=policy).answer_general(
        question="Qué puedes hacer", memory=ConversationContext(conversation_id="synthetic", summary="x" * 100000),
        model_name=policy.deep_model, choice=ModelChoice.DEEP, intent=Intent.CAPABILITIES,
    )
    assert result.model == policy.fast_model
    assert len(client.calls) == 1
    system = client.calls[0]["messages"][0]["content"]
    assert "Capacidades comprobadas" in system and "Conocimiento general habilitado: False" in system
    assert "MEMORIA_CONVERSACION" not in client.calls[0]["messages"][1]["content"]
