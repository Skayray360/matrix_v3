# Creado por Aldo Garcia.
"""La recuperacion jerarquica conserva la identidad global de las fuentes."""
from dataclasses import replace

import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.common.errors import AnswerValidationError
from app.config import get_settings
from app.llm.model_policy import Intent, ModelPolicy
from tests.unit.test_document_summary_synthesis import Client, claim, source

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("same_text", [False, True])
def test_summary_rejects_source_identity_collision_across_map_batches_before_generation(monkeypatch, same_text):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    monkeypatch.setattr(get_settings(), "answer_structured_output", True)
    first = source(text="La guia describe la autenticacion Kerberos.")
    other = replace(
        first, document_id="different-authorized-document", chunk_id="different-chunk",
        text=first.text if same_text else "La guia describe la comprobacion de tickets.",
    )
    client = Client(claim(first.text), claim(other.text), "{}")
    policy = ModelPolicy()
    agent = KnowledgeAgent(llm=client, policy=policy)
    monkeypatch.setattr(agent, "_summary_exceeds_context", lambda evidences, **_: len(evidences) > 1)
    monkeypatch.setattr(agent, "_partition_summary_evidence", lambda *_, **__: ((first,), (other,)))
    with pytest.raises(AnswerValidationError):
        agent.synthesize(question="Resume la guia", evidences=(first, other),
                         model_name=policy.fast_model, intent=Intent.DOCUMENT_SUMMARY)
    assert not client.calls
