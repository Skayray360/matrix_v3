# Creado por Aldo Garcia.
"""Recuperacion observable SQL/Qdrant con archivos y embeddings sinteticos.

Se ejecuta el reconciliador publico, un manifest SQLite y Qdrant local reales.
La perdida o alteracion de los vectores se introduce despues de una ingesta
correcta; no se simula la nueva comprobacion de integridad del indice.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config.settings import Settings
from app.database.models import Base, Conversation, Document, DocumentVersion, JobLock, User
from app.ingestion.loaders import extract_document
from app.ingestion.reconciler import reconcile_with_lock
from app.ingestion.service import IngestionService
from app.rag.retriever import Retriever
from app.rag.schemas import SCOPE_CONVERSATION, SCOPE_CORPORATE
from app.rag.vector_store import VectorStore
from tests.conftest import make_context
from tests.unit.test_rag_pipeline_isolated import FakeEmbeddingClient

pytestmark = pytest.mark.unit

CORPORATE_TEXT = (
    "# Convenio sintetico\n\n"
    "## Vacaciones\n\nEl convenio de vacaciones otorga veinte dias al personal.\n\n"
    "## Solicitud\n\nLa solicitud de vacaciones se entrega al responsable del area.\n\n"
    "## Registro\n\nEl registro de vacaciones se conserva en el expediente del empleado.\n"
)
PRIVATE_TEXT = (
    "# Convenio privado sintetico\n\n"
    "## Vacaciones\n\nEl convenio privado de vacaciones incluye el codigo PRIVADO_PROPIO.\n\n"
    "## Solicitud\n\nLa solicitud privada se registra solo en esta conversacion.\n\n"
    "## Seguimiento\n\nEl seguimiento privado corresponde al propietario del adjunto.\n"
)


@pytest.fixture
def recovery_env(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        rag_knowledge_root=str(tmp_path / "data" / "knowledge"),
        upload_storage_root=str(tmp_path / "uploads"),
        qdrant_path=tmp_path / "qdrant",
        rag_chunk_size_tokens=64,
        rag_chunk_overlap_tokens=8,
        rag_sync_stability_seconds=0,
        rag_min_similarity=0,
    )
    settings.knowledge_root_path.mkdir(parents=True)
    monkeypatch.setattr("app.ingestion.knowledge_layout.PROJECT_ROOT", tmp_path)
    for module in (
        "app.ingestion.service", "app.ingestion.reconciler", "app.ingestion.knowledge_layout",
        "app.rag.index_manifest", "app.rag.retriever", "app.rag.vector_store",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    # Extraer Markdown sigue siendo real; solo se evita abrir el subproceso
    # de aislamiento, que tiene su propia cobertura en la suite de extraccion.
    monkeypatch.setattr("app.ingestion.service.extract_document", extract_document)
    engine = create_engine(f"sqlite:///{tmp_path / 'manifest.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def scope():
        with factory() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr("app.rag.index_manifest.session_scope", scope)
    monkeypatch.setattr("app.database.engine.session_scope", scope)
    with factory() as db:
        env = SimpleNamespace(
            db=db, factory=factory, settings=settings, root=tmp_path,
            llm=FakeEmbeddingClient(),
            store=VectorStore(client=QdrantClient(path=str(settings.qdrant_storage_path))),
        )
        monkeypatch.setattr("app.ingestion.service.get_vector_store", lambda: env.store)
        monkeypatch.setattr("app.ingestion.service.get_ollama_client", lambda: env.llm)
        monkeypatch.setattr("app.rag.vector_store.get_vector_store", lambda: env.store)
        env.service = IngestionService(store=env.store, llm=env.llm)

        def restart():
            env.store.close()
            env.store = VectorStore(client=QdrantClient(path=str(settings.qdrant_storage_path)))
            env.service = IngestionService(store=env.store, llm=env.llm)

        env.restart = restart
        db.add_all([
            User(id="owner", username="synthetic-owner", display_name="Synthetic", auth_source="local_test"),
            User(id="other", username="synthetic-other", display_name="Synthetic", auth_source="local_test"),
        ])
        db.flush()
        db.add_all([
            Conversation(id="chat", user_id="owner", title="Sintetico propio"),
            Conversation(id="another-chat", user_id="owner", title="Sintetico separado"),
            Conversation(id="foreign-chat", user_id="other", title="Sintetico ajeno"),
        ])
        db.commit()
        try:
            yield env
        finally:
            env.store.close()
    engine.dispose()


def _reconcile(env):
    stats = reconcile_with_lock(env.db, trigger="synthetic_recovery_acceptance")
    assert stats is not None
    env.db.expire_all()
    return stats


def _corporate(env):
    path = env.settings.knowledge_root_path / "prestaciones" / "convenio.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CORPORATE_TEXT * 3, encoding="utf-8")
    stats = _reconcile(env)
    assert stats.new_documents == 1 and not stats.failures
    row = env.db.scalar(select(Document).where(Document.scope == SCOPE_CORPORATE))
    assert row is not None and row.chunk_count >= 3
    return row


def _private(env, *, owner="owner", conversation="chat", text=PRIVATE_TEXT):
    result = env.service.ingest_conversation_attachment(
        env.db, data=(text * 3).encode("utf-8"), display_name="convenio-privado.md",
        internal_filename="same-basename.md", mime_type="text/markdown",
        owner_user_id=owner, conversation_id=conversation,
    )
    env.db.commit()
    row = env.db.get(Document, result.document_id)
    assert row is not None and row.chunk_count >= 3
    return row


def _points(env, scope):
    points, cursor = env.store._client.scroll(
        collection_name=env.store.collection_for(scope), limit=1000,
        with_payload=True, with_vectors=True,
    )
    assert cursor is None
    return points


def _document_points(env, row):
    return [
        point for point in _points(env, row.scope)
        if point.payload["document_id"] == row.id
        and point.payload["generation"] == row.active_generation
    ]


def _visible(env, *, owner="owner", conversation=None, categories=frozenset({"prestaciones"})):
    ctx = make_context(user_id=owner, wildcard=False, categories=categories)
    return Retriever(store=env.store, llm=env.llm).retrieve(
        question="convenio vacaciones solicitud registro", ctx=ctx,
        authorized_categories=categories, conversation_id=conversation,
    ).evidences


def _snapshot(row):
    return (
        row.id, row.sha256, row.scope, row.category, row.owner_user_id,
        row.conversation_id, row.active_generation, row.index_fingerprint,
        row.status, row.chunk_count, row.storage_path,
    )


@pytest.mark.parametrize("cleanup_pending", (False, True))
@pytest.mark.parametrize("damage", ("missing_collection", "empty_collection", "one_missing_point"))
def test_full_reconcile_recovers_unchanged_sources_with_missing_vectors(recovery_env, damage, cleanup_pending):
    env = recovery_env
    row = _corporate(env)
    document_id, previous_generation = row.id, row.active_generation
    original_bytes = (env.settings.knowledge_root_path / "prestaciones/convenio.md").read_bytes()
    expected_chunks = row.chunk_count
    row.index_cleanup_pending = cleanup_pending
    env.db.commit()
    collection = env.store.collection_for(SCOPE_CORPORATE)
    if damage in {"missing_collection", "empty_collection"}:
        env.store._client.delete_collection(collection)
        if damage == "empty_collection":
            env.store._client.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(size=768, distance=models.Distance.COSINE),
            )
    else:
        point = _document_points(env, row)[0]
        env.store._client.delete(
            collection_name=collection, wait=True,
            points_selector=models.PointIdsList(points=[point.id]),
        )
    # El instalador abre el indice en un proceso nuevo, con cache vacia.
    env.restart()
    embedding_calls = len(env.llm.calls)
    stats = _reconcile(env)
    recovered = env.db.get(Document, document_id)
    assert not stats.failures
    assert (stats.updated_documents, stats.unchanged_documents, stats.new_documents) == (1, 0, 0)
    assert len(env.llm.calls) > embedding_calls
    assert recovered.active_generation != previous_generation
    assert recovered.status == "indexed" and recovered.chunk_count == expected_chunks
    active = _document_points(env, recovered)
    assert len(active) == expected_chunks
    assert {point.payload["chunk_index"] for point in active} == set(range(expected_chunks))
    assert {item.document_id for item in _visible(env)} == {document_id}
    assert env.db.scalar(select(func.count()).select_from(Document)) == 1
    assert env.db.scalar(select(func.count()).select_from(JobLock)) == 0
    assert (env.settings.knowledge_root_path / "prestaciones/convenio.md").read_bytes() == original_bytes


def test_missing_collection_recovers_without_restarting_cached_store(recovery_env):
    env = recovery_env
    row = _corporate(env)
    document_id = row.id
    env.store._client.delete_collection(env.store.collection_for(SCOPE_CORPORATE))
    stats = _reconcile(env)
    assert not stats.failures and stats.updated_documents == 1
    assert {item.document_id for item in _visible(env)} == {document_id}


@pytest.mark.parametrize("corruption", ("duplicate_chunk_index", "wrong_fingerprint", "wrong_document_sha"))
def test_equal_point_count_does_not_hide_an_incomplete_generation(recovery_env, corruption):
    env = recovery_env
    row = _corporate(env)
    previous_generation = row.active_generation
    points = sorted(_document_points(env, row), key=lambda point: point.payload["chunk_index"])
    replacement = {
        "duplicate_chunk_index": {"chunk_index": points[0].payload["chunk_index"]},
        "wrong_fingerprint": {"index_fingerprint": "synthetic-stale-fingerprint"},
        "wrong_document_sha": {"document_sha256": "0" * 64},
    }[corruption]
    env.store._client.set_payload(
        collection_name=env.store.collection_for(SCOPE_CORPORATE),
        payload=replacement, points=[points[1].id], wait=True,
    )
    assert len(_document_points(env, row)) == row.chunk_count
    stats = _reconcile(env)
    assert not stats.failures and stats.updated_documents == 1 and not stats.unchanged_documents
    assert row.active_generation != previous_generation
    active = _document_points(env, row)
    assert {point.payload["chunk_index"] for point in active} == set(range(row.chunk_count))
    assert all(point.payload["document_sha256"] == row.sha256 for point in active)
    assert all(point.payload["index_fingerprint"] == row.index_fingerprint for point in active)


def test_complete_corporate_and_private_indices_remain_idempotent(recovery_env):
    env = recovery_env
    corporate = _corporate(env)
    private = _private(env)
    previous = {row.id: _snapshot(row) for row in (corporate, private)}
    versions = env.db.scalar(select(func.count()).select_from(DocumentVersion))
    embedding_calls = len(env.llm.calls)
    stats = _reconcile(env)
    assert not stats.failures
    assert (stats.unchanged_documents, stats.private_unchanged) == (1, 1)
    assert stats.updated_documents == stats.private_reindexed == 0
    assert len(env.llm.calls) == embedding_calls
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == versions
    for row in (corporate, private):
        assert _snapshot(row) == previous[row.id]
        assert len(_document_points(env, row)) == row.chunk_count


@pytest.mark.parametrize("damage", ("missing_private_collection", "wrong_private_owner"))
def test_private_recovery_preserves_upload_bytes_and_both_acl_dimensions(recovery_env, damage):
    env = recovery_env
    corporate = _corporate(env)
    private = _private(env)
    foreign = _private(env, owner="other", conversation="foreign-chat", text=PRIVATE_TEXT.replace(
        "PRIVADO_PROPIO", "PRIVADO_AJENO",
    ))
    original_paths = [env.settings.upload_storage_path / row.storage_path for row in (private, foreign)]
    original_bytes = {path: path.read_bytes() for path in original_paths}
    original_corporate = _snapshot(corporate)
    previous_private_generation = private.active_generation
    collection = env.store.collection_for(SCOPE_CONVERSATION)
    if damage == "missing_private_collection":
        env.store._client.delete_collection(collection)
    else:
        env.store._client.set_payload(
            collection_name=collection,
            payload={"owner_user_id": "other"},
            points=[point.id for point in _document_points(env, private)], wait=True,
        )
    env.restart()
    stats = _reconcile(env)
    assert not stats.failures
    assert stats.private_reindexed == (2 if damage == "missing_private_collection" else 1)
    assert private.active_generation != previous_private_generation
    assert _snapshot(corporate) == original_corporate and stats.unchanged_documents == 1
    for row in (private, foreign):
        assert row.scope == SCOPE_CONVERSATION and row.category is None
        active = _document_points(env, row)
        assert len(active) == row.chunk_count
        assert all(point.payload["owner_user_id"] == row.owner_user_id for point in active)
        assert all(point.payload["conversation_id"] == row.conversation_id for point in active)
    assert {item.document_id for item in _visible(env, conversation="chat", categories=frozenset())} == {private.id}
    assert not _visible(env, conversation="another-chat", categories=frozenset())
    assert not _visible(env, owner="other", conversation="chat", categories=frozenset())
    assert {item.document_id for item in _visible(
        env, owner="other", conversation="foreign-chat", categories=frozenset(),
    )} == {foreign.id}
    assert {item.document_id for item in _visible(env)} == {corporate.id}
    assert all(path.read_bytes() == original_bytes[path] for path in original_paths)


def test_missing_private_original_is_not_replaced_by_another_users_same_basename(recovery_env):
    env = recovery_env
    private = _private(env)
    foreign = _private(env, owner="other", conversation="foreign-chat", text=PRIVATE_TEXT.replace(
        "PRIVADO_PROPIO", "PRIVADO_AJENO",
    ))
    previous = _snapshot(private)
    own_path = env.settings.upload_storage_path / private.storage_path
    foreign_path = env.settings.upload_storage_path / foreign.storage_path
    foreign_bytes = foreign_path.read_bytes()
    own_path.unlink()
    env.store._client.delete_collection(env.store.collection_for(SCOPE_CONVERSATION))
    env.restart()
    stats = _reconcile(env)
    assert len(stats.failures) == 1 and stats.private_reindexed == 1
    assert stats.failures[0]["path"] == f"private:{private.id}"
    assert "convenio-privado.md" not in str(stats.as_dict())
    assert "PRIVADO_AJENO" not in str(stats.as_dict())
    assert _snapshot(private) == previous
    assert not own_path.exists() and foreign_path.read_bytes() == foreign_bytes
    assert not _visible(env, conversation="chat", categories=frozenset())
    assert {item.document_id for item in _visible(
        env, owner="other", conversation="foreign-chat", categories=frozenset(),
    )} == {foreign.id}


def test_failed_recovery_preserves_manifest_and_does_not_publish_candidate(recovery_env, monkeypatch):
    env = recovery_env
    row = _corporate(env)
    previous = _snapshot(row)
    versions = env.db.scalar(select(func.count()).select_from(DocumentVersion))
    points = _document_points(env, row)
    env.store._client.delete(
        collection_name=env.store.collection_for(SCOPE_CORPORATE), wait=True,
        points_selector=models.PointIdsList(points=[points[0].id]),
    )

    def unavailable(_texts):
        raise RuntimeError("synthetic embedding service unavailable")

    monkeypatch.setattr(env.llm, "embed", unavailable)
    stats = _reconcile(env)
    assert len(stats.failures) == 1 and stats.updated_documents == 0
    assert _snapshot(row) == previous
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == versions
    assert {point.payload["generation"] for point in _points(env, SCOPE_CORPORATE)} == {row.active_generation}
    assert env.db.scalar(select(func.count()).select_from(JobLock)) == 0
