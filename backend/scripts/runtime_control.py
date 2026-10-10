# Creado por Aldo Garcia.
"""Solicitud local de parada cooperativa del proceso registrado, sin API remota."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import psutil


@contextmanager
def runtime_identity(root: Path) -> Iterator[dict[str, Any]]:
    """Registra el Python real, también detrás del launcher venv de Windows."""
    try:
        process = psutil.Process(os.getpid())
        created = process.create_time()
    except psutil.Error:
        # Sin permiso de inspección no se acredita identidad: sigue disponible
        # la parada forzada por árbol verificado y el lease temporal antiguo.
        yield {"pid": os.getpid(), "created_at": None}
        return
    identity = {
        "pid": process.pid, "created_at": created,
        "root": str(root.resolve()), "nonce": uuid4().hex,
    }
    path = root / "knowledge-base" / "state" / "run" / "matrixrh-backend.identity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{identity['nonce']}.tmp")
    try:
        with temporary.open("x", encoding="ascii") as output:
            json.dump(identity, output, ensure_ascii=True)
        os.replace(temporary, path)
        yield identity
    finally:
        temporary.unlink(missing_ok=True)
        # Nunca borra el registro de un reemplazo que ya haya arrancado.
        with suppress(OSError, ValueError):
            if not path.is_symlink() and json.loads(path.read_text(encoding="ascii")) == identity:
                path.unlink(missing_ok=True)


def watch_shutdown(
    server: Any, path: Path, finished: Event, *,
    process_id: int | None = None, created_at: float | None = None,
) -> None:
    expected = process_id if process_id is not None else os.getpid()
    try:
        expected_creation = created_at if created_at is not None else psutil.Process(expected).create_time()
    except psutil.Error:
        return  # No detener un PID cuya identidad no se pudo verificar.
    while not finished.wait(0.25):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 512:
                continue
            request = json.loads(path.read_text(encoding="ascii"))
            if not isinstance(request, dict) or type(request.get("pid")) is not int:
                continue
            requested_creation = request.get("created_at")
            if type(requested_creation) not in (int, float) or not math.isfinite(requested_creation):
                continue
            # CIM y psutil representan el mismo FILETIME con precisión distinta;
            # tolerancia de menos de 1 ms, nunca un PID por sí solo.
            if request["pid"] != expected or abs(requested_creation - expected_creation) >= 0.001:
                continue
            path.unlink(missing_ok=True)
            server.should_exit = True
            return
        except (OSError, UnicodeError, ValueError):
            continue
