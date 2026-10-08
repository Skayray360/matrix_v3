# Creado por Aldo Garcia.
"""Generaciones visibles: Qdrant almacena, SQL activa despues de escritura completa."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import Document

# Contrato unico para la huella y el manifest SQL. La version 7 limita el solape
# al presupuesto restante y conserva una sola pagina/hoja en cada fragmento.
PIPELINE_VERSION = 7


def indexing_fingerprint(llm=None) -> str:
    settings = get_settings()
    revision = settings.llm_embedding_revision
    if llm is not None and hasattr(llm, "embedding_revision"):
        revision = llm.embedding_revision()
    values = {
        "pipeline": PIPELINE_VERSION,
        "embedding_overflow_policy": (
            "ollama_truncate_false" if settings.llm_embedding_provider == "ollama" else "runtime_defined"
        ),
        # v4 aplica el limite de filas/columnas durante la lectura de XLSX.
        # La huella obliga a reextraer documentos con bytes identicos para que
        # un indice anterior no conserve filas ahora fuera del limite.
        "extractor": 4,
        "model": settings.ollama_embedding_model,
        "revision": revision,
        "provider": settings.llm_embedding_provider,
        "endpoint": settings.ollama_base_url
        if settings.llm_embedding_provider == "ollama"
        else settings.llm_api_base_url,
        "dimension": settings.rag_embedding_dimension,
        "size": settings.rag_chunk_size_tokens,
        "overlap": settings.rag_chunk_overlap_tokens,
        "query_template": settings.llm_query_template,
        "document_template": settings.llm_document_template,
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def visible_candidates(candidates):
    """Solo generaciones activadas y documentos no borrados llegan al modelo."""
    ids = {str(item.payload.get("document_id", "")) for item in candidates}
    if not ids:
        return []
    with session_scope() as db:
        rows = db.execute(
            select(Document.id, Document.active_generation, Document.index_fingerprint).where(
                Document.id.in_(ids), Document.deleted_at.is_(None), Document.status == "indexed"
            )
        ).all()
        active = {row.id: (row.active_generation, row.index_fingerprint) for row in rows}
    return [
        item
        for item in candidates
        if active.get(str(item.payload.get("document_id")))
        == (item.payload.get("generation"), item.payload.get("index_fingerprint"))
        and item.payload.get("generation")
    ]


def active_filter(query_filter):
    """Aplica generacion ANTES del ranking: candidatos nuevos no desplazan al activo."""
    from qdrant_client import models

    query = select(Document.active_generation).where(
        Document.status == "indexed", Document.deleted_at.is_(None), Document.active_generation.is_not(None)
    )
    for condition in query_filter.must or []:
        field = getattr(condition, "key", "")
        column = {
            "scope": Document.scope,
            "category": Document.category,
            "owner_user_id": Document.owner_user_id,
            "conversation_id": Document.conversation_id,
            "index_fingerprint": Document.index_fingerprint,
        }.get(field)
        if column is None:
            continue
        match = getattr(condition, "match", None)
        if hasattr(match, "value"):
            query = query.where(column == match.value)
        elif hasattr(match, "any"):
            query = query.where(column.in_(match.any))
    with session_scope() as db:
        generations = list(db.execute(query).scalars())
    return models.Filter(
        must=[
            query_filter,
            models.FieldCondition(key="generation", match=models.MatchAny(any=generations or ["__none__"])),
        ]
    )
