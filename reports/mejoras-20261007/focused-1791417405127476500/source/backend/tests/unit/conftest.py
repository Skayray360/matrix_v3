# Creado por Aldo Garcia.
"""Manifest SQL real en las pruebas aisladas de Qdrant (sin servicios externos)."""

from contextlib import contextmanager
from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.database.models import Base, Document
from app.rag.index_manifest import indexing_fingerprint
from app.rag.vector_store import VectorStore


@compiles(MEDIUMTEXT, "sqlite")
def compile_mediumtext(_type, _compiler, **_kwargs):
    return "TEXT"


@pytest.fixture
def manifest_db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def scope():
        with factory() as db:
            yield db
            db.commit()

    monkeypatch.setattr("app.rag.index_manifest.session_scope", scope)
    original = VectorStore.upsert_chunks

    def publish(self, chunks, vectors):
        prepared = [
            replace(
                chunk,
                metadata=replace(
                    chunk.metadata,
                    generation=chunk.metadata.generation or "fixture-generation",
                    index_fingerprint=chunk.metadata.index_fingerprint or indexing_fingerprint(),
                ),
            )
            for chunk in chunks
        ]
        count = original(self, prepared, vectors)
        with scope() as db:
            for chunk in prepared:
                m = chunk.metadata
                row = db.get(Document, m.document_id)
                if row is None:
                    row = Document(
                        id=m.document_id,
                        scope=m.scope,
                        category=m.category,
                        filename=m.filename,
                        mime_type=m.mime_type,
                        sha256=m.document_sha256,
                        status="indexed",
                        owner_user_id=m.owner_user_id,
                        conversation_id=m.conversation_id,
                    )
                    db.add(row)
                row.active_generation = m.generation
                row.index_fingerprint = m.index_fingerprint
        return count

    monkeypatch.setattr(VectorStore, "upsert_chunks", publish)
    yield factory
    engine.dispose()
