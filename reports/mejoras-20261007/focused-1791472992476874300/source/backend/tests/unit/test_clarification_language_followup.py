# Creado por Aldo Garcia.
"""Variantes de aclaracion observadas al responder con memoria conversacional."""

import pytest

from app.common.answers import safe_nonfactual_text
from app.rag.claim_context import unverified_current_claim
from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence


def verify_tail(tail):
    source = Evidence(
        source_id="synthetic#0", text="El documento describe las prestaciones del personal.",
        score=0.9, category="prestaciones", filename="Prestaciones.pdf", page_or_sheet="pagina 1",
        section="pagina 1", document_id="synthetic-followup", chunk_id="0",
    )
    return verify_grounding(f"{source.text} [[{source.source_id}]]\n\n{tail}", (source,), mode="cited")


@pytest.mark.parametrize("question", [
    "Para poder indicarle con precisión cuáles son *sus* prestaciones, ¿podría indicarme su tipo de contrato y si pertenece a la División Minas?",
    "Para poder indicarle con precisión cuáles son sus prestaciones, ¿podría indicarme su condición sindical o tipo de contratación?",
    "Para precisar cuáles son tus prestaciones, ¿puedes compartirnos tu tipo de contratación?",
    "Para detallar sus prestaciones, ¿podría indicarme su condición sindical?",
    "Para identificar las prestaciones, ¿cuál es el documento de referencia?",
    "Para explicar **tus** prestaciones, ¿puedes indicar tu tipo de contratación?",
    "¿Podría indicarme su **condición sindical** o *tipo de contratación*?",
    "Para poder detallar sus prestaciones específicas, ¿podría indicarme su condición sindical o tipo de contratación?",
    "Para explicarle sus prestaciones de manera precisa, ¿podría indicarme su tipo de contratación?",
    "Para indicarte con mayor precisión tus prestaciones concretas, ¿puedes indicar tu contratación?",
    "Para precisar cuáles son tus prestaciones de forma específica, ¿cuál es tu tipo de contratación?",
    "¿Podría decirme su tipo de contratación?",
    "Para orientarle mejor, ¿cuál es su tipo de contratación?",
    "Para poder ayudarte, ¿puedes decirme tu condición sindical?",
    "¿Cuál es su condición sindical o tipo de contratación (planta, eventual, etc.)?",
    "¿Podría indicar su tipo de contratación (planta, eventual, etc.) y su división?",
    'Para poder detallar "mis prestaciones", ¿podría indicar si usted pertenece al personal no sindicalizado, planta o eventual, y si aplica alguna de las coberturas específicas mencionadas (como Lentes y Anteojos)?',
    'Para explicar «tus prestaciones», ¿podría decirme tu condición sindical?',
])
def test_purpose_and_emphasis_preserve_a_nonfactual_clarification(question):
    assert safe_nonfactual_text(question)
    assert verify_tail(question).grounded


@pytest.mark.parametrize("text", [
    "Para indicarle cuáles son sus prestaciones, el plan concede un bono.",
    "Para poder indicarle con precisión cuáles son sus prestaciones, podría indicarme su contratación.",
    "Para confirmar que tienes derecho a sus prestaciones, ¿puedes indicar tu contratación?",
    "Para indicarle sus prestaciones que incluyen un vehículo, ¿podría indicar su contratación?",
    "Para indicarle con precisión cuáles son sus prestaciones obligatorias, ¿podría indicar su contratación?",
    "Para indicarle con precisión cuáles son sus prestaciones, ¿podría indicar si recibe **12 días**?",
    "Para indicarle con precisión cuáles son sus prestaciones, ¿podría indicar su contratación? El plan concede un vehículo.",
    "Para indicarle con precisión cuáles son sus prestaciones, ¿podría indicar el documento que concede un vehículo?",
    "Para indicarle con precisión cuáles son sus prestaciones, ¿podría indicar si tiene *derecho* al bono?",
    "El plan está vigente. Para indicar sus prestaciones, ¿podría indicar su contratación?",
    "Para detallar sus prestaciones específicas que incluyen un vehículo, ¿podría indicar su contratación?",
    "Para explicar sus prestaciones obligatorias, ¿puede indicar su contratación?",
    "Para explicar sus prestaciones de manera precisa, el plan concede un bono. ¿Cuál es su contratación?",
    "Para detallar sus prestaciones específicas, ¿podría indicar qué documento concede el bono? El bono es obligatorio.",
    "Para orientarle mejor y garantizar su bono, ¿cuál es su tipo de contratación?",
    "Para ayudarle, el plan concede un vehículo. ¿Cuál es su tipo de contratación?",
    "¿Cuál es su contratación (planta, eventual, etc.)? El plan concede un vehículo.",
    "¿Cuál es su contratación? El plan concede un vehículo, etc.",
    "¿Cuál es su contratación (planta, eventual, etc.) y recibe 12 días?",
    'Para detallar "mis prestaciones obligatorias", ¿podría indicar su contratación?',
    'Para detallar "mis prestaciones", ¿podría indicar su contratación? La vigencia de la información documentada varía según la fecha de cada fuente.',
])
def test_purpose_does_not_exempt_facts_numbers_or_nonquestions(text):
    assert not safe_nonfactual_text(text)
    report = verify_tail(text)
    assert not report.grounded
    assert report.reason == "afirmacion documental final sin cita"


def test_request_for_current_profile_does_not_claim_current_policy():
    claim = (
        "Para explicarle sobre sus prestaciones, necesito saber a qué población pertenece "
        "(planta, eventual, etc.) y si tiene información sobre su situación laboral actual.\n\n"
        "Las prestaciones son beneficios adicionales a los que se tiene derecho por pertenecer a un vínculo laboral"
    )
    assert not unverified_current_claim(claim)


@pytest.mark.parametrize("claim", [
    "Necesito información sobre su situación laboral actual. El plan está vigente.",
    "Puede indicarme su situación laboral actual; las prestaciones son actuales.",
    "Su situación laboral actual es de planta y el plan está vigente.",
    "El documento demuestra que sus prestaciones están vigentes.",
])
def test_current_profile_request_never_exempts_current_policy(claim):
    assert unverified_current_claim(claim)
