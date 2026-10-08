# Creado por Aldo Garcia.
"""Salida calculada para un contrato de pregunta completo y deliberadamente estrecho.

No recibe borradores ni memoria. El llamador decide si dos generaciones fallidas
justifican usarla. Toda salida vuelve a pasar por el verificador documental normal.
"""

from __future__ import annotations

import re
from decimal import Decimal, localcontext
from fractions import Fraction

from app.rag.claim_context import _DATE, _DURATION, _metadata_claim, _subjects, claim_context
from app.rag.grounding import GroundingReport, verify_grounding
from app.rag.numeric_grounding import _plain
from app.rag.schemas import Evidence

_QUESTION = re.compile(
    r"segun\s+(?P<document>[^?\n]{1,250}?),\s*si\s+"
    r"ingrese (?:en|el)\s+(?P<entry>" + _DATE.pattern + r")\s+y\s+"
    r"me retiro con\s+(?P<tenure>" + _DURATION.pattern + r")\s+"
    r"de antiguedad antes de jubilarme,\s*¿?que porcentaje me corresponde de\s+"
    r"(?P<subjects>[^?\n]{1,300})\?\s*indica documento y pagina\.?"
)


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
    if (requested_ids.intersection(context.conflicting_sources)
            or any(source in requested_ids for source, _ in context.limitations)):
        return None
    applications = {
        (app.tenure, app.low, app.high, app.percent, app.rule, by_id[app.source_id].page_or_sheet): app
        for app in context.applications if app.source_id in requested_ids
    }
    if len(applications) != 1:
        return None
    application = next(iter(applications.values()))
    evidence = by_id[application.source_id]
    values = (*application.rule.fixed, *((subject, application.percent) for subject in application.rule.table_subjects))
    if not application.rule.table_subjects or not values:
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
