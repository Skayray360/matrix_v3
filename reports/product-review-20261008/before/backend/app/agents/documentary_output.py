# Creado por Aldo Garcia.
"""Contrato opcional de transporte; las afirmaciones aun requieren grounding.

El JSON nunca es una respuesta publica ni una prueba de veracidad. Cada unidad
se presenta con sus propias citas y despues pasa por el verificador habitual.
"""
from __future__ import annotations

import json
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.common.answers import DOCUMENTED_HEADING, safe_nonfactual_text
from app.config import get_settings

STRUCTURED_ANSWER_INSTRUCTIONS = (
    "TRANSPORTE: devuelve solo JSON con status (answered o insufficient), claims "
    "(lista de {text,citations}) y clarification. Cada text contiene una afirmacion "
    "en una linea, sin encabezados ni citas incrustadas; citations contiene los "
    "aliases E1, E2 de sus fuentes. No combines conceptos sin respaldo. Para "
    "insufficient deja claims vacio. clarification es una pregunta de precision "
    "sin hechos ni cifras, o cadena vacia. El servidor agrega encabezados y citas."
)

DOCUMENTARY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "claims", "clarification"],
    "properties": {
        "status": {"type": "string", "enum": ["answered", "insufficient"]},
        "claims": {
            "type": "array", "maxItems": 12,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["text", "citations"],
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 1600},
                    "citations": {"type": "array", "minItems": 1, "maxItems": 4,
                                  "uniqueItems": True,
                                  "items": {"type": "string", "pattern": "^E[1-9][0-9]*$"}},
                },
            },
        },
        "clarification": {"type": "string", "maxLength": 600},
    },
}
_VALIDATOR = Draft202012Validator(DOCUMENTARY_SCHEMA)
SCHEMA_BUDGET_CHARS = len(json.dumps(DOCUMENTARY_SCHEMA, ensure_ascii=False))


def structured_output_enabled(*, document_summary: bool, has_structured: bool) -> bool:
    settings = get_settings()
    return (settings.answer_structured_output and settings.answer_evidence_mode == "cited"
            and not document_summary and not has_structured)


def _unique_object(pairs: list[tuple]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("propiedad JSON duplicada")
        value[key] = item
    return value


def render_documentary_output(content: str, aliases: dict[str, str]) -> str:
    """Falla de forma cerrada; no repara JSON ni elimina afirmaciones invalidas."""
    try:
        value = json.loads(content, object_pairs_hook=_unique_object)
        _VALIDATOR.validate(value)
    except (ValueError, RecursionError, ValidationError) as exc:
        # No incorporar texto del modelo a mensajes de error o logs ordinarios.
        raise ValueError("contrato documental JSON invalido") from exc
    clarification = value["clarification"].strip()
    if clarification and (not clarification.endswith("?") or not safe_nonfactual_text(clarification)):
        raise ValueError("aclaracion documental no valida")
    claims = value["claims"]
    if value["status"] == "insufficient":
        if claims:
            raise ValueError("insuficiencia con afirmaciones")
        answer = "No cuento con informacion suficiente para responder esta pregunta."
        return answer + (f"\n\n{clarification}" if clarification else "")
    if not claims:
        raise ValueError("respuesta documental sin afirmaciones")
    paragraphs = []
    for claim in claims:
        text = claim["text"].strip()
        if not text or re.search(r"[\r\n]|\[\[|\]\]|^\s*#", text):
            raise ValueError("afirmacion documental con formato no valido")
        if any(alias not in aliases for alias in claim["citations"]):
            raise ValueError("cita fuera del contexto documental")
        citations = " ".join(f"[[{alias}]]" for alias in claim["citations"])
        paragraphs.append(f"{text} {citations}")
    if clarification:
        paragraphs.append(clarification)
    return DOCUMENTED_HEADING + "\n\n" + "\n\n".join(paragraphs)
