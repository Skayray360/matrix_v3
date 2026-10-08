# Creado por Aldo Garcia.
"""Servicio de auditoria.

Registra quien pregunto, con que rol, que intencion se detecto, que herramientas
y modelo se usaron, que fuentes se citaron, cual fue la decision de autorizacion,
cuanto tardo y como termino.

Lo que **no** se registra (seccion 19): contrasenas, client secrets, bearer
tokens, llaves privadas, documentos completos y prompts completos. Del contenido
solo se guardan identificadores de fuente, nunca el texto recuperado.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.context import UserContext
from app.common.ids import new_id, utcnow_naive
from app.common.logging import get_logger
from app.database.models import AuditEvent

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Datos de un evento auditable."""

    request_id: str
    event_type: str
    user_opaque_id: str | None = None
    role_set_hash: str | None = None
    conversation_id: str | None = None
    intent: str | None = None
    selected_model: str | None = None
    selected_tools: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    authorization_decision: str | None = None
    resource: str | None = None
    latency_ms: int | None = None
    status: str = "ok"
    error_code: str | None = None


class AuditService:
    """Persiste eventos de auditoria y los emite tambien al log estructurado."""

    def record(self, db: Session, record: AuditRecord) -> str:
        event = AuditEvent(
            id=new_id(),
            request_id=record.request_id,
            event_type=record.event_type,
            user_opaque_id=record.user_opaque_id,
            role_set_hash=record.role_set_hash,
            conversation_id=record.conversation_id,
            intent=record.intent,
            selected_model=record.selected_model,
            selected_tools=list(record.selected_tools) or None,
            source_ids=list(record.source_ids) or None,
            authorization_decision=record.authorization_decision,
            resource=record.resource,
            latency_ms=record.latency_ms,
            status=record.status,
            error_code=record.error_code,
            created_at=utcnow_naive(),
        )
        db.add(event)
        db.flush()

        logger.info(
            f"audit.{record.event_type}",
            extra={
                "request_id": record.request_id,
                "user_opaque_id": record.user_opaque_id,
                "role_set_hash": record.role_set_hash,
                "conversation_id": record.conversation_id,
                "intent": record.intent,
                "selected_model": record.selected_model,
                "selected_tools": list(record.selected_tools),
                "source_ids": list(record.source_ids),
                "authorization_decision": record.authorization_decision,
                "latency_ms": record.latency_ms,
                "result_status": record.status,
                "error_code": record.error_code,
            },
        )
        return event.id

    def record_from_context(
        self,
        db: Session,
        ctx: UserContext,
        *,
        event_type: str,
        **fields: Any,
    ) -> str:
        """Atajo que rellena identidad y hash de roles desde el contexto."""
        return self.record(
            db,
            AuditRecord(
                request_id=ctx.request_id,
                event_type=event_type,
                user_opaque_id=ctx.user_id,
                role_set_hash=ctx.role_set_hash,
                **fields,
            ),
        )

    def recent_for_user(self, db: Session, user_id: str, *, limit: int = 50) -> list[AuditEvent]:
        return list(
            db.execute(
                select(AuditEvent)
                .where(AuditEvent.user_opaque_id == user_id)
                .order_by(AuditEvent.created_at.desc())
                .limit(limit)
            ).scalars().all()
        )


_service: AuditService | None = None


def get_audit_service() -> AuditService:
    global _service
    if _service is None:
        _service = AuditService()
    return _service
