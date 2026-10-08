# Creado por Aldo Garcia.
"""Purga reintentable de bajas y retencion de contenido con plazos explicitos.

Las bajas solicitadas por el usuario se completan siempre. Eliminar contenido de
conversaciones vivas, resultados o auditoria exige RETENTION_ENABLED y un plazo
configurado por el responsable de los datos. Las claves idempotentes y las bajas
de conversaciones se conservan vacias: borrarlas permitiria republicar trabajos.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import delete, null, or_, select, update
from sqlalchemy.orm import Session

from app.auth.sessions import purge_expired_sessions
from app.common.errors import ConversationCleanupPendingError
from app.common.ids import utcnow_naive
from app.common.logging import get_logger
from app.config import get_settings
from app.database.models import AuditEvent, ChatOperation, Conversation, OidcLoginState
from app.ingestion.service import IngestionService
from app.memory.service import MemoryService

logger = get_logger(__name__)


@dataclass(slots=True)
class MaintenanceStats:
    conversations_scrubbed: int = 0
    documents_removed: int = 0
    pending_cleanup: int = 0
    expired_sessions_removed: int = 0
    expired_login_states_removed: int = 0
    operations_scrubbed: int = 0
    audit_events_removed: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _batch_limit(batch_size: int | None) -> int:
    limit = get_settings().retention_batch_size if batch_size is None else batch_size
    if not 1 <= limit <= 1000:
        raise ValueError("El lote de purga debe estar entre 1 y 1000.")
    return limit


def _conversation_candidates(db: Session, *, limit: int, deleted_only: bool) -> list[str]:
    # purged_at=NULL cubre instalaciones anteriores, adjuntos tombstoned y
    # vectores huerfanos sin fila Document. Una limpieza completa no se repite.
    conditions = [(Conversation.deleted_at.is_not(None)) & (Conversation.purged_at.is_(None))]
    settings = get_settings()
    if not deleted_only and settings.retention_enabled and settings.retention_conversation_days is not None:
        cutoff = utcnow_naive() - timedelta(days=settings.retention_conversation_days)
        conditions.append((Conversation.deleted_at.is_(None)) & (Conversation.updated_at < cutoff))
    return list(db.scalars(
        select(Conversation.id).where(or_(*conditions))
        .order_by(Conversation.updated_at, Conversation.id).limit(limit)
    ))


def preview_maintenance(
    db: Session, *, batch_size: int | None = None, deleted_only: bool = False
) -> dict[str, int | bool]:
    """Solo lectura: cuenta el lote visible sin abrir Qdrant ni tocar archivos."""
    limit = _batch_limit(batch_size)
    settings = get_settings()
    return {
        "apply": False,
        "batch_limit": limit,
        "conversation_candidates": len(_conversation_candidates(db, limit=limit, deleted_only=deleted_only)),
        "retention_enabled": bool(settings.retention_enabled and not deleted_only),
    }


def run_maintenance(
    db: Session, *, batch_size: int | None = None, deleted_only: bool = False,
    ingestion: IngestionService | None = None,
) -> MaintenanceStats:
    """Procesa un lote con commits cortos; nunca confirma bytes antes de la baja.

    Un error externo deja contenido inaccesible y cuota reservada. Se registra
    solo el tipo de error, y los otros identificadores del lote siguen avanzando.
    El llamador no debe mezclar escrituras ajenas con esta unidad de trabajo.
    """
    limit = _batch_limit(batch_size)
    settings = get_settings()
    stats = MaintenanceStats()
    memory = MemoryService()
    candidates = _conversation_candidates(db, limit=limit, deleted_only=deleted_only)
    db.commit()  # Cerrar el snapshot antes de las lecturas actuales FOR UPDATE.
    for conversation_id in candidates:
        conversation = MemoryService._lock_conversation(db, conversation_id, allow_deleted=True)
        if conversation.deleted_at is None:
            if deleted_only or not settings.retention_enabled or settings.retention_conversation_days is None:
                db.rollback()
                continue
            cutoff = utcnow_naive() - timedelta(days=settings.retention_conversation_days)
            if memory.expire_conversation(db, conversation_id, cutoff=cutoff) is None:
                db.rollback()
                continue
        else:
            memory.purge_deleted_conversation(db, conversation_id)
        IngestionService.delete_conversation_documents(db, conversation_id)
        db.commit()  # La baja es durable aunque cliente/vector/filesystem fallen.
        stats.conversations_scrubbed += 1
        # Cliente perezoso: una pasada sin bajas nunca abre el almacen embedded.
        try:
            service = ingestion or IngestionService()
        except Exception as exc:  # noqa: BLE001 - mantener la baja y reintentar
            stats.pending_cleanup += 1
            logger.warning("retention.cleanup_pending", extra={"error_type": type(exc).__name__})
            continue
        try:
            stats.documents_removed += service.purge_conversation_documents(db, conversation_id)
            db.commit()
        except ConversationCleanupPendingError:
            db.rollback()
            stats.pending_cleanup += 1
    stats.expired_sessions_removed = purge_expired_sessions(db, limit=limit)
    login_ids = list(db.scalars(
        select(OidcLoginState.id).where(OidcLoginState.expires_at <= utcnow_naive())
        .order_by(OidcLoginState.expires_at).limit(limit)
    ))
    if login_ids:
        stats.expired_login_states_removed = len(login_ids)
        db.execute(delete(OidcLoginState).where(OidcLoginState.id.in_(login_ids)))
    if settings.retention_enabled and not deleted_only:
        if settings.retention_operation_days is not None:
            cutoff = utcnow_naive() - timedelta(days=settings.retention_operation_days)
            operation_ids = list(db.scalars(
                select(ChatOperation.id).where(
                    ChatOperation.created_at < cutoff,
                    ChatOperation.status.not_in(("queued", "running")),
                    or_(ChatOperation.response.is_not(None), ChatOperation.message.is_not(None),
                        ChatOperation.session_id.is_not(None), ChatOperation.request_id.is_not(None)),
                ).order_by(ChatOperation.created_at).limit(limit)
            ))
            if operation_ids:
                db.execute(update(ChatOperation).where(ChatOperation.id.in_(operation_ids))
                           .values(status="expired", response=null(), message=None, session_id=None, request_id=None))
                stats.operations_scrubbed = len(operation_ids)
        if settings.retention_audit_days is not None:
            cutoff = utcnow_naive() - timedelta(days=settings.retention_audit_days)
            event_ids = list(db.scalars(
                select(AuditEvent.id).where(AuditEvent.created_at < cutoff)
                .order_by(AuditEvent.created_at).limit(limit)
            ))
            if event_ids:
                db.execute(delete(AuditEvent).where(AuditEvent.id.in_(event_ids)))
                stats.audit_events_removed = len(event_ids)
    db.commit()
    return stats
