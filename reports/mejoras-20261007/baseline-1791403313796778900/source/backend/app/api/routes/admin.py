# Creado por Aldo Garcia.
"""Rutas administrativas de conocimiento y diagnostico.

Protegidas por permisos explicitos (``knowledge.admin`` / ``diagnostics.read``).
El diagnostico expone **parametros**, nunca secretos: ni DSN, ni claves, ni
system prompt. Un administrador de negocio administra conocimiento y roles, no
infraestructura (requisito 35).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_csrf, require_permission
from app.api.schemas import AdminIngestRequest, AdminIngestResponse, DocumentStatusResponse
from app.audit.service import get_audit_service
from app.authorization.categories import get_registry, refresh_registry, validate_category_name
from app.authorization.context import (
    PERM_DIAGNOSTICS_READ,
    PERM_KNOWLEDGE_ADMIN,
    UserContext,
)
from app.authorization.policy import get_policy_engine
from app.common.errors import ForbiddenError, ValidationFailedError
from app.common.logging import get_logger
from app.config import get_settings
from app.database.models import Document
from app.ingestion.reconciler import reconcile_with_lock
from app.ingestion.service import IngestionService
from app.rag.schemas import SCOPE_CORPORATE
from app.security.upload_guard import resolve_within, sanitize_display_name, validate_upload

logger = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_ingestion: IngestionService | None = None


def _official_relative_path(category: str, filename: str) -> str:
    """Devuelve la ruta oficial de conocimiento para una categoria validada."""
    if category == "general":
        return f"general/{filename}"
    return f"especializadas/{category}/{filename}"


def _require_effective_category(ctx: UserContext, category: str) -> None:
    """Impide que un administrador funcional publique fuera de su dominio."""
    if category not in get_policy_engine().effective_categories(ctx):
        raise ForbiddenError()


def _service() -> IngestionService:
    global _ingestion
    if _ingestion is None:
        _ingestion = IngestionService()
    return _ingestion


@router.post("/knowledge/reconcile", response_model=AdminIngestResponse, dependencies=[Depends(require_csrf)])
def reconcile(
    payload: AdminIngestRequest,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(require_permission(PERM_KNOWLEDGE_ADMIN)),
) -> AdminIngestResponse:
    """Lanza una reconciliacion incremental del knowledge root."""
    stats = reconcile_with_lock(db, trigger="admin_api", force=payload.force)
    get_audit_service().record_from_context(
        db, ctx, event_type="admin.reconcile", authorization_decision="ALLOW"
    )
    if stats is None:
        return AdminIngestResponse(
            started=False, detail="Ya hay una reconciliacion en curso."
        )
    return AdminIngestResponse(started=True, stats=stats.as_dict())


@router.post(
    "/knowledge/documents",
    response_model=DocumentStatusResponse,
    dependencies=[Depends(require_csrf)],
)
def upload_corporate_document(
    category: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(require_permission(PERM_KNOWLEDGE_ADMIN)),
) -> DocumentStatusResponse:
    """Publica un documento como conocimiento corporativo permanente.

    Requiere permiso administrativo: es exactamente la barrera que impide que un
    usuario restringido promueva un adjunto privado a una categoria corporativa
    para eludir la ACL (requisito 6.3).
    """
    settings = get_settings()
    safe_category = validate_category_name(category)
    _require_effective_category(ctx, safe_category)

    from app.security.upload_guard import read_bounded

    data = read_bounded(file.file)
    validation = validate_upload(data, filename=file.filename or "documento")

    safe_filename = sanitize_display_name(validation.safe_display_name)
    relative = _official_relative_path(safe_category, safe_filename)
    # resolve_within vuelve a validar que la ruta no escapa del knowledge root.
    absolute = resolve_within(settings.knowledge_root_path, relative)
    target_dir = absolute.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(data)

    outcome = _service().ingest_corporate_file(
        db, absolute_path=absolute, relative_path=relative, category=safe_category, force=True
    )
    refresh_registry()

    document = db.get(Document, outcome.document_id)
    if document is None:  # pragma: no cover
        raise ValidationFailedError("No fue posible registrar el documento.")

    get_audit_service().record_from_context(
        db,
        ctx,
        event_type="admin.document_published",
        resource=relative,
        authorization_decision="ALLOW",
    )
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


@router.get("/knowledge/summary")
def knowledge_summary(
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(require_permission(PERM_KNOWLEDGE_ADMIN)),
) -> dict[str, Any]:
    """Resumen de documentos indexados por categoria."""
    effective_categories = get_policy_engine().effective_categories(ctx)
    if not effective_categories:
        return {"categories": [], "known_categories": []}
    rows = db.execute(
        select(Document.category, func.count(Document.id), func.sum(Document.chunk_count))
        .where(
            Document.scope == SCOPE_CORPORATE,
            Document.deleted_at.is_(None),
            Document.category.in_(effective_categories),
        )
        .group_by(Document.category)
    ).all()
    registry = get_registry()
    return {
        "categories": [
            {
                "category": row[0],
                "documents": int(row[1] or 0),
                "chunks": int(row[2] or 0),
                "sensitivity": registry.get(str(row[0])).sensitivity,
                "wildcard_eligible": registry.is_wildcard_eligible(str(row[0])),
            }
            for row in rows
        ],
        "known_categories": sorted(effective_categories),
    }


@router.get("/audit/recent")
def recent_audit(
    limit: int = 50,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(require_permission(PERM_DIAGNOSTICS_READ)),
) -> dict[str, Any]:
    """Ultimos eventos de auditoria del propio usuario.

    Deliberadamente acotado al usuario que consulta: la auditoria completa de
    terceros es competencia de la plataforma de logs, no de la UI de chat. Se
    devuelven metadatos (modelo, herramientas, decision, fuentes citadas), nunca
    el contenido de los mensajes.
    """
    from app.database.models import AuditEvent

    rows = db.execute(
        select(AuditEvent)
        .where(AuditEvent.user_opaque_id == ctx.user_id)
        .order_by(AuditEvent.created_at.desc())
        .limit(max(1, min(limit, 200)))
    ).scalars().all()
    return {
        "events": [
            {
                "request_id": row.request_id,
                "event_type": row.event_type,
                "intent": row.intent,
                "selected_model": row.selected_model,
                "selected_tools": row.selected_tools or [],
                "source_ids": row.source_ids or [],
                "authorization_decision": row.authorization_decision,
                "latency_ms": row.latency_ms,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.get("/diagnostics")
def diagnostics(
    ctx: UserContext = Depends(require_permission(PERM_DIAGNOSTICS_READ)),
) -> dict[str, Any]:
    """Parametros efectivos del sistema. Sin secretos, por diseno.

    Es la vista que exige la seccion 9: los parametros del RAG deben ser
    auditables desde el propio sistema y no quedar enterrados en constantes.
    """
    settings = get_settings()
    from app.structured_data.sources import get_source_catalog

    return {
        "app_env": str(settings.app_env),
        "auth_provider": str(settings.auth_provider),
        "local_test_auth_enabled": settings.local_test_auth_enabled,
        "rag": settings.rag_parameters_snapshot(),
        "vector_store": {
            "engine": settings.rag_vector_store,
            "mode": str(settings.qdrant_mode),
            "collections": [settings.rag_collection_corporate, settings.rag_collection_private],
        },
        "models": {
            "fast": settings.ollama_fast_model,
            "deep": settings.ollama_deep_model,
            "embedding": settings.ollama_embedding_model,
            "embedding_dimension": settings.ollama_embedding_dimension,
        },
        "structured_sources": get_source_catalog().status_report(),
        "categories": sorted(get_registry().known()),
    }
