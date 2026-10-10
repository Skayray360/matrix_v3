# Creado por Aldo Garcia.
"""Contrato de datos declarados en memoria; no usa modelo ni politicas reales."""

from fractions import Fraction

import pytest

from app.agents.contextual_query import contextualize_question, is_case_followup
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.llm.model_policy import ModelPolicy
from app.rag.claim_context import declared_case

pytestmark = pytest.mark.unit

BASE = (
    "Según el documento de prestaciones, si ingresé en mayo de 2016 y tengo "
    "7 años y 6 meses de antigüedad antes de jubilarme, ¿qué porcentaje corresponde?"
)


@pytest.mark.parametrize("followup", [
    "¿Y si llevo ocho años?", "¿Y con 8 años?", "Pero si tengo ocho años de antigüedad?",
])
def test_followup_replaces_tenure_and_preserves_other_declared_fields(followup):
    policy = ModelPolicy()
    reference = policy.contextual_reference(followup, prior_questions=(BASE,))
    assert reference == BASE
    result = contextualize_question(followup, reference)
    case = declared_case(result)
    assert case.tenure == Fraction(8)
    assert case.entry == declared_case(BASE).entry
    assert case.before_retirement
    assert "7 años y 6 meses" not in result
    assert "Según el documento" in result


def test_successive_changes_keep_only_current_tenure():
    policy = ModelPolicy()
    latest = "¿Y con diez años?"
    reference = policy.contextual_reference(latest, prior_questions=(BASE, "¿Y si llevo ocho años?"))
    case = declared_case(contextualize_question(latest, reference))
    assert case.tenure == Fraction(10)
    assert case.entry == declared_case(BASE).entry


def test_entry_change_keeps_tenure_and_does_not_keep_two_dates():
    latest = "¿Y si ingresé en junio de 2018?"
    result = contextualize_question(latest, BASE)
    case = declared_case(result)
    assert case.entry.first.year == 2018
    assert case.entry.first.month == 6
    assert case.tenure == Fraction(15, 2)
    assert "mayo de 2016" not in result


@pytest.mark.parametrize("question", [
    "¿Qué es la fotosíntesis?", "Cambiando de tema, tengo ocho años de crédito",
    "Según el otro documento, tengo ocho años", "¿Y con ocho años de vida de mi perro?",
])
def test_a_new_topic_or_unknown_field_is_not_a_case_only_followup(question):
    assert not is_case_followup(question)
    assert contextualize_question(question, BASE).endswith(question)
    assert ModelPolicy().contextual_reference(question, prior_questions=(BASE,)) == ""


def test_ambiguous_duration_is_preserved_for_clarification():
    followup = "¿Y si tengo ocho años o nueve años?"
    assert not is_case_followup(followup)
    result = contextualize_question(followup, BASE)
    assert followup in result
    assert declared_case(result).tenure is None


def test_a_followup_without_authorized_history_never_invents_an_antecedent():
    followup = "¿Y si llevo ocho años?"
    assert contextualize_question(followup, "") == followup
    assert declared_case(followup).entry is None


def test_long_authorized_question_preserves_document_and_entry_at_the_start():
    base = BASE + " Detalle complementario." * 100
    latest = "¿Y con diez años?"
    reference = ModelPolicy().contextual_reference(latest, prior_questions=(base, "¿Y con ocho años?"))
    result = contextualize_question(latest, reference)
    assert result.startswith("Según el documento")
    assert declared_case(result).entry == declared_case(BASE).entry
    assert declared_case(result).tenure == Fraction(10)


def test_oversized_context_is_reported_instead_of_silently_removing_conditions():
    with pytest.raises(InferenceFailureError) as raised:
        contextualize_question("¿Y sus requisitos?", BASE + " x" * 8000)
    assert raised.value.failure_kind is InferenceFailureKind.CONTEXT_LIMIT
