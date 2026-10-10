# Creado por Aldo Garcia.
"""Caso de uso compartido de chat y publicacion con permisos vigentes.

HTTP y el dispatcher llaman al mismo servicio. La reautenticacion se recibe
como funcion para que el caso de uso no dependa de cookies, rutas ni FastAPI.
La respuesta se confirma solo si la sesion, ownership y operacion siguen validos.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from threading import Event
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.agents.orchestrator import Orchestrator
from app.authorization.context import UserContext
from app.authorization.policy import get_policy_engine
from app.common.answers import AnswerBasis
from app.common.chat_failures import chat_failure_code
from app.common.errors import ForbiddenError, ValidationFailedError
from app.database.engine import get_sessionmaker
from app.database.models import ChatOperation, Conversation
from app.llm.provider import inference_deadline
from app.llm.request_control import check_inference_control
from app.memory.service import MemoryService, authorization_fingerprint

_orchestrator: Orchestrator | None = None
_memory = MemoryService()


class ChatExecutionResult(BaseModel):
    """Resultado publico del caso de uso, independiente del transporte."""

    conversation_id: str
    message_id: str
    answer: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    intent: str
    grounded: bool
    answer_basis: AnswerBasis = "documented"
    latency_ms: int


def get_orchestrator() -> Orchestrator:
    """Construye clientes solo cuando un turno necesita el orquestador."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


def reset_orchestrator() -> None:
    global _orchestrator
    _orchestrator = None


def authorization_scope(db: Session, ctx: UserContext) -> str:
    """Huella comun a la aceptacion y la publicacion del turno."""
    return authorization_fingerprint(db, ctx, get_policy_engine().effective_categories(ctx))


def execute_chat(
    db: Session, *, ctx: UserContext, conversation: Conversation, message: str,
    expected_scope: str, operation_id: str | None, reauthorize: Callable[[Session], UserContext],
    cancel_event: Event | None = None, absolute_deadline: float | None = None,
) -> ChatExecutionResult:
    """Ejecuta un turno y descarta respuestas tardias, canceladas o revocadas."""
    try:
        with inference_deadline(cancel_event=cancel_event, absolute_deadline=absolute_deadline):
            if operation_id:
                pending = db.get(ChatOperation, operation_id)
                if pending is None:
                    raise ValidationFailedError("Solicitud no registrada.")
                db.refresh(pending)
                if pending.status != "running":
                    raise ValidationFailedError("Solicitud cancelada o expirada.")
            outcome = get_orchestrator().handle_chat(db, ctx=ctx, conversation=conversation, message=message)
            response = ChatExecutionResult(
                conversation_id=outcome.conversation_id, message_id=outcome.message_id,
                answer=outcome.answer, sources=outcome.public_sources(), intent=outcome.intent,
                grounded=outcome.grounded, latency_ms=outcome.latency_ms,
                answer_basis=outcome.answer_basis,
            )
            check_inference_control()
            # Una transaccion nueva ve revocaciones/cancelaciones ocurridas durante IA.
            with get_sessionmaker()() as check_db:
                current = reauthorize(check_db)
                if authorization_scope(check_db, current) != expected_scope:
                    raise ForbiddenError("Sus permisos cambiaron durante la consulta. Vuelva a consultar.")
                _memory.get_owned_conversation(check_db, current, outcome.conversation_id)
                operation = check_db.get(ChatOperation, operation_id) if operation_id else None
                if operation_id and (operation is None or operation.status != "running"):
                    raise ValidationFailedError("Solicitud cancelada o expirada.")
            if response.sources:
                _memory.set_source_details(
                    db, ctx=current, message_id=outcome.message_id, sources=response.sources,
                )
            check_inference_control()
            if operation_id:
                changed = db.execute(
                    update(ChatOperation)
                    .where(ChatOperation.id == operation_id, ChatOperation.status == "running")
                    .values(
                        status="completed", response=response.model_dump(), error_code=None,
                        message=None, session_id=None,
                    )
                )
                if changed.rowcount != 1:
                    raise ValidationFailedError("Solicitud cancelada o expirada.")
            # SQL tambien consume plazo; una escritura tardia sigue pendiente
            # y puede revertirse antes de confirmar el asistente y la operacion.
            check_inference_control()
        db.commit()
        return response
    except Exception as exc:
        db.rollback()
        if operation_id:
            with suppress(Exception), get_sessionmaker()() as failure_db:
                failure_db.execute(
                    update(ChatOperation)
                    .where(ChatOperation.id == operation_id, ChatOperation.status == "running")
                    .values(status="failed", error_code=chat_failure_code(exc), message=None, session_id=None)
                )
                failure_db.commit()
        raise
