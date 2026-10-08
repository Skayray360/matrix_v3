# Creado por Aldo Garcia.
"""Presupuesto por tarea y evidencia completa con ventanas iguales; sin runtime real."""

from __future__ import annotations

import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.prompts import build_answer_messages
from app.config.settings import Settings
from app.llm.model_policy import Intent, ModelChoice, ModelPolicy
from app.llm.ollama_client import ChatResult
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit


def policy_with_settings(monkeypatch, **overrides):
    config = {
        "_env_file": None, "app_env": "test", "answer_evidence_mode": "cited",
        "ollama_fast_model": "synthetic-generator", "ollama_deep_model": "synthetic-generator",
        "ollama_fast_num_ctx": 8192, "ollama_deep_num_ctx": 8192,
        "ollama_fast_max_tokens": 1536, "ollama_deep_max_tokens": 3072,
        "llm_system_prefix": "",
    }
    settings = Settings(**(config | overrides))
    for module in ("app.agents.prompts", "app.agents.knowledge_agent", "app.llm.model_policy"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    return settings, ModelPolicy()


@pytest.mark.parametrize("intent", (Intent.DOCUMENTAL, Intent.STRUCTURED, Intent.MIXED))
@pytest.mark.parametrize("retry", (False, True))
def test_equal_windows_keep_same_evidence_capacity_on_deep(monkeypatch, intent, retry):
    settings, policy = policy_with_settings(monkeypatch)
    fast = policy.generation_profile(policy.fast_model, intent=intent, choice=ModelChoice.FAST, retry=retry)
    deep = policy.generation_profile(policy.deep_model, intent=intent, choice=ModelChoice.DEEP, retry=retry)
    assert fast.num_ctx == deep.num_ctx == 8192
    assert deep.max_tokens == fast.max_tokens == 1536
    assert deep.temperature == (settings.llm_retry_temperature if retry else settings.llm_temperature)


@pytest.mark.parametrize(
    ("deep_context", "deep_ceiling", "expected_output"),
    ((8192, 768, 768), (8704, 3072, 2048), (16384, 3072, 3072), (4096, 3072, 1536)),
)
def test_context_changes_and_lower_output_ceilings_remain_explicit(
    monkeypatch, deep_context, deep_ceiling, expected_output,
):
    _, policy = policy_with_settings(
        monkeypatch, ollama_deep_num_ctx=deep_context, ollama_deep_max_tokens=deep_ceiling,
    )
    profile = policy.generation_profile(policy.deep_model, intent=Intent.DOCUMENTAL, choice=ModelChoice.DEEP)
    assert profile.num_ctx == deep_context
    assert profile.max_tokens == expected_output
    assert profile.max_tokens <= deep_ceiling
    if deep_context >= 8192:
        assert profile.num_ctx - profile.max_tokens >= 8192 - 1536


@pytest.mark.parametrize("intent", (Intent.DOCUMENT_SUMMARY, Intent.GENERAL))
def test_extended_summary_and_general_output_preserve_their_configured_ceiling(monkeypatch, intent):
    settings, policy = policy_with_settings(monkeypatch)
    profile = policy.generation_profile(policy.deep_model, intent=intent, choice=ModelChoice.DEEP)
    assert profile.max_tokens == settings.ollama_deep_max_tokens
    assert profile.num_ctx == settings.ollama_deep_num_ctx
    expected_temperature = (
        settings.llm_summary_temperature if intent is Intent.DOCUMENT_SUMMARY else settings.llm_general_temperature
    )
    assert profile.temperature == expected_temperature


class CitedAnswerLlm:
    """Returns a short cited answer while recording the exact admitted evidence."""

    def __init__(self, answer: str):
        self.answer = answer
        self.calls: list[dict] = []

    def chat(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        return ChatResult(content=self.answer, model=model, latency_ms=1)


@pytest.mark.parametrize("choice", tuple(ModelChoice))
def test_agent_preserves_end_condition_and_citation_in_new_context_margin(monkeypatch, choice):
    _, policy = policy_with_settings(monkeypatch)
    final_condition = "La solicitud requiere autorizacion escrita antes del pago."
    source = Evidence(
        source_id="synthetic/reglas.pdf#0",
        text=("Contenido descriptivo de la solicitud para su consulta. " * 145) + final_condition,
        score=0.9, category="prestaciones", filename="reglas.pdf", section="Solicitud",
        page_or_sheet="pagina 9", document_id="synthetic-document", chunk_id="synthetic-chunk",
    )
    llm = CitedAnswerLlm(f"### Información documentada\n{final_condition} [[E1]]")
    agent = KnowledgeAgent(llm=llm, policy=policy)
    question = "Que requisito previo tiene la solicitud?"
    original_messages = build_answer_messages(question=question, evidences=(source,), documentary_only=True)
    # Before the change DEEP had only 11160 input characters; the entire new
    # prompt exceeds that budget. The old packer could not admit this unit.
    assert 11160 < agent._messages_chars(original_messages) <= agent._input_budget_chars(
        policy.model_for(choice), Intent.DOCUMENTAL, choice=choice,
    )
    result = agent.synthesize(
        question=question, evidences=(source,), model_name=policy.model_for(choice), choice=choice,
    )
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["max_tokens"] == 1536
    assert source.text in call["messages"][1]["content"]
    assert source.page_or_sheet in call["messages"][1]["content"]
    assert result.grounding.grounded and result.grounding.citations_valid
    assert result.cited_source_ids == (source.source_id,)
    assert final_condition in result.answer
    assert f"[[{source.source_id}]]" in result.answer
