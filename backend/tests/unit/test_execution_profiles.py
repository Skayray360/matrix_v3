# Creado por Aldo Garcia.
"""Dos perfiles con un solo modelo; regresiones sin GPU ni servicios reales."""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.orchestrator import Orchestrator
from app.agents.query_planner import build_query_plan
from app.common.errors import OllamaUnavailableError
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.config.settings import Settings
from app.llm.model_policy import Intent, ModelChoice, ModelPolicy
from app.llm.ollama_client import ChatResult
from app.memory.service import MemoryService
from app.rag.retriever import RetrievalResult
from app.rag.schemas import Evidence
from app.structured_data.schemas import StructuredQueryPlan
from tests.conftest import make_context
from tests.unit import test_implementation_v2

pytestmark = pytest.mark.unit
sql = test_implementation_v2.sql

_CATALOG = [{
    "source": "rh_demo", "entities": [{"name": "employees", "columns": ["department"]}],
}]
_PLAN = {"source": "rh_demo", "entity": "employees", "fields": ["department"], "limit": 10}


def configured_policy(monkeypatch, *, shared: bool = True, **overrides):
    # Ventanas explicitamente distintas para probar el contrato configurable;
    # no representan los defaults de instalacion con un unico Gemma.
    overrides.setdefault("ollama_deep_num_ctx", 32768)
    overrides.setdefault("answer_structured_output", False)
    settings = Settings(
        _env_file=None, app_env="test", ollama_fast_model="local-generator",
        ollama_deep_model="local-generator" if shared else "local-deep-generator",
        **overrides,
    )
    for module in ("app.llm.model_policy", "app.agents.knowledge_agent", "app.agents.query_planner",
                   "app.agents.documentary_output", "app.agents.prompts"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    return settings, ModelPolicy()


def evidence(index: int = 0, *, text: str | None = None) -> Evidence:
    return Evidence(
        source_id=f"prestaciones/reglas.md#{index}",
        text=text or f"Regla {index}: la solicitud requiere autorizacion escrita.",
        score=0.9, category="prestaciones", filename="reglas.md", section=f"Regla {index}",
        page_or_sheet="", document_id="document", chunk_id=f"chunk-{index}",
    )


class RecordingLlm:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return ChatResult(content=response, model=model, latency_ms=1)


class EvidenceLlm:
    def __init__(self, evidences):
        self.evidences = {item.source_id: item for item in evidences}
        self.calls = []

    def chat(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        prompt = messages[-1]["content"]
        ids = tuple(dict.fromkeys(re.findall(r"\[source_id: ([^\]]+)\]", prompt)))
        if not ids:
            ids = tuple(dict.fromkeys(re.findall(r"\[\[([^\]]+)\]\]", prompt)))
        ids = tuple(sid for sid in ids if sid in self.evidences)
        content = "\n\n".join(f"{self.evidences[sid].text} [[{sid}]]" for sid in ids)
        return ChatResult(content=content, model=model, latency_ms=1)


def assert_profile(call, settings, choice):
    deep = choice is ModelChoice.DEEP
    assert call["execution_profile"] == str(choice)
    assert call["num_ctx"] == (settings.ollama_deep_num_ctx if deep else settings.ollama_fast_num_ctx)
    assert call["max_tokens"] == (settings.ollama_deep_max_tokens if deep else settings.ollama_fast_max_tokens)


@pytest.mark.parametrize("choice", tuple(ModelChoice))
def test_shared_name_has_independent_generation_profiles(monkeypatch, choice):
    settings, policy = configured_policy(monkeypatch)
    profile = policy.generation_profile(policy.fast_model, intent=Intent.DOCUMENTAL, choice=choice)
    deep = choice is ModelChoice.DEEP
    assert profile.num_ctx == (settings.ollama_deep_num_ctx if deep else settings.ollama_fast_num_ctx)
    assert profile.max_tokens == (settings.ollama_deep_max_tokens if deep else settings.ollama_fast_max_tokens)


def test_legacy_name_resolution_is_fast_when_shared_and_preserves_distinct_deep(monkeypatch):
    settings, policy = configured_policy(monkeypatch)
    assert policy.resolve_choice(policy.fast_model) is ModelChoice.FAST
    assert policy.generation_profile(policy.fast_model, intent=Intent.GENERAL).num_ctx == settings.ollama_fast_num_ctx
    _, distinct_policy = configured_policy(monkeypatch, shared=False)
    assert distinct_policy.resolve_choice(distinct_policy.deep_model) is ModelChoice.DEEP
    assert distinct_policy.resolve_choice(distinct_policy.fast_model) is ModelChoice.FAST


def test_shared_model_preserves_business_routing_decisions(monkeypatch):
    _, policy = configured_policy(monkeypatch)
    simple = policy.route("Que dice la politica de vacaciones?")
    deep = policy.route("Compara en profundidad las politicas de vacaciones.")
    assert simple.choice is ModelChoice.FAST
    assert deep.choice is ModelChoice.DEEP
    assert simple.model_name == deep.model_name == policy.fast_model


@pytest.mark.parametrize("choice", tuple(ModelChoice))
@pytest.mark.parametrize("general", (False, True))
def test_agent_passes_shared_profile_to_general_and_documental_calls(monkeypatch, choice, general):
    settings, policy = configured_policy(monkeypatch)
    source = evidence()
    llm = RecordingLlm(['Un concepto general.' if general else f"{source.text} [[{source.source_id}]]"])
    agent = KnowledgeAgent(llm=llm, policy=policy)
    if general:
        agent.answer_general(question="Explica un concepto.", memory=None, model_name=policy.fast_model, choice=choice)
    else:
        agent.synthesize(question="Que requiere la solicitud?", evidences=(source,),
                         model_name=policy.fast_model, choice=choice)
    assert len(llm.calls) == 1
    assert_profile(llm.calls[0], settings, choice)


@pytest.mark.parametrize(
    ("question", "choice"),
    (
        ("Explica la fotosintesis.", ModelChoice.FAST),
        ("Explica la fotosintesis en profundidad.", ModelChoice.DEEP),
        ("Que dice la politica de solicitudes?", ModelChoice.FAST),
        ("Compara en profundidad la politica de solicitudes.", ModelChoice.DEEP),
    ),
)
def test_orchestrator_preserves_shared_routing_choice_until_inference(monkeypatch, sql, question, choice):
    settings, policy = configured_policy(monkeypatch)
    db, _, _ = sql
    context = make_context(permissions=frozenset(), sources=frozenset())
    memory = MemoryService()
    conversation = memory.create_conversation(db, context)
    source = evidence()
    llm = RecordingLlm([f"{source.text} [[{source.source_id}]]"])
    policies = MagicMock()
    policies.effective_categories.return_value = frozenset({"prestaciones"})
    retriever = MagicMock()
    retriever.retrieve.return_value = RetrievalResult(evidences=(source,), best_score=0.9)
    result = Orchestrator(
        llm=llm, policy_engine=policies, retriever=retriever, structured_tool=MagicMock(),
        memory=memory, audit=MagicMock(),
    ).handle_chat(db, ctx=context, conversation=conversation, message=question)
    assert result.model == policy.fast_model
    assert len(llm.calls) == 1
    assert_profile(llm.calls[0], settings, choice)


def test_shared_profile_controls_packing_and_full_evidence_is_never_truncated(monkeypatch):
    # La ventana mayor es una configuracion explicita de este escenario;
    # el paquete usa 8192 en ambos perfiles del mismo Gemma.
    settings, policy = configured_policy(monkeypatch, ollama_deep_num_ctx=32768)
    source = evidence(text="La excepcion requiere autorizacion. " * 700)
    llm = EvidenceLlm((source,))
    agent = KnowledgeAgent(llm=llm, policy=policy)
    with pytest.raises(InferenceFailureError) as caught:
        agent.synthesize(question="Que regla aplica?", evidences=(source,),
                         model_name=policy.fast_model, choice=ModelChoice.FAST)
    assert caught.value.failure_kind is InferenceFailureKind.CONTEXT_LIMIT
    assert not llm.calls
    deep = agent.synthesize(question="Que regla aplica?", evidences=(source,),
                            model_name=policy.deep_model, choice=ModelChoice.DEEP)
    assert deep.grounding.grounded and deep.grounding.citations_valid
    assert source.text in deep.answer
    assert source.text in llm.calls[0]["messages"][-1]["content"]
    assert_profile(llm.calls[0], settings, ModelChoice.DEEP)


def test_shared_model_can_escalate_summary_retry_from_fast_to_deep_profile(monkeypatch):
    settings, policy = configured_policy(monkeypatch)
    source = evidence()
    llm = RecordingLlm(["No cuento con informacion documental suficiente.",
                        f"{source.text} [[{source.source_id}]]"])
    result = KnowledgeAgent(llm=llm, policy=policy).synthesize(
        question="Resume el documento.", evidences=(source,), model_name=policy.fast_model,
        deep_model_name=policy.deep_model, choice=ModelChoice.FAST, intent=Intent.DOCUMENT_SUMMARY,
    )
    assert result.regenerated and result.escalated_to_deep
    assert [call["model"] for call in llm.calls] == [policy.fast_model, policy.fast_model]
    assert_profile(llm.calls[0], settings, ModelChoice.FAST)
    assert_profile(llm.calls[1], settings, ModelChoice.DEEP)


@pytest.mark.parametrize("choice", tuple(ModelChoice))
def test_grounding_retry_keeps_profile_without_escalation_signal(monkeypatch, choice):
    settings, policy = configured_policy(monkeypatch)
    source = evidence()
    llm = RecordingLlm(["Afirmacion sin cita.", f"{source.text} [[{source.source_id}]]"])
    result = KnowledgeAgent(llm=llm, policy=policy).synthesize(
        question="Que requiere la solicitud?", evidences=(source,), model_name=policy.fast_model,
        choice=choice, deep_model_name=policy.deep_model,
    )
    assert result.regenerated and not result.escalated_to_deep
    assert len(llm.calls) == 2
    for call in llm.calls:
        assert_profile(call, settings, choice)


@pytest.mark.parametrize("choice", tuple(ModelChoice))
def test_distinct_model_is_not_silently_substituted_after_failure(monkeypatch, choice):
    settings, policy = configured_policy(monkeypatch, shared=False)
    source = evidence()
    llm = RecordingLlm([OllamaUnavailableError(detail="HTTP 404"), f"{source.text} [[{source.source_id}]]"])
    with pytest.raises(OllamaUnavailableError):
        KnowledgeAgent(llm=llm, policy=policy).synthesize(
            question="Que requiere la solicitud?", evidences=(source,), model_name=policy.model_for(choice), choice=choice,
        )
    assert len(llm.calls) == 1
    assert_profile(llm.calls[0], settings, choice)


def test_fallback_to_smaller_profile_is_rejected_without_truncating_prompt(monkeypatch):
    _, policy = configured_policy(monkeypatch, shared=False)
    llm = RecordingLlm([OllamaUnavailableError(detail="HTTP 404")])
    agent = KnowledgeAgent(llm=llm, policy=policy)
    prompt = "x" * (agent._input_budget_chars(policy.fast_model, Intent.DOCUMENTAL, choice=ModelChoice.FAST) + 1)
    with pytest.raises(OllamaUnavailableError):
        agent._chat_with_fallback(
            model_name=policy.deep_model, fallback_model_name=policy.fast_model,
            messages=[{"role": "user", "content": prompt}], intent=Intent.DOCUMENTAL, choice=ModelChoice.DEEP,
        )
    assert len(llm.calls) == 1
    assert llm.calls[0]["messages"][0]["content"] == prompt


def test_missing_shared_model_is_not_retried_as_its_own_fallback(monkeypatch):
    _, policy = configured_policy(monkeypatch)
    llm = RecordingLlm([OllamaUnavailableError(detail="HTTP 404")])
    with pytest.raises(OllamaUnavailableError):
        KnowledgeAgent(llm=llm, policy=policy).answer_general(
            question="Explica un concepto.", memory=None, model_name=policy.fast_model, choice=ModelChoice.FAST,
        )
    assert len(llm.calls) == 1


def test_shared_model_hierarchy_uses_fast_maps_and_deep_reduce(monkeypatch):
    settings, policy = configured_policy(monkeypatch)
    sources = tuple(evidence(i, text=f"Regla {i}: " + "La persona entrega los comprobantes completos. " * 9)
                    for i in range(40))
    llm = EvidenceLlm(sources)
    result = KnowledgeAgent(llm=llm, policy=policy).synthesize(
        question="Resume todo el documento.", evidences=sources, model_name=policy.fast_model,
        deep_model_name=policy.deep_model, choice=ModelChoice.FAST, intent=Intent.DOCUMENT_SUMMARY,
    )
    assert result.hierarchical and result.map_batches > 1
    assert set(result.cited_source_ids) == {item.source_id for item in sources}
    assert {call["model"] for call in llm.calls} == {policy.fast_model}
    for call in llm.calls[:-1]:
        assert_profile(call, settings, ModelChoice.FAST)
    assert_profile(llm.calls[-1], settings, ModelChoice.DEEP)


def test_shared_model_large_indivisible_summary_unit_maps_with_deep_profile(monkeypatch):
    settings, policy = configured_policy(monkeypatch)
    source = evidence(text="La excepcion requiere autorizacion escrita. " * 600)
    llm = EvidenceLlm((source,))
    result = KnowledgeAgent(llm=llm, policy=policy).synthesize(
        question="Resume todo el documento.", evidences=(source,), model_name=policy.fast_model,
        deep_model_name=policy.deep_model, choice=ModelChoice.FAST, intent=Intent.DOCUMENT_SUMMARY,
    )
    assert result.hierarchical and result.map_batches == 1
    assert result.cited_source_ids == (source.source_id,)
    assert len(llm.calls) == 2
    for call in llm.calls:
        assert_profile(call, settings, ModelChoice.DEEP)


def test_extractive_summary_budget_uses_explicit_shared_profile(monkeypatch):
    _, policy = configured_policy(monkeypatch, rag_summary_max_chunks=64)
    sources = tuple(evidence(i, text=f"Regla {i}: " + "Se conservan los comprobantes completos. " * 15)
                    for i in range(40))
    agent = KnowledgeAgent(llm=RecordingLlm([]), policy=policy)
    common = {"model": policy.fast_model, "latency_ms": 0, "evidence_truncated": False, "regenerated": True}
    fast = agent._extractive_summary(sources, choice=ModelChoice.FAST, **common)
    deep = agent._extractive_summary(sources, choice=ModelChoice.DEEP, **common)
    assert len(fast.cited_source_ids) < len(deep.cited_source_ids)
    assert set(deep.cited_source_ids) == {source.source_id for source in sources}


def test_planner_explicit_fast_profile_preserves_full_catalog_and_schema(monkeypatch):
    settings, policy = configured_policy(monkeypatch)
    llm = RecordingLlm([json.dumps(_PLAN)])
    result = build_query_plan(question="Lista departamentos.", catalog=_CATALOG, llm=llm, model_name=policy.fast_model)
    assert result is not None
    call = llm.calls[0]
    assert call["execution_profile"] == "fast"
    assert call["num_ctx"] == settings.ollama_fast_num_ctx
    assert call["max_tokens"] == settings.llm_planner_max_tokens
    assert call["response_schema"] == StructuredQueryPlan.model_json_schema()
    assert "rh_demo" in call["messages"][-1]["content"]
    assert "employees" in call["messages"][-1]["content"]
    assert "department" in call["messages"][-1]["content"]


@pytest.mark.parametrize("component", ("catalog", "prefix", "question", "schema"))
def test_planner_excessive_context_fails_closed_without_inference(monkeypatch, component):
    overrides = {"llm_system_prefix": "x" * 100000} if component == "prefix" else {}
    _, policy = configured_policy(monkeypatch, **overrides)
    catalog = [{**_CATALOG[0], "description": "x" * 100000}] if component == "catalog" else _CATALOG
    question = "x" * 100000 if component == "question" else "Lista departamentos."
    if component == "schema":
        monkeypatch.setattr(StructuredQueryPlan, "model_json_schema", lambda: {"description": "x" * 100000})
    llm = RecordingLlm([])
    assert build_query_plan(question=question, catalog=catalog, llm=llm, model_name=policy.fast_model) is None
    assert not llm.calls
