# Creado por Aldo Garcia.
"""Fuentes explicitas sobreviven MMR; fechas personales no seleccionan ediciones."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.rag.document_selection import explicit_document_candidates
from app.rag.retriever import Retriever, maximal_marginal_relevance
from app.rag.vector_store import AuthorizedDocument, ScoredPayload
from tests.conftest import make_context

pytestmark = pytest.mark.unit


def candidate(filename, score, **overrides):
    payload = {
        "filename": filename, "document_id": filename, "text": "Regla de aportacion del plan.",
        "scope": "corporate", "category": "prestaciones", "chunk_index": 0,
        "_vector": [1.0, 0.0],
    }
    return ScoredPayload(score, payload | overrides)


@pytest.mark.parametrize("question", (
    "Segun la Plática de Pensiones de Diciembre2022.pdf, que condiciones hay?",
    "Segun la PLATICA DE PENSIONES DE DICIEMBRE 2022, que condiciones hay?",
    "Explica la plática pensiones diciembre2022; ingrese en marzo de 2016.",
))
def test_complete_filename_and_title_match_across_accents_and_separators(question):
    old = candidate("Plática de Pensiones de Diciembre2020.pdf", 0.95)
    requested = candidate("Plática de Pensiones de Diciembre2022.pdf", 0.7)
    assert explicit_document_candidates(question, [old, requested]) == [requested]


@pytest.mark.parametrize("question", (
    "Ingrese en diciembre2022, que condiciones tienen las pensiones?",
    "Mi antiguedad es de 2022, que dice la platica?",
    "Que dice la platica de pensiones?",
    "Que documentos de 2022 tienen informacion de pensiones?",
))
def test_personal_date_and_partial_title_do_not_anchor_an_edition(question):
    requested = candidate("Plática de Pensiones de Diciembre2022.pdf", 0.7)
    assert explicit_document_candidates(question, [requested]) == []


def test_single_topic_word_or_year_requires_explicit_filename():
    short = candidate("plan.pdf", 0.7)
    year = candidate("2022.pdf", 0.8)
    assert explicit_document_candidates("Ingrese al plan en 2022.", [short, year]) == []
    assert explicit_document_candidates("Consulta plan.pdf y 2022.pdf", [short, year]) == [year, short]
    assert explicit_document_candidates("Consulta a PDF", [candidate("a.pdf", 0.7)]) == []


def test_imported_latin_accent_escape_matches_title_without_changing_source_identity():
    filename = "PLATICA DE PLAN DE PENSIONES POR JUBILACI#U00d3N DICIEMBRE 2022.pdf"
    source = candidate(filename, 0.7)
    question = "Según la plática de plan de pensiones por jubilación diciembre 2022?"
    assert explicit_document_candidates(question, [source]) == [source]
    assert source.payload["filename"] == filename and source.payload["document_id"] == filename


def test_only_best_chunk_per_document_is_reserved_and_scope_remains_distinct():
    first = candidate("Plan de Ahorro.pdf", 0.9)
    later = candidate("Plan de Ahorro.pdf", 0.8, chunk_index=1)
    private = candidate("Plan de Ahorro.pdf", 0.7, scope="conversation", owner_user_id="u", conversation_id="c")
    assert explicit_document_candidates("Que indica Plan de Ahorro.pdf?", [later, private, first]) == [first, private]


def test_preferred_input_cannot_add_a_candidate_outside_the_authorized_list():
    allowed = candidate("Permitido.pdf", 0.8)
    foreign = candidate("Otro usuario.pdf", 0.99)
    assert maximal_marginal_relevance(
        [1.0, 0.0], [allowed], top_k=1, lambda_mult=0.65, preferred=[foreign],
    ) == [allowed]


def retrieval_setup(monkeypatch, corporate, *, private=(), top_k=1):
    settings = Settings(_env_file=None, app_env="test", rag_top_k=top_k, rag_fetch_k=24)
    monkeypatch.setattr("app.rag.retriever.get_settings", lambda: settings)
    monkeypatch.setattr("app.rag.retriever.indexing_fingerprint", lambda _: "synthetic-fingerprint")
    calls = []

    def search(**kwargs):
        calls.append(kwargs)
        items = list(corporate if kwargs["collection"] == "corporate" else private)
        for condition in kwargs["query_filter"].must:
            if condition.key == "document_id":
                items = [item for item in items if item.payload["document_id"] == condition.match.value]
        return items

    def catalog(**kwargs):
        items = corporate if kwargs["scope"] == "corporate" else private
        return [AuthorizedDocument(
            item.payload["document_id"], item.payload["filename"], "active-generation", 1,
        ) for item in items]

    store = SimpleNamespace(collection_for=lambda scope: scope, search=search, list_authorized_documents=catalog)
    llm = SimpleNamespace(embed_one=lambda _: [1.0, 0.0])
    return Retriever(store=store, llm=llm), calls, settings


def test_requested_source_survives_where_unanchored_mmr_discards_it(monkeypatch):
    old = candidate("Platica Pensiones Diciembre2020.pdf", 0.95)
    requested = candidate("Platica Pensiones Diciembre2022.pdf", 0.7)
    items = [old, requested]
    assert maximal_marginal_relevance([1.0, 0.0], items, top_k=1, lambda_mult=0.65) == [old]
    retriever, calls, settings = retrieval_setup(monkeypatch, items)
    result = retriever.retrieve(
        ctx=make_context(), question="Segun Platica Pensiones Diciembre2022, que condiciones hay?",
        authorized_categories=frozenset({"prestaciones"}),
    )
    assert [item.filename for item in result.evidences] == [requested.payload["filename"]]
    assert result.best_score == 0.7  # No artificial confidence boost.
    assert result.fetched == 1 and result.after_dedup == 1
    assert len(calls) == 1  # Una busqueda, ya restringida al documento autorizado.
    assert calls[0]["score_threshold"] == settings.rag_min_similarity
    serialized = calls[0]["query_filter"].model_dump_json()
    assert "prestaciones" in serialized and "corporate" in serialized and "synthetic-fingerprint" in serialized


def test_explicit_comparison_keeps_both_named_documents_in_same_category(monkeypatch):
    named_first = candidate("Reglas de Pensiones 2020.pdf", 0.8)
    named_second = candidate("Reglas de Pensiones 2022.pdf", 0.6)
    other_category = candidate("Otra politica.pdf", 0.95, category="nomina")
    retriever, _, _ = retrieval_setup(monkeypatch, [other_category, named_first, named_second], top_k=2)
    result = retriever.retrieve(
        ctx=make_context(), question="Compara Reglas de Pensiones 2020 y Reglas de Pensiones 2022",
        authorized_categories=frozenset({"prestaciones", "nomina"}), comparative=True,
    )
    assert [item.document_id for item in result.evidences] == [
        named_first.payload["document_id"], named_second.payload["document_id"],
    ]


def test_private_named_source_still_uses_owner_and_conversation_filters(monkeypatch):
    ctx = make_context()
    source = candidate(
        "Plan Privado.pdf", 0.7, scope="conversation", owner_user_id=ctx.user_id, conversation_id="thread-a",
    )
    retriever, calls, _ = retrieval_setup(monkeypatch, [], private=[source])
    result = retriever.retrieve(
        ctx=ctx, question="Segun Plan Privado.pdf?", authorized_categories=frozenset(), conversation_id="thread-a",
    )
    assert len(result.evidences) == 1 and result.used_private_scope
    assert len(calls) == 1
    private = calls[0]["query_filter"].model_dump_json()
    assert ctx.user_id in private and "owner_user_id" in private and "thread-a" in private
    assert "conversation" in private and "synthetic-fingerprint" in private


def test_unavailable_named_document_does_not_create_or_fetch_evidence(monkeypatch):
    available = candidate("Permitido.pdf", 0.8)
    retriever, calls, _ = retrieval_setup(monkeypatch, [available])
    result = retriever.retrieve(
        ctx=make_context(), question="Segun Restringido.pdf?", authorized_categories=frozenset({"prestaciones"}),
    )
    assert not calls
    assert not result.evidences and result.clarification
