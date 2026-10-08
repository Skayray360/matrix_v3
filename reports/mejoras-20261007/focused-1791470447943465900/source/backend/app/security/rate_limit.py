# Creado por Aldo Garcia.
"""Rate limiting por ventana deslizante en memoria.

Alcance deliberado: Matrix RH se despliega como un proceso backend unico detras
del proxy, por lo que un limitador en memoria es suficiente y no anade una
dependencia (Redis) al instalador Windows. Si en el futuro se escala a varias
replicas, la interfaz permite sustituir el backend por uno compartido sin tocar
las rutas.

Se aplica **tambien en modo local** (requisito 5.3): el proveedor de pruebas no
es una excusa para dejar el login abierto a fuerza bruta.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiter:
    """Ventana deslizante de 60 segundos por clave."""

    def __init__(self, *, window_seconds: int = 60) -> None:
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int) -> RateLimitResult:
        """Registra un intento y decide si se permite."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = int(self._window - (now - bucket[0])) + 1
                return RateLimitResult(allowed=False, remaining=0, retry_after_seconds=retry_after)
            bucket.append(now)
            return RateLimitResult(
                allowed=True, remaining=max(0, limit - len(bucket)), retry_after_seconds=0
            )

    def reset(self, key: str | None = None) -> None:
        """Limpia el estado (usado por las pruebas)."""
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
