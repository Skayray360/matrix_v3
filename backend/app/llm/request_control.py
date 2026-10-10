# Creado por Aldo Garcia.
"""Plazo y cancelacion de un turno, independientes del transporte y del modelo."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event

from app.common.inference_errors import InferenceFailureError, InferenceFailureKind

_deadline: ContextVar[float | None] = ContextVar("matrix_inference_deadline", default=None)
_cancel_events: ContextVar[tuple[Event, ...]] = ContextVar("matrix_inference_cancellation", default=())


def inference_cancelled() -> bool:
    return any(event.is_set() for event in _cancel_events.get())


def check_inference_control() -> None:
    """Punto cooperativo para etapas CPU/SQL; el transporte tambien vigila en espera."""
    if inference_cancelled():
        raise InferenceFailureError(InferenceFailureKind.CANCELLED, "La solicitud fue cancelada.")
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise InferenceFailureError(InferenceFailureKind.DEADLINE, "La consulta alcanzo su tiempo limite.")


def remaining_inference_seconds() -> float | None:
    check_inference_control()
    deadline = _deadline.get()
    return max(0.0, deadline - time.monotonic()) if deadline is not None else None


@contextmanager
def inference_control(
    seconds: float, *, cancel_event: Event | None = None, absolute_deadline: float | None = None,
):
    """El anidamiento solo reduce el plazo; ninguna etapa renueva el turno."""
    limits = [time.monotonic() + seconds]
    if _deadline.get() is not None:
        limits.append(_deadline.get())
    if absolute_deadline is not None:
        limits.append(absolute_deadline)
    deadline_token = _deadline.set(min(limits))
    events = _cancel_events.get()
    event_token = _cancel_events.set(events + ((cancel_event,) if cancel_event not in events and cancel_event else ()))
    try:
        check_inference_control()
        yield
    finally:
        _cancel_events.reset(event_token)
        _deadline.reset(deadline_token)
