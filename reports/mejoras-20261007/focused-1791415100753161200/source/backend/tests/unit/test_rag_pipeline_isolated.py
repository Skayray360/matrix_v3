# Creado por Aldo Garcia.
"""Pipeline RAG completo contra Qdrant en memoria y un LLM simulado.

Permite ejercitar ingesta, ACL, recuperacion y borrado **sin** Qdrant en disco ni
Ollama: el cliente oficial de Qdrant admite ``location=":memory:"`` y el cliente
de embeddings se sustituye por un doble determinista.

Es la prueba que verifica de verdad que un chunk restringido NO entra en
``fetch_k``, no que se filtre despues.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from qdrant_client import QdrantClient

from app.config import get_settings
from app.rag.embedding_prompts import format_document, format_query
from app.rag.retriever import Retriever
from app.rag.schemas import SCOPE_CONVERSATION, SCOPE_CORPORATE, Chunk, ChunkMetadata
from app.rag.vector_store import VectorStore, payload_to_evidence
from tests.conftest import make_context

pytestmark = pytest.mark.unit

DIMENSION = 768


class FakeEmbeddingClient:
    """Embeddings deterministas basados en el hash del texto.

    Dos textos que comparten palabras producen vectores cercanos, lo bastante
    para que la busqueda por coseno ordene de forma estable y reproducible.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * DIMENSION
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for offset in range(0, 32, 4):
                index = int.from_bytes(digest[offset : offset + 4], "big") % DIMENSION
                vector[index] += 1.0
        norm = sum(v * v for v in vector) ** 0.5
        return [v / norm for v in vector] if norm else [1.0] + [0.0] * (DIMENSION - 1)

    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:  # noqa: ARG002
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    def embed_one(self, text: str, *, model: str | None = None) -> list[float]:  # noqa: ARG002
        return self.embed([text])[0]


@pytest.fixture()
def store(manifest_db) -> VectorStore:
    """Vector store con Qdrant en memoria: mismo API, sin disco."""
    return VectorStore(client=QdrantClient(location=":memory:"))


def make_chunk(
    text: str,
    *,
    category: str,
    index: int = 0,
    scope: str = SCOPE_CORPORATE,
    document_id: str = "doc-1",
    owner: str | None = None,
    conversation: str | None = None,
) -> Chunk:
    # Los identificadores de punto de Qdrant deben ser UUID (o enteros). En
    # produccion los genera new_id(); aqui se derivan de forma determinista para
    # que la prueba sea reproducible.
    chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}/{category}/{index}/{scope}"))
    metadata = ChunkMetadata(
        chunk_id=chunk_id,
        document_id=document_id,
        document_sha256="0" * 64,
        filename=f"{category}.md",
        relative_path=f"{category}/{category}.md",
        category=category,
        subpath="",
        mime_type="text/markdown",
        page_or_sheet="",
        section=f"Seccion {index}",
        chunk_index=index,
        embedding_model="embeddinggemma:latest",
        embedding_dimension=DIMENSION,
        ingestion_version="3",
        scope=scope,
        owner_user_id=owner,
        conversation_id=conversation,
    )
    return Chunk(text=text, metadata=metadata)


CORPUS = {
    "prestaciones": "Con cinco anios de antiguedad corresponden veinte dias habiles de vacaciones",
    "nomina": "La nomina del personal de confianza se paga los dias quince y ultimo dia habil",
    "reclutamiento": "El proceso de reclutamiento tiene nueve etapas desde la requisicion",
    "relaciones_laborales": "La tolerancia es de diez minutos antes de considerar un retardo",
    "salud_ambiental": "Los simulacros de emergencia se realizan de forma trimestral",
}


@pytest.fixture()
def poblado(store: VectorStore) -> tuple[VectorStore, FakeEmbeddingClient]:
    llm = FakeEmbeddingClient()
    chunks = [make_chunk(text, category=cat, document_id=f"doc-{cat}") for cat, text in CORPUS.items()]
    vectors = llm.embed([format_document(c.text, title=c.metadata.section) for c in chunks])
    store.upsert_chunks(chunks, vectors)
    return store, llm


class TestVectorStore:
    def test_upsert_e_inventario(self, poblado):
        store, _ = poblado
        assert store.count(get_settings().rag_collection_corporate) == len(CORPUS)

    def test_health_reporta_el_modo(self, store: VectorStore):
        ok, detail = store.health()
        assert ok is True
        assert detail

    def test_rechaza_un_vector_de_dimension_incorrecta(self, store: VectorStore):
        from app.common.errors import QdrantUnavailableError

        chunk = make_chunk("texto", category="prestaciones")
        with pytest.raises(QdrantUnavailableError) as excinfo:
            store.upsert_chunks([chunk], [[0.1] * 16])
        # El mensaje al cliente es generico; el detalle tecnico es el que explica.
        assert "vector de dimension 16" in (excinfo.value.detail or "")

    def test_rechaza_longitudes_desalineadas(self, store: VectorStore):
        from app.common.errors import QdrantUnavailableError

        chunk = make_chunk("texto", category="prestaciones")
        with pytest.raises(QdrantUnavailableError):
            store.upsert_chunks([chunk], [])

    def test_borrar_documento_elimina_sus_chunks(self, poblado):
        store, _ = poblado
        antes = store.count(get_settings().rag_collection_corporate)
        store.delete_document("doc-nomina", scope=SCOPE_CORPORATE)
        assert store.count(get_settings().rag_collection_corporate) == antes - 1

    def test_borrar_conversacion_elimina_los_vectores_privados(self, store: VectorStore):
        llm = FakeEmbeddingClient()
        chunk = make_chunk(
            "documento privado del usuario",
            category="__private__",
            scope=SCOPE_CONVERSATION,
            document_id="doc-priv",
            owner="user-1",
            conversation="conv-1",
        )
        store.upsert_chunks([chunk], llm.embed([chunk.text]))
        assert store.count(get_settings().rag_collection_private) == 1

        store.delete_conversation("conv-1")
        assert store.count(get_settings().rag_collection_private) == 0


class TestFiltroAclEnLaConsulta:
    """El chunk restringido no debe aparecer NI SIQUIERA en la busqueda cruda."""

    def test_el_filtro_excluye_las_categorias_no_autorizadas(self, poblado):
        store, llm = poblado
        vector = llm.embed_one(format_query("cuando se paga la nomina de confianza"))

        autorizado = store.search(
            collection=get_settings().rag_collection_corporate,
            query_vector=vector,
            query_filter=VectorStore.build_corporate_filter({"prestaciones"}),
            limit=24,
            score_threshold=None,
        )
        categorias = {r.payload["category"] for r in autorizado}
        assert categorias <= {"prestaciones"}
        assert "nomina" not in categorias

    def test_sin_categorias_no_se_recupera_nada(self, poblado):
        store, llm = poblado
        resultados = store.search(
            collection=get_settings().rag_collection_corporate,
            query_vector=llm.embed_one("cualquier cosa"),
            query_filter=VectorStore.build_corporate_filter(set()),
            limit=24,
            score_threshold=None,
        )
        assert resultados == []

    def test_el_namespace_privado_aisla_por_usuario_y_conversacion(self, store: VectorStore):
        llm = FakeEmbeddingClient()
        chunk = make_chunk(
            "codigo interno de guardia zeta",
            category="__private__",
            scope=SCOPE_CONVERSATION,
            document_id="doc-priv",
            owner="user-A",
            conversation="conv-A",
        )
        store.upsert_chunks([chunk], llm.embed([chunk.text]))
        vector = llm.embed_one("codigo interno de guardia zeta")

        propio = store.search(
            collection=get_settings().rag_collection_private,
            query_vector=vector,
            query_filter=VectorStore.build_private_filter(user_id="user-A", conversation_id="conv-A"),
            limit=10,
            score_threshold=None,
        )
        assert len(propio) == 1

        for user, conversation in (("user-B", "conv-A"), ("user-A", "conv-B")):
            ajeno = store.search(
                collection=get_settings().rag_collection_private,
                query_vector=vector,
                query_filter=VectorStore.build_private_filter(
                    user_id=user, conversation_id=conversation
                ),
                limit=10,
                score_threshold=None,
            )
            assert ajeno == [], f"fuga con {user}/{conversation}"


class TestRetriever:
    def test_recupera_de_la_categoria_correcta(self, poblado):
        store, llm = poblado
        retriever = Retriever(store=store, llm=llm)
        ctx = make_context()

        result = retriever.retrieve(
            ctx=ctx,
            question="cuantos dias habiles de vacaciones con cinco anios de antiguedad",
            authorized_categories=frozenset(CORPUS),
        )
        assert result.has_evidence
        assert result.evidences[0].category == "prestaciones"
        assert result.fetched >= 1

    def test_un_usuario_restringido_no_recibe_otras_categorias(self, poblado):
        store, llm = poblado
        retriever = Retriever(store=store, llm=llm)
        ctx = make_context(
            username="MatrixR1",
            roles=frozenset({"prestaciones_reader_test"}),
            permissions=frozenset(),
            categories=frozenset({"prestaciones"}),
            wildcard=False,
        )

        result = retriever.retrieve(
            ctx=ctx,
            question="cuando se paga la nomina del personal de confianza",
            authorized_categories=frozenset({"prestaciones"}),
        )
        assert all(e.category == "prestaciones" for e in result.evidences)

    def test_sin_categorias_autorizadas_no_hay_evidencia(self, poblado):
        store, llm = poblado
        retriever = Retriever(store=store, llm=llm)
        result = retriever.retrieve(
            ctx=make_context(),
            question="cualquier consulta",
            authorized_categories=frozenset(),
        )
        assert result.has_evidence is False
        assert result.source_ids() == ()

    def test_expone_las_categorias_y_los_source_id(self, poblado):
        store, llm = poblado
        retriever = Retriever(store=store, llm=llm)
        result = retriever.retrieve(
            ctx=make_context(),
            question="los simulacros de emergencia se realizan de forma trimestral",
            authorized_categories=frozenset(CORPUS),
            comparative=True,
        )
        # Con `comparative=True` se fuerza diversidad por categoria, asi que
        # pueden aparecer varias; lo que debe cumplirse es que la mejor sea la
        # correcta y que todas esten dentro del alcance autorizado.
        assert result.evidences[0].category == "salud_ambiental"
        assert set(result.distinct_categories()) <= set(CORPUS)
        assert result.source_ids()
        assert result.authorized_categories == tuple(sorted(CORPUS))

    def test_marca_baja_confianza_cuando_el_score_es_flojo(self, poblado):
        store, llm = poblado
        retriever = Retriever(store=store, llm=llm)
        result = retriever.retrieve(
            ctx=make_context(),
            question="tema completamente ajeno al corpus corporativo",
            authorized_categories=frozenset(CORPUS),
        )
        # Sin evidencia, el score es 0 y la senal de baja confianza se activa.
        assert result.low_confidence is True


class TestConversionAEvidencia:
    def test_reconstruye_el_source_id(self):
        evidence = payload_to_evidence(
            {
                "category": "prestaciones",
                "filename": "politica.md",
                "chunk_index": 3,
                "text": "contenido",
                "section": "Seccion",
                "page_or_sheet": "",
                "document_id": "d1",
                "chunk_id": "c1",
            },
            0.75,
        )
        assert evidence.source_id == "prestaciones/politica.md#3"
        assert evidence.citation_label() == "politica.md"
        assert evidence.to_public_dict()["score"] == 0.75

    def test_la_etiqueta_incluye_pagina_cuando_existe(self):
        evidence = payload_to_evidence(
            {"category": "c", "filename": "f.pdf", "chunk_index": 0, "page_or_sheet": "pagina 4"},
            0.5,
        )
        assert "pagina 4" in evidence.citation_label()


class TestPrefijosDeEmbedding:
    def test_prefijo_de_consulta(self):
        assert format_query("  hola  ").startswith("task: search result | query:")

    def test_prefijo_de_documento_con_titulo(self):
        assert format_document("texto", title="Vacaciones").startswith("title: Vacaciones | text:")

    def test_sin_titulo_usa_none(self):
        assert format_document("texto", title="  ").startswith("title: none | text:")
