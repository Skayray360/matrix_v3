# Creado por Aldo Garcia.
"""Alcance de resumenes y procedencia de comparativas con evidencia sintetica."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.orchestrator import Orchestrator
from app.llm.model_policy import Intent
from app.rag.retriever import RetrievalResult, Retriever, deduplicate
from app.rag.schemas import Evidence
from app.rag.vector_store import ScoredPayload

pytestmark = pytest.mark.unit


def evidence(*, private: bool) -> Evidence:
    category = "privados" if private else "prestaciones"
    return Evidence(
        source_id=f"{category}/documento.md#0",
        text="Menu del comedor." if private else "La prima vacacional es del 25%.",
        score=1.0 if private else 0.8, category=category, filename="documento.md",
        section="Reglas", page_or_sheet="", document_id=category, chunk_id=category,
        scope="conversation" if private else "corporate",
    )


class SummaryRetriever:
    def __init__(self, *, has_private: bool = True) -> None:
        self.has_private = has_private
        self.calls: list[tuple[str, dict]] = []

    def retrieve_attachment_summary(self, **kwargs) -> RetrievalResult:
        self.calls.append(("private", kwargs))
        return RetrievalResult(
            evidences=(evidence(private=True),) if self.has_private else (),
            fetched=int(self.has_private), after_dedup=int(self.has_private),
            best_score=float(self.has_private), used_private_scope=self.has_private,
            truncated=self.has_private,
        )

    def retrieve(self, **kwargs) -> RetrievalResult:
        self.calls.append(("corporate", kwargs))
        return RetrievalResult(
            evidences=(evidence(private=False),), fetched=1, after_dedup=1,
            best_score=0.8, authorized_categories=("prestaciones",),
        )


def gather(question: str, *, has_private: bool = True):
    retriever = SummaryRetriever(has_private=has_private)
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._retriever = retriever
    result, _, tools = orchestrator._gather_evidence(
        ctx=SimpleNamespace(user_id="user"), conversation=SimpleNamespace(id="conversation"),
        question=question, intent=Intent.DOCUMENT_SUMMARY,
        authorized_categories=frozenset({"prestaciones"}), comparative=False,
    )
    return result, tools, retriever.calls


@pytest.mark.parametrize("question", [
    "Resume la politica corporativa de vacaciones",
    "Resume la política de vacaciones",
    "Resume el reglamento interno",
    "Resume la documentacion de la empresa",
])
def test_explicit_corporate_summary_does_not_use_an_unrelated_attachment(question):
    result, tools, calls = gather(question)
    assert [scope for scope, _ in calls] == ["corporate"]
    assert calls[0][1]["include_private"] is False
    assert calls[0][1]["authorized_categories"] == frozenset({"prestaciones"})
    assert [item.category for item in result.evidences] == ["prestaciones"]
    assert tools == ["rag"]
    assert not result.used_private_scope


@pytest.mark.parametrize("question", [
    "Resume el adjunto",
    "Resume la politica corporativa adjunta",
    "Resume este archivo",
    "Resume el documento cargado",
    "Resume esto",
])
def test_private_and_generic_summaries_keep_the_attachment_flow(question):
    result, tools, calls = gather(question)
    assert [scope for scope, _ in calls] == ["private"]
    assert tools == ["private_attachment_summary"]
    assert result.used_private_scope
    assert [item.category for item in result.evidences] == ["privados"]


@pytest.mark.parametrize("question", [
    "Resume el adjunto y la politica corporativa de vacaciones",
    "Resume el reglamento interno junto con este archivo",
])
def test_explicit_combined_summary_preserves_both_scopes_and_limits(question):
    result, tools, calls = gather(question)
    assert [scope for scope, _ in calls] == ["private", "corporate"]
    assert calls[1][1]["include_private"] is False
    assert {item.category for item in result.evidences} == {"privados", "prestaciones"}
    assert tools == ["private_attachment_summary", "rag"]
    assert result.used_private_scope and result.truncated
    assert result.fetched == result.after_dedup == 2


def test_missing_explicit_attachment_is_not_replaced_with_corporate_content():
    result, tools, calls = gather("Resume el adjunto", has_private=False)
    assert [scope for scope, _ in calls] == ["private"]
    assert not result.has_evidence
    assert tools == []


def test_generic_summary_without_attachments_keeps_corporate_fallback():
    result, tools, calls = gather("Resume esto", has_private=False)
    assert [scope for scope, _ in calls] == ["private", "corporate"]
    assert result.has_evidence and not result.used_private_scope
    assert tools == ["rag"]


def candidate(document: str, category: str, text: str, score: float) -> ScoredPayload:
    return ScoredPayload(score=score, payload={
        "document_id": document, "category": category, "text": text,
        "scope": "corporate", "filename": f"{document}.md",
    })


@pytest.mark.parametrize("second_category", ["empresa_a", "empresa_b"])
def test_comparison_keeps_equal_policies_from_distinct_sources(second_category):
    items = [
        candidate("a", "empresa_a", "La prima es del 25%.", 0.9),
        candidate("b", second_category, "La prima es del 25%.", 0.8),
        candidate("a", "empresa_a", "LA PRIMA ES DEL 25%.", 0.7),
    ]
    result = deduplicate(items, preserve_sources=True)
    assert [item.payload["document_id"] for item in result] == ["a", "b"]
    assert len(deduplicate(items)) == 1


def test_comparison_keeps_contained_text_from_another_policy():
    items = [
        candidate("a", "empresa_a", "La prima es del 25%. Solo para personal permanente.", 0.9),
        candidate("b", "empresa_b", "La prima es del 25%.", 0.8),
    ]
    assert len(deduplicate(items, preserve_sources=True)) == 2


def test_retrieval_comparison_can_cite_both_equal_policies(monkeypatch):
    from app.rag import retriever as retriever_module

    items = [
        candidate("a", "empresa_a", "La prima es del 25%.", 0.9),
        candidate("b", "empresa_b", "La prima es del 25%.", 0.8),
    ]
    store = SimpleNamespace(collection_for=lambda scope: scope, search=lambda **kwargs: items)
    llm = SimpleNamespace(embed_one=lambda question: [1.0, 0.0])
    monkeypatch.setattr(retriever_module, "indexing_fingerprint", lambda client: "synthetic")
    result = Retriever(store=store, llm=llm).retrieve(
        ctx=SimpleNamespace(user_id="user"), question="Compara las primas de las dos empresas",
        authorized_categories=frozenset({"empresa_a", "empresa_b"}), comparative=True,
    )
    assert result.distinct_categories() == ("empresa_a", "empresa_b")
    assert len(set(result.source_ids())) == 2
