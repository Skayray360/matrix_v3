# Creado por Aldo Garcia.
"""HTTP local cancelable incluso antes de recibir cabeceras o el primer byte."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.llm.request_control import inference_cancelled


async def _request_json(
    method: str, url: str, *, payload: dict[str, Any] | None, headers: dict[str, str] | None,
    timeout: float, max_bytes: int,
) -> dict[str, Any]:
    # Un cliente por peticion permite cerrar SOLO su socket. No se cierra el
    # singleton compartido ni se descarga un modelo usado por otra persona.
    async with (
        httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
        client.stream(
            method, url, json=payload, headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        ) as response,
    ):
        if response.status_code >= 300:
            raise InferenceFailureError(
                InferenceFailureKind.BUSY if response.status_code in (429, 503) else InferenceFailureKind.HTTP,
                http_status=response.status_code,
            )
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > max_bytes:
                raise InferenceFailureError(InferenceFailureKind.RESPONSE_SIZE)
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("respuesta no es objeto")
        return data


async def _watch_cancellation() -> None:
    while not inference_cancelled():
        await asyncio.sleep(0.05)
    raise InferenceFailureError(InferenceFailureKind.CANCELLED, "La solicitud fue cancelada.")


async def _bounded_request(**arguments: Any) -> dict[str, Any]:
    request = asyncio.create_task(_request_json(**arguments))
    cancellation = asyncio.create_task(_watch_cancellation())
    try:
        done, _ = await asyncio.wait(
            {request, cancellation}, timeout=arguments["timeout"], return_when=asyncio.FIRST_COMPLETED,
        )
        # Cancelar gana si coincide con la llegada de una respuesta tardia.
        if inference_cancelled():
            raise InferenceFailureError(InferenceFailureKind.CANCELLED, "La solicitud fue cancelada.")
        if request in done:
            return await request
        if cancellation in done:
            await cancellation
        raise TimeoutError("Presupuesto HTTP agotado")
    finally:
        request.cancel()
        cancellation.cancel()
        # Esperar el cierre del socket antes de liberar el cupo de inferencia.
        await asyncio.gather(request, cancellation, return_exceptions=True)


def request_json(method: str, url: str, **arguments: Any) -> dict[str, Any]:
    """Fachada sincrona para los workers; no crea threads de inferencia huerfanos."""
    return asyncio.run(_bounded_request(method=method, url=url, **arguments))
