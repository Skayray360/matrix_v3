# Creado por Aldo Garcia.
"""Contrato real HTTP/ModelClient: generacion final, privacidad y limites."""

from __future__ import annotations

import json
import logging
from copy import deepcopy

import httpx
import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.common.inference_errors import InferenceFailureError
from app.config.settings import Settings
from app.llm import provider
from app.llm.model_policy import Intent, ModelChoice, ModelPolicy
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit
PRIVATE = "PRIVATE_THINKING_AND_PARTIAL_128"
SOURCE = "prestaciones/fixture.md#1"
FINAL = f"Corresponden 20 dias habiles de vacaciones [[{SOURCE}]]."


def configure(monkeypatch, **overrides):
    # FINAL es una muestra Markdown. La prueba JSON envia response_schema
    # explicitamente y comprueba su preservacion en ambos intentos HTTP.
    overrides.setdefault("answer_structured_output", False)
    settings = Settings(_env_file=None, app_env="test", **overrides)
    for module in ("app.llm.provider", "app.llm.ollama_client", "app.llm.model_policy", "app.agents.knowledge_agent",
                   "app.agents.prompts", "app.agents.documentary_output"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    return settings


def reply(content=FINAL, *, done=True, reason="stop", thinking=""):
    return {"message": {"content": content, "thinking": thinking}, "done": done,
            "done_reason": reason, "eval_count": 768}


@pytest.mark.parametrize("profile", ["fast", "deep"])
@pytest.mark.parametrize("mode,expected", [("auto", False), ("default", None), ("enabled", True), ("disabled", False)])
def test_thinking_controls_are_explicit_for_both_ollama_profiles(monkeypatch, profile, mode, expected):
    settings = configure(monkeypatch, **{f"llm_{profile}_thinking": mode})
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=reply())

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        provider.ModelClient(client=http).chat(
            model=settings.ollama_deep_model if profile == "deep" else settings.ollama_fast_model,
            messages=[], execution_profile=profile,
        )
    assert payloads[0].get("think") is expected


def test_http_reproduction_recovers_thinking_token_limit_with_authorized_citations(monkeypatch, caplog):
    settings = configure(monkeypatch, llm_fast_thinking="default")
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=(reply("", reason="length", thinking=PRIVATE)
                                        if len(payloads) == 1 else reply()))

    evidence = Evidence(source_id=SOURCE, text="Corresponden 20 dias habiles de vacaciones.", score=0.8,
                        category="prestaciones", filename="fixture.md", section="", page_or_sheet="",
                        document_id="synthetic-doc", chunk_id="synthetic-chunk")
    with httpx.Client(transport=httpx.MockTransport(handle)) as http, caplog.at_level(logging.INFO):
        agent = KnowledgeAgent(llm=provider.ModelClient(client=http), policy=ModelPolicy())
        result = agent.synthesize(question="Que dice el documento sobre vacaciones?", evidences=(evidence,),
                                  model_name=settings.ollama_fast_model, intent=Intent.DOCUMENTAL,
                                  choice=ModelChoice.FAST)
    assert result.answer == FINAL
    assert result.grounding.grounded
    assert result.answer_basis == "documented"
    assert result.cited_source_ids == (SOURCE,)
    assert len(payloads) == 2
    assert "think" not in payloads[0] and payloads[1]["think"] is False
    assert payloads[0]["options"]["num_predict"] == payloads[1]["options"]["num_predict"]
    assert all(SOURCE in str(payload) for payload in payloads)
    assert PRIVATE not in str(payloads) + str([record.__dict__ for record in caplog.records])
    diagnostic = next(r for r in caplog.records if r.message == "llm.completion_regenerating")
    assert diagnostic.finish_reason == "length" and diagnostic.thinking_chars == len(PRIVATE)
    assert diagnostic.output_token_limit == settings.ollama_fast_max_tokens


@pytest.mark.parametrize("first", [reply(PRIVATE, reason="length"), reply("", thinking=PRIVATE), reply("")])
def test_complete_truncated_or_thinking_only_restarts_without_using_partial_text(monkeypatch, first):
    settings = configure(monkeypatch)
    messages = [{"role": "user", "content": "Pregunta sintetica"}]
    original = deepcopy(messages)
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=first if len(calls) == 1 else reply("Respuesta final completa."))

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        result = provider.ModelClient(client=http).chat(model=settings.ollama_fast_model, messages=messages,
                                                       max_tokens=1536, num_ctx=8192)
    assert result.content == "Respuesta final completa." and result.generation_attempts == 2
    assert len(calls) == 2 and PRIVATE not in str(calls)
    assert messages == original


@pytest.mark.parametrize("fault", ["timeout", "transport", "http", "unfinished", "unknown", "invalid", "unknown_empty"])
def test_ambiguous_or_non_completion_failures_never_trigger_another_post(monkeypatch, fault, caplog):
    settings = configure(monkeypatch)
    calls = []

    def handle(request):
        calls.append(request)
        if fault == "timeout":
            raise httpx.ReadTimeout(PRIVATE, request=request)
        if fault == "transport":
            raise httpx.ConnectError(PRIVATE, request=request)
        if fault == "http":
            return httpx.Response(503, json={"error": PRIVATE})
        if fault == "invalid":
            return httpx.Response(200, json={"message": PRIVATE})
        if fault == "unknown_empty":
            return httpx.Response(200, json={"message": {"content": ""}, "done_reason": "stop"})
        return httpx.Response(200, json=reply(PRIVATE, done=fault != "unfinished",
                                             reason=PRIVATE if fault == "unknown" else "length"))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        caplog.at_level(logging.WARNING),
        pytest.raises(InferenceFailureError) as caught,
    ):
        provider.ModelClient(client=http).chat(model=settings.ollama_fast_model, messages=[])
    assert len(calls) == 1
    assert PRIVATE not in str(caught.value) + str(caught.value.__dict__)
    assert PRIVATE not in str([record.__dict__ for record in caplog.records])


@pytest.mark.parametrize("retries,expected", [(0, 1), (1, 2)])
def test_recovery_is_bounded_and_a_second_truncation_is_not_published(monkeypatch, retries, expected):
    settings = configure(monkeypatch, llm_completion_retries=retries)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=reply(PRIVATE, reason="length", thinking=PRIVATE))

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        pytest.raises(InferenceFailureError) as caught,
    ):
        provider.ModelClient(client=http).chat(model=settings.ollama_fast_model, messages=[])
    assert len(calls) == expected and caught.value.failure_kind.value == "incomplete"
    assert PRIVATE not in str(caught.value.__dict__)


@pytest.mark.parametrize("global_seconds,kind", [(600, "timeout"), (10, "deadline")])
def test_recovery_does_not_renew_stage_or_global_time_budget(monkeypatch, global_seconds, kind):
    settings = configure(monkeypatch, llm_request_deadline_seconds=global_seconds)
    clock = [100.0]
    monkeypatch.setattr(provider.time, "monotonic", lambda: clock[0])
    timeouts = []

    def handle(request):
        timeouts.append(request.extensions["timeout"]["read"])
        clock[0] += 175 if len(timeouts) == 2 and global_seconds == 600 else 6
        return httpx.Response(200, json=reply(PRIVATE, reason="length"))

    with httpx.Client(transport=httpx.MockTransport(handle)) as http, provider.inference_deadline():
        with pytest.raises(InferenceFailureError) as caught:
            provider.ModelClient(client=http).chat(model=settings.ollama_fast_model, messages=[])
        assert caught.value.failure_kind.value == kind
    assert timeouts == ([10, 4] if kind == "deadline" else [180, 174])
    assert provider._stage_deadline.get() is None


def test_model_pin_is_rechecked_before_regeneration(monkeypatch):
    settings = configure(monkeypatch, llm_fast_digest="a" * 64)
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/api/tags":
            digest = "a" * 64 if len(paths) == 1 else "b" * 64
            return httpx.Response(200, json={"models": [{"name": settings.ollama_fast_model, "digest": digest}]})
        return httpx.Response(200, json=reply("", reason="length", thinking=PRIVATE))

    from app.common.errors import ConfigurationError

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        pytest.raises(ConfigurationError, match="digest"),
    ):
        provider.ModelClient(client=http).chat(model=settings.ollama_fast_model, messages=[])
    assert paths == ["/api/tags", "/api/chat", "/api/tags"]


def test_structured_response_is_validated_after_recovery(monkeypatch):
    settings = configure(monkeypatch, llm_deep_thinking="enabled")
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"],
              "additionalProperties": False}
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=reply('{"value":' if len(calls) == 1 else '{"value":12}',
                                             reason="length" if len(calls) == 1 else "stop"))

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        result = provider.ModelClient(client=http).chat(model=settings.ollama_deep_model, messages=[],
                                                       response_schema=schema, execution_profile="deep")
    assert json.loads(result.content) == {"value": 12}
    assert all(payload["format"] == schema and payload["think"] is False for payload in calls)
