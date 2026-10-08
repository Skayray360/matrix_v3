# Creado por Aldo Garcia.
"""Regresiones: diagnosticar fallos de proveedor/SQL sin registrar contenido privado."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from sqlalchemy.exc import OperationalError, StatementError

from app.agents.chat_queue import _failure_fields
from app.common.errors import DatabaseUnavailableError, OllamaUnavailableError
from app.common.logging import JsonFormatter, bind_request_context, reset_request_context
from app.llm.provider import ModelClient

pytestmark = pytest.mark.unit
PRIVATE_MARKER = "contenido-privado-que-no-debe-aparecer-en-logs"


@pytest.mark.parametrize("status", [400, 429, 503])
def test_fallo_http_conserva_diagnostico_y_request_id_sin_cuerpo_o_prompt(caplog, status):
    caplog.handler.setFormatter(JsonFormatter())
    client = ModelClient(client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"error": PRIVATE_MARKER})
    )))
    token = bind_request_context(request_id="solicitud-sintetica")
    try:
        with pytest.raises(OllamaUnavailableError):
            client._post("/api/chat", {"model": client.settings.ollama_fast_model,
                                     "messages": [{"role": "user", "content": PRIVATE_MARKER}]})
    finally:
        reset_request_context(token)
        client.close()
    records = [record for record in caplog.records if record.message == "llm.request_failed"]
    assert records and records[-1].http_status == status
    assert records[-1].failure_kind == ("busy" if status in {429, 503} else "http")
    assert PRIVATE_MARKER not in caplog.text
    entries = [json.loads(line) for line in caplog.text.splitlines()]
    assert next(e for e in entries if e["message"] == "llm.request_failed")["request_id"] == "solicitud-sintetica"


def test_timeout_no_refleja_el_mensaje_privado_de_la_excepcion(caplog):
    def fail(request):
        raise httpx.ReadTimeout(PRIVATE_MARKER, request=request)

    client = ModelClient(client=httpx.Client(transport=httpx.MockTransport(fail)))
    try:
        with pytest.raises(OllamaUnavailableError):
            client._post("/api/chat", {"model": client.settings.ollama_fast_model})
    finally:
        client.close()
    records = [record for record in caplog.records if record.message == "llm.request_failed"]
    assert records and records[-1].failure_kind == "timeout"
    assert PRIVATE_MARKER not in caplog.text


def test_error_sql_no_publica_sentencia_parametros_ni_mensaje_del_driver():
    error = OperationalError("SQL " + PRIVATE_MARKER, {"dato": PRIVATE_MARKER}, Exception(2013, PRIVATE_MARKER))
    metadata = _failure_fields(error)
    assert metadata["database_error_number"] == 2013
    assert PRIVATE_MARKER not in str(metadata)


@pytest.mark.parametrize("error", [
    StatementError(PRIVATE_MARKER, "INSERT " + PRIVATE_MARKER, {"content": PRIVATE_MARKER}, ValueError(PRIVATE_MARKER)),
    OperationalError("SELECT " + PRIVATE_MARKER, {"content": PRIVATE_MARKER}, Exception(2013, PRIVATE_MARKER)),
])
def test_formatter_no_serializa_sql_parametros_ni_detalle_arbitrario(error):
    record = logging.LogRecord("matrix", logging.ERROR, "", 0, "http.error", (), None)
    record.exc_info = (type(error), error, None)
    record.error_detail = PRIVATE_MARKER
    record.request_id = "solicitud-segura"
    data = JsonFormatter().format(record)
    assert PRIVATE_MARKER not in data
    parsed = json.loads(data)
    assert parsed["error_type"] == type(error).__name__
    assert parsed["request_id"] == "solicitud-segura"
    if isinstance(error, OperationalError):
        assert parsed["database_error_number"] == 2013


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [False, True])
async def test_handlers_retenienen_codigo_y_tipo_sin_contenido_privado(caplog, typed):
    from fastapi import Request

    from app.api.middleware import matrix_error_handler, unhandled_error_handler

    request = Request({"type": "http", "state": {"request_id": "solicitud-segura"}})
    error = OperationalError("SELECT " + PRIVATE_MARKER, {"content": PRIVATE_MARKER}, Exception(2013, PRIVATE_MARKER))
    caplog.handler.setFormatter(JsonFormatter())
    if typed:
        wrapped = DatabaseUnavailableError(detail=str(error))
        wrapped.__cause__ = error
        response = await matrix_error_handler(request, wrapped)
        assert response.status_code == 503
    else:
        response = await unhandled_error_handler(request, error)
        assert response.status_code == 500
    assert PRIVATE_MARKER not in caplog.text
    assert PRIVATE_MARKER.encode() not in response.body
    entries = [json.loads(line) for line in caplog.text.splitlines()]
    assert entries[-1]["database_error_number"] == 2013
    assert entries[-1]["request_id"] == "solicitud-segura"


def test_echo_sql_se_reduce_a_evento_sin_sentencia():
    record = logging.LogRecord("sqlalchemy.engine.Engine", logging.INFO, "", 0, "INSERT " + PRIVATE_MARKER, (), None)
    data = JsonFormatter().format(record)
    assert PRIVATE_MARKER not in data
    assert json.loads(data)["message"] == "db.engine_event"
