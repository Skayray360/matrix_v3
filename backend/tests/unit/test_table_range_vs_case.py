# Creado por Aldo Garcia.
"""Distingue limites de una fila documentada de la antiguedad de un empleado."""
from dataclasses import replace

import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.config import get_settings
from app.llm.model_policy import ModelPolicy
from app.rag.grounding import verify_grounding
from scripts import diagnosticar_solicitud
from tests.unit.test_calculated_application import QUESTION, SOURCE
from tests.unit.test_documentary_output import Client, payload

pytestmark = pytest.mark.unit

ROW_PHRASES = (
    "la tabla indica que entre 6 y 6.99 años corresponde el 63%",
    "la tabla de antigüedad establece que entre 6 y 6.99 años corresponde el 63%",
    "corresponde el 63% según la tabla de antigüedad (6 a 6.99 años)",
    "corresponde el 63% según la tabla: 6 – 6.99 años",
    "la tabla señala que entre 6 y 6.99 años corresponde el 63%",
)


def check(claim, sources=(SOURCE,), citation="synthetic#0", question=QUESTION):
    return verify_grounding(
        f"Si los datos declarados son correctos, {claim}. [[{citation}]]",
        sources, mode="cited", require_citation=True, question=question,
    )


@pytest.mark.parametrize("phrase", ROW_PHRASES)
def test_documented_range_is_not_a_changed_personal_tenure(phrase):
    report = check(f"para 6 años y 6 meses, {phrase}")
    assert report.grounded, report.validation_detail


@pytest.mark.parametrize("phrase", ROW_PHRASES)
def test_an_actual_changed_tenure_is_still_rejected_next_to_a_valid_range(phrase):
    report = check(f"para tu antigüedad de 6.99 años, {phrase}")
    assert not report.grounded
    assert report.validation_detail == "antiguedad atribuida al caso distinta de la declarada"


@pytest.mark.parametrize("changed", ["7 y 7.99", "6 y 7.99", "5 y 6.99"])
def test_other_or_widened_table_row_is_rejected(changed):
    report = check(f"la tabla indica que entre {changed} años corresponde el 63%")
    assert not report.grounded
    assert report.validation_detail == "intervalo no corresponde a la antiguedad declarada"


def test_table_wording_does_not_allow_wrong_percentage_or_uncited_support():
    claim = ROW_PHRASES[0]
    assert not check(claim.replace("63%", "81%")).grounded
    unrelated = replace(SOURCE, source_id="synthetic#1", text="Información sin tabla de antigüedad.")
    report = check(claim, (SOURCE, unrelated), citation="synthetic#1")
    assert not report.grounded
    assert report.validation_detail == "tabla o regla incompleta, ambigua o fuera de intervalo"


@pytest.mark.parametrize("phrase", [
    "tu antigüedad según la tabla es de 6.99 años y corresponde el 63%",
    "la tabla indica que tu antigüedad es de 6.99 años y corresponde el 63%",
])
def test_table_mention_does_not_exempt_personal_statements(phrase):
    assert not check(phrase).grounded


def test_diagnostic_keeps_the_specific_tenure_failure_without_free_text():
    result = diagnosticar_solicitud._safe_event({
        "message": "agent.answer_validation_failed",
        "validation_detail": "antiguedad atribuida al caso distinta de la declarada",
        "draft": "synthetic private draft",
    })
    assert result == {"message": "agent.answer_validation_failed", "validation_detail": "case_tenure_mismatch"}


@pytest.mark.parametrize("structured", [False, True])
def test_documented_interval_is_published_in_text_and_json_without_retry(monkeypatch, structured):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    monkeypatch.setattr(get_settings(), "answer_structured_output", structured)
    claim = "Si los datos declarados son correctos, para 6 años y 6 meses, " + ROW_PHRASES[0] + "."
    client = Client(payload(claim) if structured else claim + " [[E1]]")
    policy = ModelPolicy()
    result = KnowledgeAgent(llm=client, policy=policy).synthesize(
        question=QUESTION, evidences=(SOURCE,), model_name=policy.fast_model,
    )
    assert result.grounding.grounded and result.cited_source_ids == (SOURCE.source_id,)
    assert len(client.calls) == 1 and not result.regenerated
    assert "63%" in result.answer
