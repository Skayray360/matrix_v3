# Creado por Aldo Garcia.
"""Inventario local por defecto; benchmark sintetico solo con --generate.

No abre documentos, SQL ni indices. Nunca guarda prompts, respuestas, URLs ni
mensajes de excepcion. La cola medida es la del cliente de este benchmark;
no representa la cola HTTP de Matrix RH ni la cola interna de Ollama.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.common.inference_errors import InferenceFailureError
from app.config import get_settings
from app.llm.provider import ModelClient, inference_deadline
from scripts.local_model_diagnostics import collect_report

SCHEMA = {
    "type": "object", "properties": {"unidades": {"type": "integer"}},
    "required": ["unidades"], "additionalProperties": False,
}
METRICS = (
    "latency_ms", "client_queue_ms", "wall_ms", "generation_attempts",
    "prompt_eval_count", "eval_count", "prompt_eval_cached_count",
    "total_duration_ns", "load_duration_ns", "prompt_eval_duration_ns",
    "eval_duration_ns", "generation_tokens_per_second",
)


def percentile(values: list[float], quantile: float) -> float | None:
    """Nearest-rank; los ausentes nunca se convierten en cero."""
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)] if values else None


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = {}
    for key in METRICS:
        values = [item[key] for item in samples if type(item.get(key)) in (int, float)
                  and math.isfinite(item[key]) and item[key] >= 0]
        metrics[key] = {"measured_samples": len(values), "p50": percentile(values, 0.5),
                        "p95": percentile(values, 0.95)}
    validated = [item["wall_ms"] for item in samples if item["status"] == "validated"]
    return {
        "samples": len(samples), "outcomes": dict(Counter(item["status"] for item in samples)),
        "metrics": metrics,
        "validated_wall_ms": {"measured_samples": len(validated), "p50": percentile(validated, 0.5),
                              "p95": percentile(validated, 0.95)},
        "extra_generation_attempts_observed": sum(max(0, item.get("generation_attempts", 1) - 1)
                                                  for item in samples),
    }


def _sample(profile: str, submitted: float) -> dict[str, Any]:
    started = time.perf_counter()
    sample: dict[str, Any] = {"status": "failed", "client_queue_ms": round((started - submitted) * 1000, 3)}
    client = None
    try:
        settings = get_settings()
        client = ModelClient()
        model = settings.ollama_deep_model if profile == "deep" else settings.ollama_fast_model
        context = settings.ollama_deep_num_ctx if profile == "deep" else settings.ollama_fast_num_ctx
        configured_output = settings.ollama_deep_max_tokens if profile == "deep" else settings.ollama_fast_max_tokens
        with inference_deadline():
            result = client.chat(
                model=model, execution_profile=profile, num_ctx=context,
                max_tokens=min(128, configured_output), temperature=0,
                response_schema=SCHEMA,
                messages=[{"role": "user", "content": (
                    'Fixture sintetico: el contenedor Alfa tiene 17 unidades y se retiran 5. '
                    'Devuelve solo el JSON con la propiedad "unidades" y la cantidad restante.'
                )}],
            )
        metrics = {"latency_ms": result.latency_ms, **result.performance_metrics()}
        sample.update({key: value for key, value in metrics.items() if key in METRICS
                       and type(value) in (int, float) and math.isfinite(value) and value >= 0})
        try:
            answer = json.loads(result.content)
            valid = isinstance(answer, dict) and set(answer) == {"unidades"} and type(answer["unidades"]) is int
            sample["status"] = "validated" if valid and answer["unidades"] == 12 else "invalid_answer"
        except (ValueError, TypeError):
            sample["status"] = "invalid_answer"
    except InferenceFailureError as exc:
        sample["failure_kind"] = exc.failure_kind.value
    except Exception:
        sample["failure_kind"] = "adapter_or_configuration_failed"
    finally:
        if client is not None:
            client.close()
        sample["wall_ms"] = round((time.perf_counter() - submitted) * 1000, 3)
    return sample


def run_benchmark(*, profile: str = "fast", samples: int = 5, concurrency: int = 1) -> dict[str, Any]:
    """Inferencia activa y acotada: CLI requiere --generate; nunca reenvia fallos."""
    if profile not in {"fast", "deep"} or not 1 <= samples <= 100 or not 1 <= concurrency <= 4:
        raise ValueError("Invalid benchmark bounds")
    report: dict[str, Any] = {
        "schema_version": 1, "observed_at_utc": datetime.now(UTC).isoformat(),
        "scope": "synthetic_local_model_benchmark", "profile": profile,
        "requested_samples": samples, "concurrency": concurrency, "status": "blocked",
        "samples": [], "errors": [], "corporate_rag_validated": False,
        "limitations": [
            "Measures one fixed synthetic arithmetic response, not retrieval or corporate answer quality.",
            "client_queue_ms is executor waiting; API admission and runtime queue are not observed.",
            "No forced unload or warmup; the first sample may include model loading.",
            "Runtime token/timing metrics describe the returned completion; retry totals may be incomplete.",
            "Generation attempts are reported only when returned by the adapter; failed attempts are unknown.",
            "No prompts, generated answers, documents, endpoint URLs or raw exceptions are retained.",
        ],
    }
    try:
        settings = get_settings()
        provider = settings.llm_deep_provider if profile == "deep" else settings.llm_provider
        if not settings.llm_local_only or provider != "ollama":
            report["errors"].append("local_ollama_required")
            return report
        if concurrency > settings.inference_max_inflight:
            report["errors"].append("concurrency_exceeds_configured_inference_limit")
            return report
        # Valida endpoint, allowlist y modelos antes de crear trabajos.
        validator = ModelClient()
        validator.close()
    except Exception:
        report["errors"].append("configuration_rejected")
        return report
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_sample, profile, time.perf_counter()) for _ in range(samples)]
        report["samples"] = [future.result() for future in futures]
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    report["summary"] = summarize(report["samples"])
    report["status"] = "completed" if all(item["status"] == "validated" for item in report["samples"]) else "failed"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true", help="Autoriza inferencia sintetica local activa.")
    parser.add_argument("--profile", choices=("fast", "deep"), default="fast")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-network", action="store_true", help="Inventario sin consultas a Ollama.")
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 100 or not 1 <= args.concurrency <= 4:
        parser.error("--samples debe ser 1..100 y --concurrency 1..4")
    if args.generate and args.no_network:
        parser.error("--generate y --no-network son excluyentes")
    try:
        report = (run_benchmark(profile=args.profile, samples=args.samples, concurrency=args.concurrency)
                  if args.generate else asyncio.run(collect_report(network=not args.no_network)))
    except Exception:
        report = {"errors": ["configuration_or_hardware_unavailable"]}
    serialized = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return int(bool(report.get("errors")) or report.get("status") in {"failed", "blocked"})


if __name__ == "__main__":
    raise SystemExit(main())
