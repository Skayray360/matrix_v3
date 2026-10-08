# Creado por Aldo Garcia.
"""Procedencia de una respuesta; las citas no acreditan veracidad semantica."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

AnswerBasis = Literal["documented", "general", "mixed", "insufficient"]
DOCUMENTED_HEADING = "### Información documentada"
GENERAL_HEADING = "### Orientación general"

APPLICATION_LIMIT_ANSWER = (
    "La evidencia disponible no permite comprobar la aplicación de la tabla a los datos declarados. "
    "Necesito una regla y una tabla completas, o precisar los datos del caso."
)

_SAFE_CLARIFICATIONS = (
    "¿Cuál es el documento de referencia?",
    "¿Puede indicar el nombre completo del beneficio o el documento de referencia?",
    "¿Cuál es su fecha de ingreso y su antigüedad?",
    "¿Cuál es su condición sindical?",
    "¿Su contratación es de planta o eventual?",
    "¿A qué empresa o división pertenece?",
    "No puedo confirmar la vigencia actual de esta información.",
    "No cuento con información documental suficiente para indicar el importe.",
    "No cuento con información documental suficiente.",
    APPLICATION_LIMIT_ANSWER,
)


def safe_nonfactual_text(text: str) -> bool:
    """Frases completas acotadas, no un marcador que habilite texto arbitrario."""
    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFKD", value.casefold())
        return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())
    remaining = normalize(text).strip()
    if not remaining:
        return False
    for phrase in sorted(_SAFE_CLARIFICATIONS, key=len, reverse=True):
        remaining = remaining.replace(normalize(phrase), "")
        # La misma abstencion seguida de una pregunta puede usar punto y coma.
        if phrase.endswith('.'):
            remaining = remaining.replace(normalize(phrase[:-1]) + ';', '')
    return not remaining.strip()

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
