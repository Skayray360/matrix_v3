# Creado por Aldo Garcia.
"""Una instalacion limpia no necesita politicas ficticias para estar operativa."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import Response

from app.api.routes.health import ready
from app.common.ids import utcnow_naive
from app.database.models import JobLock
from app.ingestion.reconciler import reconcile_knowledge
from app.rag.retriever import Retriever
from scripts import bootstrap
from tests.conftest import make_context
from tests.unit import test_data_alias_ingestion as corpus_fixtures

pytestmark = pytest.mark.unit
project = corpus_fixtures.project
corpus_environment = corpus_fixtures.corpus_environment


def test_reconciler_and_bootstrap_accept_zero_corporate_documents(corpus_environment, monkeypatch):
    env = corpus_environment
    stats = reconcile_knowledge(env.db, service=env.service)
    assert stats.scanned_files == stats.new_documents == stats.deleted_documents == 0
    assert stats.failures == []
    assert env.llm.calls == []
    assert env.store._client.get_collections().collections == []

    @contextmanager
    def synthetic_scope():
        yield env.db

    monkeypatch.setattr("app.database.engine.session_scope", synthetic_scope)
    monkeypatch.setattr("app.ingestion.reconciler.reconcile_with_lock", lambda *args, **kwargs: stats)
    assert bootstrap.cmd_ingest(SimpleNamespace(force=False)) == 0


def test_documental_query_on_empty_index_returns_no_invented_sources(corpus_environment):
    env = corpus_environment
    ctx = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    result = Retriever(store=env.store, llm=env.llm).retrieve(
        ctx=ctx, question="Cual es la politica corporativa de vacaciones?",
        authorized_categories=ctx.allowed_categories,
    )
    assert result.evidences == ()
    assert result.source_ids() == ()


def test_readiness_accepts_a_healthy_vector_store_without_collections(corpus_environment, monkeypatch):
    env = corpus_environment
    now = utcnow_naive()
    env.db.add(JobLock(
        lock_name="chat_dispatcher", locked_by="synthetic-owner", locked_at=now,
        expires_at=now + timedelta(minutes=5),
    ))
    env.db.commit()

    @contextmanager
    def synthetic_scope():
        yield env.db

    monkeypatch.setattr("app.database.engine.check_database", lambda: (True, "synthetic"))
    monkeypatch.setattr("app.database.engine.get_sessionmaker", lambda: synthetic_scope)
    monkeypatch.setattr("app.database.migrator.pending_migrations", lambda: [])
    monkeypatch.setattr("app.agents.chat_queue.get_chat_queue", lambda: SimpleNamespace(owner="synthetic-owner"))
    monkeypatch.setattr("app.llm.ollama_client.get_ollama_client", lambda: SimpleNamespace(
        ping=lambda: True, list_models=lambda: SimpleNamespace(has=lambda _: True),
    ))
    monkeypatch.setattr("app.rag.vector_store.get_vector_store", lambda: env.store)
    monkeypatch.setattr("app.auth.provider.get_identity_provider", lambda: SimpleNamespace(
        name="local_test", status=lambda: "CONNECTED_AND_VALIDATED",
    ))

    response = Response()
    result = ready(response)
    assert result.ready and response.status_code == 200
    assert env.store._client.get_collections().collections == []
