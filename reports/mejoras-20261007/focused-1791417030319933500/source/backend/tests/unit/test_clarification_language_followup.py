# Creado por Aldo Garcia.
"""Variantes de aclaracion observadas al responder con memoria conversacional."""

import pytest

from app.common.answers import safe_nonfactual_text
from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence


def verify_tail(tail):
    source = Evidence(
        source_id="synthetic#0", text="El documento describe las prestaciones del personal.",
        score=0.9, category="prestaciones", filename="Prestaciones.pdf", page_or_sheet="pagina 1",
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
])
def test_purpose_does_not_exempt_facts_numbers_or_nonquestions(text):
    assert not safe_nonfactual_text(text)
    report = verify_tail(text)
    assert not report.grounded
    assert report.reason == "afirmacion documental final sin cita"
