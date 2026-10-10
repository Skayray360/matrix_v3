# Creado por Aldo Garcia.
"""Propiedades deterministas acotadas, sin Hypothesis ni servicios/modelos.

Contrato: preservar valor/unidad y fila, no interpretar agrupaciones ambiguas
de un solo separador ni multiplicadores de quinquenio. Semilla fija y <= 250
casos por propiedad; los oraculos son enteros/Fraction del dominio generado.
"""

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from random import Random

import pytest

from app.rag.claim_context import declared_case
from app.rag.grounding import verify_grounding
from app.rag.numeric_grounding import numeric_claim_supported, tenure_tables
from tests.unit.test_calculated_application import SOURCE

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('amount', ('1,000,000', '1.000.000'))
def test_million_tokens_cannot_disappear_from_validation(amount):
    assert not numeric_claim_supported(f'El importe es {amount} pesos.', ('El importe es 1 peso.',))
    assert numeric_claim_supported(f'El importe es {amount} pesos.', (f'El importe es {amount} pesos.',))


def test_generated_thousands_keep_exact_value_and_unit():
    rng = Random(20261008)
    for _ in range(250):
        amount = rng.randint(1_000_000, 999_999_999)
        canonical = f'El importe es {amount} pesos.'
        for separator in (',', '.'):
            rendered = f'{amount:,}'.replace(',', separator)
            assert numeric_claim_supported(f'El importe es {rendered} pesos.', (canonical,))
            assert not numeric_claim_supported(f'El importe es {rendered} años.', (canonical,))
            assert not numeric_claim_supported(f'El importe es {rendered} pesos.', (f'El importe es {amount + 1} pesos.',))


def test_generated_grouped_currency_decimal_keeps_cents_and_sign():
    rng = Random(20261009)
    for _ in range(150):
        cents = rng.randint(100_000, 99_999_999)
        value = Decimal(cents) / 100
        canonical = f'El importe es {value:.2f} pesos.'
        english = f'{value:,.2f}'
        spanish = english.translate(str.maketrans({',': '.', '.': ','}))
        for rendered in (english, spanish):
            assert numeric_claim_supported(f'El importe es {rendered} pesos.', (canonical,))
            assert not numeric_claim_supported(f'El importe es -{rendered} pesos.', (canonical,))


@pytest.mark.parametrize('number', ('1,00,000', '1.00.000', '1,,000', '1..000', '1,000.00.1'))
def test_malformed_numbers_fail_closed_even_when_repeated(number):
    text = f'El importe es {number} pesos.'
    assert not numeric_claim_supported(text, (text,))
    source = replace(SOURCE, text=text)
    report = verify_grounding(f'{text} [[{source.source_id}]]', (source,), mode='cited')
    assert not report.grounded


@pytest.mark.parametrize('prefix', ('doble', 'triple', 'cuádruple', 'medio', 'la mitad de un'))
def test_modified_quinquennium_never_becomes_five_years(prefix):
    modified = f'El periodo dura un {prefix} quinquenio.'
    ordinary = 'El periodo dura 5 años.'
    assert not numeric_claim_supported(ordinary, (modified,))
    assert not numeric_claim_supported(modified, (ordinary,))


def test_generated_table_selection_preserves_row_and_cannot_borrow_other_percentage():
    rng = Random(20261010)
    for _ in range(120):
        start = rng.randint(1, 30)
        count = rng.randint(2, 8)
        rates = rng.sample(range(1, 100), count)
        text = 'Antigüedad %\n' + '\n'.join(
            f'{start + index} – {start + index}.99 {rate}' for index, rate in enumerate(rates)
        )
        table, = tenure_tables(text)
        selected = rng.randrange(count)
        years = start + selected
        months = rng.randrange(12)
        row = table.select(Fraction(years) + Fraction(months, 12))
        assert row is not None and row.percent == rates[selected]
        correct = f'La fila {years}–{years}.99 corresponde a {rates[selected]}%.'
        wrong = f'La fila {years}–{years}.99 corresponde a {rates[(selected + 1) % count]}%.'
        assert numeric_claim_supported(correct, (text,))
        assert not numeric_claim_supported(wrong, (text,))


@pytest.mark.parametrize('years', range(1, 21))
def test_years_and_half_agree_with_six_months(years):
    first = declared_case(f'Llevo {years} años y medio y me retiro sin jubilarme.')
    second = declared_case(f'Tengo {years} años y 6 meses de antigüedad antes de jubilarme.')
    assert first.tenure == second.tenure == Fraction(2 * years + 1, 2)
    assert first.before_retirement and second.before_retirement


@pytest.mark.parametrize('years', ('1,000', '1.000', '1,000,000', '1.000.000'))
def test_ambiguous_or_grouped_tenure_is_not_silently_reduced_to_one_year(years):
    assert declared_case(f'Tengo {years} años de antigüedad.').tenure is None


@pytest.mark.parametrize('field,value', [('filename', 'Otro.pdf'), ('page_or_sheet', 'pagina 99'), ('document_id', 'other')])
def test_source_identifier_does_not_hide_conflicting_metadata(field, value):
    other = replace(SOURCE, **{field: value})
    answer = f'{SOURCE.text} [[{SOURCE.source_id}]]'
    report = verify_grounding(answer, (SOURCE, other), mode='cited')
    assert not report.grounded
    assert 'ambiguo' in report.reason
