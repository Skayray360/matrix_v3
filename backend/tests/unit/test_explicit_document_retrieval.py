# Creado por Aldo Garcia.
"""Documento solicitado antes del ranking: SQL y Qdrant sinteticos en memoria."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

from app.common.ids import new_id, utcnow_naive
from app.config.settings import Settings
from app.database.models import Document
from app.rag.index_manifest import indexing_fingerprint
from app.rag.retriever import Retriever
from app.rag.schemas import Chunk, ChunkMetadata
from app.rag.vector_store import VectorStore
from tests.conftest import make_context

pytestmark = pytest.mark.unit


@pytest.fixture
def corpus(monkeypatch, manifest_db):
    settings = Settings(
        _env_file=None, app_env="test", rag_embedding_dimension=3, ollama_embedding_dimension=3,
        rag_top_k=2, rag_fetch_k=4,
    )
    for module in ("retriever", "vector_store", "index_manifest"):
        monkeypatch.setattr(f"app.rag.{module}.get_settings", lambda: settings)
    store = VectorStore(client=QdrantClient(location=":memory:"))
    embedding_calls = []
    search_calls = []
    fingerprint = indexing_fingerprint()

    def embed_one(question):
        embedding_calls.append(question)
        return [1.0, 0.0, 0.0]

    original_search = store.search

    def search(**kwargs):
        search_calls.append(kwargs)
        return original_search(**kwargs)

    monkeypatch.setattr(store, "search", search)

    def publish(filename, *, vector=(0.6, 0.8, 0.0), category="prestaciones", scope="corporate",
                user="owner", conversation="thread", fingerprint_value=None):
        chunk = Chunk(
            f"Contenido sintetico autorizado de {filename}. Aportacion documentada del 70%.",
            ChunkMetadata(
                chunk_id=new_id(), document_id=new_id(), document_sha256="a" * 64,
                filename=filename, relative_path=f"{category}/{filename}", category=category,
                subpath="", mime_type="application/pdf", page_or_sheet="pagina 9", section="Portabilidad",
                chunk_index=0, embedding_model=settings.ollama_embedding_model, embedding_dimension=3,
                ingestion_version="synthetic", generation=new_id(),
                index_fingerprint=fingerprint_value or fingerprint, scope=scope,
                owner_user_id=user if scope == "conversation" else None,
                conversation_id=conversation if scope == "conversation" else None,
            ),
        )
        store.upsert_chunks([chunk], [list(vector)])
        return chunk

    retriever = Retriever(store=store, llm=SimpleNamespace(embed_one=embed_one))

    def ask(question, *, categories=frozenset({"prestaciones"}), conversation=None,
            include_private=True, comparative=False):
        return retriever.retrieve(
            ctx=make_context(user_id="owner"), question=question, authorized_categories=categories,
            conversation_id=conversation, include_private=include_private, comparative=comparative,
        )

    yield SimpleNamespace(
        store=store, settings=settings, publish=publish, ask=ask, db=manifest_db,
        embedding_calls=embedding_calls, search_calls=search_calls, fingerprint=fingerprint,
    )
    store.close()


def test_named_document_is_resolved_before_unrelated_chunks_exhaust_fetch_k(corpus):
    for number in range(8):
        corpus.publish(f"Manual no solicitado {number}.pdf", vector=(1.0, 0.0, 0.0))
    requested = corpus.publish("Plan de Pensiones Diciembre2022.pdf")
    baseline = corpus.ask("¿Cual es el porcentaje de aportacion?")
    assert requested.metadata.document_id not in {item.document_id for item in baseline.evidences}

    result = corpus.ask("Segun el Plan de Pensiones Diciembre2022.pdf, ¿cual es la aportacion?")
    assert {item.document_id for item in result.evidences} == {requested.metadata.document_id}
    assert result.evidences[0].score == pytest.approx(0.6)
    query = corpus.search_calls[-1]["query_filter"].model_dump_json()
    assert requested.metadata.document_id in query and requested.metadata.generation in query
    assert corpus.fingerprint in query and "prestaciones" in query and "corporate" in query


def test_full_title_without_extension_resolves_imported_accent_escape(corpus):
    requested = corpus.publish("PLATICA DE PLAN DE PENSIONES POR JUBILACI#U00d3N DICIEMBRE 2022.pdf")
    corpus.publish("Platica Prestaciones Generales.pdf", vector=(1.0, 0.0, 0.0))
    result = corpus.ask(
        "Segun la Plática del Plan de Pensiones por Jubilación de diciembre de 2022, "
        "¿que establece la portabilidad?"
    )
    assert {item.document_id for item in result.evidences} == {requested.metadata.document_id}


@pytest.mark.parametrize("question", [
    "Segun Restringido.pdf, ¿que porcentaje se establece?",
    'Segun “Manual de Nomina Restringido”, ¿que porcentaje se establece?',
])
def test_unavailable_literal_document_never_substitutes_another_authorized_file(corpus, question):
    corpus.publish("Manual de Prestaciones.pdf", vector=(1.0, 0.0, 0.0))
    result = corpus.ask(question)
    assert not result.has_evidence and result.clarification
    assert corpus.embedding_calls == corpus.search_calls == []


@pytest.mark.parametrize("second_scope", ["corporate", "conversation"])
def test_homonymous_authorized_documents_request_disambiguation(corpus, second_scope):
    corpus.publish("Manual de Prestaciones.pdf")
    corpus.publish("Manual de Prestaciones.pdf", scope=second_scope)
    result = corpus.ask("Segun Manual de Prestaciones.pdf, ¿cual es la regla?", conversation="thread")
    assert not result.has_evidence and "varios documentos" in result.clarification
    assert not corpus.embedding_calls and not corpus.search_calls


def test_named_private_document_keeps_owner_conversation_and_corporate_acl(corpus):
    requested = corpus.publish("Plan Privado.pdf", scope="conversation")
    corpus.publish("Plan Privado.pdf", scope="conversation", user="other-user", vector=(1.0, 0.0, 0.0))
    corpus.publish("Plan Privado.pdf", scope="conversation", conversation="other-thread")
    corpus.publish("Plan Privado.pdf", category="restricted")
    result = corpus.ask("Segun Plan Privado.pdf, ¿cual es la aportacion?", conversation="thread")
    assert {item.document_id for item in result.evidences} == {requested.metadata.document_id}
    assert result.used_private_scope and len(corpus.search_calls) == 1
    query = corpus.search_calls[0]["query_filter"].model_dump_json()
    assert all(term in query for term in ("owner_user_id", "owner", "conversation_id", "thread", "generation"))


def test_private_documents_are_not_resolved_when_private_scope_is_disabled(corpus):
    corpus.publish("Plan Privado.pdf", scope="conversation")
    result = corpus.ask("Segun Plan Privado.pdf, ¿cual es la aportacion?", conversation="thread", include_private=False)
    assert not result.has_evidence and result.clarification
    assert not corpus.search_calls


@pytest.mark.parametrize("state", ["deleted", "pending", "incompatible"])
def test_deleted_pending_and_incompatible_documents_are_not_resolved(corpus, state):
    chunk = corpus.publish("Manual de Prestaciones.pdf")
    with corpus.db() as db:
        row = db.get(Document, chunk.metadata.document_id)
        if state == "deleted":
            row.deleted_at = utcnow_naive()
        elif state == "pending":
            row.status = "pending"
        else:
            row.index_fingerprint = "different-index-fingerprint"
        db.commit()
    result = corpus.ask("Segun Manual de Prestaciones.pdf, ¿cual es la regla?")
    assert not result.has_evidence and result.clarification and not corpus.search_calls


def test_unpublished_generation_cannot_replace_the_catalogued_generation(corpus):
    active = corpus.publish("Plan de Pensiones.pdf")
    stale = replace(active, text="Regla sin publicar del 99%.", metadata=replace(
        active.metadata, chunk_id=new_id(), generation=new_id(),
    ))
    corpus.store.upsert_chunks([stale], [[1.0, 0.0, 0.0]])
    # El fixture publica automaticamente; restablecer la generacion autorizada.
    with corpus.db() as db:
        db.get(Document, active.metadata.document_id).active_generation = active.metadata.generation
        db.commit()
    result = corpus.ask("Segun Plan de Pensiones.pdf, ¿cual es la regla?")
    assert [item.chunk_id for item in result.evidences] == [active.metadata.chunk_id]
    assert "99%" not in result.evidences[0].text


def test_catalog_snapshot_does_not_restore_a_revoked_generation(corpus, monkeypatch):
    active = corpus.publish("Plan de Pensiones.pdf")
    original_search = corpus.store.search

    def revoke_before_search(**kwargs):
        with corpus.db() as db:
            db.get(Document, active.metadata.document_id).active_generation = "new-generation"
            db.commit()
        return original_search(**kwargs)

    monkeypatch.setattr(corpus.store, "search", revoke_before_search)
    assert not corpus.ask("Segun Plan de Pensiones.pdf, ¿cual es la regla?").has_evidence


def test_explicit_comparison_searches_each_document_before_global_ranking(corpus):
    first = corpus.publish("Plan Pensiones 2020.pdf", vector=(1.0, 0.0, 0.0))
    second = corpus.publish("Plan Pensiones 2022.pdf", vector=(0.5, 0.866, 0.0))
    corpus.publish("Otro plan.pdf", vector=(1.0, 0.0, 0.0))
    result = corpus.ask("Compara Plan Pensiones 2020.pdf y Plan Pensiones 2022.pdf", comparative=True)
    assert {item.document_id for item in result.evidences} == {
        first.metadata.document_id, second.metadata.document_id,
    }
    assert len(corpus.search_calls) == 2
    assert len(corpus.embedding_calls) == 1


def test_explicit_document_does_not_bypass_semantic_relevance_threshold(corpus):
    corpus.publish("Plan de Pensiones.pdf", vector=(0.0, 1.0, 0.0))
    result = corpus.ask("Segun Plan de Pensiones.pdf, ¿como reparo un motor?")
    assert not result.has_evidence
    assert corpus.search_calls[0]["score_threshold"] == corpus.settings.rag_min_similarity


def test_empty_category_permissions_do_not_reveal_matching_document(corpus):
    corpus.publish("Plan de Pensiones.pdf")
    result = corpus.ask("Segun Plan de Pensiones.pdf, ¿cual es la regla?", categories=frozenset())
    assert not result.has_evidence and result.clarification
    assert "Plan de Pensiones" not in result.clarification
    assert not corpus.search_calls
