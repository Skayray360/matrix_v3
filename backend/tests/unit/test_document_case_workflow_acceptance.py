# Creado por Aldo Garcia.
"""Casos consecutivos con memoria SQL real y reglas completamente sintéticas."""

import json

import pytest

from app.common.errors import ForbiddenError
from app.rag.retriever import RetrievalResult
from app.rag.schemas import Evidence
from tests.unit import test_document_workflow_acceptance as acceptance

pytestmark = pytest.mark.unit
workflow = acceptance.workflow
recovered = acceptance.recovered

RULE = Evidence(
    source_id="tecnologia/Reglas_Aportaciones_2035.pdf#0", filename="Reglas_Aportaciones_2035.pdf",
    page_or_sheet="pagina 7", document_id="case-acceptance-rule", chunk_id="case-acceptance-rule-0",
    section="Portabilidad", score=0.9, category="tecnologia",
    text=(
        "Portabilidad del Programa Sintético\n"
        "Los empleados que ingresaron antes del 1 de febrero de 2020 tienen derecho al 92% de la "
        "Aportación Base y para recibir la Aportación Base Complementaria y Adicional Complementaria, "
        "es de acuerdo a la tabla de antigüedad.\n"
        "Los empleados que ingresen a partir del 1 de febrero de 2020 tendrán derecho a la Aportación Base, "
        "Aportación Base Complementaria y Adicional Complementaria de acuerdo a la siguiente tabla de antigüedad:\n"
        "Antigüedad %\n0 – 4.99 0\n5 – 5.99 41\n6 – 6.99 63\n7 – 7.99 81\n8 en adelante 92"
    ),
)
QUESTION = (
    "Según el documento Reglas Aportaciones 2035, si ingresé en mayo de 2020 y me retiro "
    "con 6 años y 6 meses de antigüedad antes de jubilarme, ¿qué porcentaje corresponde "
    "a las aportaciones base, base complementaria y adicional complementaria? Indica documento y página."
)


def case_answer(percentages):
    concepts = ("Aportación Base", "Aportación Base Complementaria", "Adicional Complementaria")
    return json.dumps({
        "status": "answered",
        "claims": [{"text": f"Si los datos declarados son correctos, la {concept} se calcula al {percentage}%.",
                    "citations": ["E1"]} for concept, percentage in zip(concepts, percentages, strict=True)],
        "clarification": "",
    }, ensure_ascii=False)


def test_document_case_chain_keeps_conditions_then_replaces_only_new_data(workflow):
    workflow.retriever.retrieve.return_value = recovered((RULE,))
    workflow.llm.responses = [case_answer((63, 63, 63)), case_answer((92, 92, 92)), case_answer((92, 63, 63))]
    initial = workflow.chat(QUESTION)
    assert initial.grounded and initial.answer.count("63%") == 3
    next_case = workflow.chat("Según ese documento, ¿y con ocho años? Indica documento y página.")
    assert next_case.grounded and next_case.answer.count("92%") == 3
    second_query = workflow.retriever.retrieve.call_args.kwargs["question"]
    assert "mayo de 2020" in second_query and "antes de jubilarme" in second_query
    assert "6 años y 6 meses" not in second_query
    # Vuelve a un caso completo independiente; la tabla no adquiere estado.
    earlier_cohort = workflow.chat(QUESTION.replace("mayo de 2020", "enero de 2020"))
    assert earlier_cohort.grounded
    assert earlier_cohort.answer.count("92%") == 1 and earlier_cohort.answer.count("63%") == 2
    assert "Si los datos declarados son correctos" in earlier_cohort.answer
    assert all(result.cited_source_ids == (RULE.source_id,) for result in (initial, next_case, earlier_cohort))
    assert len(workflow.llm.calls) == 3


def test_case_history_does_not_accredit_an_exact_account_balance(workflow):
    workflow.retriever.retrieve.return_value = recovered((RULE,))
    workflow.llm.responses = [case_answer((63, 63, 63)),
                              json.dumps({"status": "insufficient", "claims": [], "clarification": ""})]
    workflow.chat(QUESTION)
    result = workflow.chat("Según ese documento, ¿cuál es mi saldo exacto acumulado en pesos?")
    assert result.answer_basis == "insufficient" and not result.grounded
    assert not result.cited_source_ids and result.public_sources() == []
    assert "63%" not in result.answer and "92%" not in result.answer
    assert len(workflow.llm.calls) == 2


def test_case_anchor_is_dropped_when_its_authorized_category_is_revoked(workflow):
    workflow.retriever.retrieve.return_value = recovered((RULE,))
    workflow.llm.responses = [case_answer((63, 63, 63))]
    workflow.chat(QUESTION)
    workflow.policies.effective_categories.return_value = frozenset()
    workflow.retriever.retrieve.return_value = RetrievalResult()
    with pytest.raises(ForbiddenError):
        workflow.chat("Según ese documento, ¿y con ocho años?")
    query = workflow.retriever.retrieve.call_args.kwargs["question"]
    assert "Reglas Aportaciones 2035" not in query and "mayo de 2020" not in query
    assert workflow.retriever.retrieve.call_args.kwargs["authorized_categories"] == frozenset()
    assert len(workflow.llm.calls) == 1
    assert len(workflow.stored_assistant_answers()) == 1
