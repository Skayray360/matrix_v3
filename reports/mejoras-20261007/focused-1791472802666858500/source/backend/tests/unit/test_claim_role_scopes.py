# Creado por Aldo Garcia.
"""Roles numéricos y ámbito condicional entre unidades documentales citadas."""

from dataclasses import replace

import pytest

from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit

QUESTION = (
    "Si ingresé en mayo de 2016 y me retiro con 7 años y 6 meses de antigüedad "
    "antes de jubilarme, ¿qué porcentaje corresponde?"
)
SOURCE = Evidence(
    source_id="synthetic#0", score=0.9, category="prestaciones", section="pagina 9",
    filename="PLATICA DE PLAN DE PENSIONES POR JUBILACI#U00d3N DICIEMBRE 2022.pdf",
    page_or_sheet="pagina 9", document_id="synthetic", chunk_id="synthetic-0",
    text=(
        "Los empleados que ingresen a partir del 1 de febrero de 2016 tendrán derecho a la "
        "Aportación Básica, Aportación Básica Complementaria y Adicional Complementaria "
        "de acuerdo a la siguiente tabla de antigüedad:\n"
        "Antigüedad %\n7 – 7.99 70\n8 en adelante 100"
    ),
)
INTRO = "Si ingresaste en mayo de 2016 y tienes 7 años y 6 meses de antigüedad: [[synthetic#0]]\n"


def verify(answer, *, extra=()):
    return verify_grounding(answer, (SOURCE, *extra), mode="cited", question=QUESTION)


@pytest.mark.parametrize("title", [
    "Según la plática del Plan de Pensiones por Jubilación de diciembre de 2022, página 9",
    'El documento de referencia es "PLATICA DE PLAN DE PENSIONES POR JUBILACIÓN DICIEMBRE 2022.pdf" '
    "y la información se encuentra en la página 9",
    'Documento: "PLATICA DE PLAN DE PENSIONES POR JUBILACIÓN DICIEMBRE 2022.pdf", página 9',
])
def test_complete_metadata_title_has_grammatical_variants(title):
    result = verify(title + " [[synthetic#0]]")
    assert result.grounded, result.validation_detail


@pytest.mark.parametrize("title", [
    "Según la plática del Plan de Pensiones de diciembre de 2022, página 9",
    "Según la plática del Plan de Pensiones por Jubilación de diciembre de 2023, página 9",
    "Según la plática del Plan de Pensiones por Jubilación de diciembre de 2022, página 8",
    'Documento: "Otro PLATICA DE PLAN DE PENSIONES POR JUBILACIÓN DICIEMBRE 2022.pdf", página 9',
    'Documento: "PLATICA DE PLAN DE PENSIONES POR JUBILACIÓN DICIEMBRE 2022.pdf.bak", página 9',
    'Documento: "PLATICA DE PLAN DE PENSIONES POR JUBILACIÓN DICIEMBRE 2022 edición falsa", página 9',
    "Según la plática del Plan de Pensiones por Jubilación de diciembre de 2022 edición falsa, página 9",
    "Según la plática del Plan de Pensiones por Jubilación de diciembre de 2022 y edición falsa, página 9",
])
def test_metadata_keeps_full_identity_year_and_page(title):
    assert not verify(title + " [[synthetic#0]]").grounded


def test_natural_title_does_not_borrow_page_from_another_document():
    other = replace(SOURCE, source_id="other#0", filename="Otro documento.pdf", document_id="other", page_or_sheet="pagina 8")
    text = "Según la plática del Plan de Pensiones por Jubilación de diciembre de 2022, página 8 [[synthetic#0]] [[other#0]]"
    assert not verify(text, extra=(other,)).grounded


@pytest.mark.parametrize("page", [", página 9", ""])
def test_natural_title_matching_multiple_documents_is_ambiguous(page):
    other = replace(SOURCE, source_id="other#0", document_id="other", filename=SOURCE.filename.replace("#U00d3", "Ó"))
    text = f"Según la plática del Plan de Pensiones por Jubilación de diciembre de 2022{page} [[synthetic#0]] [[other#0]]"
    assert not verify(text, extra=(other,)).grounded


@pytest.mark.parametrize("row", [
    "rango de 7 – 7.99 años", "fila 7 - 7.99 años", "tramo entre 7 y 7.99 años", "rango de 7 a 7.99 años",
])
def test_documentary_row_is_not_personal_duration_even_without_percent(row):
    text = INTRO + f"La antigüedad de 7 años y 6 meses se encuentra en el {row}. [[synthetic#0]]\n"
    text += "La Aportación Básica se calcula al 70%. [[synthetic#0]]"
    result = verify(text)
    assert result.grounded, result.validation_detail


def test_realistic_claim_keeps_conditional_scope_across_citations():
    text = INTRO + (
        "La antigüedad de 7 años y 6 meses se encuentra en el rango de 7 – 7.99 años, "
        "por lo que el porcentaje correspondiente es del 70% para la Aportación Básica, "
        "Aportación Básica Complementaria y Adicional Complementaria [[synthetic#0]]"
    )
    result = verify(text)
    assert result.grounded, result.validation_detail


@pytest.mark.parametrize("row", [
    "Si la antigüedad es de 7 – 7.99 años",
    "Si la antigüedad está entre 7 y 7.99 años",
])
def test_explicit_conditional_interval_describes_the_selected_documentary_row(row):
    text = (
        "Aplicando los supuestos declarados (ingreso en mayo de 2016 y antigüedad de 7 años y 6 meses), "
        f"se usa la tabla:\n* {row}, corresponde un 70% a la Aportación Básica. [[synthetic#0]]"
    )
    result = verify(text)
    assert result.grounded, result.validation_detail


@pytest.mark.parametrize("claim", [
    "La antigüedad es de 8 años y el porcentaje es 70%",
    "El ingreso fue en mayo de 2017 y la Aportación Básica es 70%",
    "El ingreso fue en abril de 2016 y la Aportación Básica es 70%",
    "Fecha de ingreso: abril de 2016. La Aportación Básica es 70%",
    "La Aportación Básica es 71%",
    "Te corresponde el 70% de la Aportación Básica",
    "Tienes derecho al 70% de la Aportación Básica",
    "La antigüedad es de 7 – 7.99 años y la Aportación Básica es 70%",
    "La fila entre 8 y 8.99 años es aplicable y la Aportación Básica es 70%",
    "El rango de 8 a 8.99 años es aplicable y la Aportación Básica es 70%",
    "Si la antigüedad es de 8 – 8.99 años, la Aportación Básica es 70%",
    "Si la antigüedad está entre 7 y 7.98 años, la Aportación Básica es 70%",
    "La antigüedad es de 7 – 7.99 años y la Aportación Básica es 70%",
    "La antigüedad está entre 7 y 7.99 años y la Aportación Básica es 70%",
    "Si la antigüedad es de 7 – 7.99 años, la Aportación Básica es 71%",
    "Con 8 años, si la antigüedad es de 7 – 7.99 años, la Aportación Básica es 70%",
])
def test_inherited_scope_never_changes_case_or_certifies_eligibility(claim):
    assert not verify(INTRO + claim + " [[synthetic#0]]").grounded


@pytest.mark.parametrize("heading", ["## Otra sección\n", "**Otra sección**\n", "__Otra sección__\n"])
def test_new_section_cannot_inherit_conditional_scope(heading):
    assert not verify(INTRO + heading + "La Aportación Básica es 70% [[synthetic#0]]").grounded


def test_new_source_cannot_inherit_conditional_scope():
    other = replace(SOURCE, source_id="other#0", document_id="other", chunk_id="other")
    assert not verify(INTRO + "La Aportación Básica es 70% [[other#0]]", extra=(other,)).grounded


def test_affirmative_intro_does_not_create_conditional_scope():
    text = "La regla está en la página 9 [[synthetic#0]]\nLa Aportación Básica es 70% [[synthetic#0]]"
    assert not verify(text).grounded
