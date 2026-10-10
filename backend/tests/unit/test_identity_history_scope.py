# Creado por Aldo Garcia.
"""Identidad publica completa conservada sin excepciones a ownership o datos RH."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.common.identity import is_identity_question
from app.database.models import CategoryPermission
from app.llm.model_policy import Intent, ModelPolicy
from app.memory.service import MemoryService, authorization_fingerprint
from tests.unit import test_security_privacy_revision as security_fixtures

pytestmark = [pytest.mark.unit, pytest.mark.security]
isolated_api = security_fixtures.isolated_api


def identity_message(**changes):
    fields = {
        "role": "user", "intent": "identity", "content": "¿Quién eres?",
        "source_ids": [], "source_details": None, "context_query": None,
        "authorized_categories": [], "authorization_scope": "previous-documentary-scope",
    }
    return SimpleNamespace(**{**fields, **changes})


@pytest.mark.parametrize("question", [
    "¿Quién eres?", "Hola, quién eres", "Cómo te llamas", "Dime tu nombre",
    "Preséntate", "Qué asistente eres", "Otra pregunta, ¿quién eres?",
])
def test_exact_identity_question_reuses_pure_classifier_without_policy_or_settings(question, monkeypatch):
    policy = ModelPolicy()

    def unavailable(*_args, **_kwargs):
        pytest.fail("La identidad publica intento leer configuracion/politica.")

    monkeypatch.setattr("app.llm.model_policy.get_settings", unavailable)
    monkeypatch.setattr("app.authorization.policy.get_policy_engine", unavailable)
    assert is_identity_question(question)
    assert policy.classify_intent(question) is Intent.IDENTITY
    assert MemoryService.message_visible(identity_message(content=question), frozenset({"prestaciones"}), "new-scope")


@pytest.mark.parametrize("changes", [
    {"intent": None},
    {"content": "¿Quién eres y cuánto gano?"},
    {"content": "¿Quién eres?\nPRIVATE-EMPLOYEE-CONTENT"},
    {"content": "Consulta privada de prestaciones"},
    {"authorized_categories": ["nomina"]},
    {"source_ids": ["nomina/private.pdf#0"]},
    {"source_details": [{"source_id": "nomina/private.pdf#0"}]},
    {"context_query": "Consulta derivada privada"},
    {"role": "assistant", "content": "Soy Matrix. Su salario es privado."},
])
def test_identity_tag_cannot_make_other_content_or_metadata_public(changes):
    assert not MemoryService.message_visible(identity_message(**changes), frozenset({"prestaciones"}), "new-scope")


def test_history_keeps_identity_pair_across_scope_change_and_denies_foreign_owner(isolated_api):
    env = isolated_api
    owner = env.sessions["a"]
    with env.factory() as db:
        conversation = env.memory.get_owned_conversation(db, owner.ctx, owner.conversation_id)
        # Es exactamente la huella sin categorias de la ruta publica de identidad.
        db.info["authorization_scope"] = authorization_fingerprint(db, owner.ctx, frozenset())
        env.memory.append_message(db, conversation, role="user", content="¿Quién eres?", intent="identity")
        env.memory.append_message(db, conversation, role="assistant", content="Soy Matrix.", intent="identity")
        db.commit()
    env.use("a")
    url = f"/api/v1/conversations/{owner.conversation_id}"
    response = env.client.get(url)
    assert response.status_code == 200
    assert [(item["role"], item["content"]) for item in response.json()["messages"]][-2:] == [
        ("user", "¿Quién eres?"), ("assistant", "Soy Matrix."),
    ]
    with env.factory() as db:
        db.execute(delete(CategoryPermission).where(CategoryPermission.role_id == "role-a"))
        db.commit()
    response = env.client.get(url)
    assert response.status_code == 200
    assert [(item["role"], item["content"]) for item in response.json()["messages"]] == [
        ("user", "¿Quién eres?"), ("assistant", "Soy Matrix."),
    ]
    assert "documented-a" not in response.text
    env.use("b")
    assert env.client.get(url).status_code == 404
