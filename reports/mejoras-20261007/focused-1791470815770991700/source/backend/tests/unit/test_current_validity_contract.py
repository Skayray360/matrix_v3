# Creado por Aldo Garcia.
"""Los limites de una fuente historica no certifican su vigencia actual."""

from dataclasses import replace

import pytest

from app.rag.claim_context import unverified_current_claim
from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence


def historical_evidence():
    return Evidence(
        source_id="prestaciones/programa#0",
        text="Programa Flexible\nEl documento describe la portabilidad del programa.",
        score=0.9,
        category="prestaciones",
        filename="Programa Flexible diciembre 2041.pdf",
        section="pagina 1",
        page_or_sheet="pagina 1",
        document_id="synthetic-historical-document",
        chunk_id="synthetic-historical-chunk",
    )


@pytest.mark.parametrize(
    "statement",
    [
        "No puedo confirmar la vigencia actual de esta información.",
        "No puedo verificar la vigencia actual con las fuentes disponibles.",
        "No es posible acreditar la vigencia actual.",
        "**Nota:** No puedo confirmar la vigencia actual de esta información.",
        "- No puedo confirmar la vigencia actual de esta información.",
        "La fecha no acredita la vigencia actual.",
        "El documento no demuestra la vigencia actual.",
        "La presentación no confirma la vigencia actual.",
    ],
)
def test_negative_validity_notice_is_not_current_policy_assertion(statement):
    assert not unverified_current_claim(statement)
    item = historical_evidence()
    answer = f"El documento describe la portabilidad. {statement} [[{item.source_id}]]"
    report = verify_grounding(answer, (item,), mode="cited")
    assert report.grounded, report


@pytest.mark.parametrize(
    "statement",
    [
        "La política está vigente.",
        "Es el nombre actual del programa.",
        "El plan aplica hoy.",
        "Según el documento de diciembre de 2041, el plan está vigente hoy.",
        "No puedo confirmar la vigencia actual, pero el programa está vigente.",
        "No puedo confirmar la vigencia actual; actualmente el programa aplica.",
        "No puedo verificar la vigencia actual. El plan está vigente.",
        "El documento no acredita la vigencia actual, aunque el plan sigue vigente.",
        "No es cierto que no puedo confirmar la vigencia actual.",
        "La fecha no acredita la vigencia actual y el plan está vigente.",
    ],
)
def test_historical_source_or_negative_clause_cannot_approve_current_assertion(statement):
    assert unverified_current_claim(statement)
    item = historical_evidence()
    report = verify_grounding(f"{statement} [[{item.source_id}]]", (item,), mode="cited")
    assert not report.grounded
    assert report.reason == "vigencia actual no acreditada"


@pytest.mark.parametrize(
    "basis",
    [
        "13 días de sueldo nominal vigente",
        "trece días de sueldo vigente",
        "2 meses de salario base vigente",
        "un mes del salario diario integrado vigente",
        "13 días de su salario mínimo general vigente",
    ],
)
def test_vigente_in_salary_calculation_basis_does_not_assert_policy_is_current(basis):
    statement = (
        f"Respecto al bono de ejemplo, es de {basis}, se paga a más tardar el 19 de diciembre, "
        "y para quienes no trabajaron el año completo, se paga proporcionalmente el periodo laborado, "
        "con una exención de 28 UMAS de impuesto."
    )
    assert not unverified_current_claim(statement)
    item = replace(historical_evidence(), text=statement)
    report = verify_grounding(f"{statement} [[{item.source_id}]]", (item,), mode="cited")
    assert report.grounded, report


@pytest.mark.parametrize(
    "statement",
    [
        "El sueldo nominal vigente es de 1000 pesos.",
        "El salario vigente es de 1000 pesos.",
        "El plan vigente concede 13 días de sueldo nominal vigente.",
        "Actualmente el bono es de 13 días de sueldo nominal vigente.",
        "El bono es de 13 días de sueldo nominal vigente hoy.",
        "El bono es de 13 días de sueldo nominal vigente y el plan sigue vigente.",
        "El bono es de 13 días de sueldo nominal vigente; el sueldo vigente es de 1000 pesos.",
        "No puedo confirmar la vigencia actual, pero el sueldo vigente es de 1000 pesos.",
    ],
)
def test_salary_basis_does_not_hide_other_current_assertions(statement):
    assert unverified_current_claim(statement)


def test_salary_basis_keeps_numeric_citation_and_personal_eligibility_checks():
    source = "El bono de ejemplo es de 13 días de sueldo nominal vigente."
    item = replace(historical_evidence(), text=source)
    unsupported = source.replace("13 días", "18 días")
    report = verify_grounding(f"{unsupported} [[{item.source_id}]]", (item,), mode="cited")
    assert not report.grounded
    assert report.reason == "afirmacion numerica sin respaldo en sus fuentes citadas"
    report = verify_grounding(f"{source} [[fuente-inexistente#0]]", (item,), mode="cited")
    assert not report.grounded and report.has_invalid_citations
    report = verify_grounding(
        f"Tienes derecho a 13 días de sueldo nominal vigente. [[{item.source_id}]]", (item,), mode="cited",
    )
    assert not report.grounded and report.reason == "elegibilidad personal no acreditada"
