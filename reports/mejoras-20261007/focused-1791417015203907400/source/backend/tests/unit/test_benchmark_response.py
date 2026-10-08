# Creado por Aldo Garcia.
"""Benchmark: opt-in, estadisticas y confidencialidad con adaptador sintetico."""

import json

import pytest

from app.config import Settings
from app.llm.ollama_client import ChatResult
from scripts import benchmark_response as benchmark

pytestmark = pytest.mark.unit


def test_inventory_default_never_generates(monkeypatch, capsys):
    async def inventory(**kwargs):
        assert kwargs == {"network": False}
        return {"errors": [], "scope": "inventory"}

    monkeypatch.setattr(benchmark, "collect_report", inventory)
    monkeypatch.setattr(benchmark, "run_benchmark", lambda **_: pytest.fail("Generation was not authorized"))
    assert benchmark.main(["--no-network"]) == 0
    assert json.loads(capsys.readouterr().out)["scope"] == "inventory"


@pytest.mark.parametrize("options", [
    ["--generate", "--no-network"], ["--samples", "0"], ["--samples", "101"], ["--concurrency", "5"],
])
def test_cli_rejects_invalid_or_conflicting_bounds(options):
    with pytest.raises(SystemExit) as exc:
        benchmark.main(options)
    assert exc.value.code == 2


def test_percentiles_preserve_unknown_metrics_and_failed_sample_latency():
    summary = benchmark.summarize([
        {"status": "validated", "wall_ms": 10, "client_queue_ms": 0, "generation_attempts": 2},
        {"status": "failed", "wall_ms": 100, "client_queue_ms": 15},
        {"status": "invalid_answer", "wall_ms": 20, "client_queue_ms": 5},
    ])
    assert summary["metrics"]["wall_ms"] == {"measured_samples": 3, "p50": 20, "p95": 100}
    assert summary["validated_wall_ms"] == {"measured_samples": 1, "p50": 10, "p95": 10}
    assert summary["metrics"]["eval_count"] == {"measured_samples": 0, "p50": None, "p95": None}
    assert summary["extra_generation_attempts_observed"] == 1


def test_rejects_cloud_or_excess_concurrency_before_creating_clients(monkeypatch):
    monkeypatch.setattr(benchmark, "ModelClient", lambda: pytest.fail("Forbidden client creation"))
    monkeypatch.setattr(benchmark, "get_settings", lambda: Settings(_env_file=None, llm_local_only=False))
    assert benchmark.run_benchmark()["errors"] == ["local_ollama_required"]
    monkeypatch.setattr(benchmark, "get_settings", lambda: Settings(_env_file=None, inference_max_inflight=1))
    assert benchmark.run_benchmark(concurrency=2)["errors"] == ["concurrency_exceeds_configured_inference_limit"]


@pytest.mark.parametrize("content,expected", [
    ('{"unidades":12}', "validated"), ('{"unidades":13}', "invalid_answer"),
    ('{"unidades":12,"secret":"private-answer"}', "invalid_answer"),
    ("private-answer", "invalid_answer"),
])
def test_synthetic_generation_checks_answer_and_reports_only_metrics(monkeypatch, content, expected):
    settings = Settings(_env_file=None, inference_max_inflight=2)
    monkeypatch.setattr(benchmark, "get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    calls = []

    class Client:
        def close(self):
            pass

        def chat(self, **kwargs):
            calls.append(kwargs)
            return ChatResult(content=content, model="private-model", latency_ms=5,
                              eval_count=2, eval_duration_ns=1_000_000_000, generation_attempts=2)

    monkeypatch.setattr(benchmark, "ModelClient", Client)
    report = benchmark.run_benchmark(samples=3, concurrency=2)
    assert len(calls) == 3
    assert all(call["max_tokens"] == 128 and call["response_schema"] == benchmark.SCHEMA for call in calls)
    assert report["summary"]["outcomes"] == {expected: 3}
    assert report["summary"]["extra_generation_attempts_observed"] == 3
    assert report["summary"]["metrics"]["generation_tokens_per_second"]["p95"] == 2
    assert "private" not in json.dumps(report)
    assert all(item["wall_ms"] >= item["client_queue_ms"] >= 0 for item in report["samples"])
    assert report["corporate_rag_validated"] is False


def test_exception_text_and_urls_never_escape(monkeypatch):
    settings = Settings(_env_file=None)
    monkeypatch.setattr(benchmark, "get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)

    class Client:
        def close(self):
            pass

        def chat(self, **kwargs):
            raise RuntimeError("private-answer at https://secret:password@host")

    monkeypatch.setattr(benchmark, "ModelClient", Client)
    report = benchmark.run_benchmark(samples=1)
    assert report["status"] == "failed"
    assert report["samples"][0]["failure_kind"] == "adapter_or_configuration_failed"
    assert "private" not in json.dumps(report) and "password" not in json.dumps(report)
