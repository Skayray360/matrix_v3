# Creado por Aldo Garcia.
"""Metricas reales del contrato Ollama: unidades, ausencia y privacidad."""

import logging

import httpx
import pytest

from app.llm.ollama_client import OllamaClient

pytestmark = pytest.mark.unit


def _chat(metrics):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"message": {"content": "contenido sintetico reservado"}, "done": True, **metrics}
        )
    )
    with httpx.Client(transport=transport) as http:
        return OllamaClient(client=http).chat(
            model="synthetic-local", messages=[{"role": "user", "content": "pregunta sintetica reservada"}]
        )


def test_runtime_durations_keep_nanoseconds_and_compute_generation_rate(caplog):
    with caplog.at_level(logging.INFO, logger="app.llm.ollama_client"):
        result = _chat({
            "prompt_eval_count": 600,
            "prompt_eval_cached_count": 200,
            "eval_count": 120,
            "total_duration": 5_000_000_000,
            "load_duration": 1_000_000_000,
            "prompt_eval_duration": 1_000_000_000,
            "eval_duration": 3_000_000_000,
        })
    metrics = result.performance_metrics()
    assert metrics["generation_tokens_per_second"] == 40
    assert metrics["load_duration_ns"] == 1_000_000_000
    assert metrics["prompt_eval_cached_count"] == 200
    record = next(record for record in caplog.records if record.message == "llm.chat")
    assert record.generation_tokens_per_second == 40
    assert "reservada" not in str(record.__dict__)
    assert "reservado" not in str(record.__dict__)
    assert not any("ttft" in key for key in metrics)


@pytest.mark.parametrize("duration", [None, 0, -1, True, "300", 1.5])
def test_unavailable_or_invalid_duration_is_not_an_invented_throughput(duration):
    metrics = _chat({"eval_count": 30, "eval_duration": duration}).performance_metrics()
    assert metrics["generation_tokens_per_second"] is None
    assert metrics["prompt_eval_count"] is None


def test_invalid_optional_counts_do_not_break_valid_content():
    result = _chat({"eval_count": True, "prompt_eval_count": "500", "eval_duration": 1_000_000_000})
    assert result.content == "contenido sintetico reservado"
    assert result.eval_count is None
    assert result.prompt_eval_count is None
    assert result.performance_metrics()["generation_tokens_per_second"] is None
