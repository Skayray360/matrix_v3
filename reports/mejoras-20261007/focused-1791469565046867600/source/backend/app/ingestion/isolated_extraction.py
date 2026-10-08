# Creado por Aldo Garcia.
"""Limita RAM, tiempo y salida del parser en Linux y Windows (watchdog del proceso)."""

import json
import os

# Aislamiento del parser con interprete y modulo fijos; ver command y shell=False.
import subprocess  # nosec B404
import sys
import tempfile
import time
from pathlib import Path

import psutil

from app.common.errors import ExtractionFailedError
from app.config import get_settings
from app.config.settings import BACKEND_ROOT
from app.ingestion.loaders import ExtractedDocument
from app.rag.chunking import TextBlock
from app.security.upload_guard import validate_upload


def extract_document(data: bytes, *, filename: str) -> ExtractedDocument:
    settings = get_settings()
    # El corpus de carpetas llega por reconciliacion y no pasa por el endpoint
    # de adjuntos. Aplicar el mismo control antes de lanzar cualquier parser:
    # tipo real, tamano y limites OOXML tambien son obligatorios en esa ruta.
    # Un archivo de texto vaciado en disco debe retirar la generacion anterior
    # como "empty". Los adjuntos vacios ya se rechazan en su endpoint; lanzar
    # aqui una excepcion haria rollback y conservaria texto corporativo antiguo.
    if data or Path(filename).suffix.lower() not in {".txt", ".md", ".csv"}:
        validate_upload(data, filename=filename)
    with tempfile.TemporaryDirectory(prefix="matrix-extract-") as tmp:
        source = Path(tmp) / "input"
        destination = Path(tmp) / "output.json"
        source.write_bytes(data)
        # Mantener solo variables del sistema necesarias para Python y DLLs.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG"}
        }
        environment["PYTHONPATH"] = str(BACKEND_ROOT)
        command = [
            sys.executable,
            "-m",
            "app.ingestion.extract_worker",
            str(source),
            str(destination),
            Path(filename).name,
            str(settings.extraction_max_chars),
        ]
        # Los nombres de archivo son argumentos de datos, nunca comandos.
        process = subprocess.Popen(  # noqa: S603  # nosec B603
            command,
            shell=False,
            cwd=tmp,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started = time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic() - started > settings.extraction_timeout_seconds:
                    raise ExtractionFailedError("La extraccion excedio el tiempo permitido.")
                try:
                    rss = psutil.Process(process.pid).memory_info().rss
                    if rss > settings.extraction_memory_mb * 1024 * 1024:
                        raise ExtractionFailedError("La extraccion excedio la memoria permitida.")
                except psutil.NoSuchProcess:
                    break
                time.sleep(0.05)
            if process.wait(timeout=2) != 0 or not destination.is_file():
                raise ExtractionFailedError("El documento no pudo procesarse de forma segura.")
            if destination.stat().st_size > settings.extraction_max_chars * 12 + 1_000_000:
                raise ExtractionFailedError("La salida del parser excedio el limite.")
            result = json.loads(destination.read_text(encoding="utf-8"))
            return ExtractedDocument(
                blocks=[TextBlock(**block) for block in result["blocks"]],
                mime_type=result["mime_type"],
                warnings=result["warnings"],
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
