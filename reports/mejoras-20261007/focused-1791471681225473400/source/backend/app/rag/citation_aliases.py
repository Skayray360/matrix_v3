# Creado por Aldo Garcia.
"""Citas de transporte breves, limitadas a la evidencia enviada al modelo.

Un alias no es una fuente nueva ni una correccion aproximada del nombre de un
PDF. Se resuelve exclusivamente mediante el mapa de ESTA llamada, despues de
empaquetar el contexto y antes de comprobar el contrato documental habitual.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.rag.schemas import Evidence

if TYPE_CHECKING:
    from app.structured_data.tool import StructuredEvidence

_CITATION = re.compile(r"\[\[([^\]]{1,240})\]\]")


def citation_aliases(
    evidences: tuple[Evidence, ...], structured: tuple[StructuredEvidence, ...] = (),
) -> dict[str, str]:
    """Alias -> source_id canonico; nunca incluir fuentes descartadas del prompt."""
    documents = tuple(item.source_id for item in evidences)
    tables = tuple(item.source_id for item in structured)
    canonical = set(documents) | set(tables)
    aliases: dict[str, str] = {}
    for prefix, sources in (("E", documents), ("S", tables)):
        for index, source in enumerate(sources, start=1):
            alias = f"{prefix}{index}"
            # Un identificador canonico no puede reinterpretarse como un alias.
            if alias not in canonical:
                aliases[alias] = source
    return aliases


def expand_citation_aliases(answer: str, aliases: dict[str, str]) -> str:
    """Expande solo etiquetas exactas; las desconocidas llegan al verificador."""
    def replace(match: re.Match[str]) -> str:
        source = aliases.get(match.group(1).strip())
        return f"[[{source}]]" if source is not None else match.group(0)

    return _CITATION.sub(replace, answer)
