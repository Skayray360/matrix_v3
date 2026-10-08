# Creado por Aldo Garcia.
"""Chat compatible y envio aceptado con estado consultable sin bloquear HTTP."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.agents.chat_queue import expire_stalled, get_chat_queue, operation_status
from app.agents.chat_service import authorization_scope as _scope
from app.agents.chat_service import execute_chat
from app.api.deps import get_db, get_user_context, require_csrf
from app.api.schemas import ChatRequest, ChatResponse, ChatSubmitRequest
from app.authorization.context import UserContext
from app.common.errors import (
    ForbiddenError,
    NotFoundError,
    OllamaUnavailableError,
    RateLimitedError,
    ValidationFailedError,
)
from app.common.ids import sha256_text, utcnow_naive
from app.config import get_settings
from app.database.models import ChatOperation
from app.memory.service import MemoryService
from app.security.rate_limit import get_rate_limiter

router = APIRouter(tags=["chat"])
_memory = MemoryService()


def _expire_stalled_operation(db: Session, operation: ChatOperation) -> None:
    expire_stalled(db)
    db.refresh(operation)


def _rate_limit(ctx: UserContext) -> None:
    if not get_rate_limiter().check(
        f"chat:{sha256_text(ctx.user_id)}", limit=get_settings().rate_limit_chat_per_minute
    ).allowed:
        raise RateLimitedError()


def _existing(
    db: Session, ctx: UserContext, operation_id: str | None, request_hash: str, expected_scope: str
) -> ChatOperation | None:
    operation = db.get(ChatOperation, operation_id) if operation_id else None
    if operation is not None:
        _expire_stalled_operation(db, operation)
        if operation.request_hash != request_hash:
            raise ValidationFailedError("La clave de solicitud ya se utilizo con otro contenido.")
        if operation.authorization_scope != expected_scope:
            raise ForbiddenError()
        _memory.get_owned_conversation(db, ctx, operation.conversation_id)
    return operation


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_csrf)])
def chat(
    payload: ChatRequest, request: Request, db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_user_context),
) -> ChatResponse:
    """Ruta compatible: mismos cupos que submit, sin crear una espera HTTP ilimitada."""
    operation_id = sha256_text(f"{ctx.user_id}|{payload.client_request_id}") if payload.client_request_id else None
    request_hash = sha256_text(f"{payload.conversation_id}|{payload.message}")
    queue = get_chat_queue()
    # La autenticacion escribe last_seen_at. Liberar esa conexion ANTES del
    # mutex evita que una rafaga agote el pool esperando al dispatcher.
    db.commit()
    with queue.lock:
        expected_scope = _scope(db, ctx)
        existing = _existing(db, ctx, operation_id, request_hash, expected_scope)
        if existing:
            if existing.status == "completed":
                return ChatResponse.model_validate(existing.response)
            raise OllamaUnavailableError(
                f"La solicitud ya esta registrada: {existing.status}. Revise su estado antes de reenviar."
            )
        _rate_limit(ctx)
        queue.ensure_owner(db)
        expire_stalled(db)
        # Autorizar el ID antes de consultar actividad: no revelar si una
        # conversacion ajena existe o tiene solicitudes pendientes.
        conversation = (
            _memory.get_owned_conversation(db, ctx, payload.conversation_id)
            if payload.conversation_id else None
        )
        queue.require_no_pending(db, ctx, payload.conversation_id)
        if conversation is None:
            conversation = _memory.create_conversation(db, ctx)
        resources = queue.reserve(user_id=ctx.user_id, conversation_id=conversation.id)
        try:
            if operation_id:
                db.add(ChatOperation(
                    id=operation_id, user_id=ctx.user_id, conversation_id=conversation.id,
                    request_hash=request_hash, authorization_scope=expected_scope,
                    status="running", started_at=utcnow_naive(),
                ))
            db.commit()
        except Exception:
            resources.close()
            raise
    with resources:
        result = execute_chat(
            db, ctx=ctx, conversation=conversation, message=payload.message,
            expected_scope=expected_scope, operation_id=operation_id,
            reauthorize=lambda check_db: get_user_context(request, check_db),
        )
        return ChatResponse.model_validate(result.model_dump())


@router.post("/chat/submit", status_code=202, dependencies=[Depends(require_csrf)])
def submit_chat(
    payload: ChatSubmitRequest, db: Session = Depends(get_db), ctx: UserContext = Depends(get_user_context),
) -> dict:
    """202 significa guardada en SQL; un rechazo de capacidad nunca se registra como aceptado."""
    operation_id = sha256_text(f"{ctx.user_id}|{payload.client_request_id}")
    request_hash = sha256_text(f"{payload.conversation_id}|{payload.message}")
    queue = get_chat_queue()
    # La autenticacion escribe last_seen_at. Liberar esa conexion ANTES del
    # mutex evita que una rafaga agote el pool esperando al dispatcher.
    db.commit()
    with queue.lock:
        expected_scope = _scope(db, ctx)
        existing = _existing(db, ctx, operation_id, request_hash, expected_scope)
        if existing is not None:
            return operation_status(db, existing)
        _rate_limit(ctx)
        queue.ensure_owner(db)
        expire_stalled(db)
        # Autorizar el ID antes de consultar actividad: no revelar si una
        # conversacion ajena existe o tiene solicitudes pendientes.
        conversation = (
            _memory.get_owned_conversation(db, ctx, payload.conversation_id)
            if payload.conversation_id else None
        )
        queue.require_no_pending(db, ctx, payload.conversation_id)
        if conversation is None:
            conversation = _memory.create_conversation(db, ctx)
        operation = ChatOperation(
            id=operation_id, user_id=ctx.user_id, conversation_id=conversation.id,
            request_hash=request_hash, authorization_scope=expected_scope, status="queued",
            message=payload.message, session_id=ctx.session_id, request_id=ctx.request_id,
        )
        return queue.submit(db, operation)


@router.get("/chat/requests/{client_request_id}")
def chat_status(
    client_request_id: str, db: Session = Depends(get_db), ctx: UserContext = Depends(get_user_context)
) -> dict:
    operation = db.get(ChatOperation, sha256_text(f"{ctx.user_id}|{client_request_id}"))
    if operation is None:
        raise NotFoundError()
    _expire_stalled_operation(db, operation)
    _memory.get_owned_conversation(db, ctx, operation.conversation_id)
    if operation.authorization_scope != _scope(db, ctx):
        raise ForbiddenError()
    return operation_status(db, operation)


@router.post("/chat/requests/{client_request_id}/cancel", dependencies=[Depends(require_csrf)])
def cancel_chat(
    client_request_id: str, db: Session = Depends(get_db), ctx: UserContext = Depends(get_user_context)
) -> dict:
    operation_id = sha256_text(f"{ctx.user_id}|{client_request_id}")
    if db.get(ChatOperation, operation_id) is None:
        raise NotFoundError()
    db.execute(
        update(ChatOperation)
        .where(ChatOperation.id == operation_id, ChatOperation.status.in_(("queued", "running")))
        .values(status="cancelled", message=None, session_id=None)
    )
    db.commit()
    return {"ok": True}
