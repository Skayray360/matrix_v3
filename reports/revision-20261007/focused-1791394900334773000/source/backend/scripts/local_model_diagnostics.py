# Creado por Aldo Garcia.
"""Diagnostico local de solo lectura, sin generar texto ni cargar modelos.

Desde backend: python -m scripts.local_model_diagnostics --output var/reports/local-models.json
No inspecciona documentos, prompts, credenciales, plantillas ni rutas de usuario.
El JSON distingue configuracion declarada de observaciones del servidor consultado.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import platform
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import psutil

from app.config import Settings, get_settings
from app.llm.provider import ModelClient

MAX_RESPONSE_BYTES = 1_048_576
CAPABILITIES = {"completion", "embedding", "vision", "tools", "insert", "thinking"}


def _label(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[\w .:+/-]{1,160}", value) and not value.startswith("/"):
        return value
    return None


def _number(value: Any) -> int | float | None:
    return value if type(value) in (int, float) and 0 <= value < 10**18 else None


def _digest(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", value) else None


def effective_configuration(s: Settings) -> dict[str, Any]:
    """Lista positiva de campos; nunca serializar Settings ni respuestas HTTP completas."""
    profiles = []
    for role, provider, model, context, output in (
        ("fast", s.llm_provider, s.ollama_fast_model, s.ollama_fast_num_ctx, s.ollama_fast_max_tokens),
        ("deep", s.llm_deep_provider, s.ollama_deep_model, s.ollama_deep_num_ctx, s.ollama_deep_max_tokens),
        ("embedding", s.llm_embedding_provider, s.ollama_embedding_model, None, None),
    ):
        profiles.append({"role": role, "provider": provider, "model": _label(model),
                         "context_tokens": context, "max_output_tokens": output,
                         "thinking_policy": s.llm_fast_thinking if role == "fast" else (
                             s.llm_deep_thinking if role == "deep" else None
                         ),
                         "timeout_seconds": s.llm_fast_timeout_seconds if role == "fast" else (
                             s.llm_deep_timeout_seconds if role == "deep" else None
                         )})
    return {
        "local_only": s.llm_local_only,
        "profiles": profiles,
        "inference": {
            "temperature": s.llm_temperature, "general_temperature": s.llm_general_temperature,
            "summary_temperature": s.llm_summary_temperature, "retry_temperature": s.llm_retry_temperature,
            "top_p": s.llm_top_p, "keep_alive": _label(s.ollama_keep_alive),
            "request_deadline_seconds": s.llm_request_deadline_seconds,
            "http_timeout_seconds": s.ollama_timeout_seconds,
            "transient_retries": s.llm_max_transient_retries,
            "completion_retries": s.llm_completion_retries,
            "structured_thinking_policy": s.llm_structured_thinking,
            "planner_max_output_tokens": s.llm_planner_max_tokens,
            "memory_max_output_tokens": s.llm_memory_max_tokens,
            "summary_parallel_batches": s.ollama_summary_parallel_batches,
            # Las claves previas se conservan como valores declarados, no como
            # promesas de reintentos/paralelismo que el ejecutor ya no aplica.
            "legacy_configuration_fields": ["transient_retries", "summary_parallel_batches"],
            "effective": {"transient_retries": 0, "summary_parallel_batches": 1,
                          "summary_execution": "sequential"},
        },
        "rag": {"vector_store": s.rag_vector_store, "qdrant_mode": s.qdrant_mode.value,
                "chunk_estimated_tokens": s.rag_chunk_size_tokens,
                "chunk_overlap_estimated_tokens": s.rag_chunk_overlap_tokens,
                "fetch_k": s.rag_fetch_k, "top_k": s.rag_top_k,
                "min_similarity": s.rag_min_similarity, "mmr_lambda": s.rag_mmr_lambda,
                "embedding_dimension": s.ollama_embedding_dimension,
                "embedding_cache_entries": s.ollama_embedding_cache_size,
                "summary_scan_max_chunks": s.rag_summary_scan_max_chunks,
                "summary_max_chunks": s.rag_summary_max_chunks},
        "admission_per_api_process": {"chat": s.chat_max_inflight, "inference": s.inference_max_inflight,
                                      "upload": s.upload_max_inflight},
        "prompt_templates": "omitted", "endpoint_urls": "omitted",
    }


def local_hardware(*, gpu: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "scope": "machine_running_this_command; may differ from inference_server",
        "os": platform.system(), "architecture": platform.machine(),
        "cpu_logical": os.cpu_count(), "cpu_physical": psutil.cpu_count(logical=False),
        "ram_total_bytes": None, "ram_available_bytes": None,
        "gpu": {"available": False, "reason": "disabled" if not gpu else "nvidia_smi_not_found"},
    }
    try:
        memory = psutil.virtual_memory()
        result.update(ram_total_bytes=memory.total, ram_available_bytes=memory.available)
    except (OSError, psutil.Error):
        result["ram_error"] = "unavailable"
    executable = shutil.which("nvidia-smi") if gpu else None
    if not executable:
        return result
    try:
        # Ruta resuelta localmente; consulta fija, sin shell, stdin ni comandos del usuario.
        completed = subprocess.run(  # noqa: S603
            [executable, "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=True, shell=False,
        )
        devices = []
        for row in csv.reader(completed.stdout.splitlines()):
            if len(row) != 6:
                continue
            numeric = [float(value.strip()) if value.strip().replace(".", "", 1).isdigit() else None
                       for value in row[1:5]]
            devices.append({"name": _label(row[0].strip()), "vram_total_mib": numeric[0],
                            "vram_used_mib": numeric[1], "utilization_pct_snapshot": numeric[2],
                            "temperature_c_snapshot": numeric[3], "driver_version": _label(row[5].strip())})
        result["gpu"] = {"available": bool(devices), "devices": devices,
                         "reason": None if devices else "unsupported_output"}
    except (OSError, subprocess.SubprocessError, ValueError):
        result["gpu"] = {"available": False, "reason": "nvidia_smi_failed"}
    return result


async def _metadata(client: httpx.AsyncClient, base_url: str, path: str, *, timeout: float,
                    model: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    try:
        # asyncio.timeout limita el tiempo total, incluido un servidor que envia datos lentamente.
        async with asyncio.timeout(timeout):
            async with client.stream("POST" if model else "GET", base_url + path,
                                     json={"model": model} if model else None,
                                     timeout=timeout, follow_redirects=False) as response:
                if response.status_code != 200:
                    return None, f"http_{response.status_code}"
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        return None, "response_too_large"
                data = json.loads(content)
                return (data, None) if isinstance(data, dict) else (None, "invalid_metadata")
    except (TimeoutError, httpx.TimeoutException):
        return None, "timeout"
    except httpx.HTTPError:
        return None, "connection_failed"
    except (ValueError, UnicodeError):
        return None, "invalid_metadata"


def _selected_models(data: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    models = data.get("models") if data else None
    if not isinstance(models, list) or any(not isinstance(model, dict) for model in models):
        return None
    return models


def _model_metadata(data: dict[str, Any]) -> dict[str, Any]:
    details = data.get("details")
    details = details if isinstance(details, dict) else {}
    info = data.get("model_info")
    info = info if isinstance(info, dict) else {}
    capabilities = data.get("capabilities")
    return {
        "format": _label(details.get("format")), "family": _label(details.get("family")),
        "parameter_size": _label(details.get("parameter_size")),
        "quantization": _label(details.get("quantization_level")),
        "context_lengths": {key: value for key, value in info.items()
                            if re.fullmatch(r"\w+\.context_length", key) and _number(value) is not None},
        "embedding_lengths": {key: value for key, value in info.items()
                              if re.fullmatch(r"\w+\.embedding_length", key) and _number(value) is not None},
        "capabilities": sorted({item for item in capabilities if isinstance(item, str) and item in CAPABILITIES})
                        if isinstance(capabilities, list) else None,
    }


async def collect_report(*, timeout: float = 3, gpu: bool = True, network: bool = True,
                         client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    settings = get_settings()
    report: dict[str, Any] = {
        "schema_version": 1, "observed_at_utc": datetime.now(UTC).isoformat(),
        "configuration": effective_configuration(settings), "hardware": local_hardware(gpu=gpu),
        "ollama": {"available": False, "models": []}, "errors": [],
        "limitations": ["No inference, embeddings, model loading, database or document access performed.",
                        "GPU utilization is a snapshot, not sustained throughput or a capacity result.",
                        "Ollama scheduling variables are not exposed by these endpoints; unverified.",
                        "Context limits are model metadata; quality at that length is not established.",
                        "Compatible and cloud providers are reported from configuration without contacting them."],
    }
    configured = [profile for profile in report["configuration"]["profiles"] if profile["provider"] == "ollama"]
    if not network or not configured:
        report["ollama"]["reason"] = "network_disabled" if not network else "not_configured"
        return report
    parsed = urlparse(settings.ollama_base_url)
    approved = {host.strip() for host in settings.llm_local_hosts.split(",")}
    # Esta herramienta siempre aplica la lista local, incluso con cloud habilitado en la aplicacion.
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in approved or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        report["errors"].append({"component": "ollama", "code": "endpoint_not_approved_local"})
        return report
    if any(not profile["model"] or "cloud" in profile["model"].casefold() for profile in configured):
        report["errors"].append({"component": "ollama", "code": "model_not_approved_local"})
        return report
    try:
        # Reutiliza la validacion real de endpoints/proveedores. No invoca list_models ni genera credenciales.
        validator = ModelClient()
        validator.close()
    except Exception:
        report["errors"].append({"component": "configuration", "code": "adapter_configuration_rejected"})
        return report
    owns_client = client is None
    client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
    try:
        responses = {}
        for endpoint in ("version", "tags", "ps"):
            data, error = await _metadata(client, settings.ollama_base_url, f"/api/{endpoint}", timeout=timeout)
            responses[endpoint] = data
            if error:
                report["errors"].append({"component": endpoint, "code": error})
        version = responses["version"]
        report["ollama"].update(available=any(value is not None for value in responses.values()),
                                version=_label(version.get("version")) if version else None)
        inventory = _selected_models(responses["tags"])
        running = _selected_models(responses["ps"])
        for endpoint, items in (("tags", inventory), ("ps", running)):
            if responses[endpoint] is not None and items is None:
                report["errors"].append({"component": endpoint, "code": "invalid_metadata"})
        for model in dict.fromkeys(profile["model"] for profile in configured):
            entry: dict[str, Any] = {"model": model,
                "profiles": [p["role"] for p in configured if p["model"] == model],
                "present": any(m.get("name", m.get("model")) == model for m in inventory)
                           if inventory is not None else None,
                "loaded": any(m.get("name", m.get("model")) == model for m in running)
                          if running is not None else None}
            for items, target in ((inventory, "installed"), (running, "running")):
                match = next((m for m in (items or []) if m.get("name", m.get("model")) == model), {})
                entry[target] = {"digest": _digest(match.get("digest")), "size_bytes": _number(match.get("size"))}
                if target == "running":
                    entry[target].update(size_vram_bytes=_number(match.get("size_vram")),
                                         context_length=_number(match.get("context_length")))
            data, error = await _metadata(client, settings.ollama_base_url, "/api/show", timeout=timeout, model=model)
            if error:
                entry["metadata_error"] = error
                report["errors"].append({"component": "show", "profile": entry["profiles"], "code": error})
            else:
                entry.update(_model_metadata(data or {}))
            report["ollama"]["models"].append(entry)
    finally:
        if owns_client:
            await client.aclose()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=3, help="Tiempo total por consulta HTTP (0.1 a 10 s).")
    parser.add_argument("--no-gpu", action="store_true")
    parser.add_argument("--no-network", action="store_true", help="Solo configuracion y hardware de esta maquina.")
    args = parser.parse_args(argv)
    if not 0.1 <= args.timeout <= 10:
        parser.error("--timeout debe estar entre 0.1 y 10 segundos")
    try:
        report = asyncio.run(collect_report(timeout=args.timeout, gpu=not args.no_gpu, network=not args.no_network))
    except Exception:
        report = {"errors": [{"component": "diagnostic", "code": "configuration_or_hardware_unavailable"}]}
    serialized = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
        except OSError:
            print(json.dumps({"errors": [{"component": "output", "code": "write_failed"}]}))
            return 1
    print(serialized)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
