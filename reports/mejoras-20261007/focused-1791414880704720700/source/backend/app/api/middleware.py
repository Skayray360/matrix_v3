# Creado por Aldo Garcia.
"""Middleware HTTP: request id, cabeceras de seguridad y manejo de errores.

Las cabeceras se aplican tambien desde el backend (no solo desde Nginx) para que
el sistema siga siendo seguro si alguien lo ejecuta sin proxy delante, que es
exactamente lo que ocurre en el arranque por doble clic en Windows.
"""

from __future__ import annotations

import time

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.common.errors import ErrorCode, MatrixError
from app.common.ids import new_id
from app.common.logging import bind_request_context, get_logger, reset_request_context, safe_error_fields

logger = get_logger(__name__)

#: CSP estricta: sin ``unsafe-eval``; el bundle de Vite no lo necesita.
#: ``style-src`` admite ``unsafe-inline`` porque React inyecta estilos calculados
#: en algunos componentes; los scripts, que son el vector real de XSS, no.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    # Se evita que respuestas de la API queden cacheadas por un proxy intermedio.
    "Cache-Control": "no-store",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Asigna un ``request_id`` y lo propaga a logs y respuestas."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = new_id()
        request.state.request_id = request_id
        token = bind_request_context(request_id=request_id, http_path=request.url.path)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            reset_request_context(token)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "http.request",
            extra={
                "request_id": request_id,
                "http_method": request.method,
                "http_path": request.url.path,
                "http_status": response.status_code,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Aplica las cabeceras de seguridad a toda respuesta."""

    def __init__(self, app, *, hsts: bool = False) -> None:  # noqa: ANN001
        super().__init__(app)
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if self._hsts:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


async def matrix_error_handler(request: Request, exc: MatrixError) -> JSONResponse:
    """Traduce errores tipados a respuestas seguras.

    Al cliente van solo ``code``, ``message`` y ``request_id``. El detalle tecnico
    se sustituye en logs por metadata tipada y se correlaciona por ``request_id``.
    """
    request_id = getattr(request.state, "request_id", None)
    log = logger.warning if exc.status_code < 500 else logger.error
    log(
        "http.error",
        extra={
            "request_id": request_id,
            "error_code": str(exc.code),
            "http_status": exc.status_code,
            **safe_error_fields(exc),
        },
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_public_dict(request_id),
        headers={"Retry-After": "5"} if exc.status_code in (429, 503) else None,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Ultima red: nunca se filtra un stack trace al cliente."""
    request_id = getattr(request.state, "request_id", None)
    logger.error(
        "http.unhandled_error",
        extra={"request_id": request_id, "error_type": type(exc).__name__},
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={
            "code": str(ErrorCode.INTERNAL_ERROR),
            "message": "Ocurrio un error interno.",
            "request_id": request_id,
        },
    )
