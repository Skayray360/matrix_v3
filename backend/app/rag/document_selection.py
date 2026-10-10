# Creado por Aldo Garcia.
"""Ancla nombres completos de documentos entre candidatos ya autorizados.

No consulta un catalogo, no infiere ediciones de fechas sueltas y no amplifica
scores semanticos. Una coincidencia parcial conserva el ranking normal.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from app.rag.vector_store import ScoredPayload

_TITLE_JOINERS = frozenset({"de", "del", "el", "la", "los", "las"})


def _normalize(text: str) -> str:
    # Normalizar tambien la consulta: copiar el nombre mostrado por una fuente
    # debe resolver el mismo archivo que escribir el titulo con su acento.
    text = re.sub(
        r"#U00([c-fC-F][0-9a-fA-F])", lambda match: chr(int("00" + match.group(1), 16)), text,
        flags=re.IGNORECASE,
    )
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


def explicit_document_filenames(question: str, filenames: Iterable[str]) -> frozenset[str]:
    """Nombres mencionados completos entre metadatos previamente autorizados.

    Resuelve un titulo aunque sus chunks no superen un umbral semantico. No
    consulta un catalogo ni devuelve nombres que el llamante no haya autorizado.
    """
    normalized = _normalize(question)
    tokens = _title_tokens(normalized)
    return frozenset(name for name in filenames if _mentions_filename(normalized, tokens, name))


def has_document_reference(question: str) -> bool:
    """Detecta referencias literales que no deben sustituirse por otro archivo.

    No adivina un titulo a partir de un tema o una fecha. Los nombres sin
    extension se resuelven mediante ``explicit_document_filenames``; esta senal
    adicional permite fallar cerrado ante nombres de archivo no disponibles.
    """
    normalized = _normalize(question)
    return bool(
        re.search(r"\.(?:docx?|pdf|txt|md|xlsx?|pptx?|csv|tsv)\b", normalized)
        or re.search(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b", normalized)
        or re.search(r"\b(?:documento|archivo|manual|guia)\s+(?:llamado|titulado)\b", normalized)
        or re.search(r'(?:documento|archivo|manual|guia)\s+[\"\u00ab\u201c]', normalized)
        or re.search(r'\bsegun\s+(?:(?:el|la)\s+)?[\"\u00ab\u201c]', normalized)
        or re.search(
            r"\b(?:documento|archivo)\s+"
            r"(?!(?:adjunto|cargado|subido|anterior|completo|entero|actual|seleccionado|original|"
            r"que|sobre|acerca|con|sin|para|por|en|de|del|y|o|incluyendo)\b)"
            r"\w+[ \t]+\w+", normalized,
        )
    )


def requests_multiple_documents(question: str) -> bool:
    """Un plural explicito autoriza resumir varios adjuntos de esta conversacion."""
    return bool(re.search(
        r"\b(?:todos|todas|ambos|ambas|documentos|archivos|adjuntos|manuales|guias|compara)\b",
        _normalize(question),
    ))


def requests_all_documents(question: str) -> bool:
    """Un pedido de todo el corpus no se reduce a documentos del top-k."""
    return bool(re.search(
        r"\b(?:todos|todas|ambos|ambas)(?:\s+(?:los|las|mis|estos|estas))?\s+"
        r"(?:documentos|archivos|adjuntos|manuales|guias|politicas|reglamentos)\b", _normalize(question),
    ))


def is_unspecified_document_request(question: str) -> bool:
    """Identifica una transformacion sin titulo, tema ni antecedente resuelto."""
    tokens = _title_tokens(_normalize(question))
    generic = frozenset({
        "resume", "resumeme", "resumelo", "resumir", "resumen", "sintetiza", "sintetizalo", "sintesis",
        "explica", "explicame", "explicamelo", "describelo", "describe", "describeme", "habla", "hablame",
        "dame", "haz", "hazme", "prepara", "elabora", "quiero", "necesito", "puedes", "podrias",
        "explicar", "explicarme", "describir", "describirme", "hacer", "un", "una", "me", "por", "favor",
        "esto", "eso", "este", "ese", "esta", "esa", "lo", "anterior", "dicho", "mis", "mi", "su",
        "documento", "archivo", "adjunto", "contenido", "completo", "entero", "cargado", "subido",
        "breve", "general", "detallado", "ejecutivo", "conciso", "pequeno", "sobre", "acerca", "en",
        "puntos", "clave", "ideas", "principales", "secciones", "pasos", "y", "con", "sin",
    })
    return bool(tokens) and all(token in generic or token.isdigit() for token in tokens)


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
