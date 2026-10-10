# Creado por Aldo Garcia.
"""Alcance explicito de un resumen sobre evidencia previamente autorizada.

No usa conocimiento del dominio ni nombres particulares. Una seccion se
identifica por su encabezado o metadatos, nunca por coincidencias del tema en
cualquier frase del cuerpo. Si no se localiza, no sustituye el pedido por todo
el documento. Las fuentes y sus textos permanecen intactos.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.rag.schemas import Evidence

_NUMBER_WORDS = {
    "uno": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
}
_POINTS = re.compile(
    r"\b(?:en\s+)?(?P<number>\d{1,2}|uno|un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)"
    r"\s+(?:puntos|vinetas|ideas)(?:\s+(?:clave|principales))?\b"
)
SECTION_NOT_FOUND = (
    "No localice la seccion solicitada en el contenido disponible. "
    "¿Puedes indicar su encabezado exacto o las paginas que deseas resumir?"
)


def _normalize(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(char))


def requested_summary_points(question: str) -> int | None:
    match = _POINTS.search(_normalize(question))
    if not match:
        return None
    value = match.group("number")
    count = int(value) if value.isdigit() else _NUMBER_WORDS[value]
    return count if 1 <= count <= 20 else None


def summary_map_question(question: str, *, part: int, total: int) -> str:
    # El numero de puntos es un requisito del resultado final, no de cada mapa.
    # NFKD no es necesario para retirar el patron: la forma de numero es ASCII
    # y se admite vinetas acentuadas en esta operacion de transporte.
    focus = re.sub(_POINTS.pattern.replace("vinetas", "vi[nñ]etas"), "", question, flags=re.IGNORECASE)
    return (f"Resume la parte {part} de {total} del material proporcionado para atender esta peticion: "
            f"{focus}\nConserva solo los temas solicitados y las citas de esta parte.")


def requested_summary_section(question: str) -> str:
    normalized = _normalize(question)
    match = re.search(r"\b(?:seccion|apartado|capitulo)\s+(?:(?:de|del|sobre)\s+)?", normalized)
    if not match:
        return ""
    tail = normalized[match.end():].lstrip()
    if not tail:
        return ""
    quoted = re.match(r'["\u201c\u00ab]([^"\u201d\u00bb]+)["\u201d\u00bb]', tail)
    if quoted:
        return quoted.group(1).strip()
    # El limite se basa en instrucciones de formato/fuente, no en vocabulario
    # de la seccion ("control de acceso" sigue siendo un encabezado completo).
    tail = re.split(
        r"[.,;:!?\n]|\s+en\s+(?:\d+|uno|un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s+"
        r"(?:puntos|vinetas|ideas)\b|\s+(?:del|de este|de ese)\s+(?:documento|archivo|manual|pdf)\b"
        r"|\s+(?:e\s+indica|y\s+(?:cita|resume|explica|compara|senala|menciona)|con\s+(?:citas|fuentes))\b",
        tail, maxsplit=1,
    )[0].strip(' "\u201d\u00bb')
    return tail if len(tail.split()) <= 16 else ""


def _heading_matches(heading: str, section: str) -> bool:
    heading = _normalize(heading).strip()
    heading = re.sub(r"^(?:#+\s*|(?:seccion|apartado|capitulo)\s+)", "", heading)
    heading = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", heading)
    # Un encabezado puede ampliar el nombre pedido ("Portabilidad del plan").
    return bool(re.match(rf"{re.escape(section)}(?:\b|$)", heading))


@dataclass(frozen=True, slots=True)
class SummarySelection:
    evidences: tuple[Evidence, ...]
    section_requested: bool = False
    clarification: str = ""


def select_summary_scope(question: str, evidences: tuple[Evidence, ...]) -> SummarySelection:
    section = requested_summary_section(question)
    if not section:
        return SummarySelection(evidences)
    locations: set[tuple[str, str, str, str]] = set()
    selected_ids: set[str] = set()
    for item in evidences:
        # PDF no tiene jerarquia de encabezados: su primera linea es el titulo
        # de la pagina. No buscar dentro del cuerpo ni en el nombre del archivo.
        first_line = next((line.strip() for line in item.text.splitlines() if line.strip()), "")
        title_line = first_line if len(first_line.split()) <= 20 else ""
        if _heading_matches(item.section, section) or _heading_matches(title_line, section):
            selected_ids.add(item.source_id)
            if item.page_or_sheet:
                locations.add((item.scope, item.document_id, item.filename, item.page_or_sheet))
    selected = tuple(item for item in evidences if item.source_id in selected_ids or (
        item.scope, item.document_id, item.filename, item.page_or_sheet,
    ) in locations)
    return SummarySelection(selected, section_requested=True,
                            clarification="" if selected else SECTION_NOT_FOUND)
