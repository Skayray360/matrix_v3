# Creado por Aldo Garcia.
"""Diagnostico RAG de solo lectura; puede ejecutarse con Matrix abierto.

Lee metadatos SQL y el inventario de modelos de Ollama. No abre Qdrant,
no genera embeddings/respuestas, no modifica documentos ni permisos.
No imprime prompts, contenido documental, credenciales ni identificadores
de sesiones, conversaciones, fuentes o hashes.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path
from uuid import uuid4


def select_only_guard(_conn, _cursor, statement, _parameters, _context, _executemany):
    """Impide DML, DDL y otros comandos en la conexion de diagnostico."""
    if not statement.lstrip().upper().startswith("SELECT "):
        raise RuntimeError("El diagnostico solo permite SELECT.")


def dispatcher_snapshot(db) -> dict:
    """Estado de la reserva sin adquirirla, renovarla ni publicar su identidad."""
    from app.agents.chat_queue import LEASE_NAME
    from app.agents.process_owner import owner_state
    from app.common.ids import utcnow_naive
    from app.database.models import JobLock

    lease = db.get(JobLock, LEASE_NAME)
    if lease is None:
        return {"present": False, "active": False, "remaining_seconds": 0, "owner_state": "none"}
    remaining = max(0, math.ceil((lease.expires_at - utcnow_naive()).total_seconds()))
    state = owner_state(lease.locked_by) if remaining else "expired"
    return {
        "present": True, "active": remaining > 0, "remaining_seconds": remaining,
        "owner_state": state,
        "reclaim_on_start": remaining == 0 or state == "dead",
        "detail": (
            "Reserva desconocida o antigua: no se atribuye a un proceso ni se elimina automaticamente."
            if state == "unknown" else
            "El inicio puede recuperar la reserva del proceso local terminado."
            if state == "dead" else
            "El propietario identificado sigue vivo; use detener.bat."
            if state == "alive" else "No hay reserva vigente."
        ),
    }


def database_snapshot(db, *, username: str, fingerprint: str | None) -> dict:
    """Usa el mismo motor de permisos del chat, sin crear sesiones ni auditoria."""
    from sqlalchemy import func, literal, select

    from app.authorization.policy import get_policy_engine
    from app.database.models import AuditEvent, Document, User

    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None:
        return {"user_found": False, "documents": [], "recent_chat_events": []}
    policy = get_policy_engine()
    context = policy.build_context(
        db, user=user, session_id=str(uuid4()), request_id=str(uuid4()),
    )
    categories = policy.effective_categories(context) if user.is_active else frozenset()
    ready_generation = Document.active_generation.is_not(None)
    compatible = (
        Document.index_fingerprint == fingerprint if fingerprint is not None else literal(None)
    )
    groups = db.execute(
        select(
            Document.category, Document.status, ready_generation, compatible,
            func.count(Document.id), func.sum(Document.chunk_count),
        ).where(
            Document.scope == "corporate", Document.deleted_at.is_(None),
            Document.category.in_(categories),
        ).group_by(Document.category, Document.status, ready_generation, compatible)
    ).all()
    documents = [{
        "category": row[0], "status": row[1], "has_active_generation": bool(row[2]),
        "fingerprint_compatible": None if row[3] is None else bool(row[3]),
        "documents": int(row[4] or 0), "chunks": int(row[5] or 0),
    } for row in groups]
    # Solo columnas diagnosticas. No seleccionar resource, source_ids,
    # role_set_hash, conversation_id ni request_id.
    events = db.execute(
        select(
            AuditEvent.event_type, AuditEvent.status, AuditEvent.selected_model,
            AuditEvent.selected_tools, AuditEvent.created_at,
        ).where(
            AuditEvent.user_opaque_id == context.user_id,
            AuditEvent.event_type.like("chat.%"),
        ).order_by(AuditEvent.created_at.desc()).limit(10)
    ).all()
    known_tools = {"rag", "structured_data", "private_attachment_summary"}
    return {
        "user_found": True,
        "user_active": bool(user.is_active),
        "roles": sorted(context.roles),
        "permissions": sorted(context.permissions),
        "effective_categories": sorted(categories),
        "documents": documents,
        "authorized_document_count": sum(row["documents"] for row in documents),
        "authorized_chunk_count": sum(row["chunks"] for row in documents),
        "compatible_indexed_chunk_count": (
            sum(row["chunks"] for row in documents if row["status"] == "indexed"
                and row["has_active_generation"] and row["fingerprint_compatible"])
            if fingerprint is not None else None
        ),
        "recent_chat_events": [{
            "event_type": row[0], "status": row[1], "model": row[2],
            "tools": [tool for tool in (row[3] or []) if isinstance(tool, str) and tool in known_tools],
            "created_at_utc": row[4].isoformat() if row[4] else None,
        } for row in events],
    }


def collect(root: Path, *, username: str = "Matrix") -> dict:
    """Emite errores por clase/componente, nunca mensajes arbitrarios del driver."""
    report: dict = {
        "read_only": True, "qdrant_opened": False,
        "inference_executed": False, "semantic_retrieval_tested": False,
        "errors": [],
    }
    root = root.resolve()
    if not (root / "backend" / "app" / "config").is_dir():
        report["errors"].append({"component": "installation", "error_type": "InvalidProjectRoot"})
        return report
    os.chdir(root)
    sys.path.insert(0, str(root / "backend"))
    try:
        import app
        from app.config import get_settings
        from app.config import settings as settings_module

        settings = get_settings()
        report["installation"] = {
            "root": str(root), "version": app.__version__,
            "app_module": str(Path(app.__file__).resolve()),
            "settings_module": str(Path(settings_module.__file__).resolve()),
        }
        report["configuration"] = {
            "generation_models": list(dict.fromkeys((settings.ollama_fast_model, settings.ollama_deep_model))),
            "embedding_model": settings.ollama_embedding_model,
            "embedding_provider": settings.llm_embedding_provider,
            "evidence_mode": settings.answer_evidence_mode,
            "minimum_similarity": settings.rag_min_similarity,
            "embedding_dimension": settings.rag_embedding_dimension,
            "corporate_collection": settings.rag_collection_corporate,
            "private_collection": settings.rag_collection_private,
            "qdrant_mode": str(settings.qdrant_mode),
            "top_k": settings.rag_top_k, "fetch_k": settings.rag_fetch_k,
        }
    except Exception as exc:
        report["errors"].append({"component": "configuration", "error_type": type(exc).__name__})
        return report

    fingerprint = None
    try:
        from app.llm.ollama_client import get_ollama_client
        from app.rag.index_manifest import indexing_fingerprint

        # ModelClient.embedding_revision() consulta /api/tags para Ollama.
        # No llamar ping()/list_models(): otros proveedores pueden sondear inferencia.
        fingerprint = indexing_fingerprint(get_ollama_client())
        report["embedding_revision_verified"] = True
    except Exception as exc:
        report["embedding_revision_verified"] = False
        report["errors"].append({"component": "embedding_inventory", "error_type": type(exc).__name__})

    try:
        from sqlalchemy import event
        from sqlalchemy.orm import Session

        from app.database.engine import get_engine

        # Conexion independiente del backend, solo SELECT, sin commit.
        with get_engine().connect() as connection:
            event.listen(connection, "before_cursor_execute", select_only_guard)
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as db:
                report["database"] = database_snapshot(db, username=username, fingerprint=fingerprint)
                report["dispatcher"] = dispatcher_snapshot(db)
    except Exception as exc:
        report["errors"].append({"component": "database", "error_type": type(exc).__name__})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Raiz de la instalacion Matrix RH")
    parser.add_argument("--username", default="Matrix", help="Cuenta cuyos permisos se revisan")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report = collect(args.root, username=args.username)
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
