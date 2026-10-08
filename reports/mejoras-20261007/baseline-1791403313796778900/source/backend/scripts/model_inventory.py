# Creado por Aldo Garcia.
"""Inventario de pesos/hardware para TI. No registra claves ni preguntas."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
from pathlib import Path

import httpx
import psutil

from app.config import get_settings
from app.llm.provider import ModelClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    settings = get_settings()
    report = {
        "os": platform.platform(),
        "cpu": platform.processor(),
        "cpu_logical": psutil.cpu_count(),
        "ram_bytes": psutil.virtual_memory().total,
        "gpu": [],
        "models": [],
        "errors": [],
    }
    executable = shutil.which("nvidia-smi")
    if executable:
        try:
            result = subprocess.run(  # noqa: S603 - ejecutable resuelto, argumentos constantes; sin shell
                [executable, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            report["gpu"] = result.stdout.strip().splitlines()
        except (OSError, subprocess.SubprocessError) as exc:
            report["errors"].append(f"gpu: {type(exc).__name__}")
    try:
        inventory = ModelClient().list_models()
        digests = dict(inventory.digests)
        with httpx.Client(trust_env=False, timeout=20) as client:
            for label, provider, model in (
                ("fast", settings.llm_provider, settings.ollama_fast_model),
                ("deep", settings.llm_deep_provider, settings.ollama_deep_model),
                ("embedding", settings.llm_embedding_provider, settings.ollama_embedding_model),
            ):
                entry = {
                    "profile": label,
                    "provider": provider,
                    "model": model,
                    "digest": digests.get(model),
                    "present": inventory.has(model),
                }
                if provider == "ollama" and inventory.has(model):
                    response = client.post(settings.ollama_base_url.rstrip("/") + "/api/show", json={"model": model})
                    response.raise_for_status()
                    data = response.json()
                    entry["details"] = data.get("details", {})
                    entry["model_info"] = data.get("model_info", {})
                    entry["parameters"] = data.get("parameters", "")
                    # La plantilla puede ser extensa. Conservar su huella, no su texto.
                    from app.common.ids import sha256_text

                    entry["template_sha256"] = sha256_text(str(data.get("template", "")))
                report["models"].append(entry)
    except Exception as exc:  # noqa: BLE001 - diagnostico informa fallo sin filtrar secretos
        report["errors"].append(f"models: {type(exc).__name__}")
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
