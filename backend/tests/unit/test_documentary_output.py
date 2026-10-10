# Creado por Aldo Garcia.
"""Contrato sintetico del transporte JSON, sin llamadas al modelo instalado."""
import json

import pytest

from app.agents.documentary_output import (
    DOCUMENTARY_GENERATION_SCHEMA,
    DocumentaryOutputError,
    render_documentary_output,
)
from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.prompts import build_answer_messages
from app.common.answers import UNVERIFIED_ANSWER_NOTICE, safe_nonfactual_text
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.config import get_settings
from app.llm.model_policy import Intent, ModelPolicy
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
    assert client.calls[0]["response_schema"] == DOCUMENTARY_GENERATION_SCHEMA
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
    result = run(client)
    assert result.answer.startswith(UNVERIFIED_ANSWER_NOTICE)
    assert safe_nonfactual_text(result.answer) and result.cited_source_ids == ()
    assert result.answer_basis == "insufficient"
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


def test_schema_instructions_cover_summary_but_exclude_extractive(monkeypatch):
    normal = build_answer_messages(question="Regla?", evidences=(evidence(),))
    summary = build_answer_messages(question="Resume", evidences=(evidence(),), document_summary=True)
    assert "TRANSPORTE:" in normal[0]["content"]
    assert "TRANSPORTE:" in summary[0]["content"]
    assert "APLICACION_CONDICIONAL" not in summary[0]["content"]
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "extractive")
    extractive = build_answer_messages(question="Regla?", evidences=(evidence(),))
    assert "TRANSPORTE:" not in extractive[0]["content"]


def test_each_claim_keeps_its_citation():
    value = json.loads(payload())
    value["claims"].append({"text": "Otro hecho.", "citations": ["E2"]})
    answer = render_documentary_output(json.dumps(value), {"E1": "a", "E2": "b"})
    assert "ejercicio. [[E1]]\n\nOtro hecho. [[E2]]" in answer


def test_schema_reserves_context_separately_from_visible_messages(monkeypatch):
    policy = ModelPolicy()
    agent = KnowledgeAgent(llm=Client(), policy=policy)
    enabled_budget = agent._input_budget_chars(policy.fast_model, Intent.DOCUMENTAL)
    monkeypatch.setattr(get_settings(), "answer_structured_output", False)
    assert agent._input_budget_chars(policy.fast_model, Intent.DOCUMENTAL) > enabled_budget


@pytest.mark.parametrize("answer,code", [
    ("not JSON with PRIVATE_TEXT", "json_parse"),
    ('{"status":"insufficient","status":"answered","claims":[],"clarification":""}', "duplicate_property"),
    ('{}', "schema_violation"),
    (payload("Una regla.\nOtra regla."), "invalid_claim_format"),
    (payload(alias="E99"), "unknown_alias"),
    ('{"status":"answered","claims":[],"clarification":""}', "empty_answer"),
    ('{"status":"insufficient","claims":[],"clarification":"PRIVATE_TEXT"}', "unsafe_clarification"),
])
def test_contract_errors_have_finite_causes_without_model_text(answer, code, caplog):
    with pytest.raises(DocumentaryOutputError) as raised:
        render_documentary_output(answer, {"E1": evidence().source_id})
    assert raised.value.code == code
    assert "PRIVATE_TEXT" not in str(raised.value)
    result = run(Client(answer, answer))
    assert result.answer.startswith(UNVERIFIED_ANSWER_NOTICE) and "PRIVATE_TEXT" not in result.answer
    event = next(record for record in caplog.records if record.message == "agent.answer_validation_failed")
    assert event.validation_detail == f"contrato documental JSON invalido: {code}"
    assert not result.cited_source_ids


def test_unknown_schema_alias_still_blocks_calculated_recovery(caplog):
    from tests.unit.test_calculated_application import QUESTION, SOURCE

    client = Client(
        payload("Si los datos declarados son correctos, la Aportación Base se calcula al 99%."),
        payload("Si los datos declarados son correctos, la Aportación Base se calcula al 63%.", alias="E99"),
    )
    result = KnowledgeAgent(llm=client, policy=ModelPolicy()).synthesize(
        question=QUESTION, evidences=(SOURCE,), model_name=get_settings().ollama_fast_model,
    )
    assert result.answer_basis == "insufficient" and not result.cited_source_ids
    assert "63%" not in result.answer and "99%" not in result.answer
    event = next(r for r in caplog.records if r.message == "agent.answer_validation_failed")
    assert event.invalid_source_count == 1
    assert event.validation_detail.endswith(": unknown_alias")
