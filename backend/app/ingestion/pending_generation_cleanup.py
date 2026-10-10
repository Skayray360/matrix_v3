# Creado por Aldo Garcia.
"""Compensaciones vectoriales durables sin conservar nombres ni contenido."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.ids import utcnow_naive
from app.common.logging import get_logger
from app.database.models import Document, IngestionJob
from app.rag.schemas import SCOPE_CONVERSATION, SCOPE_CORPORATE

logger = get_logger(__name__)
CLEANUP_JOB_TYPE = "generation_cleanup"


def _payload(values: dict[str, str]) -> dict[str, str]:
    result = {key: values.get(key, "") for key in ("document_id", "generation", "scope")}
    if (any(not isinstance(value, str) or not value or len(value) > 128 for value in result.values())
            or result["scope"] not in {SCOPE_CORPORATE, SCOPE_CONVERSATION}):
        raise ValueError("Identificadores de compensacion invalidos.")
    return result


def persist_pending_generation_cleanup(db: Session, values: dict[str, str]) -> bool:
    """Llamar DESPUES del rollback de ingesta; no reutiliza su transaccion."""
    try:
        payload = _payload(values)
        job_id = str(uuid5(NAMESPACE_URL, "matrix-rh:cleanup:" + ":".join(payload.values())))
        if db.get(IngestionJob, job_id) is None:
            db.add(IngestionJob(
                id=job_id, job_type=CLEANUP_JOB_TYPE, status="pending",
                trigger_source="ingestion_compensation", started_at=utcnow_naive(), stats=payload,
            ))
        db.commit()
        logger.warning("ingestion.generation_cleanup_pending")
        return True
    except Exception as exc:  # noqa: BLE001 - preservar el error original de ingesta
        db.rollback()
        logger.error("ingestion.generation_cleanup_persistence_failed", extra={"error_type": type(exc).__name__})
        return False


def retry_pending_generation_cleanup(db: Session, *, limit: int = 8) -> dict[str, int]:
    """Reintentos acotados; no borra nunca una generacion actualmente publicada."""
    from app.rag.vector_store import get_vector_store

    if not 1 <= limit <= 8:
        raise ValueError("El lote de compensacion debe estar entre 1 y 8.")
    job_ids = list(db.scalars(select(IngestionJob.id).where(
        IngestionJob.job_type == CLEANUP_JOB_TYPE, IngestionJob.status == "pending",
    ).order_by(func.coalesce(IngestionJob.finished_at, IngestionJob.started_at), IngestionJob.id).limit(limit)))
    counts = {"completed": 0, "failed": 0, "preserved_active": 0}
    for job_id in job_ids:
        try:
            job = db.get(IngestionJob, job_id)
            payload = _payload(job.stats or {})
            active = db.scalar(select(Document.active_generation).where(
                Document.id == payload["document_id"], Document.scope == payload["scope"],
                Document.status == "indexed", Document.deleted_at.is_(None),
            ))
            if active == payload["generation"]:
                counts["preserved_active"] += 1
            else:
                get_vector_store().delete_generation(
                    payload["document_id"], generation=payload["generation"], scope=payload["scope"],
                )
            job.status = "completed"
            job.finished_at = utcnow_naive()
            job.error_message = None
            db.commit()
            counts["completed"] += 1
        except Exception as exc:  # noqa: BLE001 - otro candidato puede limpiarse
            db.rollback()
            counts["failed"] += 1
            try:
                job = db.get(IngestionJob, job_id)
                if job is not None:
                    # Al final de la cola: un fallo repetido no bloquea otros jobs.
                    job.finished_at = utcnow_naive()
                    job.error_message = type(exc).__name__
                    db.commit()
            except Exception as persistence_error:  # noqa: BLE001
                db.rollback()
                logger.error("ingestion.generation_cleanup_persistence_failed", extra={
                    "error_type": type(persistence_error).__name__,
                })
            logger.warning("ingestion.generation_cleanup_retry_failed", extra={"error_type": type(exc).__name__})
    return counts
