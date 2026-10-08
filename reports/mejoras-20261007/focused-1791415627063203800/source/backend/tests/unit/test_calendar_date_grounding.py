# Creado por Aldo Garcia.
"""Equivalencias de fechas completas sin convertir sus dias en cantidades."""

import pytest

from app.rag.grounding import verify_grounding
from app.rag.numeric_grounding import numeric_claim_supported
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit

SOURCE = "El programa flexible inicia el primero de julio del 2041."


@pytest.mark.parametrize("day", ["primero", "1", "01"])
@pytest.mark.parametrize("connector", ["de", "del"])
def test_first_day_spellings_preserve_the_complete_calendar_date(day, connector):
    claim = f"El programa inicia el {day} de julio {connector} 2041."
    assert numeric_claim_supported(claim, (SOURCE,))
    assert numeric_claim_supported(SOURCE, (claim,))


def test_date_whitespace_and_markdown_preserve_the_same_date():
    assert numeric_claim_supported("Inicia el **1** de\njulio del 2041.", (SOURCE,))


@pytest.mark.parametrize("claim", ["Se entrega 1 peso.", "El plazo es 1 año.", "La tasa es 1%.", "El plazo es 2041 años."])
def test_calendar_day_and_year_do_not_supply_other_units(claim):
    assert not numeric_claim_supported(claim, (SOURCE,))


@pytest.mark.parametrize("claim", [
    "Inicia el 2 de julio de 2041.",
    "Inicia el 1 de agosto de 2041.",
    "Inicia el 1 de julio de 2042.",
])
def test_changed_date_cannot_use_other_numbers_or_calendar_components(claim):
    additions = "Se admiten 2 pagos. Otro programa inicia el primero de agosto de 2042."
    assert not numeric_claim_supported(claim, (SOURCE + " " + additions,))
    assert not numeric_claim_supported(claim, (SOURCE, additions))


def test_date_requires_all_components_in_one_source():
    assert not numeric_claim_supported("Inicia el 1 de julio de 2041.", ("Inicia el 1 de julio.", "Edición de 2041."))


@pytest.mark.parametrize("date_text", [
    "31 de abril de 2041", "29 de febrero de 2041", "0 de julio de 2041",
    "32 de julio de 2041", "1 de julio de 0000", "1.5 de julio de 2041", "-1 de julio de 2041",
])
def test_impossible_or_noninteger_calendar_dates_are_rejected_even_if_copied(date_text):
    text = "Inicia el " + date_text + "."
    assert not numeric_claim_supported(text, (text,))


def test_valid_leap_day_and_multiple_complete_dates_keep_exact_matching():
    source = "Inicia el 29 de febrero del 2040 y termina el primero de julio del 2041."
    assert numeric_claim_supported("Inicia el 29 de febrero de 2040 y termina el 1 de julio de 2041.", (source,))
    assert not numeric_claim_supported("Inicia el 29 de febrero de 2041.", (source,))


def test_cited_claim_accepts_only_the_date_in_its_cited_document():
    source = Evidence(source_id="synthetic#0", text=SOURCE, score=0.9, category="prestaciones",
                      filename="Programa flexible.pdf", page_or_sheet="pagina 4")
    answer = "* Programa flexible: inicia el 1 de julio de 2041 [[synthetic#0]]"
    result = verify_grounding(answer, (source,), question="Explica mis prestaciones", mode="cited")
    assert result.grounded, result.validation_detail
    assert not verify_grounding(answer.replace("2041", "2042"), (source,), mode="cited").grounded
