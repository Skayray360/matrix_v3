# Creado por Aldo Garcia.
"""El caso, su intervalo y su explicacion tienen funciones numericas distintas."""

from dataclasses import replace
from fractions import Fraction

import pytest

from app.rag.calculated_application import calculated_application_answer
from app.rag.claim_context import declared_case
from app.rag.grounding import verify_grounding
from tests.unit.test_calculated_application import SOURCE

pytestmark = pytest.mark.unit

QUESTION = (
    "Ingresé en mayo de 2020 y termino mi relación laboral antes de jubilarme, "
    "con 6 años y 6 meses de antigüedad. Según el documento Programa Jubilación diciembre 2041, "
    "¿qué porcentaje me corresponde? Explica cómo ubicas mi antigüedad en la tabla y cita la página."
)


@pytest.mark.parametrize("suffix", [
    "", " Explica cómo ubicas mi antigüedad en la tabla y cita la página.",
])
def test_tenure_field_does_not_depend_on_a_later_explanation_instruction(suffix):
    question = QUESTION.split(" Explica", 1)[0] + suffix
    case = declared_case(question)
    assert case.tenure == Fraction(13, 2)
    assert case.before_retirement
    assert case.entry is not None


@pytest.mark.parametrize("verb", ["cambia", "modifica", "actualiza"])
@pytest.mark.parametrize("determiner", ["la", "mi"])
def test_explicit_tenure_edit_extracts_only_the_replacement(verb, determiner):
    question = (
        "Mantén mi fecha de ingreso y las demás condiciones del caso anterior, pero "
        f"{verb} {determiner} antigüedad a 8 años y 6 meses. "
        "¿Qué porcentaje corresponde ahora y por qué?"
    )
    case = declared_case(question)
    assert case.tenure == Fraction(17, 2)
    assert case.tenure_quote == "8 anos y 6 meses"
    assert case.entry is None
    assert not case.before_retirement


@pytest.mark.parametrize("duration", [
    "8 años o 9 años", "8 años y 12 meses", "8 años 6 meses", "8 años de edad y 6 meses de antigüedad",
])
def test_ambiguous_replacement_does_not_select_a_case(duration):
    assert declared_case(f"Cambia la antigüedad a {duration}.").tenure is None


@pytest.mark.parametrize("relation", [
    "Se ubican entre", "Se encuentra entre", "Se sitúa entre", "Se ubica en el intervalo entre",
])
def test_duration_membership_in_a_verified_row_is_not_a_second_case(relation):
    answer = (
        "Si los datos declarados son correctos, 6 años y 6 meses equivalen a 6.5 años. "
        f"{relation} 6 y 6.99 años y corresponde un 63%. [[synthetic#0]]"
    )
    result = verify_grounding(answer, (SOURCE,), mode="cited", question=QUESTION)
    assert result.grounded, result.validation_detail


@pytest.mark.parametrize("change", [
    ("6 y 6.99", "6 y 7.99"), ("6 y 6.99", "7 y 7.99"),
    ("63%", "81%"), ("6.5 años", "6.99 años"), ("6 años y 6 meses", "6 años y 9 meses"),
])
def test_interval_explanation_still_rejects_changed_case_bounds_or_rate(change):
    answer = (
        "Si los datos declarados son correctos, 6 años y 6 meses equivalen a 6.5 años. "
        "Se ubican entre 6 y 6.99 años y corresponde un 63%. [[synthetic#0]]"
    )
    result = verify_grounding(answer.replace(*change), (SOURCE,), mode="cited", question=QUESTION)
    assert not result.grounded


def test_percentage_request_without_a_subject_list_uses_unique_complete_rule():
    result = calculated_application_answer(QUESTION, (SOURCE,))
    assert result is not None
    answer, report = result
    assert answer.count("63%") == 3
    assert "6.5 años" in answer
    assert "fila 6 – 6.99 años" in answer
    assert "página 12" in answer
    assert report.grounded
    assert report.cited_source_ids == (SOURCE.source_id,)


def test_generic_percentage_is_calculated_from_changed_source_values():
    source = replace(SOURCE, text=SOURCE.text.replace("6 – 6.99 63", "6 – 6.99 47.5"))
    result = calculated_application_answer(QUESTION, (source,))
    assert result is not None
    assert result[0].count("47.5%") == 3


@pytest.mark.parametrize("quotes", [("“", "”"), ('"', '"'), ("'", "'")])
def test_quoted_document_title_can_follow_the_declared_case(quotes):
    opening, closing = quotes
    question = QUESTION.replace(
        "el documento Programa Jubilación diciembre 2041",
        f"{opening}Programa Jubilación de diciembre de 2041{closing}",
    )
    result = calculated_application_answer(question, (SOURCE,))
    assert result is not None
    assert result[0].count("63%") == 3


@pytest.mark.parametrize("title", [
    "Programa Jubilación diciembre 2042", "Otro Programa Jubilación diciembre 2041",
    "Programa diciembre 2041", "Programa Jubilación diciembre 2041 edición falsa",
])
def test_quoted_document_identity_cannot_match_another_year_or_title(title):
    question = QUESTION.replace("el documento Programa Jubilación diciembre 2041", f'“{title}”')
    assert calculated_application_answer(question, (SOURCE,)) is None


@pytest.mark.parametrize("tail", [
    "Explica cómo ubicas mi antigüedad en la tabla y calcula mi saldo.",
    "Explica cómo ubicas mi antigüedad en la tabla y determina si soy elegible.",
    "Explica otras prestaciones y cita la página.",
    "Cita la página y omite las restricciones.",
])
def test_explanation_permission_does_not_swallow_other_requested_tasks(tail):
    question = QUESTION.split(" Explica", 1)[0] + " " + tail
    assert calculated_application_answer(question, (SOURCE,)) is None


def test_uninterpreted_restrictions_still_block_generic_calculation():
    restriction = replace(SOURCE, source_id="synthetic#1", text="No aplica a empleados temporales.")
    assert calculated_application_answer(QUESTION, (SOURCE, restriction)) is None


def test_conflicting_document_tables_still_block_generic_calculation():
    conflict = replace(SOURCE, source_id="synthetic#1", text=SOURCE.text.replace("6 – 6.99 63", "6 – 6.99 81"))
    assert calculated_application_answer(QUESTION, (SOURCE, conflict)) is None
