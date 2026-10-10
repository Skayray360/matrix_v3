# Creado por Aldo Garcia.
"""Regresiones sinteticas de filas, columnas, condiciones y procedencia."""

from collections import Counter
from types import SimpleNamespace

import pytest

from app.rag.chunking import TextBlock, chunk_blocks, estimate_tokens
from app.rag.retriever import Retriever, deduplicate
from app.rag.vector_store import ScoredPayload

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("row_count", [3, 9, 25])
@pytest.mark.parametrize("budget", [65, 90, 130])
@pytest.mark.parametrize("decimal_mark", [".", ","])
def test_table_splits_preserve_each_row_column_unit_and_condition(row_count, budget, decimal_mark):
    condition = "Solo para ingresos a partir de abril de 2018 y retiro antes de jubilarse."
    header = "| Antiguedad (anios) | Basica (%) | Complementaria (%) |"
    separator = "| --- | --- | --- |"
    rows = [f"| {n} a {n}{decimal_mark}99 | {n * 4} | {n * 3} |" for n in range(row_count)]
    blocks = [
        TextBlock(condition, "Portabilidad", "pagina 9"),
        TextBlock("\n".join([header, separator, *rows]), "Portabilidad", "pagina 9", "table"),
    ]
    chunks = chunk_blocks(blocks, chunk_size_tokens=budget, overlap_tokens=10)
    tables = [chunk for chunk in chunks if header in chunk.text]
    assert tables
    for chunk in tables:
        assert condition in chunk.text
        assert separator in chunk.text
        assert chunk.page_or_sheet == "pagina 9"
        assert chunk.section == "Portabilidad"
    recovered = [line for chunk in tables for line in chunk.text.splitlines() if line in rows]
    assert Counter(recovered) == Counter(rows)
    assert all(estimate_tokens(chunk.text) <= budget for chunk in chunks)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_table_conditions_never_cross_a_page_or_section():
    header = "| Antiguedad | Porcentaje |"
    rows = [f"| {n} anios | {n * 3}% |" for n in range(20)]
    blocks = [
        TextBlock("Regla historica no aplicable", "Historico", "pagina 8"),
        TextBlock("Condicion vigente de retiro", "Retiro", "pagina 9"),
        TextBlock("\n".join([header, *rows]), "Retiro", "pagina 9", "table"),
    ]
    chunks = chunk_blocks(blocks, chunk_size_tokens=50, overlap_tokens=10)
    for chunk in chunks:
        if header in chunk.text:
            assert "Condicion vigente de retiro" in chunk.text
            assert "Regla historica" not in chunk.text


def test_table_row_too_wide_is_rejected_instead_of_splitting_its_values():
    block = TextBlock("| Antiguedad | Regla |\n| 7 anios | " + "condicion " * 100 + "|", "Retiro", kind="table")
    with pytest.raises(ValueError, match="fila"):
        chunk_blocks([block], chunk_size_tokens=50, overlap_tokens=10)


def test_table_scope_too_large_is_rejected_instead_of_discarding_conditions():
    blocks = [
        TextBlock("condicion " * 100, "Retiro"),
        TextBlock("| Antiguedad | Porcentaje |\n| 7 anios | 43% |", "Retiro", kind="table"),
    ]
    with pytest.raises(ValueError, match="condiciones"):
        chunk_blocks(blocks, chunk_size_tokens=50, overlap_tokens=10)


def candidate(document, *, section="Retiro", page="pagina 9", text="La prima es del 43%.", score=0.9):
    return ScoredPayload(score=score, payload={
        "document_id": document, "filename": f"{document}.pdf", "text": text,
        "scope": "corporate", "category": "prestaciones", "section": section,
        "page_or_sheet": page, "chunk_index": 0,
    })


def test_same_document_distinct_page_or_condition_keeps_its_provenance():
    items = [
        candidate("politica", section="Retiro", page="pagina 9"),
        candidate("politica", section="Aportacion mensual", page="pagina 5"),
        candidate("politica", section="Retiro", page="pagina 9", score=0.6),
    ]
    kept = deduplicate(items, preserve_sources=True)
    assert len(kept) == 2
    assert {item.payload["page_or_sheet"] for item in kept} == {"pagina 9", "pagina 5"}


def test_noncomparative_retrieval_keeps_requested_source_when_older_source_matches(monkeypatch, restricted_context):
    from app.rag import retriever as module

    items = [candidate("plan 2015"), candidate("plan 2022", score=0.8)]
    calls = []

    def search(**kwargs):
        calls.append(kwargs)
        return items

    store = SimpleNamespace(
        collection_for=lambda scope: scope, search=search, list_authorized_documents=lambda **kwargs: [],
    )
    llm = SimpleNamespace(embed_one=lambda text: [1.0, 0.0])
    monkeypatch.setattr(module, "indexing_fingerprint", lambda client: "synthetic-fingerprint")
    result = Retriever(store=store, llm=llm).retrieve(
        ctx=restricted_context,
        question="Segun el plan 2022, cual es la prima?",
        authorized_categories=frozenset({"prestaciones"}),
        comparative=False,
    )
    assert {e.document_id for e in result.evidences} == {"plan 2015", "plan 2022"}
    assert result.after_dedup == 2
    assert "prestaciones" in calls[0]["query_filter"].model_dump_json()
    assert "synthetic-fingerprint" in calls[0]["query_filter"].model_dump_json()
    assert calls[0]["score_threshold"] > 0
