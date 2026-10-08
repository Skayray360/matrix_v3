# Creado por Aldo Garcia.
"""Salida calculada para un contrato de pregunta completo y deliberadamente estrecho.

No recibe borradores ni memoria. El llamador decide si dos generaciones fallidas
justifican usarla. Toda salida vuelve a pasar por el verificador documental normal.
"""

from __future__ import annotations

import re
from decimal import Decimal, localcontext
from fractions import Fraction

from app.rag.claim_context import _DATE, _DURATION, _date_span, _metadata_claim, _subject, _subjects, claim_context
from app.rag.grounding import GroundingReport, verify_grounding
from app.rag.numeric_grounding import _NUMBER, _plain, tenure_tables
from app.rag.schemas import Evidence

_QUESTION = re.compile(
    r"segun\s+(?P<document>[^?\n]{1,250}?),\s*si\s+"
    r"ingrese (?:en|el)\s+(?P<entry>" + _DATE.pattern + r")\s+y\s+"
    r"me retiro con\s+(?P<tenure>" + _DURATION.pattern + r")\s+"
    r"de antiguedad antes de jubilarme,\s*¿?que porcentaje me corresponde de\s+"
    r"(?P<subjects>[^?\n]{1,300})\?\s*indica documento y pagina\.?"
)

_BEFORE_RULE = re.compile(
    r"(?:los empleados que ingresaron antes del?\s+|"
    r"si un empleado que ingreso(?: a [a-z]+)? antes del?\s+)" + _DATE.pattern
    + r"(?: tienen derecho| deja la compania antes de cumplir con los supuestos de jubilacion, tiene derecho)"
    + r" al?\s+(?P<percent>" + _NUMBER + r")\s*% de (?P<fixed>.+?) y para recibir "
    r"(?P<table>.+?),? es de acuerdo a la tabla de antiguedad"
)
_AFTER_RULE = re.compile(
    r"los empleados que ingresen a partir del?\s+" + _DATE.pattern
    + r" tendran derecho a (?P<table>.+?) de acuerdo a la siguiente tabla de antiguedad"
)


def _complete_portability_rules(evidence: Evidence, subjects: tuple[str, ...]) -> bool:
    """Contrato de fuente completo; no basta encontrar una clausula interna.

    Solo admite titulo corto y las dos cohortes conocidas, sin requisitos de
    poblacion, edad ni parrafos accesorios que el calculo no interpreta.
    """
    tables = tenure_tables(evidence.text)
    if len(tables) != 1 or tables[0].suffix.strip():
        return False
    raw_lines = evidence.text.strip().splitlines()
    if not raw_lines or not re.fullmatch(
        r"Portabilidad del (?:Plan|Programa)(?: [^\W\d_]+){0,2}", raw_lines[0].strip(),
    ):
        return False
    title_names = raw_lines[0].strip().split()[3:]
    if any(not word[0].isupper() for word in title_names):
        return False
    # Un modificador de alcance tampoco puede ocultarse como nombre propio.
    if re.search(
        r"\b(?:exclusiv[oa]s?|solo|unicamente|personal|empleados?|planta|temporales?|sindicalizad[oa]s?)\b",
        _plain(raw_lines[0]),
    ):
        return False
    prefix_lines = tables[0].prefix.strip().splitlines()
    if len(prefix_lines) < 2:
        return False
    rules = re.split(r"(?<=\.)\s+", " ".join(" ".join(prefix_lines[1:]).split()))
    if len(rules) != 2:
        return False
    before = _BEFORE_RULE.fullmatch(rules[0].removesuffix("."))
    after = _AFTER_RULE.fullmatch(rules[1].rstrip(".:"))
    if before is None or after is None:
        return False
    cutoff = _date_span(before)
    if cutoff is None or cutoff.first != cutoff.last or cutoff != _date_span(after):
        return False
    fixed_subject = _subject(before["fixed"])
    before_subjects = (fixed_subject, *_subjects(before["table"]))
    after_subjects = _subjects(after["table"])
    return (len(before_subjects) == len(set(before_subjects)) == len(subjects)
            and len(after_subjects) == len(set(after_subjects)) == len(subjects)
            and set(before_subjects) == set(after_subjects) == set(subjects))


def _requested_subjects(text: str, subjects: tuple[str, ...]) -> tuple[str, ...] | None:
    """Lista exacta; permite elipsis del sustantivo tomado de la propia regla."""
    parts = list(_subjects(text))
    if not parts or len(parts) != len(subjects) or len(set(subjects)) != len(subjects):
        return None
    heads = {subject.split()[0] for subject in subjects}
    shared = ""
    for head in heads:
        plural = head + ("s" if head[-1] in "aeiou" else "es")
        if parts[0].startswith(plural + " "):
            parts[0] = head + parts[0][len(plural):]
            shared = head
            break
        if parts[0].startswith(head + " "):
            shared = head
    resolved = []
    for part in parts:
        choices = {part, shared + " " + part} if shared else {part}
        matches = choices.intersection(subjects)
        if len(matches) != 1:
            return None
        resolved.append(matches.pop())
    return tuple(resolved) if set(resolved) == set(subjects) else None


def _display_subject(subject: str, source: str) -> str:
    """Conserva mayusculas y acentos de la etiqueta presente en la fuente."""
    words = list(re.finditer(r"\w+", source))
    target = subject.split()
    for start in range(len(words) - len(target) + 1):
        window = words[start:start + len(target)]
        if [_plain(word.group()) for word in window] == target:
            return " ".join(source[window[0].start():window[-1].end()].split())
    return subject.capitalize()


def _decimal(value: Fraction | Decimal) -> str:
    if isinstance(value, Fraction):
        with localcontext() as precision:
            precision.prec = max(28, len(str(abs(value.numerator))) + len(str(value.denominator)) + 10)
            value = Decimal(value.numerator) / Decimal(value.denominator)
    return format(value, "f")


def calculated_application_answer(
    question: str, evidences: tuple[Evidence, ...],
) -> tuple[str, GroundingReport] | None:
    """Devuelve solo una aplicacion completa solicitada, o ninguna alternativa.

    El fullmatch evita contestar parcialmente comparaciones, varias consultas,
    otros beneficios o peticiones de importes/condiciones adicionales.
    """
    request = _QUESTION.fullmatch(_plain(question).strip())
    if request is None:
        return None
    context = claim_context(question, evidences)
    if (context.case.entry is None or context.case.tenure is None or not context.case.before_retirement
            or not context.application_requested):
        return None

    by_id: dict[str, Evidence] = {}
    for evidence in evidences:
        previous = by_id.get(evidence.source_id)
        if previous is not None and (
            previous.text, previous.document_id, previous.filename, previous.page_or_sheet
        ) != (evidence.text, evidence.document_id, evidence.filename, evidence.page_or_sheet):
            return None
        by_id[evidence.source_id] = evidence
    requested_sources = []
    for evidence in evidences:
        identified, error = _metadata_claim(request["document"], (evidence,))
        if not error and re.fullmatch(r"(?:(?:el|la) )?documento identificado", identified.strip()):
            requested_sources.append(evidence)
    documents = {item.document_id or item.filename for item in requested_sources}
    if len(documents) != 1:
        return None
    requested_ids = {item.source_id for item in requested_sources}
    document_sources = tuple(item for item in evidences if (item.document_id or item.filename) in documents)
    document_source_ids = {item.source_id for item in document_sources}
    # Otra pagina puede contener un alcance o una excepcion no interpretados.
    # No se descarta ese fragmento para elegir solamente la tabla favorable.
    application_ids = {app.source_id for app in context.applications}
    if not document_source_ids.issubset(application_ids):
        return None
    if (document_source_ids.intersection(context.conflicting_sources)
            or any(source in document_source_ids for source, _ in context.limitations)):
        return None
    applications = {
        (app.tenure, app.low, app.high, app.percent, app.rule, by_id[app.source_id].page_or_sheet): app
        for app in context.applications if app.source_id in document_source_ids
    }
    if len(applications) != 1:
        return None
    application = next(app for app in context.applications if app.source_id in requested_ids)
    evidence = by_id[application.source_id]
    values = (*application.rule.fixed, *((subject, application.percent) for subject in application.rule.table_subjects))
    if not application.rule.table_subjects or not values:
        return None
    if not all(_complete_portability_rules(item, tuple(subject for subject, _ in values))
               for item in document_sources):
        return None
    subjects = _requested_subjects(request["subjects"], tuple(subject for subject, _ in values))
    page = re.fullmatch(r"pagina ([1-9]\d*)", _plain(evidence.page_or_sheet).strip())
    if subjects is None or page is None:
        return None

    citation = f"[[{evidence.source_id}]]"
    tenure = re.sub(r"\b(?:anos|anios)\b", "años", context.case.tenure_quote)
    interval = (
        f"{_decimal(application.low)} en adelante" if application.high is None
        else f"{_decimal(application.low)} – {_decimal(application.high)} años"
    )
    lines = [
        f"Datos declarados: ingreso en {request['entry']}; antigüedad de {tenure}.",
        "",
        "Si los datos declarados son correctos y el retiro ocurre antes de jubilarse, "
        "la aplicación condicional de la regla "
        f"y la fila {interval} da estos porcentajes:",
    ]
    for subject in subjects:
        lines.append(f"- {_display_subject(subject, evidence.text)}: {_decimal(dict(values)[subject])}%.")
    lines[-1] += " " + citation
    filename = re.sub(
        r"#U00([c-fC-F][0-9a-fA-F])", lambda match: chr(int("00" + match[1], 16)), evidence.filename,
    )
    lines.extend(("", f'Documento: "{filename}", página {page[1]}. {citation}'))
    answer = "\n".join(lines)
    report = verify_grounding(answer, evidences, mode="cited", require_citation=True, question=question)
    return (answer, report) if report.grounded else None
