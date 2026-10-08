# Creado por Aldo Garcia.
"""Presupuesto de prefijos locales: agente y adapter con transporte sintetico."""

import json

import httpx
import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.common.errors import OllamaUnavailableError
from app.config.settings import Settings
from app.llm.model_policy import ModelPolicy
from app.llm.provider import ModelClient
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit


def configured_agent(monkeypatch, prefix: str):
    settings = Settings(_env_file=None, app_env="test", llm_system_prefix=prefix, answer_evidence_mode="extractive")
    for module in ("app.llm.provider", "app.llm.ollama_client", "app.llm.model_policy", "app.agents.knowledge_agent"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    evidence = Evidence(
        source_id="prestaciones/politica.md#0", text="El bono requiere autorizacion escrita.", score=0.9,
        category="prestaciones", filename="politica.md", section="Bono",
        page_or_sheet="pagina 1", document_id="doc", chunk_id="chunk-0",
    )
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "message": {"content": f"{evidence.text} [[{evidence.source_id}]]"},
            "done": True, "done_reason": "stop",
        })

    client = ModelClient(client=httpx.Client(transport=httpx.MockTransport(respond)))
    return KnowledgeAgent(llm=client, policy=ModelPolicy()), settings, evidence, calls


def test_general_prefix_over_context_is_rejected_before_inference(monkeypatch):
    agent, settings, _, calls = configured_agent(monkeypatch, "p" * 30000)
    with pytest.raises(OllamaUnavailableError):
        agent.answer_general(question="Explica un concepto.", memory=None, model_name=settings.ollama_fast_model)
    assert not calls


def test_documental_prefix_reserves_context_before_packing(monkeypatch):
    agent, settings, evidence, calls = configured_agent(monkeypatch, "p" * 18000)
    result = agent.synthesize(
        question="Que requiere el bono?", evidences=(evidence,), model_name=settings.ollama_fast_model,
    )
    assert not calls
    assert result.grounding.declares_insufficiency
    assert not result.grounding.grounded


@pytest.mark.parametrize("prefix", ("", "Responde en espanol."))
def test_small_or_empty_prefix_preserves_complete_evidence(monkeypatch, prefix):
    agent, settings, evidence, calls = configured_agent(monkeypatch, prefix)
    result = agent.synthesize(
        question="Que requiere el bono?", evidences=(evidence,), model_name=settings.ollama_fast_model,
    )
    assert result.grounding.extractive_verified
    assert len(calls) == 1
    assert evidence.text in calls[0]["messages"][-1]["content"]
    assert sum(message["content"] == prefix for message in calls[0]["messages"]) == bool(prefix)
