# Creado por Aldo Garcia.
"""Procedencia y aplicaciones acotadas, sin modelos ni acceso a datos externos.

Soporta tablas de antiguedad/% de dos columnas y reglas de ingreso con fecha
explicita. Una aplicacion no certifica perfil, elegibilidad ni vigencia. Las
gramaticas desconocidas fallan cerradas; no se evalua codigo del documento.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction

from app.rag.numeric_grounding import (
    _NUMBER,
    _QUANTITY_RE,
    _WORD_PATTERN,
    _WORD_VALUES,
    _is_percent,
    _plain,
    numeric_claim_supported,
    tenure_tables,
)
from app.rag.schemas import Evidence

_MONTHS = "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split()
_DATE = re.compile(r"(?:(?P<day>\d{1,2}) de )?(?P<month>" + "|".join(_MONTHS) + r") de (?P<year>\d{4})\b")
_DURATION = re.compile(
    rf"(?<![\w.,])(?P<years>{_NUMBER}|{_WORD_PATTERN}) (?:anos|anios)\b"
    rf"(?:\s+y\s+(?P<months>{_NUMBER}|{_WORD_PATTERN}) meses\b)?"
)
_CONDITIONAL = re.compile(
    r"\b(?:si\b[^.!?\n]{0,140}\b(?:declara|declarados|correctos|supuestos)|"
    r"si\b[^.!?\n]{0,140}\b(?:ingresaste|ingrese|ingreso|antiguedad)|"
    r"bajo (?:esos|estos|los) supuestos|aplicacion condicional)\b"
)
_UNCERTAIN_RULE = re.compile(r"\b(?:excepto|salvo|siempre que|a condicion de|si ademas|unicamente si)\b")
_RANGE = re.compile(rf"({_NUMBER})\s*[-–—]\s*({_NUMBER})")


def _number(value: str) -> Fraction:
    return Fraction(_WORD_VALUES.get(value, value).replace(",", "."))


def _duration(match: re.Match) -> Fraction | None:
    years, months = _number(match["years"]), _number(match["months"] or "0")
    if years < 0 or months < 0 or months >= 12 or (months and years.denominator != 1):
        return None
    return years + months / 12


@dataclass(frozen=True, slots=True)
class DateSpan:
    first: date
    last: date


def _date_span(match: re.Match) -> DateSpan | None:
    try:
        year, month = int(match["year"]), _MONTHS.index(match["month"]) + 1
        if match["day"]:
            value = date(year, month, int(match["day"]))
            return DateSpan(value, value)
        return DateSpan(date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class DeclaredCase:
    tenure: Fraction | None = None
    tenure_quote: str = ""
    entry: DateSpan | None = None
    entry_quote: str = ""
    before_retirement: bool = False


def declared_case(question: str) -> DeclaredCase:
    """Solo campos identificados del caso; no recoge importes ni todos los numeros."""
    text = _plain(question)
    durations = (
        list(_DURATION.finditer(text))
        if re.search(
            r"\b(?:tengo|cuento con|me retiro|si |y con|mi antiguedad)",
            text,
        )
        else []
    )
    duration = durations[0] if len(durations) == 1 else None
    if duration and (
        not re.search(r"(?:tengo|cuento con|con|antiguedad(?: es|:)?(?: de)?)\s*$", text[: duration.start()])
        or re.search(r"\b(?:meses|anos|anios)\b", text[: duration.start()] + text[duration.end() :])
    ):
        duration = None
    entries = list(re.finditer(r"\b(?:ingrese (?:en|el)|mi fecha de ingreso es)\s+(" + _DATE.pattern + ")", text))
    entry = entries[0] if len(entries) == 1 else None
    return DeclaredCase(
        tenure=_duration(duration) if duration else None,
        tenure_quote=duration.group() if duration else "",
        entry=_date_span(entry) if entry else None,
        entry_quote=entry.group() if entry else "",
        before_retirement=bool(re.search(r"antes de (?:jubilarme|la jubilacion|cumplir[^.]*jubil)", text)),
    )


def _subject(value: str) -> str:
    value = re.sub(r"^(?:el|la|los|las|al)\s+", "", _plain(value).strip(" ,.:;"))
    return " ".join(value.split())


def _subjects(value: str) -> tuple[str, ...]:
    return tuple(_subject(part) for part in re.split(r",|\s+y\s+", value) if _subject(part))


@dataclass(frozen=True, slots=True)
class RuleBinding:
    quote: str
    table_subjects: tuple[str, ...]
    fixed: tuple[tuple[str, Decimal], ...] = ()


def _binding(prefix: str, case: DeclaredCase) -> tuple[RuleBinding | None, str]:
    """Mantiene fecha -> sujetos -> tabla o tasa fija. No reconoce empresas."""
    if _UNCERTAIN_RULE.search(prefix):
        return None, "condiciones adicionales no estructuradas"
    if re.search(r"antes de cumplir[^.]*jubil", prefix) and not case.before_retirement:
        return None, "falta supuesto de retiro anterior a jubilacion"
    conditions = list(
        re.finditer(
            r"\bingres\w*\b[^.\n]{0,100}?\b(?P<op>antes del?|a partir del?|desde el?)\s*" + _DATE.pattern,
            " ".join(prefix.split()),
        )
    )
    if not conditions:
        if re.search(r"\bingres\w*\b|\b(?:si|cuando|hasta|despues)\b", prefix):
            return None, "regla condicional no reconocida"
        return RuleBinding(prefix, ()), ""
    if case.entry is None:
        return None, "falta fecha de ingreso declarada inequívoca"
    flat = " ".join(prefix.split())
    selected = []
    for condition in conditions:
        cutoff = _date_span(condition)
        if cutoff is None or cutoff.first != cutoff.last:
            return None, "fecha de corte incompleta"
        # Una fecha declarada por mes representa todo ese mes, nunca el dia 1
        # supuesto. Un mes que cruza el corte no identifica una regla unica.
        before = condition["op"].startswith("antes")
        applies = case.entry.last < cutoff.first if before else case.entry.first >= cutoff.first
        if applies:
            sentence_start = flat.rfind(".", 0, condition.start()) + 1
            sentence_end = flat.find(".", condition.end())
            quote = flat[sentence_start : sentence_end if sentence_end >= 0 else len(flat)].strip()
            selected.append(quote)
    if len(selected) != 1:
        return None, "fecha de ingreso no selecciona una regla unica"
    quote = selected[0]
    if re.search(r"antes de cumplir[^.]*jubil", quote) and not case.before_retirement:
        return None, "falta supuesto de retiro anterior a jubilacion"
    # Regla normal: derecho a los conceptos enumerados segun la tabla.
    target = re.search(
        r"(?:derecho al? |para recibir )(.+?)(?:,? es )?de acuerdo (?:a|con) (?:la |a la )?(?:siguiente )?tabla", quote
    )
    if target is None:
        return None, "no se identifica la relacion conceptos y tabla"
    subjects = _subjects(target[1])
    fixed = ()
    if "%" in target[1]:
        # Regla mixta: tasa fija para un concepto, tabla para los restantes.
        target = re.search(
            r"para recibir (.+?)(?:,? es )?de acuerdo (?:a|con) (?:la |a la )?(?:siguiente )?tabla", quote
        )
        fixed_match = re.search(r"derecho al?\s+(" + _NUMBER + r")% de (.+?) y para recibir", quote)
        if target is None or fixed_match is None:
            return None, "regla mixta no reconocida"
        subjects = _subjects(target[1])
        fixed = ((_subject(fixed_match[2]), Decimal(fixed_match[1].replace(",", "."))),)
    if not subjects or any(len(subject) > 100 or "%" in subject for subject in subjects):
        return None, "conceptos de la tabla ambiguos"
    return RuleBinding(quote, subjects, fixed), ""


@dataclass(frozen=True, slots=True)
class TableApplication:
    source_id: str
    tenure: Fraction
    row_text: str
    low: Fraction
    high: Fraction | None
    percent: Decimal
    rule: RuleBinding


@dataclass(frozen=True, slots=True)
class ClaimContext:
    case: DeclaredCase
    applications: tuple[TableApplication, ...]
    limitations: tuple[tuple[str, str], ...]
    application_requested: bool = False
    conflicting_sources: frozenset[str] = frozenset()


def requests_application(question: str) -> bool:
    text = _plain(question)
    return bool(
        re.search(r"\b(?:si|mi|me|ingrese|tengo|con)\b", text)
        and re.search(r"\b(?:anos?|anios?|mes(?:es)?|antiguedad|fecha|porcentaje)\b", text)
    )


def claim_context(question: str, evidences: tuple[Evidence, ...]) -> ClaimContext:
    case = declared_case(question)
    applications, limitations = [], []
    requested = requests_application(question)
    if case.tenure is None:
        return ClaimContext(case, (), (), requested)
    for evidence in evidences:
        tables = tenure_tables(evidence.text)
        if not tables:
            continue
        if len(tables) != 1:
            limitations.append((evidence.source_id, "varias tablas sin seleccion inequívoca"))
            continue
        table = tables[0]
        if table.suffix.strip():
            limitations.append((evidence.source_id, "notas posteriores a la tabla no estructuradas"))
            continue
        row = table.select(case.tenure)
        binding, reason = _binding(table.prefix, case)
        if row is None or binding is None:
            limitations.append((evidence.source_id, reason or "antiguedad fuera de intervalos explicitos"))
            continue
        applications.append(
            TableApplication(evidence.source_id, case.tenure, row.text, row.low, row.high, row.percent, binding)
        )
    # Dos fragmentos del MISMO documento no pueden dar aplicaciones distintas
    # para los mismos conceptos y supuestos. Citar solo uno no resuelve el
    # conflicto. No comparar automaticamente beneficios/documentos diferentes.
    documents = {e.source_id: e.document_id for e in evidences}
    grouped: dict[tuple, list[TableApplication]] = {}
    for application in applications:
        subjects = tuple(sorted((*application.rule.table_subjects, *(s for s, _ in application.rule.fixed))))
        document = documents[application.source_id]
        if document:
            grouped.setdefault((document, subjects), []).append(application)
    conflicts = set()
    for group in grouped.values():
        mappings = {
            tuple(sorted((*a.rule.fixed, *((s, a.percent) for s in a.rule.table_subjects))))
            for a in group
        }
        if len(mappings) > 1:
            conflicts.update(a.source_id for a in group)
    return ClaimContext(case, tuple(applications), tuple(limitations), requested, frozenset(conflicts))


def application_hints(question: str, evidences: tuple[Evidence, ...], aliases: dict[str, str]) -> str:
    """Datos calculados por software, ligados a fuentes presentes en ESTA llamada."""
    context = claim_context(question, evidences)
    if not context.application_requested:
        return ""
    labels = {source: alias for alias, source in aliases.items()}
    lines = [
        "<<<APLICACION_CONDICIONAL>>>",
        "Supuestos del usuario; no acreditan elegibilidad ni vigencia: " + context.case.tenure_quote,
        context.case.entry_quote,
    ]
    for item in context.applications:
        if item.source_id in context.conflicting_sources:
            continue
        targets = ", ".join(item.rule.table_subjects) or "columna de la tabla"
        lines.append(
            f"Fuente [[{labels.get(item.source_id, item.source_id)}]]; antiguedad exacta "
            f"{item.tenure.numerator}/{item.tenure.denominator} anios; fila {item.row_text}; "
            f"conceptos de esa fila: {targets}. Regla: {item.rule.quote}"
        )
        for subject, value in item.rule.fixed:
            lines.append(f"La misma regla distingue {subject}: {value}% (no usar la fila para ese concepto).")
    if context.conflicting_sources:
        lines.append(
            "Hay evidencia contradictoria en un mismo documento para los conceptos y supuestos. "
            "Explica el conflicto sin elegir un porcentaje; citar solo un fragmento no lo resuelve."
        )
    if not context.applications:
        lines.append(
            "No se pudo comprobar una aplicacion: falta una tabla/regla compatible completa. "
            "No afirmes un porcentaje para el caso; explica la limitacion o pide precision."
        )
    lines.append(
        "No traslades estos supuestos o su aplicacion a Orientacion general. "
        "Toda aplicacion debe ser condicional y citar la regla. No copies estas etiquetas internas."
    )
    lines.append("<<</APLICACION_CONDICIONAL>>>")
    return "\n".join(line for line in lines if line)


def _metadata_claim(claim: str, evidences: tuple[Evidence, ...]) -> tuple[str, str]:
    # Los numeros de un nombre o pagina pertenecen a metadata, no a la tabla.
    # Solo reconocer nombres exactos atribuidos expresamente a una fuente.
    identified_sources: set[str] = set()
    for evidence in evidences:
        filename = _plain(evidence.filename)
        names = {filename, filename.rsplit(".", 1)[0]}
        for name in sorted(names, key=len, reverse=True):
            if not name:
                continue
            title = re.compile(
                r"\b(?:documento|archivo|fuente|presentacion|platica)"
                r"(?:\s*:\s*|\s+(?:(?:titulado|llamado|denominado)\s+)?)"
                r"[\"'“]?" + re.escape(name) + r"(?!\w)[\"'”]?"
            )
            if title.search(claim):
                identified_sources.add(evidence.source_id)
                claim = title.sub("documento identificado", claim)
    # Solo una atribucion de fecha a documento, nunca a la vigencia de la regla.
    pattern = re.compile(
        r"\b(?:documento|archivo|platica|presentacion|fuente) "
        r"(?:historico |historica )?(?:de |del |fechado en )"
        r"(?:(?P<month>" + "|".join(_MONTHS) + r") de )?(?P<year>\d{4})\b"
    )

    def replace(match):
        valid = [
            e
            for e in evidences
            if re.search(r"(?<!\d)" + match["year"] + r"(?!\d)", e.filename)
            and (not match["month"] or match["month"] in _plain(e.filename))
        ]
        identified_sources.update(e.source_id for e in valid)
        return "documento con fecha identificada" if valid else match.group()

    claim = pattern.sub(replace, claim)
    page_pattern = re.compile(r"\b(?:pagina|pag\.?|p\.)\s+(\d+)\b(?![.,]\d)")
    located_sources = tuple(e for e in evidences if e.source_id in identified_sources) if identified_sources else evidences
    if (identified_sources and page_pattern.search(claim)
            and len({e.document_id or e.filename for e in located_sources}) > 1):
        return claim, "atribucion documento y pagina ambigua; cite cada localizador por separado"
    pages = {
        int(match[1]) for e in located_sources
        if (match := page_pattern.fullmatch(_plain(e.page_or_sheet).strip()))
    }
    if any(int(match[1]) not in pages for match in page_pattern.finditer(claim)):
        return claim, "pagina no corresponde a la metadata de sus fuentes citadas"
    return page_pattern.sub("pagina comprobada", claim), ""


def _mentioned_subjects(text: str, values: list[tuple[str, Decimal]]) -> list[tuple[str, Decimal]]:
    """Coincidencias completas y largas primero; 'base' no es 'base complementaria'."""
    subjects = sorted({subject for subject, _ in values}, key=len, reverse=True)
    if not subjects:
        return []
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(s) for s in subjects) + r")(?!\w)")
    mentioned = {match.group() for match in pattern.finditer(text)}
    return [(subject, value) for subject, value in values if subject in mentioned]


def strip_declared_lines(text: str, context: ClaimContext) -> tuple[str, bool]:
    """Admite campos declarados sin cita solo en lineas acotadas y verificadas."""
    conditional = False
    lines = []
    for line in text.splitlines():
        plain = _plain(line).strip()
        if not plain.startswith("datos declarados:"):
            lines.append(line)
            continue
        rest = plain.removeprefix("datos declarados:").strip()
        for match in reversed(list(_DURATION.finditer(rest))):
            if context.case.tenure is not None and _duration(match) == context.case.tenure:
                rest = rest[: match.start()] + rest[match.end() :]
        for match in reversed(list(_DATE.finditer(rest))):
            if context.case.entry is not None and _date_span(match) == context.case.entry:
                rest = rest[: match.start()] + rest[match.end() :]
        rest = re.sub(r"\b(?:ingreso|antiguedad|en|de|y)\b|[;,:. ]", "", rest)
        if rest:
            lines.append(line)
        else:
            conditional = True
    return "\n".join(lines), conditional


def check_numeric_claim(
    claim: str,
    evidences: tuple[Evidence, ...],
    context: ClaimContext,
    *,
    declared_lines: bool = False,
    extra_sources: tuple[str, ...] = (),
) -> str:
    """Devuelve causa concreta; cadena vacia acredita solo este contrato acotado."""
    text, metadata_error = _metadata_claim(_plain(claim), evidences)
    if metadata_error:
        return metadata_error
    conditional = bool(_CONDITIONAL.search(text))
    # Una linea de datos previa no basta para afirmar elegibilidad: la
    # aplicacion sigue requiriendo 'si...' o su encabezado condicional.
    conditional = conditional or (declared_lines and "condicional" in text)
    sources = tuple(e.text for e in evidences) + extra_sources
    percentages = [m for m in _QUANTITY_RE.finditer(text) if _is_percent(m["unit"])]
    applies_to_case = context.application_requested and bool(percentages)
    if applies_to_case:
        if not conditional and not _RANGE.search(text):
            return "datos declarados sin aplicacion condicional"
        applications = [app for app in context.applications if app.source_id in {e.source_id for e in evidences}]
        if any(app.source_id in context.conflicting_sources for app in applications):
            return "evidencia contradictoria para los mismos conceptos y supuestos"
        # El solape de recuperacion puede repetir una misma regla/tabla. No es
        # ambiguedad si la aplicacion completa es identica.
        applications = list({
            (app.tenure, app.low, app.high, app.percent, app.rule): app for app in applications
        }.values())
        if len(applications) != 1:
            return "tabla o regla incompleta, ambigua o fuera de intervalo"
        application = applications[0]
        for span in _RANGE.finditer(text):
            if (Fraction(span[1].replace(",", ".")), Fraction(span[2].replace(",", "."))) != (
                application.low,
                application.high,
            ):
                return "intervalo no corresponde a la antiguedad declarada"
        for span in re.finditer(rf"({_NUMBER})\s+en adelante", text):
            if application.high is not None or Fraction(span[1].replace(",", ".")) != application.low:
                return "intervalo no corresponde a la antiguedad declarada"
        values = list(application.rule.fixed) + [(s, application.percent) for s in application.rule.table_subjects]
        for clause in re.split(r"[;\n]|(?<=[.!?])\s+", text):
            clause_percentages = [m for m in _QUANTITY_RE.finditer(clause) if _is_percent(m["unit"])]
            if not clause_percentages:
                continue
            mentioned = _mentioned_subjects(clause, values)
            selected_values = mentioned or (
                [(s, application.percent) for s in application.rule.table_subjects] if _RANGE.search(clause) else values
            )
            expected = {value for _, value in selected_values} or {application.percent}
            actual = {Decimal(_WORD_VALUES.get(m["value"], m["value"]).replace(",", ".")) for m in clause_percentages}
            if len(expected) != 1 or actual != expected:
                return "porcentaje no corresponde a la fila y conceptos aplicables"
        # Solo los porcentajes ya ligados a esa fila/regla se retiran del cotejo
        # literal. Otros importes/numeros de la misma frase siguen verificandose.
        text = _QUANTITY_RE.sub(
            lambda m: "porcentaje comprobado" if _is_percent(m["unit"]) else m.group(), text
        )
    if conditional:
        # Conversion comprobable anos + meses/12, no calculo libre ni eval().
        formula = re.compile(
            rf"(?P<y>{_NUMBER})\s*\+\s*(?P<m>{_NUMBER})\s*/\s*12\s*=\s*(?P<result>{_NUMBER})\s*(?:anos|anios)"
        )
        original = _DURATION.fullmatch(context.case.tenure_quote)

        def valid_conversion(match):
            return (
                original
                and _number(match["y"]) == _number(original["years"])
                and _number(match["m"]) == _number(original["months"] or "0")
                and _number(match["result"]) == context.case.tenure
            )

        if any(not valid_conversion(match) for match in formula.finditer(text)):
            return "conversion de antiguedad no corresponde a las entradas declaradas"
        text = formula.sub("conversion exacta de antiguedad declarada", text)
        if any(
            context.case.tenure is None or _duration(match) != context.case.tenure for match in _DURATION.finditer(text)
        ):
            return "antiguedad atribuida al caso distinta de la declarada"

        def duration(match):
            return (
                "antiguedad declarada"
                if context.case.tenure is not None and _duration(match) == context.case.tenure
                else match.group()
            )

        text = _DURATION.sub(duration, text)
        # Fecha declarada solo junto al campo ingreso; no puede acreditar una
        # fecha de vigencia, un plazo o una prestacion en la misma afirmacion.
        entry = re.compile(r"\b(?:ingreso (?:declarado )?|ingrese |ingresaste )(?:en |el )?" + _DATE.pattern)
        if any(not context.case.entry or _date_span(match) != context.case.entry for match in entry.finditer(text)):
            return "fecha de ingreso atribuida al caso distinta de la declarada"
        text = entry.sub("ingreso declarado", text)
    if not numeric_claim_supported(text, sources):
        return "afirmacion numerica sin respaldo en sus fuentes citadas"
    return ""


def evidence_topic(evidence: Evidence) -> str:
    """Etiqueta acotada: Tema explicito, Markdown o titulo corto en primera linea."""
    topic = re.search(r"(?im)^\s*tema:\s*([^\n]+)", evidence.text)
    first = topic[1] if topic else evidence.text.strip().splitlines()[0] if evidence.text.strip() else ""
    first = _subject(first.strip("# *?¿:"))
    first = re.sub(r"^(?:que es|cuales son) (?:el |la |los |las )?", "", first)
    if len(first) > 110 or re.search(r"\d|[.!;]", first):
        return ""
    return first


def topic_mismatch(heading: str, cited: tuple[Evidence, ...], all_evidence: tuple[Evidence, ...]) -> bool:
    def words(value):
        return set(re.findall(r"\w+", value)) - {"el", "la", "los", "las", "de", "del", "por", "y", "que", "es"}

    named = words(_plain(heading))
    matched = {
        evidence_topic(e)
        for e in all_evidence
        if len(words(evidence_topic(e))) >= 2 and words(evidence_topic(e)).issubset(named)
    }
    return bool(matched) and any(evidence_topic(e) not in matched for e in cited)


def unverified_current_claim(claim: str) -> bool:
    """No convertir metadata/fecha historica en prueba de vigencia actual."""
    for sentence in re.split(r"[.!?\n]|\b(?:pero|sin embargo)\b", _plain(claim)):
        if not re.search(r"\b(?:actual(?:mente)?|vigentes?|hoy)\b", sentence):
            continue
        if re.fullmatch(
            r"\s*(?:no puedo confirmar la vigencia actual de esta informacion|"
            r"la (?:fecha|publicacion) no (?:acredita|demuestra|confirma) (?:la )?vigencia actual)\s*",
            sentence,
        ):
            continue
        return True
    return False


def personal_eligibility_claim(claim: str) -> bool:
    """Una operacion sobre supuestos no certifica perfil o derecho personal."""
    return bool(
        re.search(
            r"\b(?:tienes derecho|usted tiene derecho|te corresponde|le corresponde a usted|"
            r"recibiras|recibira usted|eres beneficiario|aplica (?:a ti|a usted))\b",
            _plain(claim),
        )
    )
