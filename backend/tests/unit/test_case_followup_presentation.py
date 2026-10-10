# Creado por Aldo Garcia.
"""Seguimientos completos: conservar el caso sin descartar condiciones nuevas."""
from fractions import Fraction

import pytest

from app.agents.contextual_query import contextualize_question, is_case_followup
from app.agents.knowledge_agent import KnowledgeAgent
from app.common.answers import UNVERIFIED_ANSWER_NOTICE, safe_nonfactual_text
from app.config import get_settings
from app.llm.model_policy import ModelPolicy
from app.rag.claim_context import declared_case
from tests.unit.test_calculated_application import QUESTION, SOURCE
from tests.unit.test_documentary_output import Client, payload

pytestmark = pytest.mark.unit

FOLLOWUP = (
    "¿Y si me retiro con 8 años de antigüedad, manteniendo la misma fecha de ingreso "
    "y las demás condiciones? Indica el porcentaje de cada aportación y la página."
)


@pytest.mark.parametrize("followup", [
    FOLLOWUP,
    FOLLOWUP.replace("8 años", "ocho años"),
    FOLLOWUP.replace("manteniendo", "conservando"),
    FOLLOWUP.replace(" el porcentaje de cada aportación y la página", " documento y página"),
    FOLLOWUP.replace(" Indica el porcentaje de cada aportación y la página.", ""),
    "¿Y si me retiro con 8 años? Indica documento y página.",
])
def test_full_followup_preserves_document_entry_and_retirement_condition(followup):
    policy = ModelPolicy()
    reference = policy.contextual_reference(followup, prior_questions=(QUESTION,))
    assert reference == QUESTION
    assert is_case_followup(followup)
    resolved = contextualize_question(followup, reference)
    case = declared_case(resolved)
    assert case.tenure == Fraction(8)
    assert case.entry == declared_case(QUESTION).entry
    assert case.before_retirement
    assert "Programa Jubilación diciembre 2041" in resolved
    assert "6 años y 6 meses" not in resolved
    assert resolved.count("?") == 1


@pytest.mark.parametrize("followup", [
    FOLLOWUP + " Además compara otro plan.",
    FOLLOWUP.replace("y las demás condiciones", "pero después de jubilarme"),
    FOLLOWUP.replace("y las demás condiciones", "y sin cumplir las demás condiciones"),
    FOLLOWUP.replace("y las demás condiciones", "y las demás condiciones salvo la fecha de ingreso"),
    FOLLOWUP.replace("8 años", "8 años o 9 años"),
    FOLLOWUP.replace("de antigüedad", "de vida de mi perro"),
    FOLLOWUP.replace("Indica el porcentaje de cada aportación y la página.", "Indica mi saldo individual."),
    FOLLOWUP.replace("Indica el porcentaje de cada aportación y la página.", "Según el otro documento."),
    FOLLOWUP.replace("la página", "la página del otro documento"),
])
def test_new_conditions_and_requests_are_never_removed(followup):
    assert not is_case_followup(followup)
    assert ModelPolicy().contextual_reference(followup, prior_questions=(QUESTION,)) == ""
    assert contextualize_question(followup, QUESTION).endswith(followup)


def test_missing_authorized_history_does_not_invent_case_fields():
    assert ModelPolicy().contextual_reference(FOLLOWUP, prior_questions=()) == ""
    assert contextualize_question(FOLLOWUP, "") == FOLLOWUP
    assert declared_case(FOLLOWUP).entry is None


def test_complete_followup_keeps_the_case_after_a_document_anaphora():
    source = "el documento Programa Jubilación diciembre 2041"
    first = f"Según {source}, ¿qué establece la sección de portabilidad? Indica documento y página."
    second = QUESTION.replace(source, "ese documento")
    reference = ModelPolicy().contextual_reference(
        FOLLOWUP, prior_questions=(first, second), documented_indices=frozenset({0, 1}),
    )
    resolved = contextualize_question(FOLLOWUP, reference)
    assert declared_case(resolved).tenure == Fraction(8)
    assert declared_case(resolved).entry == declared_case(QUESTION).entry
    assert declared_case(resolved).before_retirement
    assert source in resolved
    assert "qué establece" not in resolved


def test_full_followup_is_validated_against_the_new_row(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    policy = ModelPolicy()
    reference = policy.contextual_reference(FOLLOWUP, prior_questions=(QUESTION,))
    resolved = contextualize_question(FOLLOWUP, reference)
    client = Client(payload("Si los datos declarados son correctos, la Aportación Base se calcula al 92%."))
    result = KnowledgeAgent(llm=client, policy=policy).synthesize(
        question=resolved, evidences=(SOURCE,), model_name=policy.fast_model,
    )
    assert result.grounding.grounded and not result.regenerated
    assert result.cited_source_ids == (SOURCE.source_id,)
    assert "92%" in result.answer and "63%" not in result.answer
    assert len(client.calls) == 1


def test_followup_does_not_accept_a_fabricated_citation(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    policy = ModelPolicy()
    reference = policy.contextual_reference(FOLLOWUP, prior_questions=(QUESTION,))
    resolved = contextualize_question(FOLLOWUP, reference)
    bad = payload("Si los datos declarados son correctos, la Aportación Base se calcula al 92%.", "E99")
    result = KnowledgeAgent(llm=Client(bad, bad), policy=policy).synthesize(
        question=resolved, evidences=(SOURCE,), model_name=policy.fast_model,
    )
    assert result.answer.startswith(UNVERIFIED_ANSWER_NOTICE) and safe_nonfactual_text(result.answer)
    assert not result.cited_source_ids and "92%" not in result.answer and "E99" not in result.answer
