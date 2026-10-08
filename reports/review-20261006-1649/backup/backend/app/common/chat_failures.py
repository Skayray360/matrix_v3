# Creado por Aldo Garcia.
"""Clasificacion publica de fallos; nunca conserva un mensaje del proveedor."""

from app.common.errors import ForbiddenError, OllamaUnavailableError


def chat_failure_code(error: Exception) -> str:
    if isinstance(error, ForbiddenError):
        return "forbidden"
    if isinstance(error, OllamaUnavailableError):
        kind = str(getattr(error, "failure_kind", ""))
        if kind in {"timeout", "deadline"} or error.detail in {"ReadTimeout", "ConnectTimeout"}:
            return "timeout"
        if kind in {"incomplete", "empty", "reasoning", "schema", "format", "context_limit"}:
            return "model_incomplete"
        return "inference_unavailable"
    return "generation_failed"
