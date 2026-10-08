# Creado por Aldo Garcia.
"""Distingue ausencia de evidencia de una respuesta que no pudo verificarse."""

from unittest.mock import MagicMock

import pytest

from app.agents.knowledge_agent import KnowledgeAgent, SynthesisResult
from app.agents.orchestrator import Orchestrator
from app.common.errors import AnswerValidationError
from app.config import get_settings
from app.llm.model_policy import Intent, ModelPolicy
from app.rag.grounding import GroundingReport
from app.rag.retriever import RetrievalResult
from tests.conftest import make_context
from tests.unit.test_memory_and_agent import EVIDENCIAS, FakeLlm


def test_unverifiable_answer_does_not_claim_no_documents(caplog, monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    llm = FakeLlm(respuestas=[
        "Son 999 dias [[prestaciones/inventado.md#9]].",
        "Son 999 dias [[prestaciones/inventado.md#9]].",
    ])
    with pytest.raises(AnswerValidationError):
        KnowledgeAgent(llm=llm, policy=ModelPolicy()).synthesize(
            question="Cuantos dias?", evidences=EVIDENCIAS, model_name="gemma4:latest",
        )
    assert len(llm.llamadas) == 2
    event = next(r for r in caplog.records if r.message == "agent.answer_validation_failed")
    assert event.evidence_count == 2 and event.invalid_source_count == 1
    assert "999" not in event.message and "inventado.md" not in event.message


def test_missing_evidence_generates_a_guarded_clarification(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    llm = FakeLlm(respuestas=["¿Qué documento desea consultar?"])
    result = KnowledgeAgent(llm=llm, policy=ModelPolicy()).synthesize(
        question="Cuantos dias?", evidences=(), model_name="gemma4:latest",
    )
    assert result.answer == "¿Qué documento desea consultar?"
    assert len(llm.llamadas) == 1


def test_rejected_synthesis_cannot_be_labeled_documented(monkeypatch):
    memory = MagicMock()
    memory.append_message.return_value.id = "synthetic-message"
    orchestrator = Orchestrator(
        llm=FakeLlm(respuestas=[]), retriever=MagicMock(), policy_engine=MagicMock(),
        memory=memory, audit=MagicMock(), structured_tool=MagicMock(),
    )
    monkeypatch.setattr(orchestrator, "_maybe_summarize", lambda *_: None)
    context = make_context()
    conversation = MagicMock(id="synthetic-conversation")
    synthesis = SynthesisResult(
        answer="Respuesta rechazada", model="gemma4:latest", latency_ms=2,
        grounding=GroundingReport(grounded=False, reason="respuesta documental sin fuentes citadas"),
        answer_basis="insufficient", regenerated=True,
    )
    routing = ModelPolicy().route("Segun el documento", intent=Intent.DOCUMENTAL)
    with pytest.raises(AnswerValidationError):
        orchestrator._finish_answer(
            MagicMock(), ctx=context, conversation=conversation, routing=routing,
            synthesis=synthesis, retrieval=RetrievalResult(evidences=EVIDENCIAS),
            structured_results=(), tools_used=["rag"], authorized_categories=frozenset({"prestaciones"}),
            started=0,
        )
    memory.append_message.assert_not_called()



@pytest.mark.parametrize("reason", [
    "respuesta documental sin ninguna cita", "respuesta documental sin fuentes citadas",
])
def test_cited_and_extractive_missing_citations_receive_specific_retry(reason):
    assert "[[source_id]]" in KnowledgeAgent._retry_note(GroundingReport(grounded=False, reason=reason), 0)


def test_numeric_retry_preserves_values_and_units():
    note = KnowledgeAgent._retry_note(GroundingReport(
        grounded=False, reason="afirmacion numerica sin respaldo en sus fuentes citadas",
    ), 0)
    assert "regla citada" in note and "condiciones" in note
