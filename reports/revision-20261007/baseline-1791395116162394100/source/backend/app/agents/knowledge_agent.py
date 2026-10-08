# Creado por Aldo Garcia.
"""Sintesis local con grounding, fallback y resumen jerarquico.

El agente solo recibe evidencia ya autorizada. No conoce el vector store ni el
motor de permisos, por lo que ninguna salida de un modelo puede ampliar el
alcance. Las respuestas documentales se verifican contra una allowlist literal
de ``source_id`` y admiten una sola regeneracion.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.agents.prompts import (
    build_answer_messages,
    build_clarification_messages,
    build_general_messages,
    build_summary_reduce_messages,
)
from app.common import answer_diagnostics
from app.common.answers import GENERAL_HEADING, AnswerBasis, safe_nonfactual_text
from app.common.answers import answer_basis as classify_answer_basis
from app.common.errors import AnswerValidationError, OllamaUnavailableError
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.common.logging import get_logger
from app.common.timing import timed_stage
from app.config import get_settings
from app.llm.model_policy import Intent, ModelChoice, ModelPolicy
from app.llm.ollama_client import ChatResult
from app.llm.provider import InferenceClient
from app.memory.service import ConversationContext
from app.rag.citation_aliases import citation_aliases, expand_citation_aliases
from app.rag.grounding import (
    GroundingReport,
    coverage_ratio,
    extract_citations,
    verify_grounding,
)
from app.rag.schemas import Evidence
from app.structured_data.tool import StructuredEvidence

logger = get_logger(__name__)

MIN_COVERAGE_WITH_RICH_EVIDENCE = 0.34
RICH_EVIDENCE_THRESHOLD = 3
_PROMPT_OVERHEAD_TOKENS = 1400
_CHARS_PER_TOKEN_BUDGET = 3
# Margen estimado del mensaje system adicional; no sustituye al tokenizer real.
_ADDITIONAL_SYSTEM_OVERHEAD_TOKENS = 32


def _safe_failure_kind(error: Exception) -> str:
    if isinstance(error, InferenceFailureError) and isinstance(error.failure_kind, InferenceFailureKind):
        return error.failure_kind.value
    return "unavailable"


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """Respuesta sintetizada y metadatos de verificacion."""

    answer: str
    model: str
    latency_ms: int
    grounding: GroundingReport
    regenerated: bool = False
    escalated_to_deep: bool = False
    cited_source_ids: tuple[str, ...] = ()
    hierarchical: bool = False
    map_batches: int = 0
    answer_basis: AnswerBasis = "documented"


@dataclass(frozen=True, slots=True)
class _FirstPass:
    """Estado de la primera pasada de sintesis, para decidir la regeneracion."""

    result: ChatResult
    report: GroundingReport
    coverage: float
    availability_fallback: bool
    insufficient_coverage: bool
    false_summary_insufficiency: bool
    choice: ModelChoice


class KnowledgeAgent:
    """Sintetiza evidencia autorizada mediante los perfiles del modelo local."""

    def __init__(self, *, llm: InferenceClient, policy: ModelPolicy) -> None:
        self._llm = llm
        self._policy = policy

    @timed_stage("generation")
    def answer_general(
        self,
        *,
        question: str,
        memory: ConversationContext | None,
        model_name: str,
        choice: ModelChoice | None = None,
        intent: Intent = Intent.GENERAL,
    ) -> SynthesisResult:
        """Responde conocimiento general sin presentarlo como politica interna."""
        choice = self._policy.resolve_choice(model_name, choice)
        messages = build_general_messages(question=question, memory=memory, capabilities=intent is Intent.CAPABILITIES)
        try:
            result, fallback_used = self._chat_with_fallback(
                model_name=model_name, fallback_model_name=self._policy.fallback_for(model_name),
                messages=messages, intent=Intent.GENERAL, choice=choice,
            )
        except OllamaUnavailableError as exc:
            if (choice is not ModelChoice.DEEP or self._policy.fast_model == model_name
                    or not self._can_fallback_to_fast(exc)):
                raise
            if self._messages_chars(messages) > self._input_budget_chars(self._policy.fast_model, Intent.GENERAL):
                messages = build_general_messages(
                    question=question, memory=None, capabilities=intent is Intent.CAPABILITIES,
                )
            result, _ = self._chat_with_fallback(
                model_name=self._policy.fast_model, fallback_model_name=self._policy.fast_model,
                messages=messages, intent=Intent.GENERAL, choice=ModelChoice.FAST, allow_fallback=False,
            )
            fallback_used = True
        answer = result.content.strip()
        if not answer:
            raise InferenceFailureError(InferenceFailureKind.EMPTY)
        if extract_citations(answer):
            raise AnswerValidationError(detail="citas inventadas en una consulta general")
        if GENERAL_HEADING not in answer:
            answer = f"{GENERAL_HEADING}\n\n{answer}"
        return SynthesisResult(
            answer=answer,
            model=result.model,
            latency_ms=result.latency_ms,
            grounding=GroundingReport(
                grounded=False, reason="conocimiento general del modelo, sin respaldo documental",
            ),
            escalated_to_deep=fallback_used and self._policy.resolve_choice(result.model) is ModelChoice.DEEP,
            answer_basis="general",
        )

    @timed_stage("generation")
    def ask_clarification(self, *, question: str, model_name: str) -> SynthesisResult:
        """Un unico intento para precisar, sin fuentes ni historial como hechos."""
        result, _ = self._chat_with_fallback(
            model_name=model_name, fallback_model_name=model_name,
            messages=build_clarification_messages(question=question), intent=Intent.CONVERSATIONAL,
            allow_fallback=False,
        )
        answer = result.content.strip()
        if not answer.endswith('?') or not safe_nonfactual_text(answer):
            raise AnswerValidationError(detail="solicitud de precision contiene afirmaciones no acreditadas")
        return SynthesisResult(
            answer=answer, model=result.model, latency_ms=result.latency_ms,
            grounding=GroundingReport(grounded=True, declares_insufficiency=True,
                                      reason="sin evidencia suficiente; precision generada"),
            answer_basis="insufficient",
        )

    @staticmethod
    @timed_stage("validation")
    def _verify_answer(answer: str, evidences: tuple[Evidence, ...], structured: tuple,
                       *, question: str = "") -> GroundingReport:
        settings = get_settings()
        return verify_grounding(
            answer, evidences, structured=structured, require_citation=True,
            mode=settings.answer_evidence_mode,
            allow_general_knowledge=False,
            question=question,
        )

    @staticmethod
    def _can_fallback_to_fast(error: OllamaUnavailableError) -> bool:
        kind = str(getattr(error, "failure_kind", ""))
        if kind in {"timeout", "transport", "incomplete", "empty", "context_limit"}:
            return True
        if kind in {"http", "busy"}:
            return getattr(error, "http_status", None) in {404, 500, 502, 503, 504}
        return (error.detail or "") in {"ReadTimeout", "ConnectTimeout", "timeout", "incomplete", "empty"}

    @timed_stage("generation")
    def synthesize(
        self,
        *,
        question: str,
        evidences: tuple[Evidence, ...],
        structured: tuple[StructuredEvidence, ...] = (),
        memory: ConversationContext | None = None,
        model_name: str,
        choice: ModelChoice | None = None,
        deep_model_name: str | None = None,
        scope_note: str = "",
        intent: Intent = Intent.DOCUMENTAL,
        evidence_truncated: bool = False,
        _allow_hierarchy: bool = True,
        _allow_model_fallback: bool = True,
    ) -> SynthesisResult:
        """Genera, verifica y como maximo una vez regenera la respuesta."""
        choice = self._policy.resolve_choice(model_name, choice)
        summary_mode = intent is Intent.DOCUMENT_SUMMARY
        if not evidences and not structured:
            return self.ask_clarification(question=question, model_name=model_name)

        if summary_mode and _allow_hierarchy and self._summary_exceeds_context(
            evidences, model_name=model_name, question=question, memory=memory,
            structured=structured, scope_note=scope_note,
            choice=choice,
        ):
            return self._synthesize_hierarchical_summary(
                question=question,
                evidences=evidences,
                model_name=model_name,
                choice=choice,
                deep_model_name=deep_model_name,
                scope_note=scope_note,
                evidence_truncated=evidence_truncated,
            )

        evidences, structured, memory, context_limited = self._pack_answer_context(
            question=question, evidences=evidences, structured=structured,
            memory=memory, model_name=model_name, scope_note=scope_note,
            intent=intent,
            choice=choice,
            documentary_only=True,
        )
        evidence_truncated = evidence_truncated or context_limited
        if not evidences and not structured:
            raise InferenceFailureError(InferenceFailureKind.CONTEXT_LIMIT)

        messages = build_answer_messages(
            question=question,
            evidences=evidences,
            structured=structured,
            memory=memory,
            scope_note=scope_note,
            document_summary=summary_mode,
            documentary_only=True,
        )
        try:
            result, availability_fallback = self._chat_with_fallback(
                model_name=model_name,
                fallback_model_name=self._policy.fallback_for(model_name),
                messages=messages,
                intent=intent,
                choice=choice,
                allow_fallback=_allow_model_fallback,
            )
        except OllamaUnavailableError as exc:
            if (not _allow_model_fallback or choice is not ModelChoice.DEEP
                    or self._policy.fast_model == model_name or not self._can_fallback_to_fast(exc)):
                raise
            logger.warning(
                "llm.model_fallback",
                extra={"primary_model": model_name, "fallback_model": self._policy.fast_model,
                       "failure_kind": _safe_failure_kind(exc)},
            )
            # Recalcular el prompt desde unidades autorizadas, no cortar texto
            # serializado ni enviar una ventana mayor al perfil alternativo.
            return self.synthesize(
                question=question, evidences=evidences, structured=structured, memory=memory,
                model_name=self._policy.fast_model, choice=ModelChoice.FAST,
                scope_note=scope_note, intent=intent, evidence_truncated=evidence_truncated,
                deep_model_name=None, _allow_hierarchy=False, _allow_model_fallback=False,
            )
        result_choice = self._policy.resolve_choice(result.model) if availability_fallback else choice
        result = replace(result, content=expand_citation_aliases(
            result.content, citation_aliases(evidences, structured),
        ))
        report = self._verify_answer(result.content, evidences, structured, question=question)
        answer_diagnostics.validated(result.content, report, retry=False)
        coverage = coverage_ratio(result.content, evidences)

        false_summary_insufficiency = summary_mode and bool(evidences) and (
            report.declares_insufficiency or not report.cited_source_ids
        )
        insufficient_coverage = (
            get_settings().answer_evidence_mode == "extractive"
            and len(evidences) >= RICH_EVIDENCE_THRESHOLD
            and coverage < MIN_COVERAGE_WITH_RICH_EVIDENCE
            and not report.declares_insufficiency
        )
        needs_retry = not report.grounded or insufficient_coverage or false_summary_insufficiency

        if not needs_retry:
            answer = self._append_limit_notice(result.content, evidence_truncated)
            return SynthesisResult(
                answer=answer,
                model=result.model,
                latency_ms=result.latency_ms,
                grounding=report,
                escalated_to_deep=(availability_fallback and result_choice is ModelChoice.DEEP),
                cited_source_ids=report.cited_source_ids,
                answer_basis=classify_answer_basis(
                    result.content, has_citations=bool(report.cited_source_ids),
                    insufficient=report.declares_insufficiency,
                ),
            )

        # La primera pasada no quedo fundamentada (o cobertura/insuficiencia
        # falsa): una unica regeneracion, con posible escalado al modelo profundo.
        return self._regenerate(
            question=question,
            evidences=evidences,
            structured=structured,
            memory=memory,
            scope_note=scope_note,
            intent=intent,
            summary_mode=summary_mode,
            evidence_truncated=evidence_truncated,
            deep_model_name=deep_model_name,
            first=_FirstPass(
                result=result,
                report=report,
                coverage=coverage,
                availability_fallback=availability_fallback,
                insufficient_coverage=insufficient_coverage,
                false_summary_insufficiency=false_summary_insufficiency,
                choice=result_choice,
            ),
        )

    def _regenerate(
        self,
        *,
        question: str,
        evidences: tuple[Evidence, ...],
        structured: tuple[StructuredEvidence, ...],
        memory: ConversationContext | None,
        scope_note: str,
        intent: Intent,
        summary_mode: bool,
        evidence_truncated: bool,
        deep_model_name: str | None,
        first: _FirstPass,
    ) -> SynthesisResult:
        """Regeneracion unica tras una primera pasada sin grounding suficiente.

        Escala al modelo profundo si el problema es cobertura o insuficiencia
        falsa de resumen. Si la regeneracion tampoco queda fundamentada: para un
        resumen cae a la red extractiva citable; para el resto se descarta la
        salida y se declara insuficiencia (nunca se conservan afirmaciones sin
        respaldo).
        """
        retry_note = self._retry_note(
            first.report,
            first.coverage,
            false_summary_insufficiency=first.false_summary_insufficiency,
        )
        escalate = (
            (first.insufficient_coverage or first.false_summary_insufficiency)
            and deep_model_name is not None
            and (first.choice is not ModelChoice.DEEP or deep_model_name != first.result.model)
        )
        retry_model = (deep_model_name or first.result.model) if escalate else first.result.model
        retry_choice = ModelChoice.DEEP if escalate else first.choice
        documentary_only = True
        # El mapa de transporte se conserva aun si hay menos fuentes tras el
        # presupuesto: E3 no pasa a designar el antiguo E4 en el reintento.
        source_aliases = citation_aliases(evidences, structured)
        evidences, structured, memory, context_limited = self._pack_answer_context(
            question=question, evidences=evidences, structured=structured,
            memory=memory, model_name=retry_model, scope_note=scope_note,
            intent=intent, retry_note=retry_note,
            choice=retry_choice, documentary_only=documentary_only,
            source_aliases=source_aliases,
        )
        evidence_truncated = evidence_truncated or context_limited
        if not evidences and not structured:
            raise InferenceFailureError(InferenceFailureKind.CONTEXT_LIMIT)
        logger.info(
            "agent.regenerating",
            extra={
                "reason": first.report.reason or "cobertura insuficiente",
                "coverage": round(first.coverage, 3),
                "escalated": escalate,
                "selected_model": retry_model,
                "model_route": str(retry_choice),
            },
        )
        retry_messages = build_answer_messages(
            question=question,
            evidences=evidences,
            structured=structured,
            memory=memory,
            scope_note=scope_note,
            retry_note=retry_note,
            document_summary=summary_mode,
            documentary_only=documentary_only,
            source_aliases=source_aliases,
        )
        answer_diagnostics.emit("retry_plan", {
            "retry_note": retry_note, "documentary_only": documentary_only,
            "selected_model": retry_model, "choice": retry_choice.value, "escalated": escalate,
        })
        try:
            retry_result, retry_fallback = self._chat_with_fallback(
                model_name=retry_model, fallback_model_name=self._policy.fallback_for(retry_model),
                messages=retry_messages, intent=intent, retry=True, choice=retry_choice,
            )
        except OllamaUnavailableError as exc:
            if (retry_choice is not ModelChoice.DEEP or self._policy.fast_model == retry_model
                    or not self._can_fallback_to_fast(exc)):
                raise
            return self.synthesize(
                question=question, evidences=evidences, structured=structured, memory=memory,
                model_name=self._policy.fast_model, choice=ModelChoice.FAST,
                deep_model_name=None, scope_note=scope_note, intent=intent,
                evidence_truncated=evidence_truncated, _allow_hierarchy=False, _allow_model_fallback=False,
            )
        result_choice = self._policy.resolve_choice(retry_result.model) if retry_fallback else retry_choice
        retry_result = replace(retry_result, content=expand_citation_aliases(
            retry_result.content, {alias: source for alias, source in source_aliases.items()
                                   if source in {e.source_id for e in (*evidences, *structured)}},
        ))
        retry_report = self._verify_answer(retry_result.content, evidences, structured, question=question)
        answer_diagnostics.validated(retry_result.content, retry_report, retry=True)
        retry_false_insufficiency = summary_mode and bool(evidences) and (
            retry_report.declares_insufficiency or not retry_report.cited_source_ids
        )
        total_latency = first.result.latency_ms + retry_result.latency_ms

        if retry_report.grounded and not retry_false_insufficiency:
            return SynthesisResult(
                answer=self._append_limit_notice(retry_result.content, evidence_truncated),
                model=retry_result.model,
                latency_ms=total_latency,
                grounding=retry_report,
                regenerated=True,
                escalated_to_deep=(
                    escalate
                    or (first.availability_fallback and first.choice is ModelChoice.DEEP)
                    or (retry_fallback and result_choice is ModelChoice.DEEP)
                ),
                cited_source_ids=retry_report.cited_source_ids,
                answer_basis=classify_answer_basis(
                    retry_result.content, has_citations=bool(retry_report.cited_source_ids),
                    insufficient=retry_report.declares_insufficiency,
                ),
            )

        # Para un resumen con texto legible, dos negativas del modelo no deben
        # convertirse en el falso mensaje que origino esta correccion. Se entrega
        # un resumen extractivo seguro y citable como ultima red.
        if summary_mode:
            return self._extractive_summary(
                evidences,
                model=retry_result.model,
                latency_ms=total_latency,
                evidence_truncated=evidence_truncated,
                regenerated=True,
                choice=result_choice,
            )

        if retry_report.has_invalid_citations:
            logger.warning(
                "agent.rejected_fabricated_citations",
                extra={"invalid_count": len(retry_report.invalid_source_ids)},
            )
        # El contrato original documental se conserva: una fuente recuperada no
        # demuestra que responda la pregunta. No convertir un extracto ajeno a
        # la consulta en una respuesta afirmativa tras dos generaciones fallidas.
        logger.warning(
            "agent.answer_validation_failed",
            extra={
                "reason": retry_report.reason,
                "evidence_count": len(evidences),
                "structured_count": len(structured),
                "cited_source_count": len(retry_report.cited_source_ids),
                "invalid_source_count": len(retry_report.invalid_source_ids),
                "selected_model": retry_result.model,
                "validation_detail": retry_report.validation_detail,
                "claim_index": retry_report.claim_index,
            },
        )
        raise AnswerValidationError(detail=retry_report.validation_detail or retry_report.reason)

    def _chat_with_fallback(
        self,
        *,
        model_name: str,
        fallback_model_name: str,
        messages: list[dict[str, str]],
        intent: Intent,
        retry: bool = False,
        choice: ModelChoice | None = None,
        allow_fallback: bool = True,
    ) -> tuple[ChatResult, bool]:
        """Fallback local acotado para rechazo HTTP especifico del modelo."""
        choice = self._policy.resolve_choice(model_name, choice)
        profile = self._policy.generation_profile(model_name, intent=intent, retry=retry, choice=choice)
        if self._messages_chars(messages) > self._input_budget_chars(model_name, intent, choice=choice):
            raise InferenceFailureError(InferenceFailureKind.CONTEXT_LIMIT)
        try:
            return (
                self._llm.chat(
                    model=model_name,
                    messages=messages,
                    temperature=profile.temperature,
                    num_ctx=profile.num_ctx,
                    max_tokens=profile.max_tokens,
                    execution_profile=choice.value,
                ),
                False,
            )
        except OllamaUnavailableError as exc:
            detail = exc.detail or ""
            # Una caida de transporte afecta a ambos modelos en el mismo Ollama;
            # no se duplica. HTTP puede ser modelo ausente u OOM y si justifica
            # probar una sola vez el otro modelo local.
            not_found = detail.startswith("HTTP 404") or getattr(exc, "http_status", None) == 404
            if not allow_fallback or not not_found or fallback_model_name == model_name:
                raise
            # Un fallback a un perfil menor no puede delegar el truncamiento al
            # runtime: ese corte puede eliminar una excepcion del documento.
            fallback_choice = self._policy.resolve_choice(fallback_model_name)
            if self._messages_chars(messages) > self._input_budget_chars(
                fallback_model_name, intent, choice=fallback_choice
            ):
                raise
            logger.warning(
                "llm.model_fallback",
                extra={"primary_model": model_name, "fallback_model": fallback_model_name},
            )
            fallback_profile = self._policy.generation_profile(
                fallback_model_name, intent=intent, retry=retry, choice=fallback_choice
            )
            return (
                self._llm.chat(
                    model=fallback_model_name,
                    messages=messages,
                    temperature=fallback_profile.temperature,
                    num_ctx=fallback_profile.num_ctx,
                    max_tokens=fallback_profile.max_tokens,
                    execution_profile=fallback_choice.value,
                ),
                True,
            )

    def _summary_exceeds_context(
        self, evidences: tuple[Evidence, ...], *, model_name: str,
        question: str = "", memory: ConversationContext | None = None,
        structured: tuple[StructuredEvidence, ...] = (), scope_note: str = "",
        choice: ModelChoice | None = None,
    ) -> bool:
        messages = build_answer_messages(
            question=question, evidences=evidences, structured=structured,
            memory=memory, scope_note=scope_note, document_summary=True, documentary_only=True,
        )
        return self._messages_chars(messages) > self._input_budget_chars(
            model_name, Intent.DOCUMENT_SUMMARY, choice=choice
        )

    def _input_budget_chars(
        self, model_name: str, intent: Intent, *, choice: ModelChoice | None = None
    ) -> int:
        profile = self._policy.generation_profile(model_name, intent=intent, choice=choice)
        prefix = get_settings().llm_system_prefix
        extra_overhead = _ADDITIONAL_SYSTEM_OVERHEAD_TOKENS if prefix else 0
        usable_tokens = max(
            0, profile.num_ctx - profile.max_tokens - _PROMPT_OVERHEAD_TOKENS - extra_overhead
        )
        # El adapter inserta el prefijo despues de construir los mensajes. Debe
        # reservarse antes de seleccionar fuentes y de comprobar cada llamada.
        return max(0, usable_tokens * _CHARS_PER_TOKEN_BUDGET - len(prefix))

    @staticmethod
    def _messages_chars(messages: list[dict[str, str]]) -> int:
        return sum(len(message["content"]) for message in messages)

    def _pack_answer_context(
        self, *, question: str, evidences: tuple[Evidence, ...],
        structured: tuple[StructuredEvidence, ...], memory: ConversationContext | None,
        model_name: str, scope_note: str, intent: Intent, retry_note: str = "",
        choice: ModelChoice | None = None,
        documentary_only: bool = False,
        source_aliases: dict[str, str] | None = None,
    ) -> tuple[tuple[Evidence, ...], tuple[StructuredEvidence, ...], ConversationContext | None, bool]:
        """Presupuesto agregado estimado; ninguna fuente se incluye parcialmente.

        La estimacion de caracteres no sustituye al tokenizer del runtime. Se
        reserva contexto para salida/protocolo y se mide el prompt serializado,
        incluyendo pregunta, memoria, metadatos y resultados SQL.
        """
        budget = self._input_budget_chars(model_name, intent, choice=choice)

        def fits(documents: tuple[Evidence, ...], tables: tuple[StructuredEvidence, ...]) -> bool:
            messages = build_answer_messages(
                question=question, evidences=documents, structured=tables,
                memory=memory, scope_note=scope_note, retry_note=retry_note,
                document_summary=intent is Intent.DOCUMENT_SUMMARY,
                documentary_only=documentary_only,
                source_aliases=source_aliases,
            )
            return self._messages_chars(messages) <= budget

        limited = False
        if memory is not None and not memory.is_empty and not fits(evidences, structured):
            # La memoria orienta el dialogo; nunca debe desplazar una fuente
            # factual que si cabe completa sin ella.
            memory = None
            limited = True
        if not fits((), ()):
            memory = None
            limited = True
        if not fits((), ()):
            answer_diagnostics.packed((), limited=True, memory_present=False, retry=bool(retry_note))
            return (), (), memory, True
        chosen_tables: tuple[StructuredEvidence, ...] = ()
        for table in structured:
            table_candidate = (*chosen_tables, table)
            if fits((), table_candidate):
                chosen_tables = table_candidate
            else:
                limited = True
        chosen_documents: tuple[Evidence, ...] = ()
        for evidence in evidences:
            document_candidate = (*chosen_documents, evidence)
            if fits(document_candidate, chosen_tables):
                chosen_documents = document_candidate
            else:
                limited = True
        answer_diagnostics.packed(
            chosen_documents, limited=limited,
            memory_present=memory is not None and not memory.is_empty, retry=bool(retry_note),
        )
        return chosen_documents, chosen_tables, memory, limited

    @staticmethod
    def _serialized_evidence_chars(evidences: tuple[Evidence, ...]) -> int:
        return sum(len(e.text) + 320 for e in evidences)

    def _partition_summary_evidence(
        self, evidences: tuple[Evidence, ...], *, model_name: str, scope_note: str = "",
        choice: ModelChoice | None = None,
    ) -> tuple[tuple[Evidence, ...], ...]:
        settings = get_settings()
        # Reservar la envoltura real y margen para la pregunta de cada parte;
        # dividir solo por texto ignoraba el system prompt y podia omitir chunks.
        empty_messages = build_answer_messages(
            question=" ".join(["parte"] * 40), evidences=(),
            scope_note=scope_note, document_summary=True,
        )
        budget = max(
            0, self._input_budget_chars(model_name, Intent.DOCUMENT_SUMMARY, choice=choice)
            - self._messages_chars(empty_messages)
        )
        batches: list[tuple[Evidence, ...]] = []
        current: list[Evidence] = []
        current_chars = 0
        for evidence in evidences:
            item_chars = len(evidence.text) + 320
            if current and (
                len(current) >= settings.rag_summary_max_chunks
                or current_chars + item_chars > budget
            ):
                batches.append(tuple(current))
                current = []
                current_chars = 0
            current.append(evidence)
            current_chars += item_chars
        if current:
            batches.append(tuple(current))
        return tuple(batches)

    def _synthesize_hierarchical_summary(
        self,
        *,
        question: str,
        evidences: tuple[Evidence, ...],
        model_name: str,
        choice: ModelChoice,
        deep_model_name: str | None,
        scope_note: str,
        evidence_truncated: bool,
    ) -> SynthesisResult:
        """Map FAST y reduce DEEP solo cuando el contexto no alcanza."""
        # Si una unidad no cabe en FAST, DEEP procesa el lote sin cortarla.
        # La eleccion es de perfil: los dos pueden compartir un mismo modelo.
        map_choice = (
            ModelChoice.DEEP
            if any(
                self._summary_exceeds_context(
                    (item,), model_name=self._policy.fast_model,
                    scope_note=scope_note, choice=ModelChoice.FAST,
                )
                for item in evidences
            )
            else ModelChoice.FAST
        )
        map_model = self._policy.model_for(map_choice)
        reduce_model = deep_model_name or model_name
        reduce_choice = ModelChoice.DEEP if deep_model_name is not None else choice
        batches = self._partition_summary_evidence(
            evidences, model_name=map_model, scope_note=scope_note, choice=map_choice
        )

        def summarize_batch(index_and_batch: tuple[int, tuple[Evidence, ...]]) -> SynthesisResult:
            index, batch = index_and_batch
            return self.synthesize(
                question=(
                    f"Resume la parte {index + 1} de {len(batches)} del contenido, " "con sus temas principales."
                ),
                evidences=batch,
                model_name=map_model,
                choice=map_choice,
                deep_model_name=deep_model_name,
                scope_note=scope_note,
                intent=Intent.DOCUMENT_SUMMARY,
                _allow_hierarchy=False,
            )

        # No crear ejecutores por solicitud; comparten admision y deadline global.
        partial_results = tuple(summarize_batch(item) for item in enumerate(batches))

        partials = tuple(result.answer for result in partial_results)
        reduce_messages = build_summary_reduce_messages(question=question, partial_summaries=partials)
        if self._messages_chars(reduce_messages) > self._input_budget_chars(
            reduce_model, Intent.DOCUMENT_SUMMARY, choice=reduce_choice
        ):
            return self._extractive_summary(
                evidences, model=reduce_model,
                latency_ms=sum(result.latency_ms for result in partial_results),
                evidence_truncated=True, regenerated=True,
                hierarchical=True, map_batches=len(batches),
                choice=reduce_choice,
            )
        try:
            reduce_result, fallback_used = self._chat_with_fallback(
                model_name=reduce_model,
                fallback_model_name=self._policy.fallback_for(reduce_model),
                messages=reduce_messages,
                intent=Intent.DOCUMENT_SUMMARY,
                choice=reduce_choice,
            )
        except OllamaUnavailableError as error:
            if reduce_choice is not ModelChoice.DEEP or not self._can_fallback_to_fast(error):
                raise
            # Los mapas ya se generaron con la evidencia autorizada. Si la
            # consolidacion DEEP falla, conservar un resumen literal verificable
            # sin otra llamada grande ni publicar la salida parcial del proveedor.
            return self._extractive_summary(
                evidences, model=map_model,
                latency_ms=sum(item.latency_ms for item in partial_results),
                evidence_truncated=True, regenerated=True, hierarchical=True,
                map_batches=len(batches), choice=map_choice,
            )
        result_choice = self._policy.resolve_choice(reduce_result.model) if fallback_used else reduce_choice
        report = self._verify_answer(reduce_result.content, evidences, ())
        final_citations = set(report.cited_source_ids)
        covers_every_part = all(
            not extract_citations(partial) or bool(final_citations.intersection(extract_citations(partial)))
            for partial in partials
        )
        map_latency = sum(result.latency_ms for result in partial_results)
        total_latency = map_latency + reduce_result.latency_ms

        if report.grounded and report.cited_source_ids and not report.declares_insufficiency and covers_every_part:
            return SynthesisResult(
                answer=self._append_limit_notice(reduce_result.content, evidence_truncated),
                model=reduce_result.model,
                latency_ms=total_latency,
                grounding=report,
                escalated_to_deep=result_choice is ModelChoice.DEEP,
                cited_source_ids=report.cited_source_ids,
                hierarchical=True,
                map_batches=len(batches),
            )

        # El reduce no puede borrar secciones ni fabricar citas. Si no preserva
        # todas las partes, se devuelve la consolidacion de mapas verificados.
        combined = "Resumen consolidado por secciones:\n\n" + "\n\n".join(partials)
        combined_report = GroundingReport(
            grounded=all(result.grounding.grounded for result in partial_results),
            cited_source_ids=tuple(dict.fromkeys(sid for result in partial_results for sid in result.cited_source_ids)),
            citations_valid=True,
            extractive_verified=all(result.grounding.extractive_verified for result in partial_results),
            reason="consolidacion de mapas con citas autorizadas; veracidad semantica no evaluada",
        )
        combined_fits = len(combined) <= self._input_budget_chars(
            reduce_result.model, Intent.DOCUMENT_SUMMARY, choice=result_choice
        )
        if combined_report.grounded and combined_fits:
            return SynthesisResult(
                answer=self._append_limit_notice(combined, evidence_truncated),
                model=reduce_result.model,
                latency_ms=total_latency,
                grounding=combined_report,
                regenerated=True,
                escalated_to_deep=result_choice is ModelChoice.DEEP,
                cited_source_ids=combined_report.cited_source_ids,
                hierarchical=True,
                map_batches=len(batches),
            )
        return self._extractive_summary(
            evidences,
            model=reduce_result.model,
            latency_ms=total_latency,
            evidence_truncated=evidence_truncated,
            regenerated=True,
            hierarchical=True,
            map_batches=len(batches),
            choice=result_choice,
        )

    def _extractive_summary(
        self,
        evidences: tuple[Evidence, ...],
        *,
        model: str,
        latency_ms: int,
        evidence_truncated: bool,
        regenerated: bool,
        hierarchical: bool = False,
        map_batches: int = 0,
        choice: ModelChoice | None = None,
    ) -> SynthesisResult:
        lines = ["Resumen extractivo del contenido disponible:"]
        cited: list[str] = []
        extractive_limit = get_settings().rag_summary_max_chunks
        budget = self._input_budget_chars(model, Intent.DOCUMENT_SUMMARY, choice=choice)
        total_chars = len(lines[0])
        units = [(item.source_id, item.text) for item in evidences]
        for source_id, text in units:
            block = f"{text.strip()} [[{source_id}]]"
            if not text.strip() or len(cited) >= extractive_limit or total_chars + len(block) + 2 > budget:
                continue
            # Verificar tambien las copias: IDs ambiguos o citas incrustadas en
            # un documento no pueden saltarse el contrato por esta ruta.
            report = verify_grounding(block, evidences)
            if not report.extractive_verified:
                continue
            lines.append(block)
            total_chars += len(block) + 2
            cited.append(source_id)
        if len(cited) < len(units):
            lines.append(
                f"Nota: esta seleccion cubre {len(cited)} de {len(units)} unidades recuperadas. "
                "Las unidades que exceden el presupuesto no se cortan; solicite el contenido por secciones."
            )
        if not cited:
            raise AnswerValidationError(detail="ninguna unidad extractiva completa verificable")
        answer = self._append_limit_notice("\n".join(lines), evidence_truncated)
        return SynthesisResult(
            answer=answer,
            model=model,
            latency_ms=latency_ms,
            grounding=GroundingReport(
                grounded=bool(cited),
                cited_source_ids=tuple(cited),
                reason="unidades extractivas completas; veracidad semantica no evaluada",
                citations_valid=bool(cited),
                extractive_verified=bool(cited),
                declares_insufficiency=not cited,
            ),
            regenerated=regenerated,
            cited_source_ids=tuple(cited),
            hierarchical=hierarchical,
            map_batches=map_batches,
        )

    @staticmethod
    def _append_limit_notice(answer: str, truncated: bool) -> str:
        if not truncated:
            return answer
        return (
            answer.rstrip()
            + "\n\nNota: el material o el contexto de esta solicitud supera el presupuesto "
            "operativo. La respuesta cubre una seleccion de unidades completas; "
            "solicite el resto por secciones."
        )

    @staticmethod
    def _retry_note(
        report: GroundingReport,
        coverage: float,
        *,
        false_summary_insufficiency: bool = False,
    ) -> str:
        if false_summary_insufficiency:
            return (
                "La evidencia contiene texto legible. Genera el resumen solicitado en vez de "
                "declarar insuficiencia y cita los source_id proporcionados."
            )
        if report.has_invalid_citations:
            return (
                "La respuesta anterior cito source_id que NO existen en la evidencia. "
                "Copia las etiquetas 'cita' del bloque EVIDENCIA DOCUMENTAL "
                "(por ejemplo [[E1]]); no reconstruyas nombres de PDF ni indices. "
                "Si algo no esta respaldado, omitelo o declara insuficiencia."
            )
        if report.reason in {
            "orientacion general atribuida a fuentes documentales",
            "secciones de procedencia mezcladas",
            "capacidad general deshabilitada",
            "aplicacion documental en orientacion general",
        }:
            return (
                "La respuesta anterior mezclo la procedencia del contenido. Responde solo "
                "con '### Información documentada' y afirmaciones respaldadas por la "
                "evidencia, cada una con su etiqueta 'cita'. Omite la seccion Orientacion "
                "general. No basta cambiar el titulo de una suposicion: elimina lo que "
                "no conste en las fuentes."
            )
        if report.reason in {"respuesta documental sin ninguna cita", "respuesta documental sin fuentes citadas"}:
            return (
                "La respuesta anterior no incluyo ninguna cita. Cada afirmacion documental "
                "debe llevar su [[source_id]]."
            )
        if report.reason == "afirmacion numerica sin respaldo en sus fuentes citadas":
            return (
                f"Causa de rechazo: {report.validation_detail or report.reason}. "
                "Separa datos declarados, regla citada y aplicacion condicional. Usa solo "
                "la fila y los conceptos comprobados en APLICACION_CONDICIONAL; no basta "
                "que el porcentaje aparezca en otra fila. No presentes supuestos como "
                "hechos del documento ni los traslades a orientacion general. Conserva "
                "encabezados y condiciones. Si la estructura no permite comprobarlo, "
                "explica el limite sin cifras o pide precision con una pregunta abierta."
            )
        if report.reason == "beneficio y fuente citada no corresponden":
            return ("El encabezado del beneficio no corresponde a la fuente citada. "
                    "Conserva el titulo, la poblacion y las condiciones de la misma fuente; "
                    "no mezcles beneficios que compartan cifras. Resume solo lo solicitado.")
        if report.reason == "vigencia actual no acreditada":
            return ("La fecha o nombre de un documento no acredita vigencia actual. "
                    "Describe lo documentado en su periodo y expresa en tus palabras "
                    "la incertidumbre de vigencia sin atribuirle actualidad.")
        if report.reason == "elegibilidad personal no acreditada":
            return ("Los datos declarados y el acceso a fuentes no acreditan elegibilidad personal. "
                    "Describe solo el calculo condicional bajo la regla citada; no confirmes "
                    "un derecho, pago o perfil individual.")
        if report.reason == "abstencion mezclada con afirmaciones documentales":
            return ("Una abstencion no acredita la afirmacion que la acompana. Separa cada "
                    "afirmacion respaldada con su cita y elimina lo no acreditado. Si no "
                    "puedes responder, usa una abstencion completa o una pregunta de precision.")
        if report.grounded and coverage < MIN_COVERAGE_WITH_RICH_EVIDENCE:
            return (
                "Faltan aspectos solicitados en la respuesta anterior. Revisa la evidencia y "
                "completa solo esos aspectos con sus citas y condiciones. No enumeres todas "
                "las fuentes ni agregues beneficios ajenos a la pregunta."
            )
        return (
            f"Causa de rechazo: {report.validation_detail or report.reason or 'respuesta no fundamentada'}. "
            "Reformula brevemente usando solo evidencia pertinente, con sus condiciones y "
            "etiquetas de cita exactas. Las aclaraciones seguras no necesitan citas ficticias."
        )
