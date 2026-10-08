# Creado por Aldo Garcia.
"""Equivalencias numericas acotadas para respuestas citadas.

No verifica la verdad de una parafrasis ni hace calculos. Conserva la unidad de
las cantidades reconocidas: ``un prestamo`` no acredita ``1 anio``. Un valor en
una tabla solo adquiere la unidad porcentaje si pertenece a la columna ``%``
de una tabla de antiguedad con encabezado y filas reconocibles en UNA fuente.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_NUMBER = r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)"
_NUMBER_RE = re.compile(rf"(?<![\w.,]){_NUMBER}(?!\d|[.,]\d)")
_WORD_VALUES = {
    "cero": "0", "un": "1", "uno": "1", "una": "1", "dos": "2",
    "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7",
    "ocho": "8", "nueve": "9", "diez": "10", "once": "11", "doce": "12",
    "trece": "13", "catorce": "14", "quince": "15", "dieciseis": "16",
    "diecisiete": "17", "dieciocho": "18", "diecinueve": "19", "veinte": "20",
}
_UNITS = {
    "ano": "year", "anos": "year", "anio": "year", "anios": "year",
    "mes": "month", "meses": "month", "dia": "day", "dias": "day",
    "hora": "hour", "horas": "hour", "minuto": "minute", "minutos": "minute",
    "segundo": "second", "segundos": "second", "prestamo": "loan", "prestamos": "loan",
    "pago": "payment", "pagos": "payment", "peso": "peso", "pesos": "peso",
}
_UNIT_PATTERN = "|".join(sorted(_UNITS, key=len, reverse=True))
_WORD_PATTERN = "|".join(sorted(_WORD_VALUES, key=len, reverse=True))
_QUANTITY_RE = re.compile(
    rf"(?<![\w.,])(?P<value>{_NUMBER}|{_WORD_PATTERN})\s*"
    rf"(?P<unit>%|por\s+ciento\b|(?:{_UNIT_PATTERN})\b)"
)
# No extraer el ultimo cardinal de una cantidad compuesta como 'treinta y dos'.
_COMPOUND_PREFIX = re.compile(r"\b(?:y|cien|ciento|\w+cientos|mil|millon|millones)\s+$")
_QUINQUENNIUM_RE = re.compile(r"\bquinquenio\b")
_FRACTION_PREFIX = re.compile(
    r"\b(?:medio|media|mitad|tercio|cuarto|fraccion|parte)\b(?:\W+\w+){0,3}\W*$"
)
_QUINQUENNIUM_SUFFIX = re.compile(
    rf"\s+(?:(?:y|mas|menos)\s+)?(?:medio|media|doble|triple|{_WORD_PATTERN}|{_NUMBER})\b"
)
_ORDERED_LIST_MARKER = re.compile(r"(?m)^[ \t]{0,3}\d{1,9}[.)][ \t]+(?=\S)")
_TENURE_HEADER = re.compile(
    r"(?:antiguedad(?:\s*\((?:anos|anios)\))?|(?:anos|anios)(?:\s+de\s+(?:servicio|antiguedad))?)"
    r"\s+(?:%|porcentaje)"
)
_TABLE_ROW = re.compile(
    rf"(?P<low>{_NUMBER})(?:\s*[-–—]\s*(?P<high>{_NUMBER})|\s+en\s+adelante)?"
    rf"\s+(?P<percent>{_NUMBER})"
)


def _plain(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Quitar solo delimitadores PAREADOS: no unir operandos como '2*3'.
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+)__", r"\1", text)
    return _ORDERED_LIST_MARKER.sub("", text)


@dataclass
class _Facts:
    raw: set[str] = field(default_factory=set)
    quantities: set[tuple[str, str]] = field(default_factory=set)
    typed_spans: list[tuple[int, int]] = field(default_factory=list)
    unsupported_compound: bool = False


def _facts(text: str) -> _Facts:
    result = _Facts(raw={m.group() for m in _NUMBER_RE.finditer(text)})
    for match in _QUANTITY_RE.finditer(text):
        value, unit = match.group("value", "unit")
        if value in _WORD_VALUES:
            if _COMPOUND_PREFIX.search(text[:match.start()]):
                result.unsupported_compound = True
                continue
            value = _WORD_VALUES[value]
        unit = "percent" if unit == "%" or unit.startswith("por") else _UNITS[unit]
        result.quantities.add((value, unit))
        result.typed_spans.append(match.span())
    for match in _QUINQUENNIUM_RE.finditer(text):
        # Singular significa cinco anios. No multiplicar 'dos quinquenios'.
        prefix = text[:match.start()]
        last = re.search(r"(\w+)\s+$", prefix)
        if (
            _FRACTION_PREFIX.search(prefix)
            or _QUINQUENNIUM_SUFFIX.match(text[match.end():])
            or (
                last and (last.group(1) in _WORD_VALUES or last.group(1).isdigit())
                and last.group(1) not in {"un", "uno", "una", "1"}
            )
        ):
            result.unsupported_compound = True
            continue
        result.quantities.add(("5", "year"))
    return result


def _table_percentages(text: str) -> set[tuple[str, str]]:
    """Lee solo la segunda columna de tablas explicitas de antiguedad/%.

    La primera columna admite intervalos o un limite seguido de 'en adelante'.
    No adivina encabezados separados de su fuente, tablas aplanadas en una linea,
    filas con columnas adicionales ni porcentajes en otro parrafo.
    """
    values: set[tuple[str, str]] = set()
    in_table = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "|" in line:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) != 2:
                in_table = False
                continue
            line = " ".join(cells)
            if in_table and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
        if _TENURE_HEADER.fullmatch(line):
            in_table = True
            continue
        if not in_table:
            continue
        row = _TABLE_ROW.fullmatch(line)
        if row is None:
            in_table = False
            continue
        try:
            percent = Decimal(row.group("percent").replace(",", "."))
            low = Decimal(row.group("low").replace(",", "."))
            high = Decimal((row.group("high") or row.group("low")).replace(",", "."))
        except InvalidOperation:
            in_table = False
            continue
        if not (0 <= percent <= 100 and 0 <= low <= high):
            in_table = False
            continue
        values.add((row.group("percent"), "percent"))
    return values


def numeric_claim_supported(claim: str, source_texts: tuple[str, ...]) -> bool:
    """Compara cifras y equivalencias tipadas solo con las fuentes citadas.

    Las cifras sin una unidad reconocida conservan la comprobacion literal.
    Nunca se transforma globalmente '70' en '70%'. Las fuentes se analizan por
    separado para impedir que un encabezado de un documento tipifique otro.
    """
    plain_claim = _plain(claim)
    claim_facts = _facts(plain_claim)
    if claim_facts.unsupported_compound:
        return False
    raw_supported: set[str] = set()
    quantities_supported: set[tuple[str, str]] = set()
    for source in source_texts:
        text = _plain(source)
        facts = _facts(text)
        raw_supported.update(facts.raw)
        quantities_supported.update(facts.quantities)
        quantities_supported.update(_table_percentages(text))
    if not claim_facts.quantities.issubset(quantities_supported):
        return False
    for match in _NUMBER_RE.finditer(plain_claim):
        if any(start <= match.start() and match.end() <= end for start, end in claim_facts.typed_spans):
            continue
        if match.group() not in raw_supported:
            return False
    return True
