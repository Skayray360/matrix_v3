# Creado por Aldo Garcia.
"""Deduplicacion, MMR, diversidad por categoria y filtros ACL de Qdrant."""

from __future__ import annotations

import pytest

from app.rag.retriever import (
    cosine_similarity,
    deduplicate,
    enforce_category_diversity,
    maximal_marginal_relevance,
)
from app.rag.vector_store import ScoredPayload, VectorStore

pytestmark = pytest.mark.unit


def payload(text: str, score: float, category: str = "prestaciones", vector=None) -> ScoredPayload:
    return ScoredPayload(
        score=score,
        payload={
            "text": text,
            "category": category,
            "filename": f"{category}.md",
            "chunk_index": 0,
            "_vector": vector or [1.0, 0.0, 0.0],
        },
    )


class TestSimilitudCoseno:
    def test_vectores_identicos(self):
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_vectores_ortogonales(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_vector_nulo_no_divide_entre_cero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_dimensiones_distintas_devuelven_cero(self):
        assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0


class TestDeduplicacion:
    def test_elimina_textos_identicos(self):
        resultado = deduplicate([payload("mismo texto", 0.9), payload("mismo texto", 0.5)])
        assert len(resultado) == 1

    def test_ignora_diferencias_de_espaciado(self):
        resultado = deduplicate([payload("un texto", 0.9), payload("  UN   TEXTO  ", 0.4)])
        assert len(resultado) == 1

    def test_elimina_fragmentos_contenidos_en_otro(self):
        resultado = deduplicate(
            [payload("el procedimiento completo con todos sus pasos", 0.9), payload("todos sus pasos", 0.6)]
        )
        assert len(resultado) == 1

    def test_conserva_textos_distintos(self):
        assert len(deduplicate([payload("texto uno", 0.9), payload("texto dos", 0.8)])) == 2

    def test_ordena_por_score_descendente(self):
        resultado = deduplicate([payload("b", 0.4), payload("a", 0.9)])
        assert resultado[0].score == 0.9


class TestMmr:
    def test_devuelve_como_maximo_top_k(self):
        candidatos = [payload(f"texto {i}", 0.9 - i * 0.05) for i in range(10)]
        assert len(maximal_marginal_relevance([1.0, 0.0, 0.0], candidatos, top_k=3, lambda_mult=0.65)) == 3

    def test_lambda_alto_prioriza_relevancia(self):
        candidatos = [
            payload("relevante", 0.95, vector=[1.0, 0.0, 0.0]),
            payload("menos relevante", 0.5, vector=[0.0, 1.0, 0.0]),
        ]
        resultado = maximal_marginal_relevance([1.0, 0.0, 0.0], candidatos, top_k=1, lambda_mult=1.0)
        assert resultado[0].payload["text"] == "relevante"

    def test_lambda_bajo_favorece_diversidad(self):
        candidatos = [
            payload("a", 0.9, vector=[1.0, 0.0, 0.0]),
            payload("b", 0.88, vector=[1.0, 0.0, 0.0]),
            payload("c", 0.6, vector=[0.0, 1.0, 0.0]),
        ]
        textos = [
            item.payload["text"]
            for item in maximal_marginal_relevance([1.0, 0.0, 0.0], candidatos, top_k=2, lambda_mult=0.1)
        ]
        assert "c" in textos

    def test_lista_vacia(self):
        assert maximal_marginal_relevance([1.0], [], top_k=5, lambda_mult=0.5) == []


class TestDiversidadPorCategoria:
    def test_en_comparativas_representa_varias_categorias(self):
        candidatos = [
            payload("a1", 0.9, "prestaciones"),
            payload("a2", 0.88, "prestaciones"),
            payload("a3", 0.86, "prestaciones"),
            payload("b1", 0.5, "nomina"),
        ]
        categorias = {
            item.payload["category"]
            for item in enforce_category_diversity(candidatos, top_k=2, comparative=True)
        }
        assert categorias == {"prestaciones", "nomina"}

    def test_sin_comparativa_solo_recorta(self):
        candidatos = [payload(f"a{i}", 0.9 - i * 0.01, "prestaciones") for i in range(5)]
        resultado = enforce_category_diversity(candidatos, top_k=2, comparative=False)
        assert len(resultado) == 2


class TestFiltroAclDeQdrant:
    """El filtro ACL viaja SIEMPRE en la consulta, nunca se aplica despues."""

    def test_el_filtro_enumera_las_categorias_autorizadas(self):
        filtro = VectorStore.build_corporate_filter({"prestaciones", "nomina"})
        serializado = filtro.model_dump_json()
        assert "prestaciones" in serializado
        assert "nomina" in serializado
        assert "corporate" in serializado

    def test_sin_categorias_el_filtro_es_imposible_de_satisfacer(self):
        """Un usuario sin categorias no debe recuperar nada: se usa una condicion
        imposible en vez de omitir el filtro."""
        filtro = VectorStore.build_corporate_filter(set())
        assert "__none__" in filtro.model_dump_json()

    def test_el_filtro_privado_exige_usuario_y_conversacion(self):
        filtro = VectorStore.build_private_filter(user_id="u-1", conversation_id="c-1")
        serializado = filtro.model_dump_json()
        assert "u-1" in serializado
        assert "c-1" in serializado
        assert "conversation" in serializado

    def test_una_categoria_no_autorizada_no_aparece_en_el_filtro(self):
        filtro = VectorStore.build_corporate_filter({"prestaciones"})
        assert "nomina" not in filtro.model_dump_json()
