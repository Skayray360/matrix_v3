# Creado por Aldo Garcia.
"""Sintesis local con grounding, reintento acotado y resumen jerarquico.

El agente solo recibe evidencia ya autorizada. No conoce el vector store ni el
motor de permisos, por lo que ninguna salida de un modelo puede ampliar el
alcance. Las respuestas documentales se verifican contra una allowlist literal
de ``source_id`` y admiten una sola regeneracion.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, replace
from time import perf_counter

from app.agents.documentary_output import (
    DOCUMENTARY_GENERATION_SCHEMA,
    SCHEMA_BUDGET_CHARS,
    DocumentaryOutputError,
    render_documentary_output,
    structured_output_enabled,
)
from app.agents.partial_documentary import recover_partial_documentary_answer
from app.agents.prompts import (
    build_answer_messages,
    build_clarification_messages,
    build_general_messages,
    build_summary_reduce_messages,
)
from app.common import answer_diagnostics
from app.common.answers import (
    GENERAL_HEADING,
    UNVERIFIED_ANSWER_NOTICE,
    AnswerBasis,
    safe_nonfactual_text,
)
from app.common.answers import answer_basis as classify_answer_basis
from app.common.errors import AnswerValidationError
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.common.logging import get_logger
from app.common.timing import timed_stage
from app.config import get_settings
from app.llm.model_policy import Intent, ModelChoice, ModelPolicy
from app.llm.ollama_client import ChatResult
from app.llm.provider import InferenceClient
from app.memory.service import ConversationContext
from app.rag.citation_aliases import citation_aliases, expand_citation_aliases
from app.rag.claim_context import application_diagnostics
from app.rag.grounding import (
    GroundingReport,
    coverage_ratio,
    extract_citations,
    verify_grounding,
)
from app.rag.schemas import Evidence
from app.rag.summary_scope import requested_summary_points, select_summary_scope, summary_map_question
from app.structured_data.tool import StructuredEvidence

logger = get_logger(__name__)

MIN_COVERAGE_WITH_RICH_EVIDENCE = 0.34
RICH_EVIDENCE_THRESHOLD = 3
_PROMPT_OVERHEAD_TOKENS = 1400
_CHARS_PER_TOKEN_BUDGET = 3
# Margen estimado del mensaje system adicional; no sustituye al tokenizer real.
_ADDITIONAL_SYSTEM_OVERHEAD_TOKENS = 32


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
    false_documentary_insufficiency: bool = False


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
        messages = build_general_messages(
            question=question, memory=None if intent is Intent.CAPABILITIES else memory,
            capabilities=intent is Intent.CAPABILITIES,
        )
        result, fallback_used = self._chat_with_fallback(
            model_name=model_name, fallback_model_name=model_name,
            messages=messages, intent=Intent.GENERAL, choice=choice,
        )
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
            # Una pregunta de precision no necesita publicar texto libre del
            # modelo ni convertir un formato incorrecto en un fallo de chat.
            answer = "¿Qué documento o apartado deseas consultar?"
            logger.info("agent.clarification_recovered", extra={"recovery_method": "safe_question"})
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

    def _decode_and_verify(
        self, result: ChatResult, evidences: tuple[Evidence, ...], structured: tuple,
        *, question: str, aliases: dict[str, str], use_schema: bool,
    ) -> tuple[ChatResult, GroundingReport]:
        if use_schema:
            try:
                result = replace(result, content=render_documentary_output(result.content, aliases))
            except DocumentaryOutputError as exc:
                # Reutiliza el unico reintento existente; no publica JSON parcial.
                return replace(result, content=""), GroundingReport(
                    grounded=False, reason="contrato documental JSON invalido",
                    validation_detail=f"contrato documental JSON invalido: {exc.code}",
                    invalid_source_ids=exc.invalid_aliases,
                )
        result = replace(result, content=expand_citation_aliases(result.content, aliases))
        report = self._verify_answer(result.content, evidences, structured, question=question)
        if report.grounded and report.declares_insufficiency and (evidences or structured):
            # La abstencion del generador no demuestra que los documentos no
            # contengan la respuesta. Describir el limite de esta consulta.
            result = replace(result, content=UNVERIFIED_ANSWER_NOTICE + "\n\n¿Qué apartado deseas precisar?")
        return result, report

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
        _summary_scope_resolved: bool = False,
    ) -> SynthesisResult:
        """Genera, verifica y como maximo una vez regenera la respuesta."""
        choice = self._policy.resolve_choice(model_name, choice)
        summary_mode = intent is Intent.DOCUMENT_SUMMARY
        if not evidences and not structured:
            return self.ask_clarification(question=question, model_name=model_name)

        if summary_mode:
            # Comprobar ANTES de empaquetar/dividir. Dos mapas no pueden
            # acreditar por separado identidades distintas bajo una misma cita.
            self._validate_summary_sources(evidences)
            if not _summary_scope_resolved:
                selection = select_summary_scope(question, evidences)
                evidences = selection.evidences
                if selection.clarification:
                    return SynthesisResult(
                        answer=selection.clarification, model=model_name, latency_ms=0,
                        grounding=GroundingReport(grounded=True, declares_insufficiency=True),
                        answer_basis="insufficient",
                    )
            points = requested_summary_points(question)
            if points:
                scope_note += (f"\nEl resumen final debe tener {points} puntos. "
                               "Cada punto corresponde a una afirmacion con sus citas. "
                               "No numeres el texto del claim; el servidor agrega la lista. "
                               "Conserva las restricciones de cada regla y evita temas ajenos al pedido.")

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
        use_schema = structured_output_enabled(document_summary=summary_mode, has_structured=bool(structured))
        result, availability_fallback = self._chat_with_fallback(
            model_name=model_name,
            fallback_model_name=model_name,
            messages=messages,
            intent=intent,
            choice=choice,
            response_schema=DOCUMENTARY_GENERATION_SCHEMA if use_schema else None,
        )
        result_choice = self._policy.resolve_choice(result.model) if availability_fallback else choice
        result, report = self._decode_and_verify(
            result, evidences, structured, question=question,
            aliases=citation_aliases(evidences, structured), use_schema=use_schema,
        )
        if summary_mode:
            report = self._summary_coverage_report(report, evidences)
            result, report = self._summary_point_format(result, report, question)
        answer_diagnostics.validated(result.content, report, retry=False)
        coverage = coverage_ratio(result.content, evidences)

        false_summary_insufficiency = summary_mode and bool(evidences) and (
            report.declares_insufficiency or not report.cited_source_ids
        )
        false_documentary_insufficiency = (
            report.declares_insufficiency
            and self._calculated_answer(question, evidences, intent=intent, structured=structured,
                                        evidence_truncated=evidence_truncated) is not None
        )
        insufficient_coverage = (
            get_settings().answer_evidence_mode == "extractive"
            and len(evidences) >= RICH_EVIDENCE_THRESHOLD
            and coverage < MIN_COVERAGE_WITH_RICH_EVIDENCE
            and not report.declares_insufficiency
        )
        needs_retry = (not report.grounded or insufficient_coverage or false_summary_insufficiency
                       or false_documentary_insufficiency)

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
                false_documentary_insufficiency=false_documentary_insufficiency,
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
        resumen cae a la red extractiva citable; el resto conserva solo unidades
        completas que pasan por separado el contrato documental. Si ninguna
        pasa, publica una limitacion controlada sin hechos del borrador.
        """
        retry_note = self._retry_note(
            first.report,
            first.coverage,
            false_summary_insufficiency=first.false_summary_insufficiency,
            false_documentary_insufficiency=first.false_documentary_insufficiency,
            structured_output=structured_output_enabled(document_summary=summary_mode, has_structured=bool(structured)),
        )
        escalate = (
            (first.insufficient_coverage or first.false_summary_insufficiency)
            and deep_model_name is not None
            and (first.choice is not ModelChoice.DEEP or deep_model_name != first.result.model)
        )
        # Aumentar el perfil de contexto puede ser util; cambiar de generador
        # durante una respuesta no es una sustitucion controlada de modelo.
        escalate = escalate and deep_model_name == first.result.model
        retry_model = first.result.model
        retry_choice = ModelChoice.DEEP if escalate else first.choice
        documentary_only = True
        # El mapa de transporte se conserva aun si hay menos fuentes tras el
        # presupuesto: E3 no pasa a designar el antiguo E4 en el reintento.
        source_aliases = citation_aliases(evidences, structured)
        first_application_diagnostics = application_diagnostics(question, evidences)
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
                "validation_detail": first.report.validation_detail,
                "claim_index": first.report.claim_index,
                "coverage": round(first.coverage, 3),
                "escalated": escalate,
                "selected_model": retry_model,
                "model_route": str(retry_choice),
                "cited_source_count": len(first.report.cited_source_ids),
                "invalid_source_count": len(first.report.invalid_source_ids),
                **first_application_diagnostics,
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
        use_schema = structured_output_enabled(document_summary=summary_mode, has_structured=bool(structured))
        retry_result, retry_fallback = self._chat_with_fallback(
            model_name=retry_model, fallback_model_name=retry_model,
            messages=retry_messages, intent=intent, retry=True, choice=retry_choice,
            response_schema=DOCUMENTARY_GENERATION_SCHEMA if use_schema else None,
        )
        result_choice = self._policy.resolve_choice(retry_result.model) if retry_fallback else retry_choice
        retry_result, retry_report = self._decode_and_verify(
            retry_result, evidences, structured, question=question, use_schema=use_schema,
            aliases={alias: source for alias, source in source_aliases.items()
                     if source in {e.source_id for e in (*evidences, *structured)}},
        )
        if summary_mode:
            retry_report = self._summary_coverage_report(retry_report, evidences)
            retry_result, retry_report = self._summary_point_format(retry_result, retry_report, question)
        answer_diagnostics.validated(retry_result.content, retry_report, retry=True)
        retry_false_insufficiency = summary_mode and bool(evidences) and (
            retry_report.declares_insufficiency or not retry_report.cited_source_ids
        )
        total_latency = first.result.latency_ms + retry_result.latency_ms

        calculated = self._calculated_answer(
            question, evidences, intent=intent, structured=structured, evidence_truncated=evidence_truncated,
        )
        retry_false_documentary_insufficiency = retry_report.declares_insufficiency and calculated is not None
        if retry_report.grounded and not retry_false_insufficiency and not retry_false_documentary_insufficiency:
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
                question=question,
                model=retry_result.model,
                latency_ms=total_latency,
                evidence_truncated=evidence_truncated,
                regenerated=True,
                choice=result_choice,
            )

        # Una consulta completa y acotada de calculo puede resolverse desde la
        # regla y fila verificadas aunque falle el formato de ambas generaciones.
        # El motivo del fallo del modelo no invalida este calculo independiente:
        # el helper exige el contrato entero y verifica su respuesta nueva.
        # Ningun borrador ni cita inventada se reutiliza como evidencia.
        if (not first.report.has_invalid_citations and not retry_report.has_invalid_citations
                and calculated is not None):
            answer, calculated_report = calculated
            logger.info("agent.calculated_application", extra={"cited_source_count":
                        len(calculated_report.cited_source_ids)})
            answer_diagnostics.emit("calculated_application", {"cited_source_count":
                                    len(calculated_report.cited_source_ids)})
            answer_diagnostics.validated(answer, calculated_report, retry=True)
            return SynthesisResult(
                answer=answer, model=retry_result.model, latency_ms=total_latency,
                grounding=calculated_report, regenerated=True,
                escalated_to_deep=(escalate or (first.availability_fallback and first.choice is ModelChoice.DEEP)
                                   or (retry_fallback and result_choice is ModelChoice.DEEP)),
                cited_source_ids=calculated_report.cited_source_ids, answer_basis="documented",
            )

        if retry_report.has_invalid_citations:
            logger.warning(
                "agent.rejected_fabricated_citations",
                extra={"invalid_count": len(retry_report.invalid_source_ids)},
            )
        # Una fuente recuperada no demuestra que conteste la pregunta. Solo
        # recuperar afirmaciones completas ya generadas y verificables, nunca
        # convertir un extracto arbitrario en respuesta afirmativa.
        if (get_settings().answer_evidence_mode == "cited"
                and not first.report.has_invalid_citations and not retry_report.has_invalid_citations):
            for candidate in (retry_result.content, first.result.content):
                recovered = recover_partial_documentary_answer(
                    candidate, evidences, structured=structured, question=question,
                )
                if recovered is None:
                    continue
                logger.info("agent.partial_answer_recovered", extra={
                    "kept_claim_count": recovered.kept_claims,
                    "discarded_claim_count": recovered.discarded_claims,
                    "reason": retry_report.reason,
                    "validation_detail": retry_report.validation_detail,
                    "cited_source_count": len(recovered.grounding.cited_source_ids),
                })
                answer_diagnostics.validated(recovered.answer, recovered.grounding, retry=True)
                return SynthesisResult(
                    answer=self._append_limit_notice(recovered.answer, evidence_truncated),
                    model=retry_result.model, latency_ms=total_latency,
                    grounding=recovered.grounding, regenerated=True,
                    escalated_to_deep=escalate,
                    cited_source_ids=recovered.grounding.cited_source_ids, answer_basis="documented",
                )
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
                **application_diagnostics(question, evidences),
            },
        )
        # El transporte funciono pero no produjo hechos publicables. Es una
        # limitacion documental, no una averia de la cola. Los fallos de red,
        # permisos, runtime y contexto conservan sus excepciones originales.
        answer = UNVERIFIED_ANSWER_NOTICE + "\n\n¿Qué documento o apartado deseas precisar?"
        return SynthesisResult(
            answer=answer, model=retry_result.model, latency_ms=total_latency,
            grounding=GroundingReport(
                grounded=True, declares_insufficiency=True,
                reason="limite de verificacion documental; ningun hecho del borrador publicado",
            ),
            regenerated=True, escalated_to_deep=escalate, answer_basis="insufficient",
        )

    @staticmethod
    def _calculated_answer(question, evidences, *, intent, structured, evidence_truncated):
        if (intent is not Intent.DOCUMENTAL or get_settings().answer_evidence_mode != "cited"
                or structured or evidence_truncated):
            return None
        from app.rag.calculated_application import calculated_application_answer

        return calculated_application_answer(question, evidences)

    def _chat_transport(self, *, response_schema: dict | None = None, **kwargs) -> ChatResult:
        started = perf_counter()
        try:
            if response_schema is not None:
                kwargs["response_schema"] = response_schema
            return self._llm.chat(**kwargs)
        except InferenceFailureError as exc:
            if response_schema is None or exc.failure_kind != InferenceFailureKind.SCHEMA:
                raise
            # El proveedor ya descarto la salida. El verificador solicitara el
            # unico reintento semantico, sin repetir errores de red/timeouts.
            return ChatResult(content="", model=kwargs["model"],
                              latency_ms=int((perf_counter() - started) * 1000))

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
        response_schema: dict | None = None,
    ) -> tuple[ChatResult, bool]:
        """Compatibilidad de llamada; nunca sustituye el generador seleccionado.

        Los argumentos historicos de fallback se aceptan para conservar APIs.
        Un modelo ausente, ocupado o fallido genera su error operativo original.
        """
        choice = self._policy.resolve_choice(model_name, choice)
        profile = self._policy.generation_profile(model_name, intent=intent, retry=retry, choice=choice)
        if self._messages_chars(messages) > self._input_budget_chars(model_name, intent, choice=choice):
            raise InferenceFailureError(InferenceFailureKind.CONTEXT_LIMIT)
        return (
            self._chat_transport(
                model=model_name, messages=messages, temperature=profile.temperature,
                num_ctx=profile.num_ctx, max_tokens=profile.max_tokens,
                execution_profile=choice.value, response_schema=response_schema,
            ),
            False,
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
        schema_chars = 0
        if intent in {Intent.DOCUMENTAL, Intent.MIXED, Intent.DOCUMENT_SUMMARY} and structured_output_enabled(
            document_summary=intent is Intent.DOCUMENT_SUMMARY, has_structured=False,
        ):
            # El schema tambien ocupa espacio en runtimes de salida restringida.
            # En MIXED se reserva conservadoramente aun si termina entrando SQL.
            schema_chars = SCHEMA_BUDGET_CHARS
        return max(0, usable_tokens * _CHARS_PER_TOKEN_BUDGET - len(prefix) - schema_chars)

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
        self._validate_summary_sources(evidences)
        # Si una unidad no cabe en FAST, DEEP procesa el lote sin cortarla.
        # La eleccion es de perfil: los dos pueden compartir un mismo modelo.
        map_choice = (
            ModelChoice.DEEP
            if self._policy.deep_model == model_name and any(
                self._summary_exceeds_context(
                    (item,), model_name=model_name,
                    scope_note=scope_note, choice=choice,
                )
                for item in evidences
            )
            else choice
        )
        map_model = model_name
        reduce_model = model_name
        reduce_choice = ModelChoice.DEEP if deep_model_name == model_name else choice
        batches = self._partition_summary_evidence(
            evidences, model_name=map_model, scope_note=scope_note, choice=map_choice
        )

        def summarize_batch(index_and_batch: tuple[int, tuple[Evidence, ...]]) -> SynthesisResult:
            index, batch = index_and_batch
            return self.synthesize(
                question=summary_map_question(question, part=index + 1, total=len(batches)),
                evidences=batch,
                model_name=map_model,
                choice=map_choice,
                deep_model_name=deep_model_name,
                scope_note=scope_note,
                intent=Intent.DOCUMENT_SUMMARY,
                _allow_hierarchy=False,
                # Una continuacion de pagina puede no repetir el encabezado;
                # el alcance ya se resolvio sobre el conjunto antes de dividir.
                _summary_scope_resolved=True,
            )

        # No crear ejecutores por solicitud; comparten admision y deadline global.
        partial_results = tuple(summarize_batch(item) for item in enumerate(batches))

        partials = tuple(result.answer for result in partial_results)
        # Solo las fuentes presentes en mapas verificados pueden citarse en
        # la reduccion. Las demas no formaron parte de su entrada.
        map_citations = {sid for result in partial_results for sid in result.cited_source_ids}
        reduce_evidences = tuple(item for item in evidences if item.source_id in map_citations)
        aliases = citation_aliases(reduce_evidences)
        use_schema = structured_output_enabled(document_summary=True, has_structured=False)
        reduce_messages = build_summary_reduce_messages(
            question=question, partial_summaries=partials,
            source_aliases=aliases if use_schema else None,
        )
        if self._messages_chars(reduce_messages) > self._input_budget_chars(
            reduce_model, Intent.DOCUMENT_SUMMARY, choice=reduce_choice
        ):
            return self._extractive_summary(
                evidences, model=reduce_model,
                question=question,
                latency_ms=sum(result.latency_ms for result in partial_results),
                evidence_truncated=True, regenerated=True,
                hierarchical=True, map_batches=len(batches),
                choice=reduce_choice,
            )
        reduce_result, fallback_used = self._chat_with_fallback(
            model_name=reduce_model,
            fallback_model_name=reduce_model,
            messages=reduce_messages,
            intent=Intent.DOCUMENT_SUMMARY,
            choice=reduce_choice,
            response_schema=DOCUMENTARY_GENERATION_SCHEMA if use_schema else None,
        )
        result_choice = self._policy.resolve_choice(reduce_result.model) if fallback_used else reduce_choice
        reduce_result, report = self._decode_and_verify(
            reduce_result, reduce_evidences, (), question=question, aliases=aliases, use_schema=use_schema,
        )
        reduce_result, report = self._summary_point_format(reduce_result, report, question)
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
        if combined_report.grounded and combined_fits and not requested_summary_points(question):
            logger.info("agent.summary_recovered", extra={
                "recovery_method": "validated_sections", "evidence_count": len(evidences),
                "cited_source_count": len(combined_report.cited_source_ids), "map_batches": len(batches),
            })
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
            question=question,
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
        question: str = "",
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
        # Dar oportunidad a cada documento y a su principio/final antes de
        # consumir el presupuesto en los primeros parrafos de un solo archivo.
        groups: dict[tuple, list[int]] = {}
        for index, item in enumerate(evidences):
            groups.setdefault(self._document_key(item), []).append(index)
        candidates: list[deque] = []
        for indices in groups.values():
            priority = [indices[0]]
            if len(indices) > 1:
                priority.append(indices[-1])
            spans = deque([(1, len(indices) - 2)])
            while spans:
                left, right = spans.popleft()
                if left <= right:
                    middle = (left + right) // 2
                    priority.append(indices[middle])
                    spans.extend(((left, middle - 1), (middle + 1, right)))
            candidates.append(deque(priority))
        order: list[int] = []
        while any(candidates):
            order.extend(group.popleft() for group in candidates if group)
        selected: dict[int, str] = {}
        for index in order:
            source_id, text = units[index]
            block = f"{text.strip()} [[{source_id}]]"
            if not text.strip() or len(selected) >= extractive_limit or total_chars + len(block) + 2 > budget:
                continue
            # Verificar tambien las copias: IDs ambiguos o citas incrustadas en
            # un documento no pueden saltarse el contrato por esta ruta.
            report = verify_grounding(block, evidences)
            if not report.extractive_verified:
                continue
            selected[index] = block
            total_chars += len(block) + 2
        points = requested_summary_points(question)
        for index, block in sorted(selected.items()):
            lines.append(block)
            cited.append(units[index][0])
        if points and selected:
            # Conservar unidades enteras (incluidas condiciones y tablas).
            # No cortar frases ni inventar puntos para completar la cuota.
            blocks = lines[1:]
            count = min(points, len(blocks))
            groups = [blocks[index * len(blocks) // count:(index + 1) * len(blocks) // count]
                      for index in range(count)]
            lines = ["Extractos verificados del contenido solicitado:"]
            if count < points:
                lines.append(
                    f"No pude validar una sintesis en {points} puntos. Presento {count} extractos completos "
                    "para conservar las condiciones del documento."
                )
            for index, group in enumerate(groups, start=1):
                lines.append(f"{index}. " + "\n\n   ".join(block.replace("\n", "\n   ") for block in group))
        if len(cited) < len(units):
            lines.append(
                f"Nota: esta seleccion cubre {len(cited)} de {len(units)} unidades recuperadas. "
                "Las unidades que exceden el presupuesto no se cortan; solicite el contenido por secciones."
            )
        if not cited:
            raise AnswerValidationError(detail="ninguna unidad extractiva completa verificable")
        answer = self._append_limit_notice("\n\n".join(lines), evidence_truncated)
        logger.info("agent.summary_recovered", extra={
            "recovery_method": "complete_extracts", "evidence_count": len(evidences),
            "cited_source_count": len(cited), "map_batches": map_batches,
        })
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
    def _summary_point_format(
        result: ChatResult, report: GroundingReport, question: str,
    ) -> tuple[ChatResult, GroundingReport]:
        """El formato pedido se verifica aparte del respaldo de las afirmaciones."""
        count = requested_summary_points(question)
        if count is None or not report.grounded or report.declares_insufficiency:
            return result, report
        paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", result.content)
                      if paragraph.strip()]
        points = [paragraph for paragraph in paragraphs if extract_citations(paragraph)]
        # Solo cabeceras decorativas pueden quedar fuera de los puntos citados.
        # No eliminar ni esconder texto factual para forzar la longitud.
        notes = [paragraph for paragraph in paragraphs if paragraph not in points
                 and safe_nonfactual_text(paragraph)]
        extras = [paragraph for paragraph in paragraphs if paragraph not in points and paragraph not in notes
                  and not paragraph.startswith("#")]
        if len(points) != count or extras:
            return result, replace(report, grounded=False,
                                   reason="resumen no respeta cantidad de puntos",
                                   validation_detail="resumen no respeta cantidad de puntos")
        formatted = "\n\n".join(
            f"{index}. " + re.sub(r"^(?:\d+[.)]|[-*])\s+", "", paragraph)
            for index, paragraph in enumerate(points, start=1)
        )
        if notes:
            formatted += "\n\n" + "\n\n".join(notes)
        return replace(result, content=formatted), report

    @staticmethod
    def _validate_summary_sources(evidences: tuple[Evidence, ...]) -> None:
        identities: dict[str, tuple] = {}
        for item in evidences:
            identity = (item.text, item.document_id, item.filename, item.page_or_sheet, item.scope)
            if identities.setdefault(item.source_id, identity) != identity:
                raise AnswerValidationError(detail="identificador de fuente ambiguo entre evidencias diferentes")

    @staticmethod
    def _document_key(item: Evidence) -> tuple:
        return (item.scope, item.document_id or (item.category, item.filename))

    @classmethod
    def _summary_coverage_report(cls, report: GroundingReport, evidences: tuple[Evidence, ...]) -> GroundingReport:
        """Cobertura minima por documento, no obligacion de citar cada chunk."""
        if not report.grounded or report.declares_insufficiency:
            return report
        expected = {cls._document_key(item) for item in evidences}
        cited = {cls._document_key(item) for item in evidences if item.source_id in report.cited_source_ids}
        if expected - cited:
            return replace(report, grounded=False, reason="resumen omite documentos recuperados",
                           validation_detail="resumen omite documentos recuperados")
        return report

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
        false_documentary_insufficiency: bool = False,
        structured_output: bool = False,
    ) -> str:
        if structured_output:
            return (
                f"Causa de rechazo: {report.validation_detail or report.reason or 'cobertura insuficiente'}. "
                "Regenera exclusivamente el objeto JSON completo con status, claims, limitations y clarification. "
                "Cada text es una afirmacion en una sola linea; citations contiene los aliases "
                "simples de sus fuentes autorizadas. No pongas encabezados, tablas ni citas dentro "
                "de text. Conserva las condiciones de la fuente en cada claim. Cifras, versiones, "
                "identificadores y comandos deben corresponder a las fuentes citadas. Solo si "
                "aplicas una regla a un caso personal, usa la fila y conceptos comprobados en "
                "APLICACION_CONDICIONAL, sin afirmar elegibilidad ni vigencia actual. "
                "No copies numeros de etiquetas internas. Conserva las afirmaciones respaldadas: si solo "
                "puedes responder una parte usa partial, claims completos independientes y limitations con "
                "los codigos del esquema. Usa insufficient sin claims solo si ninguna parte tiene respaldo. "
                "clarification debe estar vacio o copiar literalmente una opcion del enum del esquema."
                + (" La evidencia recibida contiene texto legible: resume ese contenido con sus citas; "
                   "la abstencion anterior no corresponde a falta de texto."
                   if false_summary_insufficiency else "")
                + (" Incluye al menos un punto respaldado de cada documento recibido, sin mezclar sus condiciones."
                   if report.reason == "resumen omite documentos recuperados" else "")
                + (" Respeta la cantidad de puntos solicitada: un claim con sus citas por punto."
                   if report.reason == "resumen no respeta cantidad de puntos" else "")
                + (" La aplicacion ya fue comprobada; la abstencion anterior no corresponde a falta de evidencia."
                   if false_documentary_insufficiency else "")
            )
        if report.reason == "resumen omite documentos recuperados":
            return ("El resumen omitio documentos recibidos. Incluye al menos un punto respaldado de "
                    "cada documento, con sus citas, sin mezclar condiciones ni agregar hechos.")
        if report.reason == "resumen no respeta cantidad de puntos":
            return ("Respeta la cantidad de puntos solicitada. Escribe un parrafo por punto, "
                    "cada uno con las citas de sus fuentes, sin agregar temas fuera de la seccion pedida.")
        if false_documentary_insufficiency:
            return (
                "La regla, los datos declarados y una fila compatible ya fueron comprobados. "
                "La abstencion anterior no corresponde a falta de evidencia. Explica la aplicacion "
                "condicional de APLICACION_CONDICIONAL con sus conceptos y citas, sin afirmar "
                "elegibilidad ni vigencia actual."
            )
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
        if report.reason == "afirmacion documental final sin cita":
            return (
                f"Causa de rechazo: {report.reason}. "
                "La respuesta termino con texto sin una cita valida. Cada afirmacion documental, "
                "incluidas las notas finales, debe llevar una etiqueta completa como [[E1]]. "
                "Para varias fuentes escribe [[E1]] [[E2]], nunca [[E1], [E2]]. "
                "Si solo necesitas un dato del usuario, termina con una pregunta abierta breve "
                "(por ejemplo, sobre su condicion sindical o contratacion), sin afirmar una "
                "regla ni explicar que prestaciones aplican. Empieza directamente con el signo "
                "de interrogacion; omite prefacios de proposito y ejemplos de beneficios o "
                "condiciones de cobertura dentro de la pregunta. Esa pregunta no requiere cita."
            )
        if report.reason == "afirmacion numerica sin respaldo en sus fuentes citadas":
            return (
                f"Causa de rechazo: {report.validation_detail or report.reason}. "
                "Conserva cifras, fechas, versiones, identificadores y comandos de las fuentes citadas. "
                "No cambies un identificador tecnico ni tomes numeros de etiquetas internas. "
                "Si la consulta aplica una regla al caso personal, separa datos declarados, regla "
                "citada y aplicacion condicional. Usa solo "
                "la fila y los conceptos comprobados en APLICACION_CONDICIONAL; no basta "
                "que el porcentaje aparezca en otra fila. No presentes supuestos como "
                "hechos del documento ni los traslades a orientacion general. Conserva "
                "encabezados y condiciones. Redacta cada resultado con su condicion "
                "en una misma oracion y coloca la cita al final, nunca entre el supuesto "
                "y el resultado. Explica solo la fila aplicable, sin copiar toda la tabla. "
                "Si la estructura no permite comprobarlo, "
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
