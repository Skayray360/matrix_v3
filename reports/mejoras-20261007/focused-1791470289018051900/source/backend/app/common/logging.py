# Creado por Aldo Garcia.
"""Logging estructurado JSON con redaccion obligatoria.

Cada linea de log es un objeto JSON con los campos definidos en la seccion 19 de
la especificacion. La redaccion se hace en el formatter (no en los llamadores)
para que sea imposible saltarsela por descuido.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.common.redaction import redact_value

#: Contexto por request. Se propaga a todos los logs emitidos durante el request
#: sin obligar a pasar el request_id por parametro en cada funcion.
#: El valor por defecto es ``None`` (no un dict): un contenedor mutable como
#: default de un ContextVar se comparte entre contextos y acabaria mezclando
#: campos de requests distintos en los logs.
request_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "matrix_request_context", default=None
)


def _current_context() -> dict[str, Any]:
    return request_context.get() or {}

#: Atributos internos de ``logging.LogRecord`` que no deben duplicarse.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"asctime", "message", "taskName"}
)


def safe_error_fields(error: BaseException | None) -> dict[str, str | int]:
    """Metadata diagnostica sin texto SQL, parametros ni mensajes del driver."""
    if error is None:
        return {}
    fields: dict[str, str | int] = {"error_type": type(error).__name__}
    cause = error.__cause__
    if cause is not None:
        fields["cause_type"] = type(cause).__name__
    sql_error = cause if cause is not None else error
    original = getattr(sql_error, "orig", None)
    arguments = getattr(original, "args", ())
    if isinstance(arguments, tuple) and arguments and type(arguments[0]) is int:
        fields["database_error_number"] = arguments[0]
    return fields


class JsonFormatter(logging.Formatter):
    """Serializa cada registro como JSON de una sola linea, ya redactado."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # SQLAlchemy echo puede contener sentencias aunque hide_parameters
            # oculte valores. Nunca convierte esa salida en un log de contenido.
            "message": "db.engine_event" if record.name.startswith("sqlalchemy.engine") else record.getMessage(),
        }
        payload.update(_current_context())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_") and key not in {"error_detail", "error_message"}:
                payload[key] = value
        if record.exc_info:
            # str(exc) puede incluir la pregunta RH en parametros SQL; tampoco
            # se serializan tracebacks ni el detalle tecnico arbitrario.
            _, exc_value, _ = record.exc_info
            payload.update(safe_error_fields(exc_value))
        safe = redact_value(payload)
        return json.dumps(safe, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Instala el formatter JSON como unico handler de la raiz."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Uvicorn duplica los mensajes de acceso; se delega todo a la raiz.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logger = logging.getLogger(noisy)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def bind_request_context(**fields: Any) -> contextvars.Token[dict[str, Any] | None]:
    """Agrega campos al contexto del request actual."""
    current = dict(_current_context())
    current.update({k: v for k, v in fields.items() if v is not None})
    return request_context.set(current)


def reset_request_context(token: contextvars.Token[dict[str, Any] | None]) -> None:
    request_context.reset(token)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
