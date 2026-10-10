# Creado por Aldo Garcia.
"""Integridad vectorial y limpieza: ausencia real distinta de fallo de lectura."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models
from sqlalchemy import select

from app.common.errors import QdrantUnavailableError
from app.common.ids import new_id
from app.config import get_settings
from app.database.models import Document
from app.rag.schemas import Chunk, ChunkMetadata
from app.rag.vector_store import VectorStore

pytestmark = pytest.mark.unit


@pytest.fixture
def indexed(manifest_db, monkeypatch):
    settings = get_settings()
    store = VectorStore(client=QdrantClient(location=":memory:"))

    @contextmanager
    def scope():
        with manifest_db() as db:
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    monkeypatch.setattr("app.database.engine.session_scope", scope)

    def publish(*, scope_name="corporate", count=3, owner="owner", conversation="conversation"):
        document_id = new_id()
        generation = new_id()
        private = scope_name == "conversation"
        chunks = [Chunk(
            f"Contenido sintetico {index} para verificar integridad.",
            ChunkMetadata(
                chunk_id=new_id(), document_id=document_id, document_sha256="a" * 64,
                filename="fixture.md", relative_path="prestaciones/fixture.md",
                category="__private__" if private else "prestaciones", subpath="", mime_type="text/markdown",
                page_or_sheet="", section="Integridad", chunk_index=index,
                embedding_model=settings.ollama_embedding_model, embedding_dimension=settings.rag_embedding_dimension,
                ingestion_version="8", generation=generation, index_fingerprint="active-fingerprint",
                scope=scope_name, owner_user_id=owner if private else None,
                conversation_id=conversation if private else None, sensitivity="private" if private else "internal",
            ),
        ) for index in range(count)]
        vector = [1.0] + [0.0] * (settings.rag_embedding_dimension - 1)
        store.upsert_chunks(chunks, [vector for _ in chunks])
        with scope() as db:
            document = db.get(Document, document_id)
            document.chunk_count = count
            document.index_cleanup_pending = True
        kwargs = {
            "document_id": document_id, "generation": generation, "scope": scope_name,
            "index_fingerprint": "active-fingerprint", "expected_chunks": count, "document_sha256": "a" * 64,
            "category": None if private else "prestaciones", "owner_user_id": owner if private else None,
            "conversation_id": conversation if private else None, "sensitivity": "private" if private else "internal",
        }
        return SimpleNamespace(chunks=chunks, kwargs=kwargs, collection=store.collection_for(scope_name), vector=vector)

    yield SimpleNamespace(store=store, publish=publish, scope=scope, settings=settings)
    store.close()


@pytest.mark.parametrize("scope", ["corporate", "conversation"])
def test_missing_collection_cleanup_does_not_create_a_false_index(indexed, scope):
    target = indexed.publish(scope_name=scope)
    indexed.store._client.delete_collection(target.collection)
    assert indexed.store.cleanup_generations() == 1
    assert indexed.store.cleanup_generations() == 0
    assert not indexed.store._client.collection_exists(target.collection)
    assert not indexed.store.has_complete_generation(**target.kwargs)
    # La cache del mismo proceso no impide recrear despues la coleccion perdida.
    indexed.store.upsert_chunks(target.chunks, [target.vector for _ in target.chunks])
    assert indexed.store.has_complete_generation(**target.kwargs)


def test_cleanup_missing_collection_without_active_generation_is_also_empty(indexed):
    target = indexed.publish()
    indexed.store._client.delete_collection(target.collection)
    with indexed.scope() as db:
        db.get(Document, target.kwargs["document_id"]).active_generation = None
    assert indexed.store.cleanup_generations() == 1
    assert not indexed.store._client.collection_exists(target.collection)


@pytest.mark.parametrize("method", ["collection_exists", "get_collection", "count", "scroll"])
def test_vector_read_failures_are_typed_errors_and_never_absence(indexed, monkeypatch, method):
    target = indexed.publish()
    before = indexed.store._client.count(collection_name=target.collection, exact=True).count

    def failed(*args, **kwargs):
        raise OSError("fixture read failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(indexed.store._client, method, failed)
        with pytest.raises(QdrantUnavailableError) as error:
            indexed.store.has_complete_generation(**target.kwargs)
        assert isinstance(error.value.__cause__, OSError)
    assert indexed.store._client.count(collection_name=target.collection, exact=True).count == before
    with indexed.scope() as db:
        assert db.get(Document, target.kwargs["document_id"]).index_cleanup_pending


@pytest.mark.parametrize("method", ["collection_exists", "delete"])
def test_cleanup_failure_preserves_pending_flags_and_active_vectors(indexed, monkeypatch, method):
    target = indexed.publish()

    def failed(*args, **kwargs):
        raise OSError("fixture cleanup failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(indexed.store._client, method, failed)
        with pytest.raises(QdrantUnavailableError):
            indexed.store.cleanup_generations()
    with indexed.scope() as db:
        assert db.get(Document, target.kwargs["document_id"]).index_cleanup_pending
    assert indexed.store.has_complete_generation(**target.kwargs)


def test_later_cleanup_read_failure_rolls_back_all_pending_flags(indexed, monkeypatch):
    first = indexed.publish()
    second = indexed.publish(scope_name="conversation")
    original = indexed.store._client.collection_exists
    calls = 0

    def fail_second(collection):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture second collection failure")
        return original(collection)

    with monkeypatch.context() as patcher:
        patcher.setattr(indexed.store._client, "collection_exists", fail_second)
        with pytest.raises(QdrantUnavailableError):
            indexed.store.cleanup_generations()
    with indexed.scope() as db:
        assert all(document.index_cleanup_pending for document in db.scalars(select(Document)))
    assert indexed.store.has_complete_generation(**first.kwargs)
    assert indexed.store.has_complete_generation(**second.kwargs)


@pytest.mark.parametrize("replacement", [
    {"category": "nomina"}, {"scope": "conversation"}, {"document_sha256": "b" * 64},
    {"embedding_model": "another-model"}, {"embedding_dimension": 12},
    {"allowed_groups": ["foreign-group"]}, {"allowed_roles": ["foreign-role"]},
    {"sensitivity": "public"}, {"owner_user_id": "foreign-owner"}, {"text": ""},
    {"source_id": "wrong/source#0"}, {"chunk_index": True},
])
def test_same_count_with_incoherent_metadata_is_not_a_complete_generation(indexed, replacement):
    target = indexed.publish()
    indexed.store._client.set_payload(
        collection_name=target.collection, payload=replacement, points=[target.chunks[0].metadata.chunk_id],
    )
    assert indexed.store._client.count(collection_name=target.collection, exact=True).count == 3
    assert not indexed.store.has_complete_generation(**target.kwargs)


def test_vectorless_point_does_not_count_as_retrievable_content(indexed):
    target = indexed.publish()
    indexed.store._client.delete_vectors(
        collection_name=target.collection, vectors=[""], points=[target.chunks[0].metadata.chunk_id],
    )
    assert not indexed.store.has_complete_generation(**target.kwargs)


def test_zero_vector_cannot_satisfy_cosine_index_integrity(indexed):
    target = indexed.publish()
    chunk = target.chunks[0]
    indexed.store._client.upsert(collection_name=target.collection, points=[models.PointStruct(
        id=chunk.metadata.chunk_id, vector=[0.0] * indexed.settings.rag_embedding_dimension,
        payload=chunk.metadata.to_payload(chunk.text),
    )])
    assert not indexed.store.has_complete_generation(**target.kwargs)


def test_complete_generation_is_checked_across_multiple_scroll_pages(indexed):
    target = indexed.publish(count=130)
    assert indexed.store.has_complete_generation(**target.kwargs)
    indexed.store._client.delete(
        collection_name=target.collection, points_selector=[target.chunks[-1].metadata.chunk_id],
    )
    assert not indexed.store.has_complete_generation(**target.kwargs)


def test_cleanup_preserves_active_generation_and_other_private_owners(indexed):
    first = indexed.publish(scope_name="conversation", owner="first-owner")
    second = indexed.publish(scope_name="conversation", owner="second-owner")
    obsolete = [replace(chunk, metadata=replace(chunk.metadata, chunk_id=new_id(), generation="obsolete"))
                for chunk in first.chunks]
    # Escritura vectorial directa: una generacion obsoleta no se publica en SQL.
    indexed.store._client.upsert(collection_name=first.collection, points=[
        models.PointStruct(id=chunk.metadata.chunk_id, vector=first.vector, payload=chunk.metadata.to_payload(chunk.text))
        for chunk in obsolete
    ])
    assert indexed.store.cleanup_generations() == 2
    assert indexed.store._client.count(collection_name=first.collection, exact=True).count == 6
    assert indexed.store.has_complete_generation(**first.kwargs)
    assert indexed.store.has_complete_generation(**second.kwargs)


@pytest.mark.parametrize("dimension,distance", [(4, models.Distance.COSINE), (768, models.Distance.DOT)])
def test_collection_geometry_mismatch_is_an_error_without_destroying_it(indexed, dimension, distance):
    target = indexed.publish()
    indexed.store._client.delete_collection(target.collection)
    indexed.store._client.create_collection(
        collection_name=target.collection, vectors_config=models.VectorParams(size=dimension, distance=distance),
    )
    with pytest.raises(QdrantUnavailableError):
        indexed.store.has_complete_generation(**target.kwargs)
    assert indexed.store._client.get_collection(target.collection).config.params.vectors.size == dimension
