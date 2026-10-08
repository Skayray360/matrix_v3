# Creado por Aldo Garcia.
"""Los limites de una fuente historica no certifican su vigencia actual."""

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
