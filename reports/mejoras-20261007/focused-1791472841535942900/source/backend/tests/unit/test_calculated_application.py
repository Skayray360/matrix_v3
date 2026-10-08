# Creado por Aldo Garcia.
"""Calculo acotado completo: nunca sustituye una consulta mixta o ambigua."""

from dataclasses import replace

import pytest

from app.rag.calculated_application import calculated_application_answer
from app.rag.grounding import GroundingReport
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit

QUESTION = (
    "Según el documento Programa Jubilación diciembre 2041, si ingresé en mayo de 2020 "
    "y me retiro con 6 años y 6 meses de antigüedad antes de jubilarme, "
    "¿qué porcentaje me corresponde de las aportaciones base, base complementaria y adicional complementaria? "
    "Indica documento y página."
)
SOURCE = Evidence(
    source_id="synthetic#0", filename="Programa Jubilación diciembre 2041.pdf", page_or_sheet="pagina 12",
    document_id="synthetic", chunk_id="synthetic-0", section="pagina 12", score=0.9, category="prestaciones",
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


def calculate(question=QUESTION, source=SOURCE, extra=()):
    return calculated_application_answer(question, (source, *extra))


def test_calculated_result_uses_only_selected_rule_row_and_metadata():
    result = calculate()
    assert result is not None
    answer, report = result
    assert answer.count("63%") == 3
    assert "92%" not in answer
    assert "fila 6 – 6.99 años" in answer
    assert "Programa Jubilación diciembre 2041.pdf" in answer
    assert "página 12" in answer
    assert "Si los datos declarados son correctos" in answer
    assert report.grounded and report.citations_valid and not report.factual_verified
    assert report.cited_source_ids == (SOURCE.source_id,)


def test_percentage_is_derived_from_source_instead_of_business_constant():
    result = calculate(source=replace(SOURCE, text=SOURCE.text.replace("6 – 6.99 63", "6 – 6.99 58.5")))
    assert result is not None
    assert result[0].count("58.5%") == 3
    assert "63%" not in result[0]


def test_mixed_selected_rule_keeps_fixed_and_table_subjects_separate():
    result = calculate(QUESTION.replace("mayo de 2020", "enero de 2020"))
    assert result is not None
    assert "Aportación Base: 92%" in result[0]
    assert "Aportación Base Complementaria: 63%" in result[0]
    assert "Adicional Complementaria: 63%" in result[0]


def test_complete_natural_document_alias_and_ellipsis_remain_supported():
    source = replace(SOURCE, filename="PLATICA DE PROGRAMA JUBILACI#U00d3N DICIEMBRE 2041.pdf")
    question = QUESTION.replace(
        "el documento Programa Jubilación diciembre 2041", "la plática del Programa Jubilación de diciembre de 2041",
    )
    result = calculate(question, source)
    assert result is not None
    assert "PLATICA DE PROGRAMA JUBILACIÓN DICIEMBRE 2041.pdf" in result[0]


@pytest.mark.parametrize("question", [
    QUESTION + " Compara también las aportaciones anteriores y posteriores al cambio.",
    QUESTION + " ¿Y cuáles son mis otras prestaciones?",
    QUESTION.replace("¿qué porcentaje", "¿qué importe y porcentaje"),
    QUESTION.replace("antes de jubilarme,", "después de jubilarme,"),
    QUESTION.replace(" y me retiro con 6 años y 6 meses de antigüedad", ""),
    QUESTION.replace("ingresé en mayo de 2020 y ", ""),
    QUESTION.replace("6 años y 6 meses", "6 años y 12 meses"),
    QUESTION.replace("6 años y 6 meses", "6 años y 6 meses o 8 años"),
    QUESTION.replace("Indica documento y página.", ""),
    QUESTION.replace("base, base complementaria y adicional complementaria", "base y base complementaria"),
    QUESTION.replace("base, base complementaria y adicional complementaria", "base, base y adicional complementaria"),
    QUESTION.replace("adicional complementaria", "seguro adicional"),
    QUESTION.replace("Programa Jubilación diciembre 2041", "Programa diciembre 2041"),
    QUESTION.replace("Programa Jubilación diciembre 2041", "Otro Programa Jubilación diciembre 2041"),
    QUESTION.replace("Programa Jubilación diciembre 2041", "Programa Jubilación diciembre 2042"),
    QUESTION.replace("el documento Programa Jubilación diciembre 2041", "el documento de diciembre de 2041"),
])
def test_incomplete_mixed_or_unidentified_questions_have_no_calculated_substitute(question):
    assert calculate(question) is None


@pytest.mark.parametrize("source", [
    replace(SOURCE, text=SOURCE.text + "\nExcepto el personal temporal."),
    replace(SOURCE, text=SOURCE.text.replace("6 – 6.99 63", "6 – 6.99 63 400")),
    replace(SOURCE, text=SOURCE.text.replace("Antigüedad %", "Antigüedad")),
    replace(SOURCE, page_or_sheet=""),
    replace(SOURCE, page_or_sheet="pagina 0"),
])
def test_incomplete_rule_table_or_locator_has_no_calculated_substitute(source):
    assert calculate(source=source) is None


def test_empty_evidence_never_supplies_authority():
    assert calculated_application_answer(QUESTION, ()) is None


def test_document_identity_collision_has_no_calculated_substitute():
    other = replace(SOURCE, source_id="other#0", document_id="other", chunk_id="other-0")
    assert calculate(extra=(other,)) is None


def test_same_document_conflicting_application_has_no_calculated_substitute():
    other = replace(SOURCE, source_id="synthetic#1", chunk_id="synthetic-1",
                    text=SOURCE.text.replace("6 – 6.99 63", "6 – 6.99 64"))
    assert calculate(extra=(other,)) is None


def test_duplicate_consistent_application_is_not_ambiguous():
    other = replace(SOURCE, source_id="synthetic#1", chunk_id="synthetic-1")
    assert calculate(extra=(other,)) is not None


def test_ambiguous_source_id_is_never_repaired():
    other = replace(SOURCE, text=SOURCE.text.replace("6 – 6.99 63", "6 – 6.99 64"))
    assert calculate(extra=(other,)) is None


def test_normal_grounding_can_still_veto_calculated_output(monkeypatch):
    from app.rag import calculated_application

    monkeypatch.setattr(calculated_application, "verify_grounding", lambda *args, **kwargs: GroundingReport(
        grounded=False, reason="fuente no verificable",
    ))
    assert calculate() is None


@pytest.mark.parametrize("replacement", [
    "Los empleados sindicalizados que ingresen",
    "Los empleados mayores de 50 años que ingresen",
])
def test_additional_population_and_age_requirements_block_calculated_output(replacement):
    source = replace(SOURCE, text=SOURCE.text.replace("Los empleados que ingresen", replacement))
    assert calculate(source=source) is None


@pytest.mark.parametrize("prefix", [
    "Este programa aplica únicamente a empleados de planta.\n",
    "Este programa no aplica a empleados temporales.\n",
])
def test_additional_scope_paragraph_is_not_discarded(prefix):
    source = replace(SOURCE, text=prefix + SOURCE.text)
    assert calculate(source=source) is None


def test_uninterpreted_fragment_of_the_same_document_blocks_calculated_output():
    restriction = replace(
        SOURCE, source_id="synthetic#1", chunk_id="synthetic-1", page_or_sheet="pagina 13",
        text="Este programa no aplica a empleados temporales.",
    )
    assert calculate(extra=(restriction,)) is None


@pytest.mark.parametrize("heading", [
    "Portabilidad del programa exclusivo para empleados de planta",
    "Portabilidad del Programa Exclusivo",
])
def test_population_restriction_cannot_hide_in_title(heading):
    source = replace(SOURCE, text=SOURCE.text.replace("Portabilidad del Programa Sintético", heading))
    assert calculate(source=source) is None


def test_explicit_departure_rule_form_with_synthetic_values_is_supported():
    source = replace(SOURCE, text=SOURCE.text.replace(
        "Portabilidad del Programa Sintético", "Portabilidad del Plan Horizonte Claro",
    ).replace(
        "Los empleados que ingresaron antes del 1 de febrero de 2020 tienen derecho",
        "Si un empleado que ingresó a Ejemplo antes del 1 de febrero de 2020 deja la Compañía "
        "antes de cumplir con los supuestos de jubilación, tiene derecho",
    ).replace("a partir del 1 de febrero", "a partir de 1 de febrero"))
    result = calculate(source=source)
    assert result is not None
    assert result[0].count("63%") == 3
