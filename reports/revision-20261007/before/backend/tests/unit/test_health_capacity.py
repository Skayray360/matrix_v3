# Creado por Aldo Garcia.
"""Readiness no anuncia disponible un proceso sin propiedad de la cola."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import Response

from app.api.routes.health import ready
from app.common.ids import utcnow_naive
from app.database.models import JobLock


@pytest.mark.parametrize("state", ["own", "foreign", "expired", "missing"])
def test_readiness_requires_current_dispatcher_lease_without_renewing(monkeypatch, state):
    monkeypatch.setattr("app.database.engine.check_database", lambda: (True, "synthetic"))
    monkeypatch.setattr("app.database.migrator.pending_migrations", lambda: [])
    monkeypatch.setattr("app.agents.chat_queue.get_chat_queue", lambda: SimpleNamespace(owner="synthetic-owner"))
    client = SimpleNamespace(ping=lambda: True, list_models=lambda: SimpleNamespace(has=lambda _: True))
    monkeypatch.setattr("app.llm.ollama_client.get_ollama_client", lambda: client)
    monkeypatch.setattr("app.rag.vector_store.get_vector_store", lambda: SimpleNamespace(health=lambda: (True, "ok")))
    monkeypatch.setattr(
        "app.auth.provider.get_identity_provider",
        lambda: SimpleNamespace(name="local_test", status=lambda: "CONNECTED_AND_VALIDATED"),
    )
    lease = None if state == "missing" else SimpleNamespace(
        locked_by="foreign-owner" if state == "foreign" else "synthetic-owner",
        expires_at=utcnow_naive() + timedelta(minutes=-1 if state == "expired" else 1),
    )
    db = MagicMock()
    db.get.return_value = lease
    scope = MagicMock()
    scope.__enter__.return_value = db
    monkeypatch.setattr("app.database.engine.get_sessionmaker", lambda: lambda: scope)
    response = Response()
    result = ready(response)
    component = next(item for item in result.components if item.name == "chat_dispatcher")
    assert result.ready is (state == "own")
    assert component.ok is (state == "own")
    assert response.status_code == (200 if state == "own" else 503)
    assert "owner" not in component.detail
    db.get.assert_called_once_with(JobLock, "chat_dispatcher")
    db.commit.assert_not_called()
    db.execute.assert_not_called()
