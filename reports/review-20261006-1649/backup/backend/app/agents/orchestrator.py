# Creado por Aldo Garcia.
"""Agente Orquestador.

Orden de la seccion 35, aplicado literalmente:

``autenticar -> autorizar -> comprender intencion -> seleccionar herramienta ->
recuperar solo datos permitidos -> validar suficiencia -> seleccionar perfil
Gemma -> sintetizar sin inventar -> verificar citas -> auditar -> responder``

La autenticacion la resuelve la capa API; el orquestador **empieza** por la
autorizacion: resuelve las categorias efectivas ANTES de tocar el vector store y
antes de construir cualquier prompt. Una consulta documental sin evidencia
autorizada no llama al modelo; las preguntas generales conservan su ruta propia.

El orquestador no consulta fuentes directamente: delega en la tool RAG y en la
tool de datos estructurados, y entrega la evidencia ya autorizada al Agente
Especializado.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.knowledge_agent import KnowledgeAgent, SynthesisResult
from app.agents.prompts import (
    DENIED_ANSWER,
    IDENTITY_ANSWER,
    INSUFFICIENT_ANSWER,
    UNKNOWN_BENEFIT_ANSWER,
    capabilities_answer,
)
from app.agents.query_planner import build_query_plan
from app.audit.service import AuditRecord, AuditService, get_audit_service
from app.authorization.context import UserContext
from app.authorization.policy import PolicyEngine, get_policy_engine
from app.common import answer_diagnostics
from app.common.answers import AnswerBasis
from app.common.answers import answer_basis as classify_answer_basis
from app.common.errors import ForbiddenError, MatrixError, StructuredQueryRejectedError
from app.common.logging import get_logger
from app.config import get_settings
from app.database.models import Conversation, ConversationMessage
from app.llm.model_policy import Intent, ModelPolicy, RoutingDecision
from app.llm.ollama_client import get_ollama_client
from app.llm.provider import InferenceClient
from app.memory.service import MemoryService, authorization_fingerprint
from app.rag.retriever import RetrievalResult, Retriever
from app.rag.schemas import Evidence
from app.security.prompt_guard import sanitize_user_message
from app.structured_data.tool import StructuredDataTool, StructuredEvidence

logger = get_logger(__name__)

#: Palabras que sugieren una pregunta comparativa (activa diversidad por categoria).
_COMPARATIVE_MARKERS = ("compara", "comparativa", "diferencia", "versus", " vs ", "frente a")


def _summary_source_scope(question: str) -> str:
    """Resuelve referencias explicitas sin usar un modelo ni ampliar permisos.

    Una politica adjunta sigue siendo privada. Dos referencias separadas por
    una conjuncion pueden pedir ambos alcances. Una peticion sin referencia
    reconocible conserva el flujo original: adjuntos primero y luego corpus.
    """
    normalized = "".join(
        char for char in unicodedata.normalize("NFKD", question.casefold())
        if not unicodedata.combining(char)
    )
    scopes: set[str] = set()
    for clause in re.split(r"\s+(?:y|e|junto con|ademas de)\s+", normalized):
        if re.search(
            r"\badjunt\w*\b|\bprivad\w*\b|\b(?:cargad|subid)\w*\b|"
            r"\b(?:este|estos|mi|mis) (?:archivo|documento)s?\b",
            clause,
        ):
            scopes.add("private")
        elif re.search(
            r"\b(?:politica|reglamento|normativa)s?\b|\bmanual(?:es)?\b|\bcorporativ\w*\b|"
            r"\bde la empresa\b|\bbase de conocimiento\b|\brepositorio\b",
            clause,
        ):
            scopes.add("corporate")
    return "mixed" if len(scopes) > 1 else next(iter(scopes), "auto")


@dataclass(frozen=True, slots=True)
class ChatOutcome:
    """Respuesta completa de un turno de chat."""

    answer: str
    conversation_id: str
    message_id: str
    model: str
    intent: str
    evidences: tuple[Evidence, ...] = field(default_factory=tuple)
    structured: tuple[StructuredEvidence, ...] = field(default_factory=tuple)
    cited_source_ids: tuple[str, ...] = field(default_factory=tuple)
    latency_ms: int = 0
    authorization_decision: str = "ALLOW"
    grounded: bool = True
    regenerated: bool = False
    answer_basis: AnswerBasis = "documented"

    def public_sources(self) -> list[dict[str, object]]:
        """Solo se exponen las fuentes efectivamente citadas."""
        cited = set(self.cited_source_ids)
        documental = [e.to_public_dict() for e in self.evidences if e.source_id in cited]
        structured = [
            {
                "source_id": s.source_id,
                "category": "datos_estructurados",
                "filename": f"{s.source}/{s.entity}",
                "section": "",
                "page_or_sheet": "",
                "score": 1.0,
                "label": f"{s.source}/{s.entity}",
                "scope": "structured",
            }
            for s in self.structured
            if s.source_id in cited
        ]
        return documental + structured


class Orchestrator:
    """Coordina autorizacion, herramientas, routing y sintesis."""

    def __init__(
        self,
        *,
        llm: InferenceClient | None = None,
        policy_engine: PolicyEngine | None = None,
        retriever: Retriever | None = None,
        structured_tool: StructuredDataTool | None = None,
        memory: MemoryService | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self._llm = llm or get_ollama_client()
        self._policies = policy_engine or get_policy_engine()
        self._model_policy = ModelPolicy()
        self._retriever = retriever or Retriever(llm=self._llm)
        self._structured = structured_tool or StructuredDataTool(policy_engine=self._policies)
        self._memory = memory or MemoryService()
        self._audit = audit or get_audit_service()
        self._agent = KnowledgeAgent(llm=self._llm, policy=self._model_policy)

    # ------------------------------------------------------------------------
    @answer_diagnostics.isolated_turn
    def handle_chat(self, db: Session, *, ctx: UserContext, conversation: Conversation, message: str) -> ChatOutcome:
        """Procesa un turno completo."""
        started = time.perf_counter()

        # --- 1. contenido no confiable del usuario --------------------------
        sanitized = sanitize_user_message(message)
        question = sanitized.text
        if not question:
            raise ForbiddenError("El mensaje esta vacio.")

        authorized_categories = self._policies.effective_categories(ctx)
        db.info["authorization_scope"] = authorization_fingerprint(db, ctx, authorized_categories)
        memory_context = self._memory.build_context(db, ctx, conversation, authorized_categories=authorized_categories)
        if self._policies.effective_categories(ctx) != authorized_categories:
            raise ForbiddenError("Sus permisos cambiaron durante la consulta. Vuelva a consultar.")
        # Clasificar despues de autorizar la memoria. Las respuestas anteriores
        # no son evidencia ni pueden inventar un antecedente para la busqueda.
        reference = self._model_policy.contextual_reference(
            question, prior_questions=tuple(t.content for t in memory_context.turns if t.role == "user"),
        )
        intent = self._model_policy.classify_intent(question, previous_question=reference)
        retrieval_question = f"{reference}\nSeguimiento: {question}" if reference else question
        answer_diagnostics.begin(
            ctx=ctx, conversation_id=conversation.id, question=question,
            retrieval_question=retrieval_question, intent=intent.value,
        )
        self._memory.rename_if_untitled(db, conversation, sanitized.text)
        self._memory.append_message(
            db,
            conversation,
            role="user",
            content=sanitized.text,
            authorized_categories=tuple(sorted(authorized_categories)),
        )
        if intent in (Intent.IDENTITY, Intent.CAPABILITIES):
            return self._finish_identity(
                db, ctx=ctx, conversation=conversation, started=started, intent=intent,
            )
        # GENERAL admite conceptos sin referencias internas. Las solicitudes
        # internas/personales conservan RAG; el fallback general posterior
        # necesita permiso de configuracion y la misma guardia de privacidad.
        if intent in (Intent.GENERAL, Intent.CONVERSATIONAL):
            return self._finish_general(
                db,
                ctx=ctx,
                conversation=conversation,
                question=question,
                intent=intent,
                authorized_categories=authorized_categories,
                started=started,
            )

        db.commit()  # Solicitud registrada; devuelve la conexion antes de embeddings/LLM.
        # --- 3. AUTORIZACION antes de cualquier recuperacion ----------------
        comparative = any(marker in question.lower() for marker in _COMPARATIVE_MARKERS)

        # --- 4/5. herramientas: solo datos permitidos -----------------------
        retrieval, structured_results, tools_used = self._gather_evidence(
            ctx=ctx,
            conversation=conversation,
            question=retrieval_question,
            intent=intent,
            authorized_categories=authorized_categories,
            comparative=comparative,
        )

        answer_diagnostics.retrieved(retrieval.evidences)
        # --- 6. suficiencia --------------------------------------------------
        if not retrieval.has_evidence and not structured_results:
            if not reference and self._allow_general_fallback(question, intent, authorized_categories):
                return self._finish_general(
                    db, ctx=ctx, conversation=conversation, question=question,
                    intent=intent, authorized_categories=authorized_categories, started=started,
                )
            return self._finish_no_evidence(
                db,
                ctx=ctx,
                conversation=conversation,
                intent=intent,
                started=started,
                authorized_categories=authorized_categories,
                question=retrieval_question,
            )

        # --- 7. seleccion de modelo -----------------------------------------
        routing: RoutingDecision = self._model_policy.route(
            question,
            intent=intent,
            categories_in_scope=len(retrieval.distinct_categories()),
            multi_tool=len(tools_used) > 1,
            low_retrieval_confidence=retrieval.low_confidence,
            evidence_count=len(retrieval.evidences),
            evidence_chars=sum(len(e.text) for e in retrieval.evidences),
        )

        # --- 8/9. sintesis y verificacion de grounding ----------------------
        scope_note = self._build_scope_note(authorized_categories, used_private_scope=retrieval.used_private_scope)
        synthesis: SynthesisResult = self._agent.synthesize(
            question=question,
            evidences=retrieval.evidences,
            structured=structured_results,
            memory=memory_context,
            model_name=routing.model_name,
            choice=routing.choice,
            deep_model_name=self._model_policy.deep_model,
            scope_note=scope_note,
            intent=intent,
            evidence_truncated=retrieval.truncated,
        )

        # --- 10. persistencia + auditoria -----------------------------------
        return self._finish_answer(
            db,
            ctx=ctx,
            conversation=conversation,
            routing=routing,
            synthesis=synthesis,
            retrieval=retrieval,
            structured_results=structured_results,
            tools_used=tools_used,
            authorized_categories=authorized_categories,
            started=started,
        )

    # ------------------------------------------------------------------------
    def _gather_evidence(
        self,
        *,
        ctx: UserContext,
        conversation: Conversation,
        question: str,
        intent: Intent,
        authorized_categories: frozenset[str],
        comparative: bool,
    ) -> tuple[RetrievalResult, tuple[StructuredEvidence, ...], list[str]]:
        """Fases 4/5: ejecuta las herramientas y devuelve solo datos permitidos.

        Para ``DOCUMENT_SUMMARY`` respeta el alcance nombrado: corporativo,
        adjuntos o ambos. Sin referencia explicita conserva adjuntos primero y
        despues RAG corporativo. El resto de intenciones van directo a RAG. La
        tool estructurada se ejecuta para ``STRUCTURED``/``MIXED``.

        Devuelve ``(retrieval, structured_results, tools_used)``.
        """
        tools_used: list[str] = []
        if intent is Intent.DOCUMENT_SUMMARY:
            scope = _summary_source_scope(question)
            retrieval = RetrievalResult()
            if scope != "corporate":
                retrieval = self._retriever.retrieve_attachment_summary(
                    ctx=ctx, conversation_id=conversation.id
                )
                if retrieval.has_evidence:
                    tools_used.append("private_attachment_summary")
            if scope in ("corporate", "mixed") or (scope == "auto" and not retrieval.has_evidence):
                corporate = self._retriever.retrieve(
                    ctx=ctx,
                    question=question,
                    authorized_categories=authorized_categories,
                    conversation_id=conversation.id,
                    include_private=False,
                    comparative=False,
                )
                tools_used.append("rag")
                retrieval = RetrievalResult(
                    evidences=(*corporate.evidences, *retrieval.evidences),
                    authorized_categories=corporate.authorized_categories,
                    fetched=corporate.fetched + retrieval.fetched,
                    after_dedup=corporate.after_dedup + retrieval.after_dedup,
                    best_score=max(corporate.best_score, retrieval.best_score),
                    used_private_scope=retrieval.used_private_scope,
                    truncated=corporate.truncated or retrieval.truncated,
                )
        else:
            retrieval = self._retriever.retrieve(
                ctx=ctx,
                question=question,
                authorized_categories=authorized_categories,
                conversation_id=conversation.id,
                comparative=comparative,
            )
            tools_used.append("rag")

        structured_results: tuple[StructuredEvidence, ...] = ()
        if intent in (Intent.STRUCTURED, Intent.MIXED):
            structured_results = self._run_structured(ctx=ctx, question=question)
            if structured_results:
                tools_used.append("structured_data")

        return retrieval, structured_results, tools_used

    @staticmethod
    def _build_scope_note(
        authorized_categories: frozenset[str], *, used_private_scope: bool
    ) -> str:
        """Nota de alcance que ve el sintetizador: categorias + adjunto privado."""
        scope_parts = sorted(authorized_categories)
        if used_private_scope:
            scope_parts.append("adjuntos privados de esta conversacion")
        return ", ".join(scope_parts) or "(solo alcance privado)"

    def _finish_answer(
        self,
        db: Session,
        *,
        ctx: UserContext,
        conversation: Conversation,
        routing: RoutingDecision,
        synthesis: SynthesisResult,
        retrieval: RetrievalResult,
        structured_results: tuple[StructuredEvidence, ...],
        tools_used: list[str],
        authorized_categories: frozenset[str],
        started: float,
    ) -> ChatOutcome:
        """Fase 10: persiste la respuesta, audita, resume si toca y arma el outcome."""
        latency_ms = int((time.perf_counter() - started) * 1000)
        basis: AnswerBasis = "insufficient" if not synthesis.grounding.grounded else classify_answer_basis(
            synthesis.answer, has_citations=bool(synthesis.cited_source_ids),
            insufficient=synthesis.grounding.declares_insufficiency,
        )
        stored = self._memory.append_message(
            db,
            conversation,
            role="assistant",
            content=synthesis.answer,
            model=synthesis.model,
            intent=str(routing.intent),
            answer_basis=basis,
            source_ids=synthesis.cited_source_ids,
            authorized_categories=tuple(sorted(authorized_categories)),
        )
        self._audit.record(
            db,
            AuditRecord(
                request_id=ctx.request_id,
                event_type="chat.answer",
                user_opaque_id=ctx.user_id,
                role_set_hash=ctx.role_set_hash,
                conversation_id=conversation.id,
                intent=str(routing.intent),
                selected_model=synthesis.model,
                selected_tools=tuple(tools_used),
                source_ids=synthesis.cited_source_ids,
                authorization_decision="ALLOW",
                latency_ms=latency_ms,
                status="ok" if synthesis.grounding.grounded else "degraded",
            ),
        )
        self._maybe_summarize(db, conversation)

        return ChatOutcome(
            answer=synthesis.answer,
            conversation_id=conversation.id,
            message_id=stored.id,
            model=synthesis.model,
            intent=str(routing.intent),
            evidences=retrieval.evidences,
            structured=structured_results,
            cited_source_ids=synthesis.cited_source_ids,
            latency_ms=latency_ms,
            grounded=synthesis.grounding.grounded and basis == "documented",
            answer_basis=basis,
            regenerated=synthesis.regenerated,
        )

    # ------------------------------------------------------------------------
    def _finish_identity(
        self,
        db: Session,
        *,
        ctx: UserContext,
        conversation: Conversation,
        started: float,
        intent: Intent = Intent.IDENTITY,
    ) -> ChatOutcome:
        """Identidad y capacidades reales, sin inferir datos ni integraciones."""
        answer = capabilities_answer() if intent is Intent.CAPABILITIES else IDENTITY_ANSWER
        latency_ms = int((time.perf_counter() - started) * 1000)
        stored = self._memory.append_message(
            db,
            conversation,
            role="assistant",
            content=answer,
            intent=str(intent),
            answer_basis="general",
        )
        self._audit.record(
            db,
            AuditRecord(
                request_id=ctx.request_id,
                event_type=f"chat.{intent}",
                user_opaque_id=ctx.user_id,
                role_set_hash=ctx.role_set_hash,
                conversation_id=conversation.id,
                intent=str(intent),
                authorization_decision="ALLOW",
                latency_ms=latency_ms,
            ),
        )
        return ChatOutcome(
            answer=answer,
            conversation_id=conversation.id,
            message_id=stored.id,
            model="",
            intent=str(intent),
            latency_ms=latency_ms,
            grounded=False,
            answer_basis="general",
        )

    def _finish_general(
        self,
        db: Session,
        *,
        ctx: UserContext,
        conversation: Conversation,
        question: str,
        intent: Intent,
        authorized_categories: frozenset[str],
        started: float,
    ) -> ChatOutcome:
        """Ruta de capacidad general: sin RAG, sin citas y sin datos internos."""
        if not get_settings().answer_allow_general_knowledge and intent is not Intent.CONVERSATIONAL:
            return self._finish_insufficient(
                db, ctx=ctx, conversation=conversation, intent=intent,
                started=started, authorized_categories=authorized_categories,
            )
        routing = self._model_policy.route(question, intent=intent)
        memory_context = self._memory.build_context(db, ctx, conversation, authorized_categories=authorized_categories)
        if intent in (Intent.GENERAL, Intent.DOCUMENTAL):
            # Una consulta general independiente (incluido el fallback sin
            # referencias internas) no necesita hechos del tema anterior.
            memory_context = None
        db.commit()  # No retener SQL durante la generacion general.
        synthesis = self._agent.answer_general(
            question=question,
            memory=memory_context,
            model_name=routing.model_name,
            choice=routing.choice,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        stored = self._memory.append_message(
            db,
            conversation,
            role="assistant",
            content=synthesis.answer,
            model=synthesis.model,
            intent=str(intent),
            answer_basis=synthesis.answer_basis,
        )
        self._audit.record(
            db,
            AuditRecord(
                request_id=ctx.request_id,
                event_type="chat.answer",
                user_opaque_id=ctx.user_id,
                role_set_hash=ctx.role_set_hash,
                conversation_id=conversation.id,
                intent=str(intent),
                selected_model=synthesis.model,
                authorization_decision="ALLOW",
                latency_ms=latency_ms,
                status="ok",
            ),
        )
        self._maybe_summarize(db, conversation)
        return ChatOutcome(
            answer=synthesis.answer,
            conversation_id=conversation.id,
            message_id=stored.id,
            model=synthesis.model,
            intent=str(intent),
            latency_ms=latency_ms,
            grounded=False,  # La capacidad general no tiene respaldo documental.
            answer_basis=synthesis.answer_basis,
        )

    @staticmethod
    def _allow_general_fallback(question: str, intent: Intent, categories: frozenset[str]) -> bool:
        """Una consulta sin fuentes no autoriza inventar un dato interno.

        Permite orientacion abierta; consultas a registros, politicas, adjuntos
        o derechos personales mantienen abstencion. No consulta categorias
        denegadas ni acredita existencia de datos privados.
        """
        if not get_settings().answer_allow_general_knowledge or not categories or intent is not Intent.DOCUMENTAL:
            return False
        if ModelPolicy.requires_internal_evidence(question):
            return False
        text = "".join(c for c in unicodedata.normalize("NFKD", question.casefold()) if not unicodedata.combining(c))
        internal = (
            "politica", "reglamento", "documento", "archivo", "adjunto", "segun", "empresa",
            "penoles", "fresnillo", "me corresponde", "nos corresponde", "mi salario", "mi sueldo",
            "salario de", "sueldo de", "salarios de", "expediente", "empleado", "empleados",
            "trabajador", "trabajadores", "base de datos", "datos personales", "confidencial",
        )
        return not any(marker in text for marker in internal)

    # ------------------------------------------------------------------------
    def _run_structured(
        self, *, ctx: UserContext, question: str
    ) -> tuple[StructuredEvidence, ...]:
        """Ejecuta la tool estructurada si el usuario tiene fuentes concedidas."""
        catalog = self._structured.available_entities(ctx)
        if not catalog:
            return ()
        plan = build_query_plan(
            question=question,
            catalog=catalog,
            llm=self._llm,
            model_name=self._model_policy.fast_model,
        )
        if plan is None:
            return ()
        try:
            return (self._structured.run(plan, ctx=ctx),)
        except (ForbiddenError, StructuredQueryRejectedError) as exc:
            # Un plan rechazado no aborta la conversacion: se continua solo con
            # RAG. El rechazo queda en el log de seguridad.
            logger.warning(
                "orchestrator.structured_rejected",
                extra={"error_code": str(exc.code), "user_opaque_id": ctx.user_id},
            )
            return ()
        except MatrixError as exc:
            logger.error("orchestrator.structured_error", extra={"error_code": str(exc.code)})
            return ()

    def _maybe_summarize(self, db: Session, conversation: Conversation) -> None:
        """Resumen incremental extractivo: sin otra llamada de inferencia interactiva.

        Conserva la ventana reciente con alcance verificable, no finge cubrir
        texto que no se leyo y guarda el cursor real para evitar el bucle >200.
        """
        if not self._memory.needs_summary(db, conversation.id):
            return
        messages = self._memory.list_messages(db, conversation.id, limit=30)
        scope = db.info.get("authorization_scope")
        visible = [m for m in messages if m.authorization_scope == scope]
        summary = "Contexto reciente (extractos, no evidencia):\n" + "\n".join(
            f"{m.role}: {m.content[:180]}" for m in visible
        )
        total = db.execute(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
        ).scalar_one()
        self._memory.store_summary(db, conversation.id, summary=summary, message_count=total)

    # ------------------------------------------------------------- terminaciones
    def _finish_no_evidence(
        self,
        db: Session,
        *,
        ctx: UserContext,
        conversation: Conversation,
        intent: Intent,
        started: float,
        authorized_categories: frozenset[str],
        question: str = "",
    ) -> ChatOutcome:
        """Fase 6 sin evidencia: distingue adjunto vacio, denegacion e insuficiencia.

        - ``DOCUMENT_SUMMARY``: un adjunto propio vacio o no indexado no es una
          denegacion de acceso; se reporta insuficiencia sin llamar al LLM.
        - Sin categorias autorizadas: puede existir un corpus restringido, pero
          no se confirma (los adjuntos propios ya se intentaron con su ACL).
        - En otro caso: evidencia insuficiente dentro del alcance autorizado.
        """
        if intent is Intent.DOCUMENT_SUMMARY:
            return self._finish_insufficient(
                db,
                ctx=ctx,
                conversation=conversation,
                intent=intent,
                started=started,
                authorized_categories=authorized_categories,
            )
        if not authorized_categories:
            return self._finish_denied(
                db, ctx=ctx, conversation=conversation, started=started
            )
        return self._finish_insufficient(
            db,
            ctx=ctx,
            conversation=conversation,
            intent=intent,
            started=started,
            authorized_categories=authorized_categories,
            answer=UNKNOWN_BENEFIT_ANSWER if self._model_policy.is_benefit_reference(question) else INSUFFICIENT_ANSWER,
        )

    def _finish_denied(
        self, db: Session, *, ctx: UserContext, conversation: Conversation, started: float
    ) -> ChatOutcome:
        latency_ms = int((time.perf_counter() - started) * 1000)
        stored = self._memory.append_message(
            db, conversation, role="assistant", content=DENIED_ANSWER, intent="denied", answer_basis="insufficient"
        )
        self._audit.record(
            db,
            AuditRecord(
                request_id=ctx.request_id,
                event_type="chat.denied",
                user_opaque_id=ctx.user_id,
                role_set_hash=ctx.role_set_hash,
                conversation_id=conversation.id,
                authorization_decision="DENY",
                latency_ms=latency_ms,
                status="denied",
            ),
        )
        return ChatOutcome(
            answer=DENIED_ANSWER,
            conversation_id=conversation.id,
            message_id=stored.id,
            model="",
            intent="denied",
            latency_ms=latency_ms,
            authorization_decision="DENY",
            answer_basis="insufficient",
        )

    def _finish_insufficient(
        self,
        db: Session,
        *,
        ctx: UserContext,
        conversation: Conversation,
        intent: Intent,
        started: float,
        authorized_categories: frozenset[str],
        answer: str = INSUFFICIENT_ANSWER,
    ) -> ChatOutcome:
        latency_ms = int((time.perf_counter() - started) * 1000)
        stored = self._memory.append_message(
            db,
            conversation,
            role="assistant",
            content=answer,
            answer_basis="insufficient",
            intent=str(intent),
            authorized_categories=tuple(sorted(authorized_categories)),
        )
        self._audit.record(
            db,
            AuditRecord(
                request_id=ctx.request_id,
                event_type="chat.insufficient_evidence",
                user_opaque_id=ctx.user_id,
                role_set_hash=ctx.role_set_hash,
                conversation_id=conversation.id,
                intent=str(intent),
                authorization_decision="ALLOW",
                latency_ms=latency_ms,
                status="insufficient_evidence",
            ),
        )
        return ChatOutcome(
            answer=answer,
            conversation_id=conversation.id,
            message_id=stored.id,
            model="",
            intent=str(intent),
            latency_ms=latency_ms,
            answer_basis="insufficient",
        )
