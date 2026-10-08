# Creado por Aldo Garcia.
"""Preguntas de precision completas pueden cerrar una respuesta documentada."""

import pytest

from app.common.answers import safe_nonfactual_text
from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence


def documented_answer_with_tail(tail):
    item = Evidence(
        source_id="prestaciones/synthetic#0",
        text="El documento describe las prestaciones del personal.",
        score=0.9,
        category="prestaciones",
        filename="Programa Sintético.pdf",
        section="pagina 1",
        page_or_sheet="pagina 1",
        document_id="synthetic-clarification-document",
        chunk_id="synthetic-clarification-chunk",
    )
    answer = f"{item.text} [[{item.source_id}]]\n\n{tail}"
    return verify_grounding(answer, (item,), mode="cited")


@pytest.mark.parametrize(
    "question",
    [
        "Para poder explicarle sus prestaciones de manera específica, ¿podría indicarme si usted pertenece al personal no sindicalizado, de planta o eventual?",
        "Para poder explicarle sus prestaciones, ¿podría indicarme su condición sindical y su tipo de contratación?",
        "Para explicarte tus prestaciones, ¿puedes precisarme tu tipo de contratación?",
        "Para explicar las prestaciones, ¿puede especificarnos su condición sindical?",
        "¿Podría indicarnos su tipo de contratación?",
        "¿Puedes precisarnos tu condición sindical?",
        "¿Puede especificarme su tipo de contratación?",
        "¿Podrías compartirme el documento de referencia?",
        "¿Podría compartirnos el documento de referencia?",
        "¿Podría indicar su tipo de contratación?",
    ],
)
def test_complete_clarifying_questions_can_follow_documented_claims(question):
    assert safe_nonfactual_text(question)
    report = documented_answer_with_tail(question)
    assert report.grounded, report


@pytest.mark.parametrize(
    "text",
    [
        "Para poder explicarle sus prestaciones, podría indicarme su condición sindical.",
        "Para poder explicarle sus prestaciones, el personal eventual recibe un bono.",
        "Para poder explicarle sus prestaciones que concede el plan, ¿podría indicarme su condición sindical?",
        "Para poder explicarle sus prestaciones, el plan es obligatorio, ¿podría indicarme su condición sindical?",
        "Para poder explicarle sus prestaciones, ¿podría indicarme el documento que concede un bono?",
        "Para poder explicarle sus prestaciones, ¿podría indicarme si tiene derecho al bono?",
        "Para poder explicarle sus prestaciones, ¿podría indicarme si recibe 12 días?",
        "Para poder explicarle sus prestaciones, ¿podría indicarme su contratación? El plan concede un bono.",
        "El plan concede un bono. Para poder explicarle sus prestaciones, ¿podría indicarme su contratación?",
        "Para poder explicarle sus prestaciones, ¿podría indicarme su contratación?, pero el plan está vigente.",
        "Para poder explicarle sus prestaciones, indique su contratación.",
    ],
)
def test_clarification_preface_does_not_approve_factual_or_declarative_tail(text):
    assert not safe_nonfactual_text(text)
    report = documented_answer_with_tail(text)
    assert not report.grounded
    assert report.reason == "afirmacion documental final sin cita"
