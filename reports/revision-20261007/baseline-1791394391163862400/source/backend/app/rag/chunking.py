# Creado por Aldo Garcia.
"""Chunking estructural.

Prioridad de separadores (seccion 9): encabezados/secciones -> parrafos ->
listas -> tablas/filas -> fallback por tokens.

Regla explicita del proyecto: **no partir una lista corta o un procedimiento
breve** si cabe entero dentro del tamano maximo. Partir un procedimiento de seis
pasos en dos chunks es la causa mas comun de respuestas incompletas en un RAG de
politicas de RH.

Estimacion de tokens: se usa una aproximacion determinista (palabras x 1.3)
en lugar de un tokenizador especifico del modelo. Es intencional -- el
tokenizador de Ollama no se expone por API y una estimacion estable y
reproducible es preferible a una exacta pero dependiente del modelo. Los valores
por defecto (900/120) dejan margen suficiente para el error de la estimacion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Factor empirico palabra -> token para texto en espanol.
_TOKENS_PER_WORD = 1.3

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM_RE = re.compile(r"^\s*([-*+•]|\d+[.)])\s+")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def estimate_tokens(text: str) -> int:
    """Estimacion determinista de tokens."""
    if not text:
        return 0
    words = len(text.split())
    return max(1, int(words * _TOKENS_PER_WORD))


#: Un chunk no se corta en un encabezado si aun no acumulo este minimo de
#: tokens. Evita generar chunks que solo contienen un titulo, que no aportan
#: evidencia y ensucian la recuperacion.
HEADING_BREAK_MIN_TOKENS = 80


@dataclass(frozen=True, slots=True)
class TextBlock:
    """Bloque estructural extraido de un documento."""

    text: str
    section: str
    page_or_sheet: str = ""
    kind: str = "paragraph"  # paragraph | heading | list | table
    #: Nivel del encabezado (1-6). Solo relevante cuando ``kind == "heading"``.
    level: int = 0


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """Chunk antes de recibir metadata de documento."""

    text: str
    section: str
    page_or_sheet: str
    index: int


def split_into_blocks(text: str, *, default_section: str = "") -> list[TextBlock]:
    """Convierte texto plano/Markdown en bloques estructurales.

    Se agrupan las lineas consecutivas del mismo tipo: una lista completa es un
    bloque, una tabla completa es un bloque. Asi el chunker puede tratarlas como
    unidades indivisibles cuando caben.
    """
    blocks: list[TextBlock] = []
    section = default_section
    buffer: list[str] = []
    buffer_kind = "paragraph"

    def flush() -> None:
        nonlocal buffer, buffer_kind
        content = "\n".join(buffer).strip()
        if content:
            blocks.append(TextBlock(text=content, section=section, kind=buffer_kind))
        buffer = []
        buffer_kind = "paragraph"

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            section = heading.group(2).strip()
            blocks.append(
                TextBlock(
                    text=line.strip(),
                    section=section,
                    kind="heading",
                    level=len(heading.group(1)),
                )
            )
            continue
        if not line.strip():
            flush()
            continue

        kind = "paragraph"
        if _TABLE_ROW_RE.match(line):
            kind = "table"
        elif _LIST_ITEM_RE.match(line):
            kind = "list"

        if buffer and kind != buffer_kind:
            flush()
        buffer_kind = kind
        buffer.append(line)

    flush()
    return blocks


def chunk_blocks(
    blocks: list[TextBlock], *, chunk_size_tokens: int, overlap_tokens: int
) -> list[ChunkDraft]:
    """Agrupa bloques en chunks respetando limites estructurales.

    Un bloque indivisible (lista o tabla) que cabe en el tamano maximo nunca se
    parte. Solo un bloque que por si solo excede el maximo se divide por frases y,
    en ultimo termino, por palabras.
    """
    if chunk_size_tokens <= overlap_tokens:
        raise ValueError("chunk_size_tokens debe ser mayor que overlap_tokens")
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens no puede ser negativo")

    drafts: list[ChunkDraft] = []
    current: list[TextBlock] = []
    current_tokens = 0
    index = 0

    def emit(items: list[TextBlock]) -> None:
        nonlocal index
        content = "\n\n".join(b.text for b in items).strip()
        if not content:
            return
        section = next((b.section for b in items if b.section), "")
        page = next((b.page_or_sheet for b in items if b.page_or_sheet), "")
        drafts.append(ChunkDraft(text=content, section=section, page_or_sheet=page, index=index))
        index += 1

    for block in blocks:
        block_tokens = estimate_tokens(block.text)

        # Una cita tiene una unica pagina/hoja. No atribuir texto de la siguiente
        # pagina a la primera ni arrastrar solape a traves de esa frontera.
        if current and block.page_or_sheet != current[-1].page_or_sheet:
            emit(current)
            current, current_tokens = [], 0

        # Un encabezado es frontera estructural: abre chunk nuevo aunque el
        # actual no este lleno. Sin esta regla un documento corto entero cae en
        # un unico chunk y su embedding queda diluido entre temas distintos, que
        # es justo lo que hunde la recuperacion en un corpus de politicas.
        if (
            block.kind == "heading"
            and current
            and current_tokens >= HEADING_BREAK_MIN_TOKENS
        ):
            emit(current)
            current, current_tokens = [], 0

        if block_tokens > chunk_size_tokens:
            # Bloque gigante: se cierra lo acumulado y se parte por frases.
            if current:
                emit(current)
                current, current_tokens = [], 0
            for piece in _split_oversized(block, chunk_size_tokens):
                emit([piece])
            continue

        if current and estimate_tokens("\n\n".join(b.text for b in [*current, block])) > chunk_size_tokens:
            emit(current)
            # El solape comparte el presupuesto con el bloque entrante. Antes
            # podia producir 910 tokens con un limite de 900. Se calcula sobre
            # el texto unido para no acumular el redondeo de bloques pequenos.
            carry: list[TextBlock] = []
            for previous in reversed(current):
                proposed = [previous, *carry]
                if estimate_tokens("\n\n".join(b.text for b in proposed)) > overlap_tokens:
                    break
                if estimate_tokens("\n\n".join(b.text for b in [*proposed, block])) > chunk_size_tokens:
                    break
                carry.insert(0, previous)
            current = carry

        current.append(block)
        current_tokens = estimate_tokens("\n\n".join(b.text for b in current))

    if current:
        emit(current)
    return drafts


def _split_oversized(block: TextBlock, chunk_size_tokens: int) -> list[TextBlock]:
    """Divide un bloque demasiado grande, primero por frases y luego por palabras."""
    sentences = re.split(r"(?<=[.!?])\s+", block.text)
    pieces: list[TextBlock] = []
    buffer: list[str] = []

    for sentence in sentences:
        sentence_tokens = estimate_tokens(sentence)
        if sentence_tokens > chunk_size_tokens:
            if buffer:
                pieces.append(TextBlock(" ".join(buffer), block.section, block.page_or_sheet, block.kind))
                buffer = []
            words = sentence.split()
            step = max(1, int(chunk_size_tokens / _TOKENS_PER_WORD))
            for start in range(0, len(words), step):
                pieces.append(
                    TextBlock(
                        " ".join(words[start : start + step]),
                        block.section,
                        block.page_or_sheet,
                        block.kind,
                    )
                )
            continue
        if buffer and estimate_tokens(" ".join([*buffer, sentence])) > chunk_size_tokens:
            pieces.append(TextBlock(" ".join(buffer), block.section, block.page_or_sheet, block.kind))
            buffer = []
        buffer.append(sentence)

    if buffer:
        pieces.append(TextBlock(" ".join(buffer), block.section, block.page_or_sheet, block.kind))
    return pieces


def chunk_text(
    text: str, *, chunk_size_tokens: int, overlap_tokens: int, default_section: str = ""
) -> list[ChunkDraft]:
    """Atajo: bloques + chunking en una sola llamada."""
    return chunk_blocks(
        split_into_blocks(text, default_section=default_section),
        chunk_size_tokens=chunk_size_tokens,
        overlap_tokens=overlap_tokens,
    )
