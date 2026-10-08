# Creado por Aldo Garcia.
"""Rutas de conversaciones y adjuntos privados.

Todas las rutas resuelven la conversacion mediante
``MemoryService.get_owned_conversation``, que devuelve 404 ante una conversacion
ajena. No existe ninguna ruta que acepte un ``user_id`` del cliente.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_user_context, require_csrf
from app.api.schemas import (
    AttachmentUploadResponse,
    ConversationDetailResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    DocumentStatusResponse,
    MessageResponse,
)
from app.audit.service import AuditRecord, get_audit_service
from app.authorization.context import UserContext
from app.authorization.policy import get_policy_engine
from app.common.errors import ConversationCleanupPendingError, NotFoundError, ValidationFailedError
from app.common.logging import get_logger
from app.config import get_settings
from app.database.models import Document
from app.ingestion.service import IngestionService
from app.memory.service import MemoryService, authorization_fingerprint
from app.rag.schemas import SCOPE_CONVERSATION
from app.security.upload_guard import UploadValidation, read_bounded, validate_upload

logger = get_logger(__name__)
router = APIRouter(tags=["conversations"])

_memory = MemoryService()
_ingestion: IngestionService | None = None


def get_ingestion_service() -> IngestionService:
    global _ingestion
    if _ingestion is None:
        _ingestion = IngestionService()
    return _ingestion


def _document_response(document: Document) -> DocumentStatusResponse:
    return DocumentStatusResponse(
        id=document.id,
        filename=document.filename,
        status=document.status,
        chunk_count=document.chunk_count,
        scope=document.scope,
        category=document.category,
        error_message=document.error_message,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


@router.get("/conversations", response_model=list[ConversationSummaryResponse])
def list_conversations(
    db: Session = Depends(get_db), ctx: UserContext = Depends(get_user_context)
) -> list[ConversationSummaryResponse]:
    return [
        ConversationSummaryResponse(
            id=c.id, title=c.title, created_at=c.created_at, updated_at=c.updated_at
        )
        for c in _memory.list_conversations(db, ctx)
    ]


@router.post(
    "/conversations",
    response_model=ConversationSummaryResponse,
    dependencies=[Depends(require_csrf)],
)
def create_conversation(
    payload: CreateConversationRequest,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_user_context),
) -> ConversationSummaryResponse:
    conversation = _memory.create_conversation(db, ctx, title=payload.title)
    return ConversationSummaryResponse(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_user_context),
    before_seq: int | None = Query(default=None, ge=1),
) -> ConversationDetailResponse:
    conversation = _memory.get_owned_conversation(db, ctx, conversation_id)
    page = _memory.list_messages(db, conversation.id, before_seq=before_seq)
    categories = get_policy_engine().effective_categories(ctx)
    scope = authorization_fingerprint(db, ctx, categories)
    messages = [m for m in page if _memory.message_visible(m, categories, scope)]
    attachments = (
        db.execute(
            select(Document).where(
                Document.conversation_id == conversation.id,
                Document.owner_user_id == ctx.user_id,
                Document.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    )

    return ConversationDetailResponse(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        next_before_seq=page[0].seq if len(page) == 200 else None,
        messages=[
            MessageResponse(
                id=m.id,
                role=m.role,
                content=m.content,
                model=m.model,
                intent=m.intent,
                answer_basis=m.answer_basis,
                created_at=m.created_at,
                sources=[{"source_id": s} for s in (m.source_ids or [])],
            )
            for m in messages
        ],
        attachments=[_document_response(a).model_dump() for a in attachments],
    )


@router.delete("/conversations/{conversation_id}", dependencies=[Depends(require_csrf)])
def delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_user_context),
) -> dict[str, bool]:
    """Borra el contenido SQL y confirma una purga privada reintentable."""
    conversation = _memory.delete_conversation(db, ctx, conversation_id)
    removed = IngestionService.delete_conversation_documents(db, conversation.id)
    get_audit_service().record_from_context(
        db,
        ctx,
        event_type="conversation.deleted",
        conversation_id=conversation.id,
        resource=f"documents_removed:{removed}",
    )
    # Qdrant y filesystem no participan en la transaccion SQL. Confirmar primero
    # oculta contenido y cancela workers; fallos posteriores permiten reintento.
    db.commit()
    try:
        service = get_ingestion_service()
    except Exception as exc:  # noqa: BLE001 - baja durable aun sin cliente vectorial
        logger.warning("conversation.cleanup_pending", extra={"error_type": type(exc).__name__})
        raise ConversationCleanupPendingError(detail=type(exc).__name__) from exc
    service.purge_conversation_documents(db, conversation.id)
    return {"ok": True}


@router.post(
    "/conversations/{conversation_id}/attachments",
    response_model=AttachmentUploadResponse,
    dependencies=[Depends(require_csrf)],
)
def upload_attachments(
    conversation_id: str,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_user_context),
) -> AttachmentUploadResponse:
    """Sube adjuntos privados de la conversacion.

    Estos documentos viven en el namespace ``user + conversation`` y **no**
    entran a la base corporativa. Un usuario restringido puede analizar su propio
    archivo sin que eso le permita eludir las ACL del conocimiento corporativo.
    """
    settings = get_settings()
    conversation = _memory.get_owned_conversation(db, ctx, conversation_id)

    if len(files) > settings.upload_max_files_per_request:
        raise ValidationFailedError(f"Maximo {settings.upload_max_files_per_request} archivos por peticion.")

    # Se valida TODO el lote antes de tocar ninguna infraestructura: un archivo
    # rechazado debe devolver 415/413 aunque el almacen vectorial este caido, y
    # no debe quedar un archivo del lote indexado y el resto no.
    validated: list[tuple[bytes, UploadValidation]] = []
    for upload in files:
        data = read_bounded(upload.file)
        validated.append((data, validate_upload(data, filename=upload.filename or "documento")))

    stored_bytes, stored_count = db.execute(
        select(func.coalesce(func.sum(Document.size_bytes), 0), func.count(Document.id)).where(
            Document.owner_user_id == ctx.user_id,
            Document.scope == SCOPE_CONVERSATION,
            or_(Document.deleted_at.is_(None), Document.storage_path.is_not(None)),
        )
    ).one()
    if stored_bytes + sum(len(data) for data, _ in validated) > settings.upload_max_total_bytes:
        raise ValidationFailedError("La carga excede su cuota de almacenamiento.")
    if stored_count + len(validated) > settings.upload_max_documents_per_user:
        raise ValidationFailedError("La carga excede su cuota de documentos.")
    from app.agents.chat_queue import get_chat_queue

    # El mismo proceso que posee los cupos de chat debe controlar la ingesta.
    # La validacion del archivo sigue ocurriendo antes de acceder a servicios.
    get_chat_queue().ensure_owner(db)
    db.commit()
    service = get_ingestion_service()
    results: list[DocumentStatusResponse] = []
    for data, validation in validated:
        outcome = service.ingest_conversation_attachment(
            db,
            data=data,
            display_name=validation.safe_display_name,
            internal_filename=validation.internal_filename,
            mime_type=validation.mime_type,
            owner_user_id=ctx.user_id,
            conversation_id=conversation.id,
        )
        document = db.get(Document, outcome.document_id)
        if document is not None:
            results.append(_document_response(document))
        get_audit_service().record(
            db,
            AuditRecord(
                request_id=ctx.request_id,
                event_type="attachment.uploaded",
                user_opaque_id=ctx.user_id,
                role_set_hash=ctx.role_set_hash,
                conversation_id=conversation.id,
                resource=validation.safe_display_name,
                authorization_decision="ALLOW",
                status=outcome.status,
            ),
        )

    return AttachmentUploadResponse(documents=results)


@router.get("/documents/{document_id}/status", response_model=DocumentStatusResponse)
def document_status(
    document_id: str,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_user_context),
) -> DocumentStatusResponse:
    """Estado de procesamiento de un documento.

    Un adjunto privado solo lo consulta su dueno. Un documento corporativo solo
    se consulta si la categoria esta autorizada, para no revelar por esta via la
    existencia de documentos restringidos.
    """
    from app.authorization.policy import get_policy_engine

    document = db.get(Document, document_id)
    if document is None or document.deleted_at is not None:
        raise NotFoundError("Documento no encontrado.")

    if document.scope == SCOPE_CONVERSATION:
        if document.owner_user_id != ctx.user_id:
            raise NotFoundError("Documento no encontrado.")
    else:
        decision = get_policy_engine().can_read_category(ctx, document.category or "")
        if not decision.allowed:
            logger.warning(
                "authorization.document_status_denied",
                extra={"user_opaque_id": ctx.user_id, "authorization_decision": "DENY"},
            )
            raise NotFoundError("Documento no encontrado.")

    return _document_response(document)
