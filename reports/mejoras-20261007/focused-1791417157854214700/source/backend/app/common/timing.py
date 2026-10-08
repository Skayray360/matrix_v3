# Creado por Aldo Garcia.
"""Duraciones por etapa, sin preguntas, documentos ni respuestas en los logs."""

from contextvars import ContextVar
from functools import wraps
from time import perf_counter

from app.common.logging import get_logger

logger = get_logger(__name__)
_active: ContextVar[frozenset[str]] = ContextVar("timed_stages", default=frozenset())


def timed_stage(stage: str):
    """Mide la llamada exterior de cada etapa; soporta resumenes recursivos."""
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            if stage in _active.get():
                return function(*args, **kwargs)
            token = _active.set(_active.get() | {stage})
            started = perf_counter()
            status = "failed"
            try:
                result = function(*args, **kwargs)
                status = "ok"
                return result
            finally:
                _active.reset(token)
                logger.info("chat.stage", extra={"stage": stage, "status": status,
                                                "duration_ms": round((perf_counter() - started) * 1000, 2)})
        return measured
    return decorate
