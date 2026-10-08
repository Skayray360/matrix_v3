# Creado por Aldo Garcia.
"""Identificadores opacos y utilidades de hashing.

Requisito 16 de la API: todos los IDs expuestos son opacos (UUID4) para que no
sea posible enumerar recursos ni inferir volumen de datos.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime


def new_id() -> str:
    """UUID4 en formato canonico: no revela orden ni cardinalidad."""
    return str(uuid.uuid4())


def new_opaque_token(nbytes: int = 32) -> str:
    """Token aleatorio criptograficamente seguro (sesiones, state, nonce)."""
    return secrets.token_urlsafe(nbytes)


def sha256_bytes(data: bytes) -> str:
    """Huella de integridad de un documento. No se usa para contrasenas."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def stable_hash(value: str, *, salt: str) -> str:
    """Hash con clave para seudonimizar valores en logs (ej. conjunto de roles)."""
    return hmac.new(salt.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def utcnow() -> datetime:
    """Instante actual en UTC, siempre timezone-aware."""
    return datetime.now(UTC)


def utcnow_naive() -> datetime:
    """Instante actual en UTC sin tzinfo.

    MySQL ``DATETIME`` no almacena zona horaria: si se guardaran valores
    timezone-aware, al releerlos vendrian naive y cualquier comparacion en
    Python (``expires_at < now``) explotaria. Todo lo que se persiste usa esta
    funcion; el criterio unico es "naive == UTC".
    """
    return datetime.now(UTC).replace(tzinfo=None)
