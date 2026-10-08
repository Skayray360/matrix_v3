# Creado por Aldo Garcia.
"""Procedencia de una respuesta; las citas no acreditan veracidad semantica."""

from __future__ import annotations

import re
from typing import Literal

AnswerBasis = Literal["documented", "general", "mixed", "insufficient"]
DOCUMENTED_HEADING = "### Información documentada"
GENERAL_HEADING = "### Orientación general"

_GENERAL_SECTION = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?(?:orientaci[oó]n general|conocimiento general)"
    r"(?: del modelo)?[ \t]*:?[ \t]*$", re.IGNORECASE | re.MULTILINE,
)
_DOCUMENTED_SECTION = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?informaci[oó]n documentada[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def split_answer_sections(answer: str) -> tuple[str, str]:
    """Reconoce una seccion general completa, sin inferir origen por vocabulario."""
    match = _GENERAL_SECTION.search(answer)
    if match is None:
        return _DOCUMENTED_SECTION.sub("", answer).strip(), ""
    documented = _DOCUMENTED_SECTION.sub("", answer[:match.start()]).strip()
    return documented, answer[match.end():].strip()


def answer_basis(answer: str, *, has_citations: bool, insufficient: bool = False) -> AnswerBasis:
    """Clasifica el origen declarado, no la exactitud factual del contenido."""
    _documented, general = split_answer_sections(answer)
    if general:
        return "mixed" if has_citations else "general"
    return "insufficient" if insufficient else "documented"
