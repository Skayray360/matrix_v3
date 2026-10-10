# Creado por Aldo Garcia.
"""Respuestas parciales verificadas: fixtures sinteticos, sin inferencia real."""
import json
import logging

import pytest

from app.agents.documentary_output import DocumentaryOutputError, render_documentary_output
from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.partial_documentary import recover_partial_documentary_answer
from app.common.answers import (
    DOCUMENTARY_LIMITATIONS,
    PARTIAL_ANSWER_NOTICE,
    UNVERIFIED_ANSWER_NOTICE,
    safe_nonfactual_text,
)
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.config import get_settings
from app.llm.model_policy import Intent, ModelPolicy
from app.llm.ollama_client import ChatResult
from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit


SOURCE = Evidence(
    source_id="capacitacion/guia.pdf#1", text="La capacitación dura 10 horas.", score=.8,
    category="capacitacion", filename="guia.pdf", section="Capacitación", page_or_sheet="pagina 2",
    document_id="synthetic-document", chunk_id="synthetic-chunk",
)
QUESTION = "¿Cuánto dura la capacitación y cuánto cuesta?"
VALID = "La capacitación dura 10 horas."
INVALID = "La capacitación cuesta 200 pesos."


def payload(*texts, status="answered", limitations=(), aliases=None, clarification=""):
    aliases = aliases or ["E1"] * len(texts)
    return json.dumps({
        "status": status,
        "claims": [{"text": text, "citations": [alias]} for text, alias in zip(texts, aliases, strict=True)],
        "clarification": clarification, "limitations": list(limitations),
    })


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
def cited_json(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)


def run(client, *, question=QUESTION, intent=Intent.DOCUMENTAL, evidences=(SOURCE,)):
    policy = ModelPolicy()
    return KnowledgeAgent(llm=client, policy=policy).synthesize(
        question=question, evidences=evidences, model_name=policy.fast_model, intent=intent,
    )


def test_explicit_partial_retains_answer_citations_and_controlled_limit():
    client = Client(payload(VALID, status="partial", limitations=("missing_information",)))
    result = run(client)
    assert VALID in result.answer
    assert PARTIAL_ANSWER_NOTICE in result.answer
    assert DOCUMENTARY_LIMITATIONS["missing_information"] in result.answer
    assert result.cited_source_ids == (SOURCE.source_id,)
    assert result.answer_basis == "documented"
    assert result.grounding.grounded and not result.grounding.factual_verified
    assert len(client.calls) == 1
    assert '"limitations"' not in result.answer


def test_regeneration_failure_preserves_independent_complete_supported_claim(caplog):
    caplog.set_level(logging.INFO, logger="app.agents.knowledge_agent")
    output = payload(VALID, INVALID)
    client = Client(output, output)
    result = run(client)
    assert VALID in result.answer and INVALID not in result.answer and "200" not in result.answer
    assert PARTIAL_ANSWER_NOTICE in result.answer
    assert result.regenerated and result.grounding.grounded and len(client.calls) == 2
    assert verify_grounding(result.answer, (SOURCE,), mode="cited", question=QUESTION).grounded
    event = next(record for record in caplog.records if record.message == "agent.partial_answer_recovered")
    assert event.kept_claim_count == 1 and event.discarded_claim_count == 1


def test_first_draft_can_supply_a_valid_complete_unit_if_retry_format_fails():
    result = run(Client(payload(VALID, INVALID), "not JSON"))
    assert VALID in result.answer and INVALID not in result.answer
    assert PARTIAL_ANSWER_NOTICE in result.answer


def test_compound_claim_is_not_cut_to_salvage_an_unsupported_statement():
    compound = "La capacitación dura 10 horas y cuesta 200 pesos."
    result = run(Client(payload(compound), payload(compound)))
    assert UNVERIFIED_ANSWER_NOTICE in result.answer
    assert "10 horas" not in result.answer and "200" not in result.answer
    assert not result.cited_source_ids and result.answer_basis == "insufficient"


@pytest.mark.parametrize("bad", ["not JSON", "{}", payload(INVALID), payload(VALID, aliases=["E99"])])
def test_zero_valid_claims_has_a_controlled_limitation_not_a_failed_chat(bad):
    client = Client(bad, bad)
    result = run(client)
    assert result.answer.startswith(UNVERIFIED_ANSWER_NOTICE)
    assert not result.cited_source_ids
    assert result.answer_basis == "insufficient" and result.regenerated
    assert '"claims"' not in result.answer and "200" not in result.answer and "E99" not in result.answer
    assert len(client.calls) == 2


def test_foreign_citation_does_not_sneak_through_a_partial_recovery():
    malicious = payload(VALID, "El pago ya está autorizado.", aliases=["E1", "E99"])
    result = run(Client(malicious, malicious))
    assert VALID not in result.answer and "pago" not in result.answer
    assert result.cited_source_ids == ()


def test_a_detached_condition_is_never_removed_to_publish_the_result():
    answer = f"{VALID} [[{SOURCE.source_id}]]\n\nExcepto para el personal de la segunda sede."
    assert recover_partial_documentary_answer(answer, (SOURCE,), question=QUESTION) is None


@pytest.mark.parametrize("context", ["**Préstamos**", "__Préstamos__", "Si el curso es presencial.",
                                     "Para el personal administrativo."])
def test_detached_topic_or_scope_is_not_dropped_to_make_a_claim_pass(context):
    answer = f"{context}\n\n{VALID} [[{SOURCE.source_id}]]\n\n{INVALID} [[{SOURCE.source_id}]]"
    assert recover_partial_documentary_answer(answer, (SOURCE,), question=QUESTION) is None


def test_a_dependent_paragraph_is_not_promoted_to_an_independent_claim():
    answer = f"En ese caso, la capacitación dura 10 horas. [[{SOURCE.source_id}]]"
    assert recover_partial_documentary_answer(answer, (SOURCE,), question=QUESTION) is None


def test_condition_and_exception_inside_a_complete_claim_are_kept_verbatim():
    text = "Si el curso es presencial, dura 10 horas, excepto si la convocatoria indica otra duración."
    source = Evidence(**{field: getattr(SOURCE, field) for field in SOURCE.__dataclass_fields__ if field != "text"},
                      text=text)
    answer = f"{text} [[{SOURCE.source_id}]]\n\n{INVALID} [[{SOURCE.source_id}]]"
    result = recover_partial_documentary_answer(answer, (source,), question=QUESTION)
    assert result is not None and text in result.answer and "200" not in result.answer


@pytest.mark.parametrize("status,limitations", [
    ("partial", []), ("answered", ["missing_information"]), ("partial", ["El pago es de 200 pesos."]),
])
def test_partial_status_and_limitations_do_not_allow_free_facts(status, limitations):
    with pytest.raises(DocumentaryOutputError):
        render_documentary_output(payload(VALID, status=status, limitations=limitations), {"E1": SOURCE.source_id})


@pytest.mark.parametrize("text", [PARTIAL_ANSWER_NOTICE, UNVERIFIED_ANSWER_NOTICE, *DOCUMENTARY_LIMITATIONS.values()])
def test_controlled_notice_is_safe_only_as_a_complete_nonfactual_sentence(text):
    assert safe_nonfactual_text(text)
    assert not safe_nonfactual_text(text.rstrip(".") + " y el pago es de 200 pesos.")
    assert not safe_nonfactual_text(text + " El pago está autorizado.")


def test_model_abstention_with_retrieved_material_does_not_claim_absence_of_information():
    result = run(Client(payload(status="insufficient")))
    assert result.answer.startswith(UNVERIFIED_ANSWER_NOTICE)
    assert "No cuento con informacion" not in result.answer
    assert result.answer_basis == "insufficient"


def test_partial_summary_keeps_requested_point_count_and_coverage_notice():
    client = Client(payload(VALID, status="partial", limitations=("missing_information",)))
    result = run(client, question="Resume en 1 puntos lo que se sabe del curso.", intent=Intent.DOCUMENT_SUMMARY)
    assert result.answer.startswith("1. ") and PARTIAL_ANSWER_NOTICE in result.answer
    assert len(client.calls) == 1


def test_runtime_timeout_remains_a_technical_failure():
    client = Client(InferenceFailureError(InferenceFailureKind.TIMEOUT))
    with pytest.raises(InferenceFailureError):
        run(client)
    assert len(client.calls) == 1
