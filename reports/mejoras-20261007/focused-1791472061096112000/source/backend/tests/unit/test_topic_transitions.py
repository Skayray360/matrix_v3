# Creado por Aldo Garcia.
"""Cambio de tema explícito sin trasladar condiciones entre prestaciones."""

from dataclasses import replace

import pytest

from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit
TOPIC = "Plan de Pensiones por Fallecimiento e Invalidez"
LENSES = Evidence(
    source_id="lentes#0", text="Lentes y Anteojos\nEl apoyo está documentado.",
    score=0.9, category="prestaciones", filename="lentes.pdf", section="pagina 1",
    page_or_sheet="pagina 1", document_id="lentes", chunk_id="lentes-0",
)
PLAN = replace(
    LENSES, source_id="plan#0", document_id="plan", chunk_id="plan-0", filename="plan.pdf",
    text=f"{TOPIC}\nEs una prestación incluida en el paquete de prestaciones.",
)
INTRO = (
    "**Cobertura de Lentes y Anteojos (Exclusivo Personal de Planta):**\n"
    "El apoyo está documentado. [[lentes#0]]\n"
)


def verify(text, *, plan=PLAN):
    return verify_grounding(
        text, (LENSES, plan), question="Explica las prestaciones", mode="cited",
    )


@pytest.mark.parametrize("opener", ["Respecto al", "Respecto a el", "Respecto del", "En cuanto al"])
def test_initial_complete_topic_transition_closes_inherited_heading(opener):
    text = INTRO + f"{opener} {TOPIC}, es una prestación incluida en el paquete de prestaciones. [[plan#0]]"
    result = verify(text)
    assert result.grounded, result.reason


def test_explicit_transition_heading_persists_for_the_next_cited_unit():
    text = INTRO + f"Respecto al {TOPIC}, es una prestación documentada. [[plan#0]]\n"
    text += "La información está en la página 1. [[plan#0]]"
    assert verify(text).grounded


@pytest.mark.parametrize("claim", [
    f"**Lentes y Anteojos:**\nRespecto al {TOPIC}, es una prestación documentada.",
    f"## Lentes y Anteojos\nRespecto al {TOPIC}, es una prestación documentada.",
    "Respecto a Lentes y Anteojos, es una prestación documentada.",
    "Respecto al Plan de Pensiones, es una prestación documentada.",
    "Respecto al Otro Plan de Pensiones por Fallecimiento e Invalidez, es una prestación documentada.",
    f"Los Lentes y Anteojos son equivalentes. Respecto al {TOPIC}, es una prestación documentada.",
    f"Es una prestación documentada respecto al {TOPIC}.",
])
def test_transition_cannot_replace_explicit_heading_or_use_partial_or_later_mentions(claim):
    result = verify(INTRO + claim + " [[plan#0]]")
    assert not result.grounded
    assert result.reason == "beneficio y fuente citada no corresponden"


def test_transition_cannot_merge_different_cited_topics():
    text = INTRO + f"Respecto al {TOPIC}, es una prestación documentada. [[plan#0]] [[lentes#0]]"
    assert not verify(text).grounded


def test_transition_requires_an_identified_source_topic():
    text = INTRO + f"Respecto al {TOPIC}, es una prestación documentada. [[plan#0]]"
    plan = replace(PLAN, text="Este párrafo carece de título explícito. Se describe una prestación.")
    assert not verify(text, plan=plan).grounded


def test_transition_does_not_change_citation_allowlist():
    text = INTRO + f"Respecto al {TOPIC}, es una prestación documentada. [[unknown#0]]"
    assert verify(text).has_invalid_citations
