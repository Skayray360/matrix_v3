# Creado por Aldo Garcia.
"""Contrato sintetico del transporte JSON, sin llamadas al modelo instalado."""
import json

import pytest

from app.agents.documentary_output import DOCUMENTARY_SCHEMA, render_documentary_output
from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.prompts import build_answer_messages
from app.common.errors import AnswerValidationError
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.config import get_settings
from app.llm.model_policy import ModelPolicy
from app.llm.ollama_client import ChatResult
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit


def payload(text="Se permite 1 prestamo por ejercicio.", alias="E1"):
    return json.dumps({"status": "answered", "claims": [{"text": text, "citations": [alias]}],
                       "clarification": ""})


def evidence():
    return Evidence(source_id="prestaciones/regla.txt#1", text="Se permite un prestamo por ejercicio.",
                    score=0.8, category="prestaciones", filename="regla.txt", section="Prestamos",
                    page_or_sheet="pagina 1", document_id="document", chunk_id="chunk")


class Client:
    def __init__(self, *answers):
        self.answers, self.calls = iter(answers), []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        return ChatResult(content=answer, model=kwargs["model"], latency_ms=1)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)


def run(client):
    policy = ModelPolicy()
    return KnowledgeAgent(llm=client, policy=policy).synthesize(
        question="Cuantos prestamos permite el documento?", evidences=(evidence(),),
        model_name=policy.fast_model,
    )


def test_rendered_claim_is_verified_and_public_citation_is_canonical():
    client = Client(payload())
    result = run(client)
    assert result.grounding.grounded and not result.grounding.factual_verified
    assert "[[prestaciones/regla.txt#1]]" in result.answer
    assert '"claims"' not in result.answer
    assert client.calls[0]["response_schema"] == DOCUMENTARY_SCHEMA
    assert len(client.calls) == 1


@pytest.mark.parametrize("answer", [
    payload("Se permiten 9 prestamos."), payload(alias="E99"),
    payload("### Orientación general"), payload("Regla.\nSegunda afirmacion"),
    payload("Regla [[E1]]"), "{}", "no es JSON", "```json\n{}\n```",
    '{"status":"insufficient","status":"answered","claims":[],"clarification":""}',
    '{"status":"answered","claims":[],"clarification":""}',
    '{"status":"insufficient","claims":[],"clarification":"Se permiten nueve prestamos."}',
    '{"status":"insufficient","claims":[],"clarification":"","extra":"regla"}',
])
def test_bad_claim_or_transport_is_never_published(answer):
    client = Client(answer, answer)
    with pytest.raises(AnswerValidationError):
        run(client)
    assert len(client.calls) == 2


def test_invalid_first_output_uses_only_existing_retry():
    client = Client("{}", payload())
    result = run(client)
    assert result.regenerated and result.grounding.grounded
    assert len(client.calls) == 2


def test_provider_schema_failure_is_retried_once():
    client = Client(InferenceFailureError(InferenceFailureKind.SCHEMA), payload())
    assert run(client).regenerated
    assert len(client.calls) == 2


def test_transport_timeout_is_not_retried():
    client = Client(InferenceFailureError(InferenceFailureKind.TIMEOUT))
    with pytest.raises(InferenceFailureError):
        run(client)
    assert len(client.calls) == 1


def test_insufficiency_has_no_documentary_facts():
    client = Client(json.dumps({"status": "insufficient", "claims": [],
                               "clarification": "Que documento deseas consultar?"}))
    result = run(client)
    assert result.answer_basis == "insufficient"
    assert not result.cited_source_ids


def test_switch_off_preserves_legacy_transport(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_structured_output", False)
    client = Client("Se permite 1 prestamo por ejercicio. [[E1]]")
    assert run(client).grounding.grounded
    assert "response_schema" not in client.calls[0]


def test_schema_instructions_are_excluded_from_summary_and_extractive(monkeypatch):
    normal = build_answer_messages(question="Regla?", evidences=(evidence(),))
    summary = build_answer_messages(question="Resume", evidences=(evidence(),), document_summary=True)
    assert "TRANSPORTE:" in normal[0]["content"]
    assert "TRANSPORTE:" not in summary[0]["content"]
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "extractive")
    extractive = build_answer_messages(question="Regla?", evidences=(evidence(),))
    assert "TRANSPORTE:" not in extractive[0]["content"]


def test_each_claim_keeps_its_citation():
    value = json.loads(payload())
    value["claims"].append({"text": "Otro hecho.", "citations": ["E2"]})
    answer = render_documentary_output(json.dumps(value), {"E1": "a", "E2": "b"})
    assert "ejercicio. [[E1]]\n\nOtro hecho. [[E2]]" in answer
