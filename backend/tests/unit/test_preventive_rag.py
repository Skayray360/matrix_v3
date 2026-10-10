# Creado por Aldo Garcia.
"""Riesgos vecinos de los fallos observados; fuentes y respuestas sintéticas."""
import json
from fractions import Fraction

import pytest
from jsonschema import Draft202012Validator

from app.agents import documentary_output
from app.agents.contextual_query import contextualize_question
from app.agents.knowledge_agent import KnowledgeAgent
from app.config import get_settings
from app.llm.model_policy import ModelPolicy
from app.rag.claim_context import application_diagnostics, declared_case
from app.rag.grounding import verify_grounding
from tests.unit.test_calculated_application import QUESTION, SOURCE
from tests.unit.test_documentary_output import Client, payload

pytestmark = pytest.mark.unit


def test_application_diagnostics_reports_missing_inputs_without_values_or_document_text():
    complete = application_diagnostics(QUESTION, (SOURCE,))
    assert complete["application_requested"]
    assert complete["application_count"] == 1
    assert complete["application_missing_fields"] == []
    missing = application_diagnostics("¿Y con ocho años?", (SOURCE,))
    assert missing["application_missing_fields"] == ["entry"]
    assert missing["application_count"] == 0
    assert application_diagnostics("Explica la tabla con sus porcentajes.", (SOURCE,)) == {
        "application_requested": False, "application_missing_fields": [], "application_count": 0,
        "application_limitation_count": 0, "application_conflict_count": 0,
    }
    assert all(isinstance(value, bool | int | list) for value in complete.values())


@pytest.mark.parametrize("question", [
    "Según ese documento, ¿y con ocho años?",
    "De acuerdo con esa fuente, ¿y con ocho años? Indica documento y página.",
    "¿Y con ocho años según ese documento?",
])
def test_document_reference_and_case_change_keep_both_contexts(question):
    reference = ModelPolicy().contextual_reference(question, prior_questions=(QUESTION,))
    case = declared_case(contextualize_question(question, reference))
    assert case.tenure == Fraction(8)
    assert case.entry == declared_case(QUESTION).entry
    assert case.before_retirement


def test_combined_reference_and_new_entry_preserve_tenure():
    question = "Según ese documento, ¿y si ingresé en enero de 2020?"
    reference = ModelPolicy().contextual_reference(question, prior_questions=(QUESTION,))
    case = declared_case(contextualize_question(question, reference))
    assert case.entry.first.month == 1
    assert case.tenure == Fraction(13, 2)
    assert case.before_retirement


@pytest.mark.parametrize("question", [
    "Según ese documento, ¿y con ocho años después de jubilarme?",
    "Según ese documento y otro manual, ¿y con ocho años?",
    "Según ese documento, ¿y con ocho años o nueve años?",
])
def test_combined_reference_does_not_erase_new_or_ambiguous_conditions(question):
    reference = ModelPolicy().contextual_reference(question, prior_questions=(QUESTION,))
    result = contextualize_question(question, reference)
    assert not declared_case(result).before_retirement
    assert ("después de jubilarme" in result or "otro manual" in result or "o nueve años" in result)


@pytest.mark.parametrize("question", [
    "Explica la tabla de antigüedad con sus porcentajes.",
    "Resume la tabla con sus intervalos de antigüedad y porcentajes.",
])
def test_explaining_a_table_is_not_a_personal_case_application(question):
    answer = "Para el tramo 6 – 6.99 corresponde el 63%. [[synthetic#0]]"
    report = verify_grounding(answer, (SOURCE,), mode="cited", require_citation=True, question=question)
    assert report.grounded, report.validation_detail
    wrong = verify_grounding(answer.replace("63%", "81%"), (SOURCE,), mode="cited", question=question)
    assert not wrong.grounded


@pytest.mark.parametrize("question", [
    "¿Y con ocho años?", "¿Qué porcentaje me corresponde?",
    "¿Qué porcentaje aplica con ocho años de antigüedad?",
])
def test_actual_cases_still_require_complete_applicable_rules(question):
    answer = "Si los datos declarados son correctos, la Aportación Base corresponde al 92%. [[synthetic#0]]"
    assert not verify_grounding(answer, (SOURCE,), mode="cited", question=question).grounded


def test_generation_constrains_clarification_without_weakening_the_parser(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    client = Client(payload("Si los datos declarados son correctos, la Aportación Base se calcula al 63%."))
    KnowledgeAgent(llm=client, policy=ModelPolicy()).synthesize(
        question=QUESTION, evidences=(SOURCE,), model_name=get_settings().ollama_fast_model,
    )
    schema = client.calls[0]["response_schema"]
    choices = schema["properties"]["clarification"].get("enum")
    assert choices and "" in choices
    assert len(json.dumps(schema, ensure_ascii=False)) <= documentary_output.SCHEMA_BUDGET_CHARS
    for clarification in choices:
        value = {"status": "insufficient", "claims": [], "clarification": clarification, "limitations": []}
        Draft202012Validator(schema).validate(value)
        answer = documentary_output.render_documentary_output(json.dumps(value), {})
        assert verify_grounding(answer, (), mode="cited", question="Dato desconocido?").grounded
    fabricated = {"status": "insufficient", "claims": [], "clarification": "¿Recibes nueve pagos?", "limitations": []}
    assert not Draft202012Validator(schema).is_valid(fabricated)
    with pytest.raises(documentary_output.DocumentaryOutputError):
        documentary_output.render_documentary_output(json.dumps(fabricated), {})
