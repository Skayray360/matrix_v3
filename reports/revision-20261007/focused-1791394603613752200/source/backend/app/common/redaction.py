# Creado por Aldo Garcia.
"""Redaccion de secretos antes de escribir a logs o reportes.

Requisito 12/19: ningun password, client secret, API key, bearer token,
connection string ni llave privada puede aparecer en logs. La redaccion se
aplica en el formatter de logging, de modo que ni siquiera un ``logger.info``
descuidado pueda filtrar un secreto.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

#: Nombres de campo que se redactan siempre, sin mirar el valor.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "cookie",
        "set-cookie",
        "session",
        "session_id",
        "private_key",
        "connection_string",
        "dsn",
        "database_url",
        "app_secret_key",
        "qdrant_api_key",
        "entra_client_secret",
        "password_hash",
    }
)

#: Patrones de secretos incrustados en texto libre.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Connection strings con credenciales embebidas.  # secrets-scan: allow (comentario que describe el patron)
    (re.compile(r"(?i)\b([a-z0-9+.\-]+://[^\s:/@]+):([^\s@]+)@"), r"\1:" + REDACTED + "@"),
    # Bearer tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer " + REDACTED),
    # Bloques PEM de llaves privadas
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        REDACTED,
    ),
    # Hashes Argon2 (no son secretos reutilizables pero no deben viajar a logs)
    (re.compile(r"\$argon2(?:id|i|d)\$[^\s\"']+"), REDACTED),
    # Asignaciones tipo clave=valor con nombre sensible
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|client_secret|api[_-]?key|token)\b\s*[=:]\s*"
            r"[\"']?([^\s\"',;}]{3,})"
        ),
        r"\1=" + REDACTED,
    ),
    # JWT completos
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"), REDACTED),
)


def redact_text(value: str) -> str:
    """Aplica todos los patrones de redaccion sobre un texto."""
    result = value
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Redacta recursivamente estructuras de datos.

    Se limita la profundidad para que un objeto ciclico o muy anidado no pueda
    convertir el logging en una denegacion de servicio.
    """
    if _depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.strip().lower() in SENSITIVE_KEYS:
                out[key] = REDACTED
            else:
                out[key] = redact_value(item, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple | set):
        return type(value)(redact_value(v, _depth=_depth + 1) for v in value)  # type: ignore[call-arg]
    return value


def truncate_for_log(text: str, limit: int = 400) -> str:
    """Evita volcar documentos completos en los logs (requisito 19)."""
    clean = redact_text(text)
    if len(clean) <= limit:
        return clean
    return clean[:limit] + f"...[+{len(clean) - limit} chars]"
