# Creado por Aldo Garcia.
"""Antecedente durable del usuario: ventana, aislamiento, migracion y borrado."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from fractions import Fraction

import pytest
from sqlalchemy import select

from app.agents.contextual_query import contextualize_question
from app.common.errors import ForbiddenError, ValidationFailedError
from app.common.ids import utcnow_naive
from app.database.models import ConversationMessage
from app.llm.model_policy import ModelPolicy
from app.memory.service import MAX_CONTEXT_QUERY_CHARS, authorization_fingerprint
from app.rag.claim_context import declared_case
from tests.unit import test_security_privacy_revision as security_fixtures

pytestmark = [pytest.mark.unit, pytest.mark.security]
isolated_api = security_fixtures.isolated_api

BASE_QUESTION = (
    "Según el documento sintético de prestaciones, si ingresé en mayo de 2016 y "
    "tengo 7 años y 6 meses de antigüedad antes de jubilarme, ¿qué porcentaje corresponde?"
)


def own_thread(env, db):
    owner = env.sessions["a"]
    conversation = env.memory.get_owned_conversation(db, owner.ctx, owner.conversation_id)
    categories = owner.ctx.allowed_categories
    db.info["authorization_scope"] = authorization_fingerprint(db, owner.ctx, categories)
    return owner.ctx, conversation, categories


def test_last_six_messages_keep_user_anchor_without_reusing_assistant_text(isolated_api):
    env = isolated_api
    with env.factory() as db:
        ctx, conversation, categories = own_thread(env, db)
        latest = BASE_QUESTION
        for question in (BASE_QUESTION, "¿Y si llevo ocho años?", "¿Y con nueve años?", "¿Y con diez años?"):
            latest = contextualize_question(question, "" if question == BASE_QUESTION else latest)
            env.memory.append_message(db, conversation, role="user", content=question,
                                      authorized_categories=tuple(categories), context_query=latest)
            assistant = env.memory.append_message(db, conversation, role="assistant", content="ASSISTANT-ONLY-MARKER",
                                                   authorized_categories=tuple(categories))
            # Un campo heredado/importado en assistant no puede volverse antecedente.
            assistant.context_query = "ASSISTANT-ONLY-MARKER"
        db.commit()
        memory = env.memory.build_context(db, ctx, conversation, authorized_categories=categories)
        assert len(memory.turns) == 6
        assert all(turn.content != BASE_QUESTION for turn in memory.turns)
        assert all(turn.context_query is None for turn in memory.turns if turn.role == "assistant")
        prior = tuple(turn.context_query or turn.content for turn in memory.turns if turn.role == "user")
        reference = ModelPolicy().contextual_reference("¿Y con once años?", prior_questions=prior)
        result = contextualize_question("¿Y con once años?", reference)
        case = declared_case(result)
        assert case.entry == declared_case(BASE_QUESTION).entry
        assert case.tenure == Fraction(11)
        assert case.before_retirement
        assert "documento sintético" in result
        assert "ASSISTANT-ONLY-MARKER" not in result


def test_derived_query_is_private_and_not_reused_after_revocation(isolated_api):
    env = isolated_api
    with env.factory() as db:
        ctx, conversation, categories = own_thread(env, db)
        stored = env.memory.append_message(
            db, conversation, role="user", content="¿Y con ocho años?",
            authorized_categories=tuple(categories), context_query="PRIVATE-RESOLVED-QUERY",
        )
        db.commit()
        context = env.memory.build_context(db, ctx, conversation, authorized_categories=categories)
        assert context.turns[-1].context_query == "PRIVATE-RESOLVED-QUERY"
        with pytest.raises(ForbiddenError):
            env.memory.build_context(db, env.sessions["b"].ctx, conversation, authorized_categories=categories)
        revoked = replace(ctx, allowed_categories=frozenset())
        context = env.memory.build_context(db, revoked, conversation, authorized_categories=frozenset())
        assert context.turns == () and context.dropped_turns > 0
        # Los mensajes legacy del usuario sin huella no se vuelven portadores
        # autorizados de contexto calculado por el simple hecho de ser visibles.
        stored.authorization_scope = None
        db.commit()
        context = env.memory.build_context(db, ctx, conversation, authorized_categories=categories)
        assert context.turns[-1].context_query is None
    env.use("a")
    response = env.client.get(f"/api/v1/conversations/{env.sessions['a'].conversation_id}")
    assert response.status_code == 200
    assert "context_query" not in response.text and "PRIVATE-RESOLVED-QUERY" not in response.text


@pytest.mark.parametrize("mode", ["delete", "retention"])
def test_erasing_conversation_removes_derived_query(mode, isolated_api):
    env = isolated_api
    with env.factory() as db:
        ctx, conversation, categories = own_thread(env, db)
        env.memory.append_message(db, conversation, role="user", content="Followup",
                                  authorized_categories=tuple(categories), context_query="PRIVATE-RESOLVED-QUERY")
        db.commit()
        if mode == "delete":
            env.memory.delete_conversation(db, ctx, conversation.id)
        else:
            env.memory.expire_conversation(db, conversation.id, cutoff=utcnow_naive() + timedelta(seconds=1))
        db.commit()
        assert not db.scalars(select(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation.id,
        )).all()


@pytest.mark.parametrize("role,scoped,size", [
    ("assistant", True, 1), ("user", False, 1), ("user", True, 0),
    ("user", True, MAX_CONTEXT_QUERY_CHARS + 1),
])
def test_derived_query_requires_user_scope_and_bounded_complete_value(isolated_api, role, scoped, size):
    env = isolated_api
    with env.factory() as db:
        _, conversation, categories = own_thread(env, db)
        if not scoped:
            db.info.pop("authorization_scope")
        with pytest.raises(ValidationFailedError):
            env.memory.append_message(db, conversation, role=role, content="test",
                                      authorized_categories=tuple(categories), context_query="x" * size)
