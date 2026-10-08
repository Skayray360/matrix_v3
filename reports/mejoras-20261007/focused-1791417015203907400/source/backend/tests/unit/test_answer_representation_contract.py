# Creado por Aldo Garcia.
"""Representaciones de filas y nombres que conservan la procedencia exacta."""

from dataclasses import replace

import pytest

from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit

QUESTION = (
    "Si ingresé en mayo de 2020 y me retiro con 6 años y 6 meses de antigüedad "
    "antes de jubilarme, ¿qué porcentaje corresponde?"
)
SOURCE = Evidence(
    source_id="synthetic#0",
    text=(
        "Los empleados que ingresen a partir del 1 de febrero de 2020 tendrán derecho a la "
        "Aportación Base, Aportación Complementaria y Adicional Complementaria de acuerdo "
        "a la siguiente tabla de antigüedad:\n"
        "Antigüedad %\n0 – 4.99 0\n5 – 5.99 41\n6 – 6.99 63\n7 – 7.99 81\n8 en adelante 92"
    ),
    score=0.9, category="prestaciones", filename="Programa Jubilaci#U00d3n diciembre 2041.pdf",
    section="pagina 12", page_or_sheet="pagina 12", document_id="synthetic-program", chunk_id="synthetic-row",
)


def verify(answer, *, source=SOURCE, question=QUESTION, extra=()):
    return verify_grounding(answer + " [[synthetic#0]]", (source, *extra), question=question, mode="cited")


@pytest.mark.parametrize("label", ["fila", "tramo", "intervalo de", "rango de antigüedad de"])
@pytest.mark.parametrize("bounds", ["6 – 6.99", "6–6.99", "6-6.99", "6 — 6,99"])
def test_documentary_row_years_are_not_the_personal_duration(label, bounds):
    answer = (
        f"Si los datos declarados son correctos, con 6 años y 6 meses se usa el {label} "
        f"{bounds} años y se calcula 63%."
    )
    result = verify(answer)
    assert result.grounded, result.validation_detail


@pytest.mark.parametrize("bounds, percent", [("7–7.99", "81"), ("6–6.98", "63"), ("6–6.99", "81")])
def test_row_description_still_requires_exact_applicable_bounds_and_percentage(bounds, percent):
    assert not verify(
        f"Si los datos declarados son correctos, para el tramo {bounds} años se calcula {percent}%."
    ).grounded


def test_row_description_does_not_hide_a_changed_personal_duration():
    answer = "Si los datos declarados son correctos, con 6.8 años se usa el tramo 6–6.99 años y se calcula 63%."
    assert not verify(answer).grounded
    assert not verify("Si los datos declarados son correctos, la antigüedad es 6–6.99 años y se calcula 63%.").grounded


@pytest.mark.parametrize("title", [
    "Programa Jubilación diciembre 2041.pdf",
    "Programa Jubilación diciembre 2041",
    "Programa Jubilaci#U00d3n diciembre 2041.pdf",
])
def test_imported_accent_escape_and_display_title_identify_same_cited_document(title):
    result = verify(f'Documento: "{title}", página 12.', question="Indica documento y página.")
    assert result.grounded, result.validation_detail


@pytest.mark.parametrize("title", [
    "Programa Jubilación diciembre 2042.pdf",
    "Otro Programa Jubilación diciembre 2041.pdf",
])
def test_display_title_alias_does_not_accept_changed_document_or_edition(title):
    assert not verify(f'Documento: "{title}", página 12.', question="Indica documento y página.").grounded


def test_display_title_alias_keeps_page_and_numeric_claims_scoped():
    assert not verify('Documento: "Programa Jubilación diciembre 2041.pdf", página 7.', question="Indica la fuente.").grounded
    assert not verify("El pago es de 2041 pesos.", question="Indica el pago.").grounded
    source = replace(SOURCE, filename="Programa #U0031 diciembre 2041.pdf")
    assert not verify('Documento: "Programa 1 diciembre 2041.pdf", página 12.', source=source,
                      question="Indica la fuente.").grounded


def test_alias_collision_cannot_attribute_a_page_to_an_arbitrary_document():
    other = replace(SOURCE, source_id="other#0", document_id="different-document", filename="Programa Jubilación diciembre 2041.pdf",
                    page_or_sheet="pagina 7")
    answer = 'Documento: "Programa Jubilación diciembre 2041.pdf", página 7. [[synthetic#0]] [[other#0]]'
    result = verify_grounding(answer, (SOURCE, other), question="Indica la fuente.", mode="cited")
    assert not result.grounded
    assert "ambigua" in result.validation_detail
