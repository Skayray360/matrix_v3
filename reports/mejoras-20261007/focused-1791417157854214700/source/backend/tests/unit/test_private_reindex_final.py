# Creado por Aldo Garcia.
"""Migracion privada: SQL y Qdrant reales, archivos sinteticos, embeddings locales dobles."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.common.errors import IngestionFailedError
from app.common.ids import sha256_bytes
from app.config.settings import Settings
from app.database.models import Base, Conversation, Document, DocumentVersion, IngestionJob, User
from app.ingestion.loaders import extract_document
from app.ingestion.reconciler import reconcile_knowledge
from app.ingestion.service import INGESTION_VERSION, IngestionService
from app.rag.index_manifest import indexing_fingerprint
from app.rag.retriever import Retriever
from app.rag.schemas import SCOPE_CONVERSATION
from app.rag.vector_store import VectorStore
from tests.conftest import make_context
from tests.unit.test_rag_pipeline_isolated import FakeEmbeddingClient, make_chunk

pytestmark = pytest.mark.unit


@pytest.fixture
def environment(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        upload_storage_root=str(tmp_path / "uploads"),
        rag_knowledge_root=str(tmp_path / "knowledge"),
    )
    settings.knowledge_root_path.mkdir()
    # La ingesta multi raiz conserva tambien data/knowledge: la prueba privada
    # debe usar un proyecto completo temporal para no indexar ejemplos del ZIP.
    (tmp_path / "data" / "knowledge").mkdir(parents=True)
    monkeypatch.setattr("app.ingestion.knowledge_layout.PROJECT_ROOT", tmp_path)
    for module in (
        "app.ingestion.service",
        "app.ingestion.reconciler",
        "app.rag.index_manifest",
        "app.rag.retriever",
        "app.rag.vector_store",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    # Se prueba el extractor real sin crear un proceso por cada archivo sintetico.
    monkeypatch.setattr("app.ingestion.service.extract_document", extract_document)
    engine = create_engine(f"sqlite:///{tmp_path / 'manifest.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def scope():
        with factory() as session:
            yield session

    monkeypatch.setattr("app.rag.index_manifest.session_scope", scope)
    store = VectorStore(client=QdrantClient(location=":memory:"))
    llm = FakeEmbeddingClient()
    service = IngestionService(store=store, llm=llm)
    with factory() as db:
        db.add(User(id="user-a", username="Usuario sintetico", display_name="Usuario sintetico", auth_source="local_test"))
        db.flush()
        db.add(Conversation(id="chat-a", user_id="user-a", title="Conversacion sintetica"))
        db.commit()
        yield SimpleNamespace(
            db=db, factory=factory, store=store, llm=llm, service=service, settings=settings, root=tmp_path
        )
    store.close()
    engine.dispose()


def legacy_attachment(env, *, document_id="attachment", owner="user-a", conversation="chat-a", old_path=None):
    data = b"# Prestaciones\n\nEl personal temporal no tiene derecho al bono."
    filename = f"{document_id}-internal.md"
    stored = env.settings.upload_storage_path / owner / conversation / filename
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(data)
    fingerprint = "4" * 64
    document = Document(
        id=document_id,
        scope=SCOPE_CONVERSATION,
        owner_user_id=owner,
        conversation_id=conversation,
        filename="politica.md",
        storage_path=old_path or rf"C:\servidor-anterior\var\uploads\{owner}\{conversation}\{filename}",
        mime_type="text/markdown",
        size_bytes=len(data),
        sha256=sha256_bytes(data),
        status="indexed",
        chunk_count=1,
        ingestion_version="4",
        active_generation=f"legacy-{document_id}",
        index_fingerprint=fingerprint,
    )
    env.db.add(document)
    env.db.commit()
    chunk = make_chunk(
        "Texto de la extraccion anterior.",
        category="__private__",
        scope=SCOPE_CONVERSATION,
        document_id=document_id,
        owner=owner,
        conversation=conversation,
    )
    chunk = replace(
        chunk,
        metadata=replace(chunk.metadata, generation=document.active_generation, index_fingerprint=fingerprint),
    )
    env.store.upsert_chunks([chunk], [[1.0] + [0.0] * 767])
    return document, stored


def summary(env, *, owner="user-a", conversation="chat-a"):
    return Retriever(store=env.store, llm=env.llm).retrieve_attachment_summary(
        ctx=make_context(user_id=owner), conversation_id=conversation
    )


def test_private_migration_requires_commit_and_preserves_identity(environment):
    env = environment
    document, stored = legacy_attachment(env)
    legacy_generation = document.active_generation
    assert not summary(env).evidences  # El scroll tampoco acepta la huella antigua.

    result = env.service.reindex_conversation_attachment(env.db, document=document)
    assert result.document_id == document.id and result.status == "indexed"
    assert document.active_generation != legacy_generation
    assert not summary(env).evidences  # El upsert aun no publica la nueva generacion.
    env.db.commit()

    recovered = summary(env)
    assert len(recovered.evidences) == 1
    assert "no tiene derecho al bono" in recovered.evidences[0].text
    assert recovered.evidences[0].document_id == document.id
    assert document.storage_path == f"user-a/chat-a/{stored.name}"
    assert (document.owner_user_id, document.conversation_id, document.category) == ("user-a", "chat-a", None)
    assert document.ingestion_version == INGESTION_VERSION
    assert document.index_fingerprint == indexing_fingerprint(env.llm)
    assert not summary(env, owner="user-b").evidences
    assert not summary(env, conversation="chat-b").evidences
    assert env.db.scalar(select(func.count()).select_from(Document)) == 1
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == 1


def test_reconciler_includes_private_attachments_and_is_idempotent(environment):
    env = environment
    document, _ = legacy_attachment(env)
    first = reconcile_knowledge(env.db, service=env.service)
    generation = document.active_generation
    embeddings = len(env.llm.calls)
    second = reconcile_knowledge(env.db, service=env.service)

    assert first.private_scanned == first.private_reindexed == 1
    assert second.private_unchanged == 1 and second.private_reindexed == 0
    assert not first.failures and not second.failures
    assert document.active_generation == generation
    assert len(env.llm.calls) == embeddings
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == 1
    assert env.db.scalar(select(func.count()).select_from(Document)) == 1


@pytest.mark.parametrize("failure", ["missing", "hash"])
def test_private_reconcile_records_failures_without_replacing_original(environment, failure):
    env = environment
    document, stored = legacy_attachment(env)
    previous = (document.active_generation, document.sha256, document.index_fingerprint)
    # Aunque exista la antigua ruta absoluta, no se lee fuera del nuevo almacen.
    external = env.root / stored.name
    external.write_bytes(stored.read_bytes())
    document.storage_path = str(external)
    env.db.commit()
    if failure == "missing":
        stored.unlink()
    else:
        stored.write_text("Otro contenido no corresponde al archivo original.")

    result = reconcile_knowledge(env.db, service=env.service)
    env.db.refresh(document)
    assert len(result.failures) == 1
    assert result.failures[0]["path"] == f"private:{document.id}"
    assert (document.active_generation, document.sha256, document.index_fingerprint) == previous
    assert document.error_message and document.status == "indexed"
    assert not env.llm.calls and not summary(env).evidences
    job = env.db.scalar(select(IngestionJob))
    assert job.status == "failed" and job.stats["failure_count"] == 1


@pytest.mark.parametrize("location", ["outside", "other-owner"])
def test_private_reindex_rejects_symlink_outside_conversation(environment, location):
    env = environment
    document, stored = legacy_attachment(env)
    destination = (
        env.root / "outside.md"
        if location == "outside"
        else env.settings.upload_storage_path / "user-b" / "chat-b" / stored.name
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(stored.read_bytes())
    stored.unlink()
    stored.symlink_to(destination)
    with pytest.raises(IngestionFailedError, match="directorio privado"):
        env.service.reindex_conversation_attachment(env.db, document=document)
    assert not env.llm.calls


def test_partial_private_generation_stays_hidden_and_retry_keeps_document(environment, monkeypatch):
    env = environment
    document, _ = legacy_attachment(env)
    previous_generation = document.active_generation
    upsert = env.store.upsert_chunks

    def interrupted(chunks, vectors):
        upsert(chunks, vectors)
        raise RuntimeError("interrupcion despues de escritura vectorial")

    monkeypatch.setattr(env.store, "upsert_chunks", interrupted)
    failed = reconcile_knowledge(env.db, service=env.service)
    env.db.refresh(document)
    assert failed.failures and document.active_generation == previous_generation
    assert not summary(env).evidences

    monkeypatch.setattr(env.store, "upsert_chunks", upsert)
    retried = reconcile_knowledge(env.db, service=env.service)
    assert retried.private_reindexed == 1 and not retried.failures
    assert len(summary(env).evidences) == 1
    assert env.db.scalar(select(func.count()).select_from(Document)) == 1
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == 1


def test_new_attachment_stores_portable_path_and_rejects_namespace_escape(environment):
    env = environment
    result = env.service.ingest_conversation_attachment(
        env.db,
        data=b"Una politica nueva.",
        display_name="politica.md",
        internal_filename="internal.md",
        mime_type="text/markdown",
        owner_user_id="user-a",
        conversation_id="chat-a",
    )
    env.db.commit()
    assert env.db.get(Document, result.document_id).storage_path == "user-a/chat-a/internal.md"
    with pytest.raises(IngestionFailedError, match="ruta interna"):
        env.service.ingest_conversation_attachment(
            env.db,
            data=b"No escribir este archivo.",
            display_name="politica.md",
            internal_filename="internal.md",
            mime_type="text/markdown",
            owner_user_id="../outside",
            conversation_id="chat-a",
        )
