# Creado por Aldo Garcia.
"""Limite del cuerpo antes de multipart; aplica tambien sin Nginx."""

import asyncio
import tempfile
import time

from starlette.responses import JSONResponse

from app.common.errors import MatrixError
from app.config import get_settings
from app.security.admission import admission


class UploadBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        if b"multipart/form-data" not in headers.get(b"content-type", b""):
            return await self.app(scope, receive, send)
        settings = get_settings()
        try:
            with (
                admission("upload", settings.upload_max_inflight),
                tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as body,
            ):
                total = 0
                deadline = time.monotonic() + settings.extraction_timeout_seconds
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    message = await asyncio.wait_for(receive(), timeout=remaining)
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    total += len(chunk)
                    if total > settings.upload_body_max_bytes:
                        response = JSONResponse(
                            {"code": "file_too_large", "message": "La carga excede el limite permitido."},
                            status_code=413,
                        )
                        return await response(scope, receive, send)
                    body.write(chunk)
                    if not message.get("more_body", False):
                        break
                body.seek(0)
                exhausted = False

                async def replay():
                    nonlocal exhausted
                    if exhausted:
                        return await receive()
                    chunk = body.read(64 * 1024)
                    more = body.tell() < total
                    exhausted = not more
                    return {"type": "http.request", "body": chunk, "more_body": more}

                return await self.app(scope, replay, send)
        except TimeoutError:
            response = JSONResponse(
                {"code": "validation_error", "message": "La carga excedio el tiempo permitido."}, status_code=408
            )
            return await response(scope, receive, send)
        except MatrixError as exc:
            return await JSONResponse(exc.to_public_dict(), status_code=exc.status_code)(scope, receive, send)
