# Creado por Aldo Garcia.
"""Lento por etapa/sonda vs deadline global, con reloj y transporte finitos."""

from __future__ import annotations

import json
from contextlib import contextmanager

import httpx
import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.config.settings import Settings
from app.llm import provider
from app.llm.model_policy import Intent, ModelChoice, ModelPolicy
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit


class ClockedBody(httpx.SyncByteStream):
    """Entrega dos bloques finitos; adelanta el reloj antes de producir datos."""

    def __init__(self, clock, seconds: float, payload):
        self.clock = clock
        self.seconds = seconds
        self.body = json.dumps(payload).encode()
        self.closed = False

    def __iter__(self):
        self.clock[0] += self.seconds
        yield self.body[:1]
        yield self.body[1:]

    def close(self):
        self.closed = True


def environment(monkeypatch, **overrides):
    settings = Settings(_env_file=None, app_env="test", **overrides)
    for module in (
        "app.llm.provider", "app.llm.ollama_client", "app.llm.model_policy", "app.agents.knowledge_agent",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    clock = [100.0]
    monkeypatch.setattr(provider.time, "monotonic", lambda: clock[0])
    return settings, clock


@pytest.mark.parametrize(("global_seconds", "expected"), ((600, "timeout"), (10, "deadline")))
def test_post_slow_body_distinguishes_stage_budget_from_global_deadline(monkeypatch, global_seconds, expected):
    settings, clock = environment(monkeypatch, llm_request_deadline_seconds=global_seconds)
    bodies = []

    def handler(_request):
        body = ClockedBody(clock, 181, {"message": {"content": "synthetic answer"}})
        bodies.append(body)
        return httpx.Response(200, stream=body)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        provider.inference_deadline(),
        pytest.raises(InferenceFailureError) as caught,
    ):
        provider.ModelClient(client=http).chat(model=settings.ollama_deep_model, messages=[], execution_profile="deep")
    assert caught.value.failure_kind.value == expected
    assert all(body.closed for body in bodies)
    assert provider._stage_deadline.get() is None


@pytest.mark.parametrize(("global_seconds", "expected"), ((600, "timeout"), (8, "deadline")))
def test_inventory_probe_ten_second_limit_is_not_the_query_global_deadline(monkeypatch, global_seconds, expected):
    _settings, clock = environment(monkeypatch, llm_request_deadline_seconds=global_seconds)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=ClockedBody(clock, 11, {"models": []}))

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        provider.inference_deadline(),
        pytest.raises(InferenceFailureError) as caught,
    ):
        provider.ModelClient(client=http)._inventory_get("http://127.0.0.1:11434/api/tags")
    assert caught.value.failure_kind.value == expected
    assert requests[0].extensions["timeout"]["read"] == min(10, global_seconds)


@pytest.mark.parametrize(("global_seconds", "expected"), ((600, "timeout"), (8, "deadline")))
def test_elapsed_admission_does_not_mislabel_stage_timeout_as_global(monkeypatch, global_seconds, expected):
    settings, clock = environment(monkeypatch, llm_request_deadline_seconds=global_seconds)
    requests = []

    @contextmanager
    def delayed_admission(_key, _limit):
        clock[0] += 181
        yield

    monkeypatch.setattr(provider, "admission", delayed_admission)

    def forbidden_post(request):
        requests.append(request)
        raise AssertionError("No debe enviar POST después de agotar el limite previo a inferencia.")

    with (
        httpx.Client(transport=httpx.MockTransport(forbidden_post)) as http,
        provider.inference_deadline(),
        pytest.raises(InferenceFailureError) as caught,
    ):
        provider.ModelClient(client=http).chat(model=settings.ollama_deep_model, messages=[], execution_profile="deep")
    assert caught.value.failure_kind.value == expected
    assert requests == []


@pytest.mark.parametrize("failure_source", ("post_body", "inventory_probe", "admission"))
@pytest.mark.parametrize("global_exhausted", (False, True))
def test_only_stage_or_probe_timeout_permits_one_gemma_fallback(
    monkeypatch, failure_source, global_exhausted,
):
    # Compatibilidad de un despliegue que configura dos modelos diferentes.
    # La instalacion actual usa un solo Gemma y no aplica este fallback.
    overrides = {"llm_request_deadline_seconds": 8 if global_exhausted else 600,
                 "ollama_deep_model": "synthetic-deep-generator"}
    if failure_source == "inventory_probe":
        overrides.update(llm_deep_digest="d" * 64, llm_fast_digest="f" * 64)
    settings, clock = environment(monkeypatch, **overrides)
    records = []
    admission_calls = []
    source = Evidence(
        source_id="prestaciones/reglas.md#0", text="La solicitud requiere autorizacion escrita.",
        score=0.9, category="prestaciones", filename="reglas.md", section="", page_or_sheet="",
        document_id="document", chunk_id="chunk",
    )
    final = f"{source.text} [[{source.source_id}]]"

    @contextmanager
    def timed_admission(_key, _limit):
        admission_calls.append(provider._stage_profile.get())
        if failure_source == "admission" and len(admission_calls) == 1:
            clock[0] += 181
        yield

    monkeypatch.setattr(provider, "admission", timed_admission)

    def handler(request):
        payload = json.loads(request.content) if request.content else {}
        records.append((request.method, payload.get("model"), request.extensions["timeout"]["read"]))
        if request.method == "GET":
            inventory = {"models": [
                {"name": settings.ollama_deep_model, "digest": "d" * 64},
                {"name": settings.ollama_fast_model, "digest": "f" * 64},
            ]}
            if len(records) == 1:
                return httpx.Response(200, stream=ClockedBody(clock, 11, inventory))
            return httpx.Response(200, json=inventory)
        if payload.get("model") == settings.ollama_deep_model:
            assert failure_source == "post_body"
            return httpx.Response(200, stream=ClockedBody(clock, 181, {"message": {"content": final}}))
        assert payload.get("model") == settings.ollama_fast_model
        return httpx.Response(200, json={"message": {"content": final}, "done": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        agent = KnowledgeAgent(llm=provider.ModelClient(client=http), policy=ModelPolicy())
        with provider.inference_deadline():
            if global_exhausted:
                with pytest.raises(InferenceFailureError) as caught:
                    agent.synthesize(
                        question="Analiza en profundidad el requisito.", evidences=(source,),
                        model_name=settings.ollama_deep_model, choice=ModelChoice.DEEP, intent=Intent.DOCUMENTAL,
                    )
                assert caught.value.failure_kind is InferenceFailureKind.DEADLINE
            else:
                result = agent.synthesize(
                    question="Analiza en profundidad el requisito.", evidences=(source,),
                    model_name=settings.ollama_deep_model, choice=ModelChoice.DEEP, intent=Intent.DOCUMENTAL,
                )
                assert result.model == settings.ollama_fast_model
                assert result.cited_source_ids == (source.source_id,)
                assert result.answer_basis == "documented"
    fast_posts = [record for record in records if record[0] == "POST" and record[1] == settings.ollama_fast_model]
    assert len(fast_posts) == (0 if global_exhausted else 1)
    assert provider._deadline.get() is None
    assert provider._stage_deadline.get() is None
