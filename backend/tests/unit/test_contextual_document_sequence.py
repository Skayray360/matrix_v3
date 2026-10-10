# Creado por Aldo Garcia.
"""El contexto de las preguntas publicadas no contamina el caso vigente."""

from fractions import Fraction

import pytest

from app.agents.contextual_query import contextualize_question, is_case_followup
from app.llm.model_policy import ModelPolicy
from app.rag.claim_context import declared_case

pytestmark = pytest.mark.unit

TITLE = "Plática del Plan de Pensiones por Jubilación de diciembre de 2022"
QUESTIONS = (
    f'Según “{TITLE}”, resume la sección de portabilidad en cinco puntos. Indica las páginas utilizadas.',
    "Según ese documento, ¿qué porcentaje corresponde a los empleados que ingresaron a partir del "
    "1 de abril de 2016 y tienen entre 7 y 7.99 años de antigüedad? ¿A cuáles aportaciones aplica? Cita la página.",
    "Ingresé en mayo de 2016 y termino mi relación laboral antes de jubilarme, con 7 años y 6 meses de antigüedad. "
    "Según ese documento, ¿qué porcentaje me corresponde? Explica cómo ubicas mi antigüedad en la tabla "
    "y cita la página.",
    "Mantén mi fecha de ingreso y las demás condiciones del caso anterior, pero cambia la antigüedad "
    "a 8 años y 6 meses. ¿Qué porcentaje corresponde ahora y por qué?",
    "Si termino mi relación laboral por renuncia voluntaria antes de la edad de jubilación, ¿a dónde "
    "pueden transferirse los fondos según ese documento? Indica las restricciones expresas y la página.",
)


def _contextualize_sequence(questions=QUESTIONS):
    policy = ModelPolicy()
    queries = []
    for question in questions:
        reference = policy.contextual_reference(
            question, prior_questions=tuple(queries), documented_indices=frozenset(range(len(queries))),
        )
        queries.append(contextualize_question(question, reference))
    return queries


def test_exact_five_question_sequence_preserves_source_and_current_case_only():
    queries = _contextualize_sequence()
    assert all(TITLE in query for query in queries)
    assert "resume" not in queries[1] and "cinco puntos" not in queries[1]
    third = declared_case(queries[2])
    assert third.tenure == Fraction(15, 2)
    assert third.entry.first.year == 2016 and third.entry.first.month == 5
    assert third.before_retirement
    assert "7.99" not in queries[2]
    fourth = declared_case(queries[3])
    assert fourth.tenure == Fraction(17, 2) and fourth.entry == third.entry
    assert fourth.before_retirement
    assert "7 años y 6 meses" not in queries[3]
    assert declared_case(queries[4]).tenure is None and declared_case(queries[4]).entry is None
    assert "mayo" not in queries[4] and "antigüedad" not in queries[4]


@pytest.mark.parametrize("opening, closing", [("“", "”"), ('"', '"'), ("«", "»"), ("'", "'")])
def test_quoted_source_is_resolved_without_copying_previous_task(opening, closing):
    reference = f"Según {opening}{TITLE}{closing}, resume todo el documento."
    current = "Según ese documento, ¿a dónde se transfiere el fondo?"
    result = contextualize_question(current, reference)
    assert result == current.replace("ese documento", f"{opening}{TITLE}{closing}")
    assert "resume" not in result


def test_title_in_middle_of_prior_question_resolves_without_old_case():
    reference = f'Con 9 años, ¿qué porcentaje corresponde según “{TITLE}”?'
    result = contextualize_question("Según ese documento, ¿qué restricciones se indican?", reference)
    assert TITLE in result and "9 años" not in result


def test_multiple_named_sources_are_not_silently_resolved_to_one():
    reference = 'Según “Manual A”, el fondo se transfiere y según “Manual B”, se conserva.'
    question = "Según ese documento, ¿qué corresponde?"
    result = contextualize_question(question, reference)
    assert result == reference + "\nSeguimiento: " + question


@pytest.mark.parametrize("verb", ["cambia", "modifica", "actualiza"])
def test_explicit_tenure_update_replaces_only_current_field(verb):
    queries = _contextualize_sequence(QUESTIONS[:3])
    question = QUESTIONS[3].replace("cambia", verb)
    assert is_case_followup(question)
    result = contextualize_question(question, queries[-1])
    assert declared_case(result).tenure == Fraction(17, 2)
    assert declared_case(result).entry == declared_case(queries[-1]).entry


@pytest.mark.parametrize("question", [
    QUESTIONS[3] + " Además compara otro plan.",
    QUESTIONS[3].replace("las demás condiciones del caso anterior", "la condición de retirarme después de jubilarme"),
    QUESTIONS[3].replace("las demás condiciones del caso anterior", "las demás condiciones salvo la fecha de ingreso"),
    QUESTIONS[3].replace("8 años y 6 meses", "8 años o 9 años"),
    QUESTIONS[3].replace("la antigüedad", "la duración de mi crédito"),
    QUESTIONS[3].replace("¿Qué porcentaje corresponde ahora y por qué?", "Indica mi saldo individual."),
    QUESTIONS[3] + " Según el otro documento.",
])
def test_uninterpreted_update_conditions_are_not_removed(question):
    assert not is_case_followup(question)
    prior = _contextualize_sequence(QUESTIONS[:3])[-1]
    assert contextualize_question(question, prior).endswith(question)


def test_update_without_authorized_history_does_not_invent_date_or_source():
    question = QUESTIONS[3]
    assert ModelPolicy().contextual_reference(question, prior_questions=()) == ""
    assert contextualize_question(question, "") == question
    assert declared_case(question).entry is None
