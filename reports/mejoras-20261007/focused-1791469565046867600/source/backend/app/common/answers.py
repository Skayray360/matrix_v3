# Creado por Aldo Garcia.
"""Procedencia de una respuesta; las citas no acreditan veracidad semantica."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

AnswerBasis = Literal["documented", "general", "mixed", "insufficient"]
DOCUMENTED_HEADING = "### Información documentada"
GENERAL_HEADING = "### Orientación general"

def safe_nonfactual_text(text: str) -> bool:
    """Actos no factuales completos, sin un catalogo de respuestas del producto.

    Admite preguntas de precision y negativas de conocimiento con objetos
    acotados. No basta que un texto contenga una palabra de abstencion. Las
    clausulas afirmativas, importes y reglas adicionales siguen necesitando cita.
    """
    normalized = unicodedata.normalize("NFKD", text.casefold())
    value = " ".join("".join(c for c in normalized if not unicodedata.combining(c)).split())
    # El enfasis tipografico no convierte una pregunta en una afirmacion.
    # Retirar solo delimitadores pareados, sin borrar el texto que encierran.
    value = re.sub(r"\*\*([^*]+)\*\*|(?<!\*)\*([^*]+)\*(?!\*)", lambda m: m[1] or m[2], value)
    if not value or len(value) > 1000 or re.search(r"\d|\[\[|[%$=]", value):
        return False
    if re.search(r"\b(?:pero|aunque|sin embargo|excepto|salvo|recibe\w*|paga\w*|otorga\w*|garantiza\w*|"
                 r"corresponde\w*|derecho|equivale\w*)\b", value):
        return False
    # Este proposito completo no afirma una regla. Solo se retira antes de una
    # pregunta explicita; prefacios con condiciones o afirmaciones se conservan.
    precision = r"(?:con (?:mayor )?precision|de (?:manera|forma) (?:precisa|especifica))"
    value = re.sub(
        r"^para (?:poder )?(?:explicar|indicar|precisar|detallar|identificar)(?:le|te|les)? "
        rf"(?:{precision} )?(?:cuales son )?(?:sus|tus|las) prestaciones"
        rf"(?: (?:especificas|particulares|concretas))?(?: {precision})?,\s*(?=¿)",
        "",
        value,
        count=1,
    )
    value = re.sub(
        r"^para (?:poder )?(?:ayudar|orientar)(?:le|te|les)?(?: mejor)?,\s*(?=¿)",
        "", value, count=1,
    )
    # "(planta, eventual, etc.)" termina una enumeracion, no una oracion.
    # Solo neutralizar la abreviatura ante cierre de parentesis; conservar los
    # demas puntos para detectar afirmaciones mezcladas con una pregunta.
    value = re.sub(r"\betc\.(?=\s*\))", "etc", value)
    pieces = [part.strip(" ¿") for part in re.findall(r"[^.!?;]+[.!?;]?", value) if part.strip(" ¿.!?;")]
    for part in pieces:
        if part.endswith('?'):
            # Preguntas abiertas sobre identificacion; sin subordinadas que
            # afirmen una regla dentro de la propia pregunta.
            if not re.match(r"(?:que |cual(?:es)? |a que |de que |en que |su contratacion |"
                            r"(?:puede|puedes|podria|podrias) "
                            r"(?:indicar|precisar|especificar|compartir|decir)(?:me|nos)? )", part):
                return False
            if re.search(r"\bque (?:incluye|establece|concede|permite|obliga|prohibe|exige)\b", part):
                return False
            continue
        part = part.rstrip('.;')
        absence = re.fullmatch(
            r"no (?:(?:cuento|contamos) con|(?:dispongo|disponemos) de|encontre|encontramos|localice|tengo) "
            r"(?:la |una |las |los )?(?:informacion|evidencia|documentacion|documentos|fuentes)"
            r"(?: (?:documental|autorizad[ao]s?|pertinente|suficiente|disponible|verificable|necesaria))*"
            r"(?: para (?:responder(?: (?:eso|esta pregunta|esa pregunta|la consulta))?|"
            r"indicar (?:el importe|el monto|el porcentaje)|identificar (?:el plan|ese plan|el beneficio)))?",
            part,
        )
        uncertainty = re.fullmatch(
            r"no (?:puedo|pude|es posible) (?:confirmar|verificar|comprobar|acreditar) "
            r"(?:la vigencia(?: actual)?|la aplicabilidad|la elegibilidad|el calculo|la aplicacion de la tabla)"
            r"(?: (?:de esta informacion|de esa informacion|con las fuentes disponibles|a los datos declarados))?",
            part,
        )
        limitation = re.fullmatch(
            r"(?:la|esta|esa) (?:evidencia|documentacion)(?: disponible| autorizada)? "
            r"no permite (?:confirmar|verificar|comprobar) (?:la aplicacion de la tabla|el calculo|la aplicabilidad)"
            r"(?: a los datos declarados)?", part,
        )
        request = re.fullmatch(
            r"(?:necesito|hace falta) (?:un documento|una fuente|una regla(?: y una tabla)?)"
            r"(?: complet[ao]s?| de referencia)?(?:, o precisar los datos del caso)?", part,
        )
        access = re.fullmatch(r"no (?:tiene|tienes) acceso a (?:esa|esta|la) informacion", part)
        if not (absence or uncertainty or limitation or request or access):
            return False
    return bool(pieces)

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
