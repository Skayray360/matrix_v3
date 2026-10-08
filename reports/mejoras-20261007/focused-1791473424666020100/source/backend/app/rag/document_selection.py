# Creado por Aldo Garcia.
"""Ancla nombres completos de documentos entre candidatos ya autorizados.

No consulta un catalogo, no infiere ediciones de fechas sueltas y no amplifica
scores semanticos. Una coincidencia parcial conserva el ranking normal.
"""

from __future__ import annotations

import re
import unicodedata

from app.rag.vector_store import ScoredPayload

_TITLE_JOINERS = frozenset({"de", "del", "el", "la", "los", "las"})


def _normalize(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(char)
    )


def _title_tokens(text: str) -> tuple[str, ...]:
    # Diciembre2022 y Diciembre 2022 designan el mismo nombre de archivo.
    spaced = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", text)
    return tuple(token for token in re.findall(r"[a-z0-9]+", spaced) if token not in _TITLE_JOINERS)


def _mentions_filename(question: str, question_tokens: tuple[str, ...], filename: str) -> bool:
    # Algunos archivos importados conservan escapes de acentos del contenedor
    # ZIP. Decodificar solo U+00C0..U+00FF, nunca controles, rutas ni IDs.
    filename = re.sub(
        r"#U00([c-fC-F][0-9a-fA-F])", lambda match: chr(int("00" + match.group(1), 16)), filename,
    )
    filename = _normalize(filename.replace("\\", "/").rsplit("/", 1)[-1])
    if not filename:
        return False
    # Una referencia literal con extension puede ser incluso un nombre corto.
    stem, separator, extension = filename.rpartition(".")
    if separator and extension and re.search(rf"(?<!\w){re.escape(filename)}(?!\w)", question):
        return True
    title = _title_tokens(stem if separator else filename)
    # Una palabra tematica o un anio aislado nunca identifica un documento.
    if sum(token.isalpha() and len(token) >= 3 for token in title) < 2:
        return False
    width = len(title)
    return any(question_tokens[start:start + width] == title for start in range(len(question_tokens) - width + 1))


def explicit_document_candidates(question: str, candidates: list[ScoredPayload]) -> list[ScoredPayload]:
    """Mejor chunk por documento nombrado; solo devuelve elementos de candidates.

    La llamada debe ocurrir despues de la consulta con filtros ACL. Los campos
    de identidad separan versiones y ambitos privados, aunque compartan nombre.
    """
    normalized_question = _normalize(question)
    question_tokens = _title_tokens(normalized_question)
    selected: list[ScoredPayload] = []
    seen: set[tuple[str, ...]] = set()
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        payload = candidate.payload
        filename = str(payload.get("filename", ""))
        if not _mentions_filename(normalized_question, question_tokens, filename):
            continue
        key = tuple(str(payload.get(field, "")) for field in (
            "scope", "owner_user_id", "conversation_id", "category", "document_id",
        )) + (filename,)
        if key in seen:
            continue
        seen.add(key)
        selected.append(candidate)
    return selected
