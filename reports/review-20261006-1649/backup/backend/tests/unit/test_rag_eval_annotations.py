# Creado por Aldo Garcia.
"""Contratos del golden set: verdad documental sintetica, sin modelos ni BD."""

from __future__ import annotations

import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.agents.prompts import DENIED_ANSWER, INSUFFICIENT_ANSWER
from app.ingestion.loaders import extract_markdown
from app.rag.chunking import chunk_blocks
from scripts import rag_eval

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
GOLDEN = yaml.safe_load(rag_eval.GOLDEN_SET.read_text(encoding="utf-8"))
GROUNDED_CASES = [case for case in GOLDEN["cases"] if case["type"] == "grounded"]


@pytest.fixture(scope="module")
def corpus_chunks():
    """Solo extraccion/chunking reales: jamas usa retrieval como respuesta correcta."""
    chunks = {}
    for path in (ROOT / "data" / "synthetic_test_data" / "knowledge").glob("*/*.md"):
        if path.name == "README.md":
            continue
        drafts = chunk_blocks(extract_markdown(path.read_bytes()).blocks, **GOLDEN["source_profile"])
        for draft in drafts:
            source_id = f"{path.parent.name}/{path.name}#{draft.index}"
            assert source_id not in chunks
            chunks[source_id] = draft.text
    return chunks


def test_original_36_case_ids_and_order_are_preserved():
    expected = (
        [f"prest-{index:02}" for index in range(1, 9)]
        + [f"{prefix}-{index:02}" for prefix in ("nom", "rec", "rel", "sal") for index in range(1, 6)]
        + ["comp-01", "comp-02", "unsup-01", "unsup-02", "unsup-03", "leak-01", "leak-02", "leak-03"]
    )
    assert [case["id"] for case in GOLDEN["cases"][:36]] == expected
    assert [case["id"] for case in GOLDEN["cases"][36:]] == [f"route-{index:02}" for index in range(1, 8)]
    assert len(GROUNDED_CASES) == 23


@pytest.mark.parametrize("case", GROUNDED_CASES, ids=lambda case: case["id"])
def test_every_grounded_source_contains_its_reviewed_fact(case, corpus_chunks):
    expected_ids = case["expected_source_ids"]
    quotes = case["expected_source_quotes"]
    assert set(expected_ids) == set(quotes)
    assert len(expected_ids) == len(set(expected_ids))
    for source_id in expected_ids:
        assert source_id in corpus_chunks, "El extractor/chunker cambio: revisar anotacion antes de medir."
        actual = " ".join(corpus_chunks[source_id].split())
        assert quotes[source_id]
        for quote in quotes[source_id]:
            assert " ".join(quote.split()) in actual
    # Los keywords originales permanecen; esta prueba valida su presencia en
    # la anotacion. No confunde coincidencia lexical con correccion semantica.
    facts = " ".join(quote for source_quotes in quotes.values() for quote in source_quotes).casefold()
    assert any(str(word).casefold() in facts for word in case["expected_keywords"])


def test_recruitment_count_is_nine_actual_steps(corpus_chunks):
    text = corpus_chunks["reclutamiento/proceso-de-reclutamiento.md#1"]
    assert re.findall(r"^(\d+)\. ", text, re.MULTILINE) == [str(number) for number in range(1, 10)]


def test_comparisons_require_both_reviewed_sources():
    for case in GROUNDED_CASES:
        if case.get("comparative"):
            assert len(case["expected_source_ids"]) == 2
            metrics = rag_eval._source_metrics(case, case["expected_source_ids"][:1])
            assert metrics["retrieval_hit"] is True
            assert metrics["recall_at_k"] == 0.5


def test_source_metrics_do_not_treat_same_category_as_correct_source():
    case = GROUNDED_CASES[0]
    metrics = rag_eval._source_metrics(case, ["prestaciones/politica-vacaciones.md#0"])
    assert metrics["source_correct"] is False
    assert metrics["retrieval_hit"] is False
    assert metrics["recall_at_k"] == 0.0
    assert metrics["reciprocal_rank"] == 0.0


def test_source_rank_is_computed_over_the_returned_order():
    case = GROUNDED_CASES[0]
    metrics = rag_eval._source_metrics(case, ["unrelated/file.md#0", *case["expected_source_ids"]])
    assert metrics["reciprocal_rank"] == 0.5
    assert metrics["recall_at_k"] == 1.0
    assert metrics["source_correct"] is False


def test_retrieval_report_never_claims_response_quality():
    report = rag_eval.EvaluationReport(mode="retrieval", results=[
        rag_eval.CaseResult("unsupported", "Matrix", "unsupported", True),
        rag_eval.CaseResult("grounded", "Matrix", "grounded", True, retrieval_hit=True),
    ])
    metrics = report.metrics()
    assert metrics["retrieval_hit_rate_pct"] == 100.0
    assert metrics["unsupported_claim_rate_pct"] is None
    assert metrics["grounded_answer_rate_pct"] is None
    assert metrics["answer_pass_rate_pct"] is None
    assert metrics["answer_measured_cases"] == 0


def test_failed_answer_does_not_lower_retrieval_hit_metric():
    report = rag_eval.EvaluationReport(mode="full", results=[
        rag_eval.CaseResult("case", "Matrix", "grounded", False,
                            retrieval_hit=True, answer_pass=False, grounding_pass=False),
    ])
    metrics = report.metrics()
    assert metrics["retrieval_hit_rate_pct"] == 100.0
    assert metrics["answer_pass_rate_pct"] == 0.0
    assert metrics["grounded_answer_rate_pct"] == 0.0
    assert report.as_dict()["cases"][0]["retrieval_hit"] is True


def test_empty_report_rates_are_unmeasured():
    metrics = rag_eval.EvaluationReport(mode="full").metrics()
    assert all(value is None for key, value in metrics.items() if key.endswith("_pct"))


def test_changed_chunking_blocks_obsolete_annotations_with_actionable_message(monkeypatch):
    monkeypatch.setattr(rag_eval, "get_settings", lambda: SimpleNamespace(
        rag_chunk_size_tokens=600, rag_chunk_overlap_tokens=120,
    ))
    with pytest.raises(ValueError, match="Revise los hechos y source_ids con el nuevo chunking"):
        rag_eval.load_cases()


@pytest.mark.parametrize("answer", [DENIED_ANSWER, INSUFFICIENT_ANSWER])
def test_refusal_contract_rejects_a_marker_followed_by_unsupported_content(answer):
    assert rag_eval._is_refusal(answer)
    assert not rag_eval._is_refusal(answer + " El director tiene 900 acciones.")


def _mock_runtime(monkeypatch, outcome, authorized=("prestaciones",)):
    monkeypatch.setattr(rag_eval, "require_disposable_database", lambda: None)
    monkeypatch.setattr(rag_eval, "session_scope", lambda: nullcontext(object()))
    monkeypatch.setattr(rag_eval, "_context", lambda _db, name: SimpleNamespace(username=name))
    monkeypatch.setattr(rag_eval, "get_policy_engine", lambda: SimpleNamespace(
        effective_categories=lambda _ctx: authorized,
    ))
    monkeypatch.setattr(rag_eval, "Orchestrator", lambda: SimpleNamespace(handle_chat=lambda *_a, **_kw: outcome))
    monkeypatch.setattr(rag_eval, "MemoryService", lambda: SimpleNamespace(
        create_conversation=lambda *_a, **_kw: object(),
    ))
    monkeypatch.setattr(rag_eval, "Retriever", lambda: SimpleNamespace(retrieve=lambda **_kw: outcome))


def _outcome(answer, sources, *, grounded=True):
    return SimpleNamespace(
        answer=answer, evidences=[SimpleNamespace(source_id=sid, category=sid.split("/")[0]) for sid in sources],
        grounded=grounded, model="synthetic-double", latency_ms=1,
    )


def test_full_flow_measures_sources_independently_of_failed_generation(monkeypatch):
    case = GROUNDED_CASES[0]
    _mock_runtime(monkeypatch, _outcome(INSUFFICIENT_ANSWER, case["expected_source_ids"], grounded=False))
    result = rag_eval.evaluate_full([case]).results[0]
    assert not result.passed
    assert result.answer_pass is False
    assert result.retrieval_hit is True
    assert result.recall_at_k == 1.0
    assert result.source_correct is True


def test_retrieval_rejects_wrong_chunk_in_the_correct_category(monkeypatch):
    case = GROUNDED_CASES[0]
    _mock_runtime(monkeypatch, _outcome("", ["prestaciones/politica-vacaciones.md#0"]))
    result = rag_eval.evaluate_retrieval([case]).results[0]
    assert result.passed is False
    assert result.retrieval_hit is False
    assert result.answer_pass is None


def test_comparative_retrieval_rejects_incomplete_sources(monkeypatch):
    case = next(case for case in GROUNDED_CASES if case["id"] == "comp-01")
    _mock_runtime(monkeypatch, _outcome("", case["expected_source_ids"][:1]), ("nomina", "relaciones_laborales"))
    result = rag_eval.evaluate_retrieval([case]).results[0]
    assert result.passed is False
    assert result.recall_at_k == 0.5


def test_full_flow_checks_effective_acl_in_addition_to_explicit_forbidden_list(monkeypatch):
    case = GROUNDED_CASES[0]
    _mock_runtime(monkeypatch, _outcome("20", case["expected_source_ids"]), ())
    result = rag_eval.evaluate_full([case]).results[0]
    assert result.acl_leak is True
    assert result.passed is False


def test_full_denied_case_requires_a_refusal_not_an_absent_category_word(monkeypatch):
    case = next(case for case in GOLDEN["cases"] if case["id"] == "nom-04")
    _mock_runtime(monkeypatch, _outcome("Se paga el 15 y el ultimo dia habil.", []))
    assert rag_eval.evaluate_full([case]).results[0].passed is False
    _mock_runtime(monkeypatch, _outcome(DENIED_ANSWER, [], grounded=False))
    assert rag_eval.evaluate_full([case]).results[0].passed is True


def test_full_eval_blocks_non_disposable_database_before_creating_services(monkeypatch):
    def blocked():
        raise ValueError("Base no descartable")

    monkeypatch.setattr(rag_eval, "require_disposable_database", blocked)
    monkeypatch.setattr(rag_eval, "Orchestrator", lambda: pytest.fail("Servicio creado antes de guardia"))
    with pytest.raises(ValueError, match="Base no descartable"):
        rag_eval.evaluate_full([])
