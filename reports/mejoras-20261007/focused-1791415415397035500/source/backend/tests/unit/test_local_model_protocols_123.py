# Creado por Aldo Garcia.
"""Contratos HTTP sinteticos; no certifican pesos, GPU ni calidad semantica."""

import json

import httpx
import pytest

from app.common.errors import ConfigurationError, OllamaUnavailableError
from app.config.settings import Settings
from app.llm.provider import ModelClient

pytestmark = pytest.mark.unit


def configured(monkeypatch, **values):
    # Ambos perfiles atienden el mismo checkpoint a traves del adapter probado.
    values.setdefault("llm_deep_provider", values.get("llm_provider", "ollama"))
    settings = Settings(_env_file=None, **values)
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)
    return settings


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_one_checkpoint_has_independent_thinking_sampling_and_json_policy(monkeypatch, provider):
    configured(
        monkeypatch, llm_provider=provider, llm_deep_provider=provider,
        ollama_fast_model="same-model", ollama_deep_model="same-model",
        llm_fast_thinking="disabled", llm_deep_thinking="enabled",
        llm_structured_thinking="disabled", llm_fast_top_k=64, llm_deep_top_k=1,
    )
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        message = {"content": '{"days":12}', "reasoning": "not final", "reasoning_content": "not final"}
        return httpx.Response(200, json={
            "message": message, "done": True,
            "choices": [{"message": message, "finish_reason": "stop"}],
        })

    schema = {"type": "object", "properties": {"days": {"type": "integer"}}, "required": ["days"]}
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = ModelClient(client=http)
        for profile, response_schema in [("fast", None), ("deep", None), ("deep", schema)]:
            assert client.chat(
                model="same-model", messages=[], execution_profile=profile, response_schema=response_schema,
            ).content == '{"days":12}'
    if provider == "ollama":
        assert [p["think"] for p in captured] == [False, True, False]
        assert [p["options"]["top_k"] for p in captured] == [64, 1, 1]
        assert captured[-1]["format"] == schema
    else:
        assert [p["chat_template_kwargs"]["enable_thinking"] for p in captured] == [False, True, False]
        assert [p["top_k"] for p in captured] == [64, 1, 1]
        assert captured[-1]["response_format"]["json_schema"]["schema"] == schema


def test_defaults_do_not_inject_model_specific_options(monkeypatch):
    configured(monkeypatch, llm_provider="openai_compatible")
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        ModelClient(client=http).chat(model="synthetic", messages=[])
    assert "chat_template_kwargs" not in captured[0]
    assert "top_k" not in captured[0]


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_facade_conserva_opciones_del_contrato_base_y_configuracion_por_defecto(monkeypatch, provider):
    configured(monkeypatch, llm_provider=provider, llm_fast_thinking="disabled", llm_fast_top_k=64)
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "message": {"content": "ok"}, "done": True,
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = ModelClient(client=http)
        client.chat(model="synthetic", messages=[])
        client.chat(model="synthetic", messages=[], think=True, top_k=17)
    if provider == "ollama":
        assert [p["think"] for p in captured] == [False, True]
        assert [p["options"]["top_k"] for p in captured] == [64, 17]
    else:
        assert [p["chat_template_kwargs"]["enable_thinking"] for p in captured] == [False, True]
        assert [p["top_k"] for p in captured] == [64, 17]


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("content", [None, "", "<think>internal</think>12", "[THINK]internal[/THINK]12",
                                      "<|channel|>thought internal", "<|channel>thought\ninternal<channel|>12",
                                      "<|channel>thought\n<channel|>12", "<channel|>12"])
def test_reasoning_is_never_substituted_for_final_content(monkeypatch, content, provider):
    configured(monkeypatch, llm_provider=provider)
    message = {"content": content, "reasoning": "private reasoning", "reasoning_content": "private reasoning"}
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"message": message, "done": True,
                       "choices": [{"message": message, "finish_reason": "stop"}]},
        ))) as http,
        pytest.raises(OllamaUnavailableError),
    ):
        ModelClient(client=http).chat(model="synthetic", messages=[])


def test_openai_usage_preserved_without_inventing_runtime_duration(monkeypatch):
    configured(monkeypatch, llm_provider="openai_compatible")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 12},
    }))) as http:
        metrics = ModelClient(client=http).chat(model="synthetic", messages=[]).performance_metrics()
    assert metrics["prompt_eval_count"] == 123
    assert metrics["eval_count"] == 12
    assert metrics["generation_tokens_per_second"] is None


def test_wrong_model_for_profile_is_rejected_before_network(monkeypatch):
    configured(monkeypatch)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected network"))) as http,
        pytest.raises(ConfigurationError, match="perfil"),
    ):
        ModelClient(client=http).chat(model="wrong-model", messages=[], execution_profile="fast")


def test_ollama_embedding_overflow_is_not_truncated_retried_or_cached(monkeypatch):
    configured(monkeypatch)
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["truncate"] is False
        return httpx.Response(400, json={"error": "input exceeds context"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = ModelClient(client=http)
        for _ in range(2):
            with pytest.raises(OllamaUnavailableError):
                client.embed(["synthetic long document"])
        with pytest.raises(OllamaUnavailableError):
            client.probe_embedding_dimension()
        assert not client._embedding_cache
    assert len(requests) == 3
