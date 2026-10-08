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
from datetime import date
from decimal import Decimal, InvalidOperation
from fractions import Fraction

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
_MONTH_NUMBERS = {
    month: number for number, month in enumerate(
        "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(), 1,
    )
}
_CALENDAR_DATE_RE = re.compile(
    rf"(?<![\w.,])(?P<day>{_NUMBER}|primero)\s+de\s+"
    r"(?P<month>" + "|".join(_MONTH_NUMBERS) + r")\s+(?:de|del)\s+"
    r"(?P<year>\d{4})(?!\w|[.,]\d)"
)


def _is_percent(unit: str) -> bool:
    return unit == "%" or bool(re.fullmatch(r"por\s+ciento", unit))


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
    quantities: set[tuple[Decimal | str, str]] = field(default_factory=set)
    typed_spans: list[tuple[int, int]] = field(default_factory=list)
    unsupported_compound: bool = False
    invalid_calendar_date: bool = False


def _quantity_value(value: str) -> Decimal | str:
    # 1,000 / 1.000 puede ser agrupacion de miles o decimal segun el origen.
    # Sin locale explicito solo admitir la misma representacion literal; nunca
    # convertir mil pesos en uno. 0.500, .5 y 0,50 no tienen esa ambiguedad.
    if re.fullmatch(r"[+-]?[1-9]\d{0,2}[.,]\d{3}", value):
        return "literal:" + value
    return Decimal(value.replace(",", "."))


def _facts(text: str) -> _Facts:
    result = _Facts(raw={m.group() for m in _NUMBER_RE.finditer(text)})
    for match in _CALENDAR_DATE_RE.finditer(text):
        # "Primero de julio del 2041" y "1 de julio de 2041" son la misma
        # fecha completa. El dia no acredita importes, plazos ni otra fecha
        # construida combinando numeros de diferentes partes de la fuente.
        day = "1" if match["day"] == "primero" else match["day"]
        try:
            if not re.fullmatch(r"\d{1,2}", day):
                raise ValueError("noninteger calendar day")
            value = date(int(match["year"]), _MONTH_NUMBERS[match["month"]], int(day))
        except ValueError:
            result.invalid_calendar_date = True
            continue
        result.quantities.add((value.isoformat(), "calendar_date"))
        result.typed_spans.append(match.span())
    for match in _QUANTITY_RE.finditer(text):
        value, unit = match.group("value", "unit")
        if value in _WORD_VALUES:
            if _COMPOUND_PREFIX.search(text[:match.start()]):
                result.unsupported_compound = True
                continue
            value = _WORD_VALUES[value]
        unit = "percent" if _is_percent(unit) else _UNITS[unit]
        # Decimal conserva signo y valor exacto: 0,50%, .5% y 0.500% son
        # representaciones de la misma cantidad, sin convertir unidades.
        result.quantities.add((_quantity_value(value), unit))
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
        result.quantities.add((Decimal(5), "year"))
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


@dataclass(frozen=True, slots=True)
class IntervalRow:
    low: Fraction
    high: Fraction | None
    percent: Decimal
    text: str

    def contains(self, value: Fraction) -> bool:
        return self.low <= value and (self.high is None or value <= self.high)


@dataclass(frozen=True, slots=True)
class TenureTable:
    header: str
    rows: tuple[IntervalRow, ...]
    prefix: str
    suffix: str = ''

    def select(self, value: Fraction) -> IntervalRow | None:
        matches = [row for row in self.rows if row.contains(value)]
        return matches[0] if len(matches) == 1 else None


def tenure_tables(text: str) -> tuple[TenureTable, ...]:
    """Contrato acotado: dos columnas, intervalos explicitos, sin redondeos.

    Una fila mal formada invalida esa tabla completa, no deja un prefijo que
    pueda parecer una regla completa. Los huecos se conservan; los solapes se
    rechazan. No se mezclan encabezados y filas de fuentes distintas.
    """
    lines = _plain(text).splitlines()
    result = []
    for index, raw in enumerate(lines):
        header = " ".join(cell.strip() for cell in raw.strip().strip("|").split("|"))
        if not _TENURE_HEADER.fullmatch(header):
            continue
        rows = []
        invalid = False
        end = index + 1
        for position, original in enumerate(lines[index + 1:], index + 1):
            end = position
            line = original.strip()
            if not line:
                break
            if "|" in line:
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                if len(cells) != 2:
                    invalid = True
                    break
                if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                    continue
                line = " ".join(cells)
            match = _TABLE_ROW.fullmatch(line)
            if match is None:
                # Texto posterior no constituye otra fila. Una linea numerica
                # incompleta/tercera columna no se ignora silenciosamente.
                invalid = bool(re.match(r"[+\-\d.,]", line))
                break
            low = Fraction(match['low'].replace(',', '.'))
            high = (Fraction(match['high'].replace(',', '.')) if match['high'] else
                    None if 'en adelante' in line else low)
            percent = Decimal(match['percent'].replace(',', '.'))
            if low < 0 or (high is not None and high < low) or not 0 <= percent <= 100:
                invalid = True
                break
            if rows and (rows[-1].high is None or low <= rows[-1].high):
                invalid = True
                break
            rows.append(IntervalRow(low, high, percent, original.strip()))
        else:
            end = len(lines)
        if rows and not invalid:
            result.append(TenureTable(header, tuple(rows), '\n'.join(lines[:index]), '\n'.join(lines[end:])))
    return tuple(result)


def _row_claim_supported(claim: str, table: TenureTable) -> bool:
    """Una cifra de la columna % necesita su fila, no una pertenencia global."""
    spans = list(re.finditer(rf"({_NUMBER})\s*[-–—]\s*({_NUMBER})", claim))
    open_spans = list(re.finditer(rf"({_NUMBER})\s+en adelante", claim))
    percentages = [Decimal(m['value'].replace(',', '.')) for m in _QUANTITY_RE.finditer(claim)
                   if _is_percent(m['unit']) and m['value'] not in _WORD_VALUES]
    if not percentages or len(spans) + len(open_spans) != 1:
        return False
    if spans:
        low, high = (Fraction(value.replace(',', '.')) for value in spans[0].groups())
    else:
        low, high = Fraction(open_spans[0][1].replace(',', '.')), None
    return any(row.low == low and row.high == high and all(p == row.percent for p in percentages)
               for row in table.rows)


def numeric_claim_supported(claim: str, source_texts: tuple[str, ...]) -> bool:
    """Compara cifras y equivalencias tipadas solo con las fuentes citadas.

    Las cifras sin una unidad reconocida conservan la comprobacion literal.
    Nunca se transforma globalmente '70' en '70%'. Las fuentes se analizan por
    separado para impedir que un encabezado de un documento tipifique otro.
    """
    plain_claim = _plain(claim)
    claim_facts = _facts(plain_claim)
    if claim_facts.unsupported_compound or claim_facts.invalid_calendar_date:
        return False
    if any(unit == 'percent' for _, unit in claim_facts.quantities):
        for source in source_texts:
            text = _plain(source)
            headers = sum(bool(_TENURE_HEADER.fullmatch(
                ' '.join(cell.strip() for cell in line.strip().strip('|').split('|')),
            )) for line in text.splitlines())
            if headers != len(tenure_tables(text)):
                # Una tabla reconocible pero mal formada no se convierte en
                # prosa libre cuyos porcentajes se admiten por coincidencia.
                return False
    tables_in_sources = tuple(table for source in source_texts for table in tenure_tables(source))
    if (tables_in_sources and re.search(rf'{_NUMBER}(?:\s*[-–—]\s*{_NUMBER}|\s+en adelante)', plain_claim)
            and any(unit == 'percent' for _, unit in claim_facts.quantities)
            and not any(_row_claim_supported(plain_claim, table) for table in tables_in_sources)):
        return False
    raw_supported: set[str] = set()
    quantities_supported: set[tuple[Decimal | str, str]] = set()
    for source in source_texts:
        text = _plain(source)
        facts = _facts(text)
        tables = tenure_tables(text)
        if tables and re.search(rf'{_NUMBER}\s*[-–—]\s*{_NUMBER}', plain_claim) and any(
            unit == 'percent' for _, unit in claim_facts.quantities
        ) and not any(_row_claim_supported(plain_claim, table) for table in tables):
            continue
        raw_supported.update(facts.raw)
        quantities_supported.update(facts.quantities)
        # Solo se tipifica la fila expresamente descrita. La aplicacion al caso
        # se verifica por separado con entradas y reglas identificadas.
        for table in tables:
            if _row_claim_supported(plain_claim, table):
                quantities_supported.update(
                    (value, unit) for value, unit in claim_facts.quantities if unit == "percent"
                )
    if not claim_facts.quantities.issubset(quantities_supported):
        return False
    for match in _NUMBER_RE.finditer(plain_claim):
        if any(start <= match.start() and match.end() <= end for start, end in claim_facts.typed_spans):
            continue
        if match.group() not in raw_supported:
            return False
    return True
