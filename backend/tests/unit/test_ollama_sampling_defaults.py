# Creado por Aldo Garcia.
"""Muestreo documental explicito contra transporte HTTP sintetico."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config.settings import Settings
from app.llm.ollama_client import OllamaClient
from app.llm.provider import ModelClient

pytestmark = pytest.mark.unit


def build_client(settings, *, facade=False):
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": "Respuesta."}})

    client_type = ModelClient if facade else OllamaClient
    return client_type(settings=settings, client=httpx.Client(transport=httpx.MockTransport(respond))), captured


def test_sampling_defaults_are_sent_instead_of_relying_on_modelfile():
    settings = Settings(_env_file=None, app_env="test")
    client, captured = build_client(settings)
    client.chat(model=settings.ollama_fast_model, messages=[{"role": "user", "content": "Pregunta sintetica."}])
    assert captured[0]["options"]["top_k"] == 40
    assert captured[0]["options"]["repeat_penalty"] == 1.0
    assert captured[0]["options"]["top_p"] == settings.llm_top_p


def test_injected_sampling_configuration_remains_coherent_with_client():
    settings = Settings(
        _env_file=None, app_env="test", llm_top_p=0.67, ollama_top_k=23, ollama_repeat_penalty=1.12,
    )
    client, captured = build_client(settings)
    client.chat(model=settings.ollama_fast_model, messages=[], num_ctx=8192, max_tokens=2048)
    assert captured[0]["options"] == {
        "temperature": 0.1, "top_p": 0.67, "top_k": 23, "repeat_penalty": 1.12,
        "num_ctx": 8192, "num_predict": 2048,
    }


def test_explicit_top_k_overrides_default_without_changing_repeat_penalty():
    settings = Settings(_env_file=None, app_env="test", ollama_top_k=23)
    client, captured = build_client(settings)
    client.chat(model=settings.ollama_fast_model, messages=[], top_k=5)
    assert captured[0]["options"]["top_k"] == 5
    assert captured[0]["options"]["repeat_penalty"] == 1.0


@pytest.mark.parametrize(("fast_top_k", "deep_top_k", "expected"), [(None, None, (40, 40)), (17, 43, (17, 43))])
def test_facade_retains_profile_overrides_and_ollama_fallback(fast_top_k, deep_top_k, expected):
    settings = Settings(
        _env_file=None, app_env="test", llm_fast_top_k=fast_top_k, llm_deep_top_k=deep_top_k,
        llm_fast_digest="", llm_deep_digest="",
    )
    client, captured = build_client(settings, facade=True)
    for profile in ("fast", "deep"):
        client.chat(model=getattr(settings, f"ollama_{profile}_model"), messages=[], execution_profile=profile)
    assert tuple(call["options"]["top_k"] for call in captured) == expected
    assert all(call["options"]["repeat_penalty"] == 1.0 for call in captured)
