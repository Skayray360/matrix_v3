# Creado por Aldo Garcia.
"""Actualiza datos declarados en seguimientos; nunca agrega reglas empresariales.

Los campos proceden exclusivamente de preguntas previamente autorizadas y del
turno actual. La recuperacion semantica y la sintesis siguen consultando fuentes.
Una expresion ambigua se conserva completa para pedir aclaracion, no se adivina.
"""

from __future__ import annotations

import re
import unicodedata

from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.memory.service import MAX_CONTEXT_QUERY_CHARS
from app.rag.claim_context import declared_case
from app.rag.numeric_grounding import _plain

_CASE_REMAINDER = re.compile(
    r"(?:(?:y|pero|si|ahora|entonces|con|tengo|llevo|cumplo|cuento|mi|es|son|"
    r"antiguedad|de|en|la|empresa|trabajando|aqui|seria|cuanto|me|corresponde|"
    r"tocaria|toca|que|pasa|ocurre|solo|solamente|actualmente|retiro|retirarme)\s*)+"
)

_CASE_PRESENTATION_TAIL = re.compile(
    r"(?<=[?.])\s*(?:indica|incluye|muestra|cita|agrega|senala) "
    r"(?:(?:el )?(?:documento|fuente) y (?:la )?pagina|"
    r"(?:el porcentaje|los porcentajes) de cada aportacion y (?:la )?pagina)\s*\.?\s*$"
)

_DOCUMENT_ANAPHORA = re.compile(
    r"\b(?:(?:ese|este|dicho|el mismo) (?:documento|archivo|manual|reglamento)|"
    r"(?:esa|esta|dicha|la misma) (?:fuente|presentaci[oó]n|pl[aá]tica))\b",
    re.IGNORECASE,
)
_QUOTED_TITLE = r'(?:“[^”\n]{1,350}”|"[^"\n]{1,350}"|«[^»\n]{1,350}»|\x27[^\x27\n]{1,350}\x27)'
_DOCUMENT_INTRO = re.compile(
    r"\b(?:seg[uú]n|de acuerdo con)\s+"
    r"(?P<source>" + _QUOTED_TITLE + r"|"
    r"(?:el|la)\s+(?:documento|archivo|manual|reglamento|fuente|presentaci[oó]n|pl[aá]tica)"
    r"[^,\n¿?]{1,350})(?=[,\n¿?])", re.IGNORECASE,
)

_EXPLICIT_TENURE_UPDATE = re.compile(
    r"(?:manten|conserva) (?:mi|la(?: misma)?) fecha de ingreso y las demas condiciones"
    r"(?: del caso anterior)? (?:pero )?(?:cambia|modifica|actualiza) (?:mi|la) antiguedad a"
    r"(?: que porcentaje(?: me)? corresponde(?: ahora)?(?: y por que)?)?"
)


def _bare_document_anaphora(question: str) -> bool:
    """Distingue «ese documento» de «ese documento Titulo_nuevo».

    Un titulo nuevo no puede recibir el titulo del turno anterior. Se reconoce
    solo la forma de la referencia; los nombres se resuelven luego dentro del
    alcance autorizado de recuperacion.
    """
    matches = tuple(_DOCUMENT_ANAPHORA.finditer(question))
    if not matches:
        return False
    for match in matches:
        tail = _plain(question[match.end():])
        if not tail.strip() or re.match(r"\s*[,.;:¿?¡!)]", tail):
            continue
        # Estas continuaciones describen o consultan el referente existente.
        # Un nombre literal entre comillas, con guiones o con espacios no entra
        # en esta lista y por tanto constituye una referencia independiente.
        if re.match(
            r"\s+(?:sobre|acerca|segun|para|que|adjunto|cargado|subido|anterior|"
            r"y|con|sin|en|indica|incluye|muestra|cita|explica)\b", tail,
        ):
            continue
        return False
    return True


def document_anaphora_remainder(question: str) -> str | None:
    """Retira solo la referencia anaforica para detectar otra fuente explicita.

    No resuelve nombres ni consulta historial; recibe una pregunta ya saneada.
    Una orden de citar no identifica por si misma un documento nuevo.
    """
    if not _bare_document_anaphora(question):
        return None
    text = _DOCUMENT_ANAPHORA.sub("referente_previo", _plain(question))
    text = re.sub(r"\b(?:segun|de acuerdo con)\s+referente_previo\b", "referente_previo", text)
    return re.sub(
        r"\b(?:indica|incluye|muestra|cita|agrega|senala)(?: el)? (?:documento|fuente)"
        r" y (?:la )?pagina\s*[.]?\s*$", "", text,
    )


def _resolve_document_anaphora(question: str, reference: str) -> str | None:
    """Traslada solo un titulo del antecedente autorizado, nunca su respuesta.

    Si no hay una unica introduccion documental delimitada se conserva el
    contexto completo. Una fuente entre comillas puede estar al inicio o tras
    los datos del caso. No se copia la pregunta previa junto con el titulo:
    sus intervalos o solicitudes anteriores no son datos del caso vigente.
    """
    if len(_DOCUMENT_ANAPHORA.findall(question)) != 1 or not _bare_document_anaphora(question):
        return None
    sources = {match["source"].strip() for match in _DOCUMENT_INTRO.finditer(reference)}
    if len(sources) != 1:
        return None
    source = sources.pop()
    return _DOCUMENT_ANAPHORA.sub(lambda _match: source, question, count=1)


def is_case_followup(question: str) -> bool:
    """Una modificacion breve de campos conocidos, sin otro tema en la misma frase."""
    case = declared_case(question)
    if case.tenure is None and case.entry is None:
        return False
    remainder = _plain(question)
    for quote in (case.tenure_quote, case.entry_quote):
        if quote:
            remainder = remainder.replace(quote, " ", 1)
    if len(_DOCUMENT_ANAPHORA.findall(question)) == 1:
        # La misma referencia puede acompañar al cambio del caso al principio
        # o al final. Retirar solo esta cláusula; otras fuentes/condiciones
        # permanecen y deben superar el contrato completo del seguimiento.
        remainder = re.sub(
            r"\b(?:segun|de acuerdo con)\s+" + _DOCUMENT_ANAPHORA.pattern, "", remainder,
            flags=re.IGNORECASE,
        )
    if case.entry is None:
        remainder = re.sub(
            r"\b(?:manteniendo|conservando) (?:la )?misma fecha de ingreso"
            r"(?: y (?:las )?demas condiciones)?\b", "", remainder,
        )
    # Solo una instruccion final de presentacion. Otra fuente, excepcion,
    # importe o accion permanece en el resto y bloquea la sustitucion del caso.
    remainder = _CASE_PRESENTATION_TAIL.sub("", remainder)
    remainder = re.sub(r"[¿?¡!.,;:]", " ", remainder)
    remainder = " ".join(remainder.split())
    if case.tenure is not None and case.entry is None and _EXPLICIT_TENURE_UPDATE.fullmatch(remainder):
        return True
    return not remainder or bool(_CASE_REMAINDER.fullmatch(remainder))


def _replace_quote(text: str, quote: str, replacement: str) -> str | None:
    """Localiza una cita sin perder acentos, mayusculas ni el texto restante."""
    normalized, positions = [], []
    for index, char in enumerate(text):
        for plain in unicodedata.normalize("NFKD", char.casefold()):
            if not unicodedata.combining(plain):
                normalized.append(plain)
                positions.append(index)
    haystack = "".join(normalized)
    if not quote or haystack.count(quote) != 1:
        return None
    start = haystack.index(quote)
    end = start + len(quote)
    return text[:positions[start]] + replacement + text[positions[end - 1] + 1:]


def _bounded(query: str) -> str:
    if len(query) > MAX_CONTEXT_QUERY_CHARS:
        raise InferenceFailureError(
            InferenceFailureKind.CONTEXT_LIMIT,
            "El contexto de la consulta es demasiado largo. "
            "Precise el documento y los datos vigentes en un nuevo turno.",
        )
    return query


def contextualize_question(question: str, reference: str) -> str:
    """Conserva el tema y reemplaza el dato anterior solo cuando es inequivoco.

    Concatenar «7 anos» y «8 anos» impedia al validador identificar el caso. Aqui
    se sustituye ese campo; no se redondean duraciones ni se calculan porcentajes.
    El mensaje original se conserva por separado en el historial.
    """
    if not reference:
        return _bounded(question)
    joined = f"{reference}\nSeguimiento: {question}"
    if not is_case_followup(question):
        resolved = _resolve_document_anaphora(question, reference)
        return _bounded(resolved if resolved is not None else joined)
    old, new = declared_case(reference), declared_case(question)
    updated = reference
    for old_value, old_quote, new_value, new_quote in (
        (old.tenure, old.tenure_quote, new.tenure, new.tenure_quote),
        (old.entry, old.entry_quote, new.entry, new.entry_quote),
    ):
        if new_value is None:
            continue
        if old_value is None or not old_quote:
            # Agregar un campo faltante mantiene la peticion original explicita.
            # Si el antecedente era ambiguo, el parser continuara absteniendose.
            return _bounded(joined)
        replacement = _replace_quote(updated, old_quote, new_quote)
        if replacement is None:
            return _bounded(joined)
        updated = replacement
    return _bounded(updated)
