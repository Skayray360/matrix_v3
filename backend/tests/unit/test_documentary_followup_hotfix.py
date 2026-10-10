# Creado por Aldo Garcia.
"""Regresiones del seguimiento documental y contrato JSON, sin inferencia real."""

from dataclasses import replace
from fractions import Fraction

import pytest

from app.agents.contextual_query import contextualize_question, is_case_followup
from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.prompts import build_answer_messages
from app.config import get_settings
from app.llm.model_policy import ModelPolicy
from app.rag.claim_context import declared_case
from tests.unit.test_calculated_application import QUESTION, SOURCE
from tests.unit.test_documentary_output import Client, payload

pytestmark = pytest.mark.unit

REFERENCE = (
    "Según el documento Programa Jubilación diciembre 2041, ¿qué establece la sección de portabilidad? "
    "Indica documento y página."
)
FOLLOWUP = QUESTION.replace("el documento Programa Jubilación diciembre 2041", "ese documento")


@pytest.mark.parametrize("reference_word", ["ese documento", "dicho documento", "el mismo documento"])
def test_explicit_document_anaphora_preserves_only_the_authorized_title(reference_word):
    question = FOLLOWUP.replace("ese documento", reference_word)
    policy = ModelPolicy()
    reference = policy.contextual_reference(question, prior_questions=(REFERENCE,), documented_indices=frozenset({0}))
    assert reference == REFERENCE
    resolved = contextualize_question(question, reference)
    assert "Programa Jubilación diciembre 2041" in resolved
    assert "qué establece" not in resolved
    assert resolved.count("?") == 1
    assert declared_case(resolved).tenure == Fraction(13, 2)
    assert declared_case(resolved).entry == declared_case(QUESTION).entry


@pytest.mark.parametrize("question", [
    FOLLOWUP.replace("ese documento", "el otro documento"),
    FOLLOWUP.replace("ese documento", "el documento Programa Ahorro diciembre 2042"),
    "Según ese documento y el manual de vacaciones, compara los requisitos.",
])
def test_anaphora_cannot_replace_an_explicit_different_document(question):
    assert ModelPolicy().contextual_reference(question, prior_questions=(REFERENCE,)) == ""


def test_anaphora_without_authorized_history_does_not_invent_a_document():
    assert ModelPolicy().contextual_reference(FOLLOWUP, prior_questions=()) == ""
    assert contextualize_question(FOLLOWUP, "") == FOLLOWUP


def test_retirement_followup_replaces_tenure_and_keeps_entry():
    question = "¿Y si me retiro con 8 años de antigüedad, manteniendo la misma fecha de ingreso?"
    reference = ModelPolicy().contextual_reference(question, prior_questions=(QUESTION,))
    assert reference == QUESTION
    resolved = contextualize_question(question, reference)
    assert declared_case(resolved).tenure == Fraction(8)
    assert declared_case(resolved).entry == declared_case(QUESTION).entry
    assert declared_case(resolved).before_retirement
    assert "6 años y 6 meses" not in resolved


@pytest.mark.parametrize("question", [
    "¿Y si me retiro después de jubilarme con ocho años?",
    "¿Y con ocho años de mi crédito, manteniendo la misma fecha de ingreso?",
    "¿Y si tengo ocho años o nueve años, manteniendo la misma fecha de ingreso?",
])
def test_uninterpreted_case_changes_are_not_silently_normalized(question):
    assert not is_case_followup(question)
    assert contextualize_question(question, QUESTION).endswith(question)


def test_schema_prompt_and_retry_never_request_markdown_or_inline_citations(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    messages = build_answer_messages(question=QUESTION, evidences=(SOURCE,))
    assert "TRANSPORTE:" in messages[0]["content"]
    assert "Empieza la parte documental con" not in messages[0]["content"]
    assert "Cada afirmacion documental termina con la etiqueta" not in messages[0]["content"]
    assert "Copia las etiquetas" not in messages[1]["content"]
    assert "APLICACION_CONDICIONAL" in messages[1]["content"]


def test_safe_schema_retry_keeps_numeric_reason_in_logs(monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    bad = payload("Si los datos declarados son correctos, la Aportación Base se calcula al 99%.")
    good = payload("Si los datos declarados son correctos, la Aportación Base se calcula al 63%.")
    client = Client(bad, good)
    with caplog.at_level("INFO", logger="app.agents.knowledge_agent"):
        result = KnowledgeAgent(llm=client, policy=ModelPolicy()).synthesize(
            question=QUESTION, evidences=(SOURCE,), model_name=get_settings().ollama_fast_model,
        )
    assert result.grounding.grounded and result.regenerated
    assert "63%" in result.answer and "99%" not in result.answer
    event = next(r for r in caplog.records if r.message == "agent.regenerating")
    assert event.validation_detail == "porcentaje no corresponde a la fila y conceptos aplicables"
    assert "JSON" in client.calls[1]["messages"][1]["content"]


def test_multiple_fragments_do_not_replace_the_numeric_validator(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    other = replace(SOURCE, source_id="synthetic#1", text="El traslado del fondo requiere una solicitud.",
                    page_or_sheet="pagina 13", chunk_id="synthetic-1")
    client = Client(payload("Si los datos declarados son correctos, la Aportación Base se calcula al 63%."))
    result = KnowledgeAgent(llm=client, policy=ModelPolicy()).synthesize(
        question=QUESTION, evidences=(SOURCE, other), model_name=get_settings().ollama_fast_model,
    )
    assert result.grounding.grounded and result.cited_source_ids == (SOURCE.source_id,)
    assert not result.grounding.factual_verified
