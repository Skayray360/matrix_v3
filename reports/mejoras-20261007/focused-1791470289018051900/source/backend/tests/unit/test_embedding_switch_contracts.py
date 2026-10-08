# Creado por Aldo Garcia.
"""Contratos de configuracion del embedding; no son benchmarks de modelos reales."""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.rag.embedding_prompts import format_document, format_query
from app.rag.index_manifest import indexing_fingerprint

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "generation_model",
    [
        "nemotron-3-nano-omni-30b-a3b-reasoning",
        "gemma-4-31B-it-FP8-dynamic",
        "gemma-4-26B-A4B-it-AWQ-4bit",
    ],
)
def test_switching_generator_does_not_replace_embedding_or_invalidate_index(monkeypatch, generation_model):
    """Cambiar generador no exige cambiar embedding, prefijos ni indices."""
    current = Settings(_env_file=None)
    monkeypatch.setattr("app.rag.index_manifest.get_settings", lambda: current)
    previous = indexing_fingerprint()

    current = current.model_copy(
        update={"ollama_fast_model": generation_model, "ollama_deep_model": generation_model}
    )

    assert current.ollama_embedding_model == "embeddinggemma:latest"
    assert current.rag_embedding_dimension == 768
    assert indexing_fingerprint() == previous


def test_e5_prefixes_dimension_and_index_contract_change_from_one_config_file(monkeypatch, tmp_path):
    """Solo demuestra contrato de configuracion; E5 no se descarga ni se ejecuta."""
    profile = tmp_path / "embedding.env"
    keys = (
        "OLLAMA_EMBEDDING_MODEL",
        "OLLAMA_EMBEDDING_DIMENSION",
        "RAG_EMBEDDING_DIMENSION",
        "LLM_QUERY_TEMPLATE",
        "LLM_DOCUMENT_TEMPLATE",
        "RAG_CHUNK_SIZE_TOKENS",
        "RAG_CHUNK_OVERLAP_TOKENS",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    profile.write_text("OLLAMA_EMBEDDING_MODEL=embeddinggemma:latest\n", encoding="utf-8")

    def current():
        return Settings(_env_file=profile)

    monkeypatch.setattr("app.config.get_settings", current)
    monkeypatch.setattr("app.rag.index_manifest.get_settings", current)
    previous = indexing_fingerprint()
    assert format_query("  vacaciones  ") == "task: search result | query: vacaciones"

    profile.write_text(
        "OLLAMA_EMBEDDING_MODEL=intfloat/multilingual-e5-large\n"
        "OLLAMA_EMBEDDING_DIMENSION=1024\n"
        "RAG_EMBEDDING_DIMENSION=1024\n"
        "LLM_QUERY_TEMPLATE='query: {text}'\n"
        "LLM_DOCUMENT_TEMPLATE='passage: {text}'\n"
        "RAG_CHUNK_SIZE_TOKENS=384\n"
        "RAG_CHUNK_OVERLAP_TOKENS=64\n",
        encoding="utf-8",
    )

    assert format_query("  vacaciones  ") == "query: vacaciones"
    assert format_document("  dato sintetico  ", title="Titulo") == "passage: dato sintetico"
    assert current().rag_embedding_dimension == current().ollama_embedding_dimension == 1024
    assert indexing_fingerprint() != previous

    # Un titulo opcional es una decision de representacion, no un prefijo Gemma.
    profile.write_text(
        profile.read_text(encoding="utf-8").replace("passage: {text}", "passage: {title}\n{text}"),
        encoding="utf-8",
    )
    assert format_document("dato", title="Titulo") == "passage: Titulo\ndato"
