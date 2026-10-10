# Creado por Aldo Garcia.
"""Un fallo de formato no invalida una aplicacion calculada independiente."""

import json

import pytest

from app.common.answers import UNVERIFIED_ANSWER_NOTICE, safe_nonfactual_text
from app.config import get_settings
from tests.unit.test_calculated_application import SOURCE
from tests.unit.test_calculated_application_agent import run

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("structured,reply", [
    (False, "No pude dar formato a la respuesta solicitada."),
    (True, '{"status":"answered","claims":['),
    (True, '{"status":"answered","claims":[],"clarification":""}'),
])
def test_format_failure_recovers_only_independently_verified_rule(monkeypatch, structured, reply):
    transport, synthesize = run(monkeypatch, text=reply)
    monkeypatch.setattr(get_settings(), "answer_structured_output", structured)
    result = synthesize()
    assert len(transport.calls) == 2
    assert result.regenerated and result.grounding.grounded
    assert result.answer_basis == "documented"
    assert result.cited_source_ids == (SOURCE.source_id,)
    assert result.answer.count("63%") == 3
    assert reply not in result.answer
    assert not result.grounding.factual_verified


@pytest.mark.parametrize("first_bad", [True, False])
def test_fabricated_alias_remains_blocked_even_if_other_attempt_is_format_only(monkeypatch, first_bad):
    fabricated = json.dumps({
        "status": "answered", "claims": [{"text": "El porcentaje es 63%.", "citations": ["E99"]}],
        "clarification": "",
    })
    replies = [fabricated, "{}"] if first_bad else ["{}", fabricated]
    transport, synthesize = run(monkeypatch, text=replies)
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)
    result = synthesize()
    assert result.answer.startswith(UNVERIFIED_ANSWER_NOTICE) and safe_nonfactual_text(result.answer)
    assert not result.cited_source_ids and "63%" not in result.answer and "E99" not in result.answer
    assert len(transport.calls) == 2
