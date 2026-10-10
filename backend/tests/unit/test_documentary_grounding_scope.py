# Creado por Aldo Garcia.
"""Roles documentales de cifras: manuales técnicos, tablas y casos sintéticos."""

from dataclasses import replace

import pytest

from app.rag.grounding import verify_grounding
from app.rag.numeric_grounding import numeric_claim_supported
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit


def evidence(text, **changes):
    item = Evidence(
        source_id="manual#0", text=text, score=0.9, category="manuales",
        filename="Guia_integracion_AD.docx", section="seccion 1", page_or_sheet="",
        document_id="synthetic-manual", chunk_id="synthetic-manual-0",
    )
    return replace(item, **changes)


def check(text, source, *, question="Explica el documento.", extra=()):
    return verify_grounding(text + " [[manual#0]]", (evidence(source), *extra), mode="cited", question=question)


@pytest.mark.parametrize("text", [
    "El servicio usa la versión 1.2.3 y Python 3.12.2.",
    "La dirección del controlador es 10.20.30.40.",
    "La dirección LDAP es ldap://dc01.example.test:389/OU=People,DC=example,DC=test.",
    r"La configuración está en C:\Servicios\Kerberos5\krb5.conf.",
    "El comando klist -li 0x3e7 muestra los tickets del contexto del sistema.",
    "El documento describe AES256 y el host DC01.",
    "La actualización documentada ocurrió el 2026-10-08.",
    "El ejemplo usa Kerberos y Active Directory (AD), con KDC en el puerto 88 y LDAP en el puerto 389.",
])
def test_technical_literals_survive_summary_with_their_cited_source(text):
    report = check(text, text)
    assert report.grounded, (report.reason, report.validation_detail)


@pytest.mark.parametrize("source,claim", [
    ("Se usa AES256.", "Se usa AES128."),
    ("El controlador es DC01.", "El controlador es DC02."),
    ("La versión es 1.2.3.", "La versión es 1.2.4."),
    ("El host es 10.20.30.40.", "El host es 10.20.30.41."),
    (r"La ruta es C:\Windows\System32\krb5.conf.", r"La ruta es C:\Windows\System64\krb5.conf."),
    ("El endpoint es ldap://dc01.example.test:389.", "El endpoint es ldap://dc02.example.test:389."),
    ("El comando es klist -li 0x3e7.", "El comando es klist -li 0x3e8."),
    ("La fecha es 2026-10-08. Otra fecha es 2025-11-09.", "La fecha es 2026-11-09."),
    ("El puerto KDC es 88.", "El puerto KDC es 89."),
])
def test_changed_technical_literal_cannot_borrow_other_numbers(source, claim):
    assert not check(claim, source).grounded


@pytest.mark.parametrize("value", ["1.2.3", "10.20.30.40", "AES256", "DC01", "0x3e7"])
def test_uncited_evidence_does_not_supply_technical_identifier(value):
    other = evidence("El valor documentado es " + value + ".", source_id="other#0", document_id="other")
    assert not check("El valor es " + value + ".", "El manual describe la integración.", extra=(other,)).grounded


@pytest.mark.parametrize("value", ["1.2.3", "10.20.30.40", "AES256"])
def test_technical_literal_never_supplies_a_quantity(value):
    for claim in ("Se pagan 3 pesos.", "La tasa es 20%.", "La vigencia es 256 años."):
        assert not numeric_claim_supported(claim, ("El identificador es " + value + ".",))


@pytest.mark.parametrize("claim", ["El plazo es 13días.", "La tasa es 13por ciento."])
def test_adjacent_unit_is_a_quantity_instead_of_an_alphanumeric_identifier(claim):
    spaced = claim.replace("13", "13 ")
    assert numeric_claim_supported(claim, (spaced,))
    assert numeric_claim_supported(spaced, (claim,))
    assert not numeric_claim_supported(claim, (spaced.replace("13", "14"),))


@pytest.mark.parametrize("value", ["1..000pesos", "1.00.000pesos", "1.2.3pesos", "1,,000dias"])
def test_invalid_amount_is_not_a_technical_literal_even_without_unit_spacing(value):
    text = "El valor es " + value + "."
    assert not numeric_claim_supported(text, (text,))


def test_filename_or_hostname_can_be_explained_as_component_of_exact_path():
    source = r"Abra C:\Windows\System32\krb5.conf y consulte ldap://dc01.example.test:389."
    assert check("El archivo es krb5.conf y el host es dc01.example.test.", source).grounded
    assert not check("El archivo es krb6.conf y el host es dc01.example.test.", source).grounded


TABLE = "Antigüedad %\n6 – 6.99 63\n7 – 7.99 81\n8 en adelante 92"


@pytest.mark.parametrize("claim", [
    "El tramo 6 – 6.99 años corresponde al 63%.",
    "La fila entre 6 y 6.99 años corresponde al 63%.",
    "La fila de 6 a 6.99 años corresponde al 63%.",
    "El tramo 6 – 6.99 años corresponde al 63%; el tramo 7 – 7.99 años corresponde al 81%.",
    "- De 6 a 6.99 años: 63%.\n- De 7 a 7.99 años: 81%.\n- De 8 en adelante: 92%.",
])
def test_table_summary_binds_each_row_without_a_personal_case(claim):
    report = check(claim, TABLE, question="Resume la tabla y explica sus porcentajes.")
    assert report.grounded, (report.reason, report.validation_detail)


@pytest.mark.parametrize("claim", [
    "El tramo 6 – 6.99 años corresponde al 81%.",
    "El tramo 6 – 6.99 años corresponde al 63%; el tramo 7 – 7.99 años corresponde al 63%.",
    "El tramo 6 – 6.99 años corresponde al 63%. Además se aplica un 81%.",
    "El tramo 6 – 7.99 años corresponde al 63%.",
])
def test_table_summary_cannot_swap_rows_or_borrow_unbound_percentage(claim):
    assert not check(claim, TABLE).grounded


def test_conditional_documentary_rule_is_not_a_user_case_declaration():
    source = "Si la antigüedad es de 7 años, el plazo de pago es de 2 meses."
    report = check(source, source)
    assert report.grounded, report.validation_detail


@pytest.mark.parametrize("text", [
    "El comando identifica el usuario actual.",
    "klist muestra los tickets vigentes.",
    "El procedimiento consulta el directorio actual y la sesión actual.",
    "El inicio de sesión utiliza las credenciales vigentes.",
])
def test_runtime_state_does_not_certify_historical_policy_validity(text):
    report = check(text, text)
    assert report.grounded, report.reason
    for additional in (" El plan está vigente.", " Hoy aplica el programa."):
        assert not check(text + additional, text + additional).grounded


def test_document_title_can_group_different_sections_of_the_same_document():
    first = evidence("Guía integración AD\nEl manual describe la autenticación.")
    second = evidence("Registro de servicios\nEl procedimiento registra el SPN.", source_id="manual#1", chunk_id="second")
    answer = (
        "## Guía integración AD\nEl manual describe la autenticación. [[manual#0]]\n"
        "El procedimiento registra el SPN. [[manual#1]]"
    )
    report = verify_grounding(answer, (first, second), mode="cited")
    assert report.grounded, report.reason
    foreign = replace(second, document_id="foreign", filename="Otro manual.docx")
    assert not verify_grounding(answer, (first, foreign), mode="cited").grounded


def test_same_filename_with_distinct_document_identity_is_not_a_shared_heading_scope():
    first = evidence("Guía integración AD\nEl manual describe la autenticación.")
    other = replace(first, source_id="other#0", document_id="foreign", text="Registro de servicios\nRegistre el SPN.")
    text = "## Guía integración AD\nRegistre el SPN. [[other#0]]"
    assert not verify_grounding(text, (first, other), mode="cited").grounded


def test_internal_benefit_heading_does_not_inherit_document_wide_scope():
    first = evidence("Apoyo de lentes\nLa tasa es 12%.")
    second = evidence("Bono anual\nLa tasa es 15%.", source_id="manual#1", chunk_id="second")
    text = "## Apoyo de lentes\nLa tasa es 15%. [[manual#1]]"
    assert not verify_grounding(text, (first, second), mode="cited").grounded


def test_row_binding_does_not_hide_compound_cardinal_or_other_quantity():
    source = "Antigüedad %\n6 – 6.99 2"
    assert not check("La fila 6 – 6.99 años recibe treinta y dos por ciento.", source).grounded
    assert not check("La fila 6 – 6.99 años recibe 2% y 900 pesos.", source).grounded


def test_complete_extracts_have_a_distinct_contract_from_numeric_paraphrases():
    # A source can contain an ambiguous value; quoting it verifies fidelity,
    # whereas interpreting it as a usable amount must still fail closed.
    text = "El importe original figura como 1.00.000 pesos."
    item = evidence(text)
    assert not check(text, text).grounded
    report = verify_grounding(text + " [[manual#0]]", (item,), mode="extractive")
    assert report.grounded and report.extractive_verified and not report.factual_verified

