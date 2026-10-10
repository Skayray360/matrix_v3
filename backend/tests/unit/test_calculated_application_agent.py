# Creado por Aldo Garcia.
"""La recuperacion calculada no publica borradores ni amplía el contrato."""

from dataclasses import replace

import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.common.answers import UNVERIFIED_ANSWER_NOTICE, safe_nonfactual_text
from app.config import get_settings
from app.llm.model_policy import Intent, ModelPolicy
from app.llm.ollama_client import ChatResult
from tests.unit.test_calculated_application import QUESTION, SOURCE

BAD = "Si los datos declarados son correctos, la Aportación Base se calcula al 99%. [[E1]]"


class Transport:
    def __init__(self, text=BAD):
        self.text = text
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        text = self.text[len(self.calls) - 1] if isinstance(self.text, list) else self.text
        return ChatResult(content=text, model=kwargs["model"], latency_ms=7)


def run(monkeypatch, *, text=BAD, question=QUESTION, source=SOURCE, **kwargs):
    settings = get_settings()
    monkeypatch.setattr(settings, "answer_evidence_mode", "cited")
    monkeypatch.setattr(settings, "answer_structured_output", False)
    transport = Transport(text)
    policy = ModelPolicy()
    agent = KnowledgeAgent(llm=transport, policy=policy)
    return transport, lambda: agent.synthesize(
        question=question, evidences=(source,), model_name=policy.fast_model, **kwargs,
    )


def test_two_numeric_failures_return_only_the_verified_calculation(monkeypatch):
    transport, synthesize = run(monkeypatch)
    result = synthesize()
    assert len(transport.calls) == 2
    assert result.regenerated and result.grounding.grounded
    assert result.latency_ms == 14
    assert result.answer_basis == "documented"
    assert result.cited_source_ids == (SOURCE.source_id,)
    assert "99%" not in result.answer
    assert "63%" in result.answer
    assert "12" in result.answer
    assert result.grounding.citations_valid
    assert not result.grounding.factual_verified


@pytest.mark.parametrize("first_numeric", [True, False])
def test_numeric_and_uncited_tail_failures_still_allow_the_complete_calculation(monkeypatch, first_numeric):
    tail = BAD.replace("99%", "63%") + " ¿Podría confirmar si la vigencia del plan es actual?"
    replies = [BAD, tail] if first_numeric else [tail, BAD]
    transport, synthesize = run(monkeypatch, text=replies)
    result = synthesize()
    assert len(transport.calls) == 2 and result.grounding.grounded
    assert "63%" in result.answer and "99%" not in result.answer
    assert "confirmar" not in result.answer


@pytest.mark.parametrize("kwargs", [
    {"evidence_truncated": True},
    {"intent": Intent.MIXED},
    {"question": QUESTION + " Además, compara los requisitos con otro plan."},
    {"source": replace(SOURCE, text="El programa requiere consultar una regla adicional.")},
])
def test_recovery_does_not_silently_answer_an_incomplete_or_broader_request(monkeypatch, kwargs):
    transport, synthesize = run(monkeypatch, **kwargs)
    result = synthesize()
    assert result.answer.startswith(UNVERIFIED_ANSWER_NOTICE) and safe_nonfactual_text(result.answer)
    assert result.answer_basis == "insufficient" and not result.cited_source_ids
    assert "63%" not in result.answer and "99%" not in result.answer
    assert len(transport.calls) == 2


def test_fabricated_citations_are_not_excused_by_a_computable_case(monkeypatch):
    transport, synthesize = run(monkeypatch, text=BAD.replace("[[E1]]", "[[unknown#0]]"))
    result = synthesize()
    assert result.answer.startswith(UNVERIFIED_ANSWER_NOTICE) and safe_nonfactual_text(result.answer)
    assert not result.cited_source_ids and "unknown#0" not in result.answer
    assert len(transport.calls) == 2


def test_a_valid_generated_answer_keeps_the_normal_single_attempt(monkeypatch):
    transport, synthesize = run(monkeypatch, text=BAD.replace("99%", "63%"))
    result = synthesize()
    assert result.grounding.grounded and not result.regenerated
    assert len(transport.calls) == 1


def test_computable_evidence_gets_one_retry_before_accepting_false_insufficiency(monkeypatch):
    transport, synthesize = run(monkeypatch, text='No cuento con informacion suficiente para responder esta pregunta.')
    result = synthesize()
    assert len(transport.calls) == 2
    assert result.regenerated and result.grounding.grounded
    assert result.answer.count('63%') == 3
    assert result.cited_source_ids == (SOURCE.source_id,)
    assert result.answer_basis == 'documented'
    assert all(call['model'] == transport.calls[0]['model'] for call in transport.calls)


def test_actual_absence_of_rule_is_not_retried_as_false_insufficiency(monkeypatch):
    transport, synthesize = run(
        monkeypatch, text='No cuento con informacion suficiente para responder esta pregunta.',
        source=replace(SOURCE, text='El programa requiere consultar una regla adicional.'),
    )
    result = synthesize()
    assert len(transport.calls) == 1
    assert result.answer_basis == 'insufficient'
