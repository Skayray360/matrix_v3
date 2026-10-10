# Creado por Aldo Garcia.
"""Cupos por proceso. Solo workers aceptados pueden esperar, con deadline acotado."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Condition, Lock
from time import monotonic

from app.common.errors import OllamaUnavailableError
from app.llm.request_control import check_inference_control, remaining_inference_seconds

_lock = Lock()
_changed = Condition(_lock)
_active: dict[str, int] = {}
_wait_until: ContextVar[float | None] = ContextVar("matrix_admission_wait", default=None)


@contextmanager
def wait_for_inference(seconds: float):
    """Habilita espera de cupo solo dentro del worker; nunca en hilos HTTP."""
    token = _wait_until.set(monotonic() + seconds)
    try:
        yield
    finally:
        _wait_until.reset(token)


@contextmanager
def admission(key: str, limit: int):
    if key == "inference":
        check_inference_control()
    with _changed:
        while _active.get(key, 0) >= limit:
            if key == "inference":
                check_inference_control()
            deadline = _wait_until.get() if key == "inference" else None
            remaining = deadline - monotonic() if deadline is not None else 0
            if remaining <= 0:
                raise OllamaUnavailableError("Capacidad ocupada. Intente de nuevo mas tarde.", detail="admission_full")
            turn_remaining = remaining_inference_seconds() if key == "inference" else None
            if turn_remaining is not None:
                remaining = min(remaining, turn_remaining)
            # Una cancelacion no produce notify en esta Condition; comprobarla
            # periodicamente evita retener un cupo HTTP durante toda la espera.
            _changed.wait(timeout=min(remaining, 0.1))
        if key == "inference":
            check_inference_control()
        _active[key] = _active.get(key, 0) + 1
    try:
        yield
    finally:
        with _changed:
            _active[key] -= 1
            if _active[key] == 0:
                del _active[key]
            _changed.notify_all()


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_active)
