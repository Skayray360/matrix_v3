# Creado por Aldo Garcia.
"""Recupera unidades documentales completas; nunca repara una afirmacion.

Este modulo recibe solo evidencia ya autorizada. No consulta el corpus ni
deduce reglas de una coincidencia de palabras. Una unidad recuperada pasa el
mismo contrato de citas y cifras por separado y el resultado se verifica otra
vez. Eso no sustituye una evaluacion semantica de la respuesta.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.common.answers import (
    DOCUMENTARY_LIMITATIONS,
    DOCUMENTED_HEADING,
    PARTIAL_ANSWER_NOTICE,
    split_answer_sections,
)
from app.rag.grounding import GroundingReport, extract_citations, verify_grounding
from app.rag.schemas import Evidence

_DEPENDENT_START = re.compile(
    r"^\s*(?:[-*]\s+|\d+[.)]\s+)?(?:adem[aá]s|asimismo|tambi[eé]n|por eso|por ello|"
    r"por lo tanto|en consecuencia|en ese caso|en este caso|ese|esa|esos|esas|esto|"
    r"estos|estas|lo anterior|dicho|dicha|dichos|dichas|el mismo|la misma)\b", re.IGNORECASE,
)
_DETACHED_CONDITION = re.compile(
    r"^\s*(?:[-*]\s+|\d+[.)]\s+)?(?:excepto|salvo|a menos que|siempre que|"
    r"[uú]nicamente si|solo si|s[oó]lo si|esta condici[oó]n|esa condici[oó]n)\b", re.IGNORECASE,
)
_CITATION_END = re.compile(r"(?:\[\[[^\]]{1,240}\]\][ \t]*)+\.?\s*$")
_DETACHED_HEADING = re.compile(r"^\s*(?:#{1,6}\s+\S.*|\*\*[^*\n]+\*\*\s*:?|__[^_\n]+__\s*:?)\s*$")
_UNCITED_SCOPE = re.compile(r"^\s*(?:si\b|para\b|cuando\b|en (?:el caso|caso)\b)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PartialDocumentaryAnswer:
    answer: str
    grounding: GroundingReport
    kept_claims: int
    discarded_claims: int


def recover_partial_documentary_answer(
    answer: str, evidences: tuple[Evidence, ...], *, question: str, structured: tuple = (),
) -> PartialDocumentaryAnswer | None:
    """Conserva solo parrafos autonomos completos de un borrador ya decodificado.

    No se rescata transporte roto, una cita ajena, un cambio de procedencia ni
    condiciones separadas del resultado. En esos casos el agente presenta una
    limitacion controlada y registra la causa. Nunca se oculta texto dentro de
    una afirmacion ni se atribuyen nuevas citas para hacerla pasar.
    """
    if not answer.strip() or not evidences and not structured:
        return None
    sources = {item.source_id for item in (*evidences, *structured)}
    if set(extract_citations(answer)) - sources:
        return None
    documented, general = split_answer_sections(answer)
    if general:
        return None
    paragraphs = tuple(part.strip() for part in re.split(r"\n\s*\n", documented) if part.strip())
    # Un encabezado separado puede aportar el concepto al parrafo siguiente;
    # una excepcion posterior puede limitar uno anterior. No separarlos.
    if any(_DETACHED_CONDITION.match(part) or _DETACHED_HEADING.match(part)
           or (not extract_citations(part) and _UNCITED_SCOPE.match(part)) for part in paragraphs):
        return None
    candidates = tuple(part for part in paragraphs if extract_citations(part))
    kept: list[str] = []
    for candidate in candidates:
        if _DEPENDENT_START.match(candidate) or not _CITATION_END.search(candidate):
            continue
        report = verify_grounding(
            candidate, evidences, structured=structured, question=question,
            mode="cited", require_citation=True, allow_general_knowledge=False,
        )
        if not report.grounded or not report.cited_source_ids or report.declares_insufficiency:
            continue
        # Conservar el texto exacto, incluidas condiciones, excepciones y citas.
        if candidate not in kept:
            kept.append(candidate)
    if not kept:
        return None
    content = DOCUMENTED_HEADING + "\n\n" + "\n\n".join((
        *kept, PARTIAL_ANSWER_NOTICE, DOCUMENTARY_LIMITATIONS["missing_information"],
    ))
    combined = verify_grounding(
        content, evidences, structured=structured, question=question,
        mode="cited", require_citation=True, allow_general_knowledge=False,
    )
    if not combined.grounded or combined.has_invalid_citations:
        return None
    return PartialDocumentaryAnswer(
        answer=content, grounding=combined, kept_claims=len(kept),
        discarded_claims=max(0, len(candidates) - len(kept)),
    )
