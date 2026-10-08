# Creado por Aldo Garcia.
"""Regresiones sintéticas: representación, conceptos y localizadores de fuente."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.rag.grounding import verify_grounding
from app.rag.numeric_grounding import numeric_claim_supported
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit

QUESTION = (
    "Si ingresé en enero de 2020 y me retiro con 6 años y 6 meses de antigüedad "
    "antes de jubilarme, ¿qué porcentaje corresponde?"
)
RULE = (
    "Portabilidad del Programa Sintético\n"
    "Los empleados que ingresaron antes del 1 de febrero de 2020 tienen derecho al 92% de la "
    "Aportación Base y para recibir la Aportación Base Complementaria y Adicional Complementaria, "
    "es de acuerdo a la tabla de antigüedad.\n"
    "Los empleados que ingresen a partir del 1 de febrero de 2020 tendrán derecho a la Aportación Base, "
    "Aportación Base Complementaria y Adicional Complementaria de acuerdo a la siguiente tabla de antigüedad:\n"
    "Antigüedad %\n0 – 4.99 0\n5 – 5.99 41\n6 – 6.99 63\n7 – 7.99 81\n8 en adelante 92"
)
SOURCE = Evidence(
    source_id="synthetic#0", text=RULE, score=0.9, category="prestaciones",
    filename="Programa Sintético diciembre 2041.pdf", section="pagina 12", page_or_sheet="pagina 12",
    document_id="synthetic-program", chunk_id="synthetic-chunk",
)


def verify(text, *, source=SOURCE, question=QUESTION, extra=()):
    return verify_grounding(text + " [[synthetic#0]]", (source, *extra), question=question, mode="cited")


def test_longest_subject_does_not_mix_base_with_base_complementaria():
    result = verify("Si esos datos declarados son correctos, la Aportación Base Complementaria se calcula al 63%.")
    assert result.grounded, result.validation_detail
    assert not verify(
        "Si esos datos declarados son correctos, la Aportación Base Complementaria se calcula al 92%."
    ).grounded


def test_each_component_keeps_its_own_value_in_same_cited_claim():
    text = (
        "Si esos datos declarados son correctos, la Aportación Base se calcula al 92%; "
        "la Aportación Base Complementaria se calcula al 63%; la Adicional Complementaria se calcula al 63%."
    )
    result = verify(text)
    assert result.grounded, result.validation_detail
    swapped = text.replace("92%", "SWAP").replace("63%", "92%").replace("SWAP", "63%")
    assert not verify(swapped).grounded


def test_page_must_match_metadata_even_if_wrong_page_number_occurs_in_table():
    assert not verify("La tabla figura en la página 7.", question="Indica la fuente.").grounded
    assert verify("La tabla figura en la página 12.", question="Indica la fuente.").grounded


def test_document_title_and_page_are_supported_by_same_cited_metadata():
    result = verify(
        'Documento: "Programa Sintético diciembre 2041.pdf", página 12.', question="Indica la fuente."
    )
    assert result.grounded, result.validation_detail
    assert not verify(
        'Documento: "Programa Sintético diciembre 2042.pdf", página 12.', question="Indica la fuente."
    ).grounded
    assert not verify("El pago es de 2041 pesos.", question="Indica el pago.").grounded


def test_page_of_another_cited_document_cannot_be_attributed_to_named_document():
    other = replace(SOURCE, source_id="other#0", document_id="other", filename="Otro diciembre 2042.pdf", page_or_sheet="pagina 7")
    answer = 'Documento: "Programa Sintético diciembre 2041.pdf", página 7. [[synthetic#0]] [[other#0]]'
    result = verify_grounding(answer, (SOURCE, other), question="Indica la fuente.", mode="cited")
    assert not result.grounded
    assert "pagina" in result.validation_detail


@pytest.mark.parametrize("unit", ["%", "por ciento", "por  ciento", "por\tciento"])
def test_percent_whitespace_cannot_bypass_component_binding(unit):
    text = "Si esos datos declarados son correctos, la Aportación Base Complementaria se calcula al "
    assert not verify(text + "92 " + unit + ".").grounded
    assert verify(text + "63 " + unit + ".").grounded


def test_conflicting_rows_for_same_document_and_case_cannot_be_selected_by_citing_only_one():
    other = replace(SOURCE, source_id="synthetic#1", chunk_id="other", text=RULE.replace("6 – 6.99 63", "6 – 6.99 64"))
    text = "Si esos datos declarados son correctos, la Adicional Complementaria se calcula al 63%."
    result = verify(text, extra=(other,))
    assert not result.grounded
    assert "contradictoria" in result.validation_detail


def test_duplicate_consistent_chunks_do_not_create_a_conflict():
    other = replace(SOURCE, source_id="synthetic#1", chunk_id="other")
    assert verify(
        "Si esos datos declarados son correctos, la Adicional Complementaria se calcula al 63%.", extra=(other,)
    ).grounded


def test_decimal_representations_are_exact_and_keep_units_property():
    # Propiedades enumeradas: sin dependencia Hypothesis ni red; 201 valores.
    for cents in range(-100, 101):
        value = Decimal(cents) / 100
        source = f"La tasa es {value:.2f}% y el plazo es 6 años."
        for spelling in (str(value), f"{value:.4f}", str(value).replace(".", ",")):
            assert numeric_claim_supported(f"La tasa es {spelling}%.", (source,)), (source, spelling)
            assert not numeric_claim_supported(f"Se pagan {spelling} pesos.", (source,))
        assert not numeric_claim_supported(f"La tasa es {value + Decimal('0.01')}%.", (source,))


def test_numeric_formats_do_not_turn_grouped_or_mixed_separators_into_suffix_values():
    for source in ("El pago es 1,234.56 pesos.", "El pago es 1.234,56 pesos."):
        assert not numeric_claim_supported("El pago es 56 pesos.", (source,))
    assert not numeric_claim_supported("La tasa es 5%.", ("La tasa es -0.5%.",))
    for ambiguous in ("1,000", "1.000", "12,345", "123.456"):
        source = f"El pago es {ambiguous} pesos."
        assert numeric_claim_supported(source, (source,))
        decimal = Decimal(ambiguous.replace(",", "."))
        assert not numeric_claim_supported(f"El pago es {decimal:.4f} pesos.", (source,))
