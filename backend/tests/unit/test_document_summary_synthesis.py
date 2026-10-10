# Creado por Aldo Garcia.
"""Resumenes tecnicos citados con modelo programado; no ejecuta comandos ni usa red."""
from __future__ import annotations

import json

import pytest

from app.agents.documentary_output import DOCUMENTARY_GENERATION_SCHEMA, SCHEMA_BUDGET_CHARS
from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.prompts import build_answer_messages
from app.config import get_settings
from app.llm.model_policy import Intent, ModelPolicy
from app.llm.ollama_client import ChatResult
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit


def source(index=0, *, text=None):
    return Evidence(
        source_id=f"__private__/Guia_Kerberos.docx#{index}",
        text=text or "La guia describe la autenticacion Kerberos y la comprobacion de tickets.",
        score=1.0, category="__private__", filename="Guia_Kerberos.docx",
        section="Configuracion", page_or_sheet=f"parrafo {index + 1}",
        document_id="authorized-guide", chunk_id=f"chunk-{index}",
    )


def claim(text, alias="E1"):
    return json.dumps({"status": "answered", "claims": [{"text": text, "citations": [alias]}],
                       "clarification": ""}, ensure_ascii=False)


class Client:
    def __init__(self, *answers):
        self.answers, self.calls = iter(answers), []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return ChatResult(content=next(self.answers), model=kwargs["model"], latency_ms=1)


@pytest.fixture(autouse=True)
def cited_schema(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)


def summarize(client, evidences=None, **kwargs):
    policy = ModelPolicy()
    return KnowledgeAgent(llm=client, policy=policy).synthesize(
        question="explicame sobre este documento Guia_Kerberos.",
        evidences=evidences or (source(),), model_name=policy.fast_model,
        intent=Intent.DOCUMENT_SUMMARY, **kwargs,
    )


def test_named_guide_summary_uses_json_and_keeps_canonical_provenance():
    client = Client(claim(source().text))
    result = summarize(client)
    assert result.grounding.grounded and not result.grounding.factual_verified
    assert result.cited_source_ids == (source().source_id,)
    assert "[[__private__/Guia_Kerberos.docx#0]]" in result.answer
    assert '"claims"' not in result.answer
    assert client.calls[0]["response_schema"] == DOCUMENTARY_GENERATION_SCHEMA
    assert len(client.calls) == 1


@pytest.mark.parametrize("bad", ["Texto sin ninguna cita.", "{}", claim("FRASE_AJENA", "E99"),
                                    '{"status":"insufficient","claims":[],"clarification":""}'])
def test_invalid_summary_is_discarded_and_complete_authorized_extract_is_published(bad):
    client = Client(bad, bad)
    result = summarize(client)
    assert result.regenerated and result.grounding.extractive_verified
    assert "Resumen extractivo" in result.answer
    assert source().text in result.answer
    assert "FRASE_AJENA" not in result.answer
    assert "E99" not in result.answer
    assert result.cited_source_ids == (source().source_id,)
    assert len(client.calls) == 2


def test_summary_without_schema_setting_remains_cited(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_structured_output", False)
    client = Client(source().text + " [[E1]]")
    assert summarize(client).grounding.grounded
    assert "response_schema" not in client.calls[0]


def test_summary_reserves_schema_context_before_packing(monkeypatch):
    policy = ModelPolicy()
    agent = KnowledgeAgent(llm=Client(), policy=policy)
    with_schema = agent._input_budget_chars(policy.fast_model, Intent.DOCUMENT_SUMMARY)
    monkeypatch.setattr(get_settings(), "answer_structured_output", False)
    without = agent._input_budget_chars(policy.fast_model, Intent.DOCUMENT_SUMMARY)
    assert without - with_schema == SCHEMA_BUDGET_CHARS


def test_summary_prompt_does_not_confuse_technical_document_with_personal_benefit_case():
    messages = build_answer_messages(question="Resume esta guia", evidences=(source(),), document_summary=True)
    assert "APLICACION_CONDICIONAL" not in messages[0]["content"]
    assert "documentacion tecnica autorizada" in messages[0]["content"]
    assert "objeto JSON" in messages[-1]["content"]


@pytest.mark.parametrize("invalid_reduce", [False, True])
def test_hierarchical_summary_uses_global_aliases_and_preserves_validated_parts(monkeypatch, invalid_reduce):
    first = source(text="La guia describe la autenticacion Kerberos.")
    last = source(1, text="El documento explica la comprobacion de tickets.")
    client = Client(claim(first.text), claim(last.text), "{}" if invalid_reduce else json.dumps({
        "status": "answered", "claims": [
            {"text": first.text, "citations": ["E1"]},
            {"text": last.text, "citations": ["E2"]},
        ], "clarification": "",
    }))
    policy = ModelPolicy()
    agent = KnowledgeAgent(llm=client, policy=policy)
    monkeypatch.setattr(agent, "_summary_exceeds_context", lambda evidences, **_: len(evidences) > 1)
    monkeypatch.setattr(agent, "_partition_summary_evidence", lambda *_, **__: ((first,), (last,)))
    result = agent.synthesize(question="Resume la guia", evidences=(first, last),
                              model_name=policy.fast_model, intent=Intent.DOCUMENT_SUMMARY)
    assert result.hierarchical and result.map_batches == 2
    assert set(result.cited_source_ids) == {first.source_id, last.source_id}
    assert first.text in result.answer and last.text in result.answer
    assert len(client.calls) == 3
    assert all(call["response_schema"] == DOCUMENTARY_GENERATION_SCHEMA for call in client.calls)
    assert "[[E1]]" in client.calls[-1]["messages"][-1]["content"]
    assert "[[E2]]" in client.calls[-1]["messages"][-1]["content"]
    assert first.source_id not in client.calls[-1]["messages"][-1]["content"]


def test_failed_clarification_cannot_publish_facts_or_turn_into_validation_error():
    client = Client("AFIRMACION_NO_AUTORIZADA y una cita [[privado]].")
    policy = ModelPolicy()
    result = KnowledgeAgent(llm=client, policy=policy).ask_clarification(
        question="Resume un documento", model_name=policy.fast_model,
    )
    assert result.answer == "¿Qué documento o apartado deseas consultar?"
    assert result.answer_basis == "insufficient" and not result.cited_source_ids
    assert "AFIRMACION_NO_AUTORIZADA" not in result.answer


def test_partial_material_is_not_presented_as_complete_document():
    result = summarize(Client(claim(source().text)), evidence_truncated=True)
    assert "seleccion de unidades completas" in result.answer


@pytest.mark.parametrize("event", ["rag.document_summary_retrieved", "rag.private_summary_retrieved"])
def test_summary_diagnostic_reports_coverage_without_document_content(tmp_path, event):
    from scripts.diagnosticar_solicitud import diagnose

    reference = "a" * 64
    logs = tmp_path / "backend/logs"
    logs.mkdir(parents=True)
    record = {"message": event, "request_id": reference, "fetched": 85, "after_dedup": 85,
              "selected_documents": 1, "truncated": False, "filename": "PRIVATE_TITLE",
              "question": "PRIVATE_QUESTION", "text": "PRIVATE_CONTENT"}
    (logs / "backend-synthetic.log").write_text(json.dumps(record) + "\n", encoding="utf-8")
    result = diagnose(tmp_path, reference)
    assert result["found"] and result["events"] == [{
        "message": event, "fetched": 85, "after_dedup": 85, "selected_documents": 1, "truncated": False,
    }]
    assert "PRIVATE_" not in json.dumps(result)
