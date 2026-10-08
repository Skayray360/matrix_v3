# Creado por Aldo Garcia.
"""Verificador de grounding y allowlist de fuentes.

Dos garantias que este modulo hace cumplir:

1. **Toda cita debe existir.** Si el modelo cita un ``source_id`` que no estaba
   entre la evidencia recuperada, la sintesis se rechaza. Un source ID inventado
   es indistinguible para el usuario de uno real, y es la forma mas peligrosa de
   alucinacion en un asistente de politicas de RH.
2. **La memoria no es evidencia.** El resumen de la conversacion entra al prompt
   como contexto, nunca como respaldo factual de una politica.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from app.common.answers import safe_nonfactual_text, split_answer_sections
from app.common.logging import get_logger
from app.rag.claim_context import (
    check_numeric_claim,
    claim_context,
    conditional_claim,
    personal_eligibility_claim,
    strip_declared_lines,
    topic_mismatch,
    unverified_current_claim,
)
from app.rag.schemas import Evidence

logger = get_logger(__name__)

#: Formato de cita esperado en la respuesta del modelo: [[categoria/archivo#n]]
CITATION_RE = re.compile(r"\[\[([^\]]{1,240})\]\]")

#: Frases con las que el asistente reconoce que no tiene respaldo. Si aparecen,
#: la ausencia de citas es correcta y no debe tratarse como fallo de grounding.
_INSUFFICIENCY_MARKERS = (
    "no cuento con informacion",
    "no cuento con información",
    "no tengo informacion",
    "no tengo información",
    "no hay evidencia",
    "evidencia insuficiente",
    "evidencia es insuficiente",
    "informacion insuficiente",
    "información insuficiente",
    "no dispongo de documentos",
    "no encontre informacion",
    "no encontré información",
    "no tiene acceso",
    "fuera del alcance",
)


@dataclass(frozen=True, slots=True)
class GroundingReport:
    """Contrato de citas y fidelidad extractiva, no una prueba de verdad.

    ``grounded`` acredita este contrato limitado. ``extractive_verified`` exige
    unidades recuperadas completas; ``factual_verified`` se reserva para una
    verificacion semantica independiente que este modulo NO implementa.
    """

    grounded: bool
    cited_source_ids: tuple[str, ...] = field(default_factory=tuple)
    invalid_source_ids: tuple[str, ...] = field(default_factory=tuple)
    declares_insufficiency: bool = False
    reason: str = ""
    citations_valid: bool = False
    factual_verified: bool = False
    extractive_verified: bool = False
    validation_detail: str = ""
    claim_index: int | None = None

    @property
    def has_invalid_citations(self) -> bool:
        return bool(self.invalid_source_ids)


def extract_citations(answer: str) -> tuple[str, ...]:
    """Extrae los source IDs citados en el texto."""
    return tuple(dict.fromkeys(m.group(1).strip() for m in CITATION_RE.finditer(answer)))


def declares_insufficiency(answer: str) -> bool:
    lowered = answer.lower()
    return any(marker in lowered for marker in _INSUFFICIENCY_MARKERS)


def _normalized_extract(value: str) -> str:
    """Tolera espacios y punto final, nunca signos numericos ni conectores.

    Se conserva la puntuacion interna, incluidas cifras decimales. No se usa
    una busqueda de substrings: la unidad es el Evidence completo, incluso si
    contiene varios parrafos o una excepcion en la ultima linea.
    """
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    # Un guion al inicio puede ser un signo, y un punto inicial puede iniciar
    # un decimal (.5). No tratarlos como bullets o puntuacion decorativa.
    return " ".join(value.split()).removesuffix(".").rstrip()


def _structured_extracts(result) -> tuple[str, ...]:
    """Filas completas con encabezados: una cifra aislada pierde su significado."""
    variants = [result.as_markdown_table()]
    header = "| " + " | ".join(result.columns) + " |"
    separator = "| " + " | ".join("---" for _ in result.columns) + " |"
    # Coincide con la ventana visible de StructuredEvidence.as_markdown_table.
    for row in result.rows[:25]:
        values = ["" if value is None else str(value) for value in row]
        variants.append("\n".join((header, separator, "| " + " | ".join(values) + " |")))
        variants.append(" | ".join(f"{column}: {value}" for column, value in zip(result.columns, values, strict=True)))
    return tuple(variants)


def verify_grounding(
    answer: str,
    evidences: tuple[Evidence, ...],
    *,
    require_citation: bool = True,
    structured: tuple = (),
    mode: Literal["extractive", "cited"] = "extractive",
    allow_general_knowledge: bool = False,
    question: str = "",
) -> GroundingReport:
    """Valida citas y unidades completas sin atribuirles verificacion semantica."""
    sources = {e.source_id: e.text for e in evidences}
    sources.update({e.source_id: e.as_markdown_table() for e in structured})
    supported_units = {e.source_id: (_normalized_extract(e.text),) for e in evidences}
    supported_units.update({
        e.source_id: tuple(_normalized_extract(text) for text in _structured_extracts(e)) for e in structured
    })
    # Un mismo identificador no puede acreditar dos documentos diferentes.
    raw_sources = [(e.source_id, e.text) for e in evidences]
    raw_sources.extend((e.source_id, e.as_markdown_table()) for e in structured)
    ambiguous = {sid for sid, text in raw_sources if sources[sid] != text}
    allowlist = set(sources)
    cited = extract_citations(answer)
    invalid = tuple(sid for sid in cited if sid not in allowlist)
    insufficiency = declares_insufficiency(answer)

    if invalid:
        logger.warning(
            "rag.grounding.invalid_citation",
            extra={"invalid_count": len(invalid), "allowlist_size": len(allowlist)},
        )
        return GroundingReport(
            grounded=False,
            cited_source_ids=cited,
            invalid_source_ids=invalid,
            declares_insufficiency=insufficiency,
            reason="citas fuera de la evidencia recuperada",
        )

    if set(cited).intersection(ambiguous):
        return GroundingReport(
            grounded=False, cited_source_ids=cited,
            reason="identificador de fuente ambiguo entre evidencias diferentes",
        )

    if mode == "cited":
        return _verify_cited_answer(
            answer, sources, cited=cited, require_citation=require_citation,
            allow_general_knowledge=allow_general_knowledge,
            evidences=evidences, question=question,
        )

    # Solo actos no factuales completos pueden prescindir de evidencia.
    if safe_nonfactual_text(answer):
        # Respuestas de abstencion completas y controladas.
        return GroundingReport(
            grounded=True,
            cited_source_ids=cited,
            declares_insufficiency=True,
            reason="declara insuficiencia de evidencia",
        )

    if require_citation and sources and not cited:
        return GroundingReport(
            grounded=False,
            cited_source_ids=(),
            declares_insufficiency=False,
            reason="respuesta documental sin ninguna cita",
        )

    if require_citation and not sources:
        # Sin evidencia, la unica respuesta admisible es declarar insuficiencia.
        return GroundingReport(
            grounded=False,
            cited_source_ids=cited,
            declares_insufficiency=False,
            reason="respuesta afirmativa sin evidencia disponible",
        )

    if require_citation:
        # Cifras solo contra las fuentes citadas, no contra todo el corpus.
        cited_text = " ".join(sources[sid] for sid in cited)
        content = CITATION_RE.sub("", answer)
        numbers = set(re.findall(r"(?<!\w)\d+(?:[.,]\d+)?%?", content))
        known_numbers = set(re.findall(r"(?<!\w)\d+(?:[.,]\d+)?%?", cited_text))
        if not numbers.issubset(known_numbers):
            return GroundingReport(
                grounded=False,
                cited_source_ids=cited,
                citations_valid=True,
                reason="afirmacion numerica sin respaldo",
            )

        cursor = 0
        previous_claim = ""
        for match in CITATION_RE.finditer(answer):
            fragment = answer[cursor : match.start()]
            if cursor:
                # El punto tras una cita cierra la unidad anterior. Solo se
                # retira cuando esta separado del nuevo texto por espacios:
                # nunca quitar el punto de un decimal como .5.
                fragment = re.sub(r"^\s*\.\s+", "", fragment, count=1)
            claim = _normalized_extract(fragment)
            if not claim:
                claim = previous_claim
            sid = match.group(1).strip()
            if not claim or claim not in supported_units[sid]:
                return GroundingReport(
                    grounded=False,
                    cited_source_ids=cited,
                    citations_valid=True,
                    reason="requiere una unidad de evidencia completa; no se acredita una frase parcial",
                )
            previous_claim = claim
            cursor = match.end()
        if _normalized_extract(answer[cursor:]):
            return GroundingReport(
                grounded=False, cited_source_ids=cited, citations_valid=True, reason="afirmacion final sin cita"
            )
        return GroundingReport(
            grounded=True,
            cited_source_ids=cited,
            reason="fidelidad extractiva de unidades completas; veracidad semantica no evaluada",
            citations_valid=True,
            extractive_verified=True,
        )
    return GroundingReport(grounded=True, cited_source_ids=cited, reason="sin contrato factual")


def _verify_cited_answer(
    answer: str, sources: dict[str, str], *, cited: tuple[str, ...],
    require_citation: bool, allow_general_knowledge: bool,
    evidences: tuple[Evidence, ...] = (), question: str = "",
) -> GroundingReport:
    """Permite sintesis con procedencia controlada; no simula una prueba de verdad.

    Las cifras de cada unidad se cotejan contra sus fuentes citadas. La seccion
    general queda fuera del contrato documental y nunca puede llevar citas.
    El significado de una parafrasis requiere evaluacion humana o semantica:
    citations_valid no equivale a factual_verified.
    """
    documented, general = split_answer_sections(answer)
    context = claim_context(question, evidences)
    documented, declared_lines = strip_declared_lines(documented, context)
    if general and context.application_requested and not safe_nonfactual_text(general):
        return GroundingReport(grounded=False, cited_source_ids=cited,
                               reason="aplicacion documental en orientacion general")
    if extract_citations(general):
        return GroundingReport(
            grounded=False, cited_source_ids=cited, reason="orientacion general atribuida a fuentes documentales",
        )
    if general and not allow_general_knowledge:
        return GroundingReport(grounded=False, cited_source_ids=cited, reason="capacidad general deshabilitada")
    if general and re.search(r"(?im)^\s*#{1,6}\s+informaci[oó]n documentada\s*$", general):
        return GroundingReport(grounded=False, cited_source_ids=cited, reason="secciones de procedencia mezcladas")
    if safe_nonfactual_text(documented) or (not documented and general):
        return GroundingReport(
            grounded=True, cited_source_ids=(), citations_valid=True,
            declares_insufficiency=bool(documented),
            reason=("sin afirmaciones documentales; orientacion general separada"
                    if general else "abstencion documental"),
        )
    if not require_citation:
        return GroundingReport(grounded=True, cited_source_ids=cited, reason="sin contrato documental")
    if not sources or not extract_citations(documented):
        return GroundingReport(
            grounded=False, cited_source_ids=cited, reason="respuesta documental sin fuentes citadas",
        )
    citation_groups = re.compile(r"(?:\[\[[^\]]{1,240}\]\][ \t]*)+")
    cursor = 0
    heading = ""
    conditional_sources: frozenset[str] = frozenset()
    for index, match in enumerate(citation_groups.finditer(documented), 1):
        claim = documented[cursor:match.start()].strip()
        claim = re.sub(r"^\.\s+", "", claim, count=1)
        if not claim:
            return GroundingReport(grounded=False, cited_source_ids=cited, reason="cita sin afirmacion documental")
        ids = extract_citations(match.group())
        # Un supuesto validado alcanza las siguientes unidades del mismo
        # bloque y las mismas fuentes. Una seccion nueva o cambio de fuente
        # cierra ese ambito; la memoria nunca introduce supuestos validados.
        new_section = bool(re.search(
            r"(?m)^\s*(?:#{1,6}\s+\S|(?:\*\*[^*\n]+\*\*|__[^_\n]+__)\s*:?\s*$)", claim,
        ))
        inherited_conditional = bool(conditional_sources) and frozenset(ids) == conditional_sources and not new_section
        current_evidence = tuple(e for e in evidences if e.source_id in ids)
        titles = re.findall(r"(?m)^\s*(?:[-*]\s+)?(?:\*\*([^*]+)\*\*|#{1,6}\s+([^\n]+))", claim)
        if titles:
            heading = next((a or b for a, b in reversed(titles)), "")
        if topic_mismatch(heading, current_evidence, evidences):
            return GroundingReport(grounded=False, cited_source_ids=cited, citations_valid=True,
                                   reason="beneficio y fuente citada no corresponden", claim_index=index)
        if unverified_current_claim(claim):
            return GroundingReport(grounded=False, cited_source_ids=cited, citations_valid=True,
                                   reason="vigencia actual no acreditada", claim_index=index)
        if personal_eligibility_claim(claim):
            return GroundingReport(grounded=False, cited_source_ids=cited, citations_valid=True,
                                   reason="elegibilidad personal no acreditada", claim_index=index)
        if declares_insufficiency(claim):
            return GroundingReport(grounded=False, cited_source_ids=cited, citations_valid=True,
                                   reason="abstencion mezclada con afirmaciones documentales", claim_index=index)
        detail = check_numeric_claim(
            claim, current_evidence, context, declared_lines=declared_lines,
            inherited_conditional=inherited_conditional,
            extra_sources=tuple(sources[sid] for sid in ids if sid not in {e.source_id for e in evidences}),
        )
        if detail:
            return GroundingReport(
                grounded=False, cited_source_ids=cited, citations_valid=True,
                reason="afirmacion numerica sin respaldo en sus fuentes citadas",
                validation_detail=detail, claim_index=index,
            )
        conditional_sources = (
            frozenset(ids) if inherited_conditional or conditional_claim(claim, declared_lines=declared_lines)
            else frozenset()
        )
        cursor = match.end()
    tail = documented[cursor:].strip().strip(". ")
    if tail and not safe_nonfactual_text(tail):
        return GroundingReport(
            grounded=False, cited_source_ids=cited, citations_valid=True, reason="afirmacion documental final sin cita",
        )
    return GroundingReport(
        grounded=True, cited_source_ids=cited, citations_valid=True,
        reason="citas autorizadas y cifras contrastadas; veracidad semantica no evaluada",
    )


def filter_answer_citations(answer: str, evidences: tuple[Evidence, ...]) -> str:
    """Elimina del texto las citas invalidas como ultima red de seguridad.

    Se usa solo cuando ya se decidio devolver una respuesta degradada: la ruta
    normal ante citas invalidas es regenerar o declarar insuficiencia.
    """
    allowlist = {e.source_id for e in evidences}

    def _replace(match: re.Match[str]) -> str:
        source_id = match.group(1).strip()
        return match.group(0) if source_id in allowlist else ""

    return CITATION_RE.sub(_replace, answer).strip()


def coverage_ratio(answer: str, evidences: tuple[Evidence, ...]) -> float:
    """Fraccion de la evidencia recuperada que la respuesta llega a citar.

    Una cobertura muy baja con evidencia abundante sugiere que la ruta rapida
    ignoro parte del material: es una de las senales que activan la ruta profunda.
    """
    if not evidences:
        return 0.0
    cited = set(extract_citations(answer))
    return len(cited & {e.source_id for e in evidences}) / len(evidences)
