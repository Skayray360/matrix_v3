# Creado por Aldo Garcia.
"""Distribucion Gemma unica: contratos HTTP reales sobre transporte sintetico."""

from __future__ import annotations

import json

import httpx
import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.query_planner import build_query_plan
from app.common.answers import GENERAL_HEADING
from app.common.inference_errors import InferenceFailureError
from app.config.settings import Settings
from app.llm.model_policy import Intent, ModelChoice, ModelPolicy
from app.llm.provider import ModelClient
from app.memory.service import ConversationContext, ConversationTurn
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit


def configure(monkeypatch) -> Settings:
    settings = Settings(_env_file=None, app_env="test")
    for module in (
        "app.llm.provider", "app.llm.ollama_client", "app.llm.model_policy",
        "app.agents.knowledge_agent", "app.agents.query_planner",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    return settings


def evidence() -> Evidence:
    return Evidence(
        source_id="prestaciones/fixture.md#0", text="La solicitud requiere autorizacion escrita.",
        score=0.9, category="prestaciones", filename="fixture.md", section="", page_or_sheet="",
        document_id="synthetic-document", chunk_id="synthetic-chunk",
    )


@pytest.mark.parametrize("choice", tuple(ModelChoice))
@pytest.mark.parametrize("fault", ("timeout", "transport", "missing", "busy"))
def test_unique_gemma_never_retries_ambiguous_failure_as_its_other_profile(monkeypatch, choice, fault):
    settings = configure(monkeypatch)
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if fault == "timeout":
            raise httpx.ReadTimeout("synthetic failure", request=request)
        if fault == "transport":
            raise httpx.ConnectError("synthetic failure", request=request)
        return httpx.Response(404 if fault == "missing" else 503)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        pytest.raises(InferenceFailureError),
    ):
        KnowledgeAgent(llm=ModelClient(client=http), policy=ModelPolicy()).synthesize(
            question="Que dice la regla?", evidences=(evidence(),),
            model_name=settings.ollama_fast_model, choice=choice, intent=Intent.DOCUMENTAL,
        )
    assert len(requests) == 1
    assert requests[0]["model"] == "gemma4:latest"


def test_general_planner_and_embedding_use_only_declared_models_and_preserve_context(monkeypatch):
    settings = configure(monkeypatch)
    requests = []
    memory = ConversationContext(conversation_id="synthetic-conversation", turns=(
        ConversationTurn(role="user", content="Necesito un ejemplo de capacitacion."),
        ConversationTurn(role="assistant", content="Podemos explicar una actividad guiada."),
    ))

    def handle(request):
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.5] * settings.ollama_embedding_dimension]})
        if payload.get("format"):
            content = json.dumps({"source": "synthetic", "entity": "employees", "fields": ["department"]})
        else:
            content = "Una actividad guiada combina explicacion y practica."
        return httpx.Response(200, json={"message": {"content": content}, "done": True, "done_reason": "stop"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = ModelClient(client=http)
        policy = ModelPolicy()
        for choice in ModelChoice:
            result = KnowledgeAgent(llm=client, policy=policy).answer_general(
                question="Explica una actividad guiada.", memory=memory,
                model_name=policy.model_for(choice), choice=choice,
            )
            assert result.answer_basis == "general" and GENERAL_HEADING in result.answer
        plan = build_query_plan(
            question="Lista departamentos.", catalog=[{
                "source": "synthetic", "entities": [{"name": "employees", "columns": ["department"]}],
            }], llm=client, model_name=policy.fast_model,
        )
        assert plan is not None and plan.fields == ["department"]
        assert len(client.embed(["Texto sintetico"])[0]) == 768

    chats = [payload for path, payload in requests if path == "/api/chat"]
    assert len(chats) == 3
    assert {payload["model"] for payload in chats} == {"gemma4:latest"}
    assert [payload["options"]["num_ctx"] for payload in chats] == [8192, 8192, 8192]
    assert [payload["options"]["num_predict"] for payload in chats] == [1536, 3072, settings.llm_planner_max_tokens]
    assert all(memory.turns[-1].content in payload["messages"][-1]["content"] for payload in chats[:2])
    assert requests[-1][1]["model"] == "embeddinggemma:latest"
    assert memory.turns[-1].content == "Podemos explicar una actividad guiada."
