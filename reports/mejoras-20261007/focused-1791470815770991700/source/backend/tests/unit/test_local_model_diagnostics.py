# Creado por Aldo Garcia.
"""Diagnostico: evita inferencia/cloud y filtra metadatos sin ocultar fallos."""

import asyncio
import json

import httpx
import pytest

from app.config import Settings
from scripts import local_model_diagnostics as diagnostic

pytestmark = pytest.mark.unit


def configure(monkeypatch, **kwargs):
    settings = Settings(_env_file=None, **kwargs)
    monkeypatch.setattr(diagnostic, "get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)
    monkeypatch.setattr(diagnostic, "local_hardware", lambda **_: {"gpu": {"available": False}})
    return settings


@pytest.mark.asyncio
async def test_reads_only_metadata_and_does_not_leak_config_or_response_secrets(monkeypatch):
    settings = configure(monkeypatch, llm_api_key="secret-key", llm_system_prefix="secret-prompt",
                         llm_query_template="secret-query {text}",
                         database_url="mysql+pymysql://user:secret-password@localhost/db")  # secrets-scan: allow (fixture sintetico sin datos reales)
    requests = []

    def server(request):
        requests.append(request)
        assert "authorization" not in request.headers
        if request.url.path == "/api/show":
            assert request.method == "POST"
            assert set(json.loads(request.content)) == {"model"}
            return httpx.Response(200, json={
                "modelfile": "secret-file", "template": "secret-template", "parameters": "stop secret-stop",
                "details": {"family": "gemma", "quantization_level": "Q4_K_M", "parameter_size": "4B",
                            "secret": "secret-details"},
                "model_info": {"gemma.context_length": 8192, "gemma.embedding_length": 768,
                               "general.description": "secret-description"},
                "capabilities": ["completion", "secret-capability"]})
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.1.0"})
        return httpx.Response(200, json={"models": [{"name": settings.ollama_fast_model,
            "digest": "a" * 64, "size": 1024, "size_vram": 512, "context_length": 4096,
            "secret": "secret-runtime"}, {"name": "secret-unconfigured-model"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        report = await diagnostic.collect_report(client=client)
    assert report["errors"] == []
    assert "secret" not in json.dumps(report)
    # Tres endpoints globales y una lectura de metadata por modelo unico.
    assert len(requests) == 5
    assert {r.url.path for r in requests} == {"/api/version", "/api/tags", "/api/ps", "/api/show"}
    first = report["ollama"]["models"][0]
    assert first["present"] is True and first["loaded"] is True
    assert first["running"]["size_vram_bytes"] == 512
    assert first["context_lengths"] == {"gemma.context_length": 8192}


@pytest.mark.asyncio
async def test_missing_capabilities_do_not_claim_verified_support(monkeypatch):
    configure(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as client:
        report = await diagnostic.collect_report(client=client)
    first = report["ollama"]["models"][0]
    assert first["capabilities"] is None
    assert first["present"] is None and first["loaded"] is None
    assert {e["component"] for e in report["errors"]} == {"tags", "ps"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"ollama_base_url": "https://cloud.example.test", "llm_local_only": False},
    {"ollama_base_url": "http://127.0.0.1:11434?secret=credential"},
    {"ollama_base_url": "http://user:secret@127.0.0.1:11434"},  # secrets-scan: allow (fixture sintetico sin datos reales)
    {"ollama_fast_model": "model:cloud", "llm_local_only": False},
])
async def test_rejects_unapproved_or_cloud_endpoints_without_http(monkeypatch, kwargs):
    configure(monkeypatch, **kwargs)

    def forbidden(_):
        pytest.fail("No debe contactar endpoints no aprobados ni modelos cloud")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        report = await diagnostic.collect_report(client=client)
    assert report["ollama"]["available"] is False
    assert report["errors"]
    assert "secret" not in json.dumps(report)


@pytest.mark.asyncio
async def test_optional_cloud_adapter_is_never_instantiated_or_contacted(monkeypatch):
    configure(monkeypatch, llm_provider="vertex", llm_deep_provider="vertex",
              llm_embedding_provider="openai_compatible", llm_local_only=False,
              llm_vertex_base_url="https://cloud.example.test/models")
    monkeypatch.setattr(diagnostic, "ModelClient", lambda: pytest.fail("No crear adapter cloud"))
    report = await diagnostic.collect_report()
    assert report["ollama"]["reason"] == "not_configured"


@pytest.mark.asyncio
async def test_read_only_diagnostics_never_follow_redirects(monkeypatch):
    configure(monkeypatch)
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://cloud.example.test/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True) as client:
        report = await diagnostic.collect_report(client=client)
    assert all(r.url.host == "127.0.0.1" for r in calls)
    assert {e["code"] for e in report["errors"]} == {"http_302"}


@pytest.mark.asyncio
async def test_total_deadline_cancels_trickling_metadata(monkeypatch):
    configure(monkeypatch)

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b" "

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=SlowBody())
    )) as client:
        async with asyncio.timeout(1):
            report = await diagnostic.collect_report(client=client, timeout=0.03)
    assert {e["code"] for e in report["errors"]} == {"timeout"}


@pytest.mark.asyncio
async def test_error_body_and_exception_urls_are_not_in_report(monkeypatch):
    configure(monkeypatch)

    def server(request):
        raise httpx.ConnectError("secret-password at secret-server/userpath", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        report = await diagnostic.collect_report(client=client)
    assert report["ollama"]["available"] is False
    assert {e["code"] for e in report["errors"]} == {"connection_failed"}
    assert "secret" not in json.dumps(report)
