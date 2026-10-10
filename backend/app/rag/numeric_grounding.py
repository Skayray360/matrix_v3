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
# Capturar la cifra completa antes de validar sus separadores evita omitir
# 1,000,000 y aceptar un importe no respaldado por una comparacion vacia.
_NUMERIC_LITERAL = r"[+-]?(?:\d+(?:[.,]+\d+)*|[.,]\d+)"
_NUMBER_RE = re.compile(rf"(?<![\w.,]){_NUMERIC_LITERAL}(?!\d|[.,]\d)")
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
    rf"(?<![\w.,])(?P<value>{_NUMERIC_LITERAL}|{_WORD_PATTERN})\s*"
    rf"(?P<unit>%|por\s+ciento\b|(?:{_UNIT_PATTERN})\b)"
)
# No extraer el ultimo cardinal de una cantidad compuesta como 'treinta y dos'.
_COMPOUND_PREFIX = re.compile(r"\b(?:y|cien|ciento|\w+cientos|mil|millon|millones)\s+$")
_QUINQUENNIUM_RE = re.compile(r"\bquinquenio\b")
_FRACTION_PREFIX = re.compile(
    r"\b(?:medio|media|mitad|tercio|cuarto|fraccion|parte|doble|triple|cuadruple)\b(?:\W+\w+){0,3}\W*$"
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
_ISO_DATE_RE = re.compile(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?!\w|-\d)")
# Estos valores son identificadores, no cantidades. Se cotejan completos:
# ADC01 no respalda ADC02; una version tampoco aporta cifras para un pago.
_PATH_LITERAL_RE = re.compile(
    r"(?<!\w)(?:[a-z][a-z0-9+.-]*://|[a-z]:[\\/]|\\\\|/)"
    r"[^\s`<>\[\]{}\"'|]+"
)
_ALPHANUMERIC_LITERAL_RE = re.compile(
    r"(?<![\w.-])(?=[\w.-]*[a-z])(?=[\w.-]*\d)"
    r"\w+(?:[.-]\w+)*(?!\w)"
)
_DOTTED_LITERAL_RE = re.compile(
    r"(?<![\w.,])(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){2,}(?!\w|\.\d)"
)
_ROW_RANGE_RE = re.compile(
    rf"(?<![\w.,])(?P<low>{_NUMBER})\s*(?:[-–—]|\b(?:a|y)\b)\s*"
    rf"(?P<high>{_NUMBER})(?:\s+(?:anos|anios)\b)?"
)
_OPEN_ROW_RANGE_RE = re.compile(
    rf"(?<![\w.,])(?P<low>{_NUMBER})\s+(?:(?:anos|anios)\s+)?en adelante\b"
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
    invalid_number: bool = False


def _quantity_value(value: str) -> Decimal | str:
    # 1,000 / 1.000 puede ser agrupacion de miles o decimal segun el origen.
    # Sin locale explicito solo admitir la misma representacion literal; nunca
    # convertir mil pesos en uno. 0.500, .5 y 0,50 no tienen esa ambiguedad.
    if re.fullmatch(r"[+-]?[1-9]\d{0,2}[.,]\d{3}", value):
        return "literal:" + value
    if re.fullmatch(_NUMBER, value):
        return Decimal(value.replace(",", "."))
    # Varios grupos completos o agrupacion mas separador decimal diferente
    # son inequívocos. Nunca eliminar puntuacion arbitraria.
    for group, decimal in ((",", "."), (".", ",")):
        grouped = rf"[+-]?[1-9]\d{{0,2}}(?:{re.escape(group)}\d{{3}})"
        if re.fullmatch(grouped + rf"{{2,}}(?:{re.escape(decimal)}\d+)?", value) or re.fullmatch(
            grouped + rf"+(?:{re.escape(decimal)}\d+)", value,
        ):
            return Decimal(value.replace(group, "").replace(decimal, "."))
    raise InvalidOperation("unsupported numeric representation")


def _document_literals(text: str) -> tuple[str, frozenset[str]]:
    """Aisla sintaxis tecnica sin convertir una cifra mal formada en importe.

    Solo se retiran literales completos y con digitos. Los separadores de
    miles reconocibles conservan su contrato de cantidad, y una version con
    unidad monetaria/temporal sigue siendo una cantidad invalida.
    """
    spans: list[tuple[int, int]] = []
    values: set[str] = set()
    numbers = tuple(_NUMBER_RE.finditer(text))
    for pattern in (_PATH_LITERAL_RE, _ALPHANUMERIC_LITERAL_RE, _DOTTED_LITERAL_RE):
        for match in pattern.finditer(text):
            value = match.group().rstrip(".,;:!?)")
            if not value or not re.search(r"\d", value):
                continue
            if pattern in (_PATH_LITERAL_RE, _ALPHANUMERIC_LITERAL_RE) and not re.search(r"[a-z]", value):
                continue
            if pattern is _ALPHANUMERIC_LITERAL_RE and any(
                number.start() < match.start() < number.end() for number in numbers
            ):
                # No rescatar el sufijo "000dias" de "1,,000dias".
                continue
            # "13dias" conserva la misma unidad que "13 dias". Un sufijo
            # de unidad no convierte cantidades invalidas en identificadores.
            if pattern is not _PATH_LITERAL_RE and _QUANTITY_RE.match(text, match.start()):
                continue
            if any(start < match.end() and match.start() < end for start, end in spans):
                if pattern is _ALPHANUMERIC_LITERAL_RE:
                    # Un nombre de archivo/host completo puede explicarse
                    # separado de la ruta que lo contiene; no recombinar rutas.
                    values.add(value)
                continue
            if pattern is _DOTTED_LITERAL_RE:
                try:
                    _quantity_value(value)
                except InvalidOperation:
                    pass
                else:
                    continue
            spans.append((match.start(), match.start() + len(value)))
            values.add(value)
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " * (end - start) + text[end:]
    return text, frozenset(values)


def _facts(text: str) -> _Facts:
    result = _Facts(raw={m.group() for m in _NUMBER_RE.finditer(text)})
    for token in result.raw:
        try:
            _quantity_value(token)
        except InvalidOperation:
            result.invalid_number = True
    for match in _ISO_DATE_RE.finditer(text):
        try:
            calendar_value = date.fromisoformat(match.group())
        except ValueError:
            result.invalid_calendar_date = True
            continue
        result.quantities.add((calendar_value.isoformat(), "calendar_date"))
        result.typed_spans.append(match.span())
    for match in _CALENDAR_DATE_RE.finditer(text):
        # "Primero de julio del 2041" y "1 de julio de 2041" son la misma
        # fecha completa. El dia no acredita importes, plazos ni otra fecha
        # construida combinando numeros de diferentes partes de la fuente.
        day = "1" if match["day"] == "primero" else match["day"]
        try:
            if not re.fullmatch(r"\d{1,2}", day):
                raise ValueError("noninteger calendar day")
            calendar_value = date(int(match["year"]), _MONTH_NUMBERS[match["month"]], int(day))
        except ValueError:
            result.invalid_calendar_date = True
            continue
        result.quantities.add((calendar_value.isoformat(), "calendar_date"))
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
        try:
            numeric_value = _quantity_value(value)
        except InvalidOperation:
            result.invalid_number = True
            continue
        result.quantities.add((numeric_value, unit))
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


def _verified_table_rows(claim: str, tables: tuple[TenureTable, ...]) -> tuple[str, bool]:
    """Liga cada porcentaje a su intervalo dentro de la misma clausula.

    Una cita puede respaldar varias filas. Se comprueba cada fila antes de
    retirar SOLO su intervalo y porcentaje del cotejo de cantidades; nunca
    se acredita globalmente un porcentaje por aparecer en otra fila.
    """
    if not tables:
        return claim, True
    parts = re.split(r"([;\n]|(?<=[.!?])\s+)", claim)
    for index, part in enumerate(parts):
        ranges = list(_ROW_RANGE_RE.finditer(part)) + list(_OPEN_ROW_RANGE_RE.finditer(part))
        percentages = [m for m in _QUANTITY_RE.finditer(part) if _is_percent(m["unit"])]
        if not ranges or not percentages:
            continue
        if len(ranges) != 1:
            return claim, False
        span = ranges[0]
        low = Fraction(span["low"].replace(",", "."))
        high = (Fraction(span["high"].replace(",", ".")) if "high" in span.groupdict() else None)
        try:
            values = {_quantity_value(_WORD_VALUES.get(m["value"], m["value"])) for m in percentages}
        except InvalidOperation:
            return claim, False
        if not any(row.low == low and row.high == high and values == {row.percent}
                   for table in tables for row in table.rows):
            return claim, False
        for start, end in sorted([span.span(), *(m.span() for m in percentages)], reverse=True):
            part = part[:start] + " " * (end - start) + part[end:]
        parts[index] = part
    return "".join(parts), True


def numeric_claim_supported(claim: str, source_texts: tuple[str, ...]) -> bool:
    """Compara cifras y equivalencias tipadas solo con las fuentes citadas.

    Las cifras sin una unidad reconocida conservan la comprobacion literal.
    Nunca se transforma globalmente '70' en '70%'. Las fuentes se analizan por
    separado para impedir que un encabezado de un documento tipifique otro.
    """
    plain_claim, literals = _document_literals(_plain(claim))
    source_parts = tuple(_document_literals(_plain(source)) for source in source_texts)
    if not literals.issubset({value for _, values in source_parts for value in values}):
        return False
    initial_facts = _facts(plain_claim)
    if initial_facts.unsupported_compound or initial_facts.invalid_calendar_date or initial_facts.invalid_number:
        return False
    if any(_is_percent(m["unit"]) for m in _QUANTITY_RE.finditer(plain_claim)):
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
    plain_claim, rows_valid = _verified_table_rows(plain_claim, tables_in_sources)
    if not rows_valid:
        return False
    claim_facts = _facts(plain_claim)
    if claim_facts.unsupported_compound or claim_facts.invalid_calendar_date or claim_facts.invalid_number:
        return False
    raw_supported: set[str] = set()
    quantities_supported: set[tuple[Decimal | str, str]] = set()
    for text, _ in source_parts:
        facts = _facts(text)
        raw_supported.update(facts.raw)
        quantities_supported.update(facts.quantities)
    if not claim_facts.quantities.issubset(quantities_supported):
        return False
    for match in _NUMBER_RE.finditer(plain_claim):
        if any(start <= match.start() and match.end() <= end for start, end in claim_facts.typed_spans):
            continue
        if match.group() not in raw_supported:
            return False
    return True
