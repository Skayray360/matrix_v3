# Creado por Aldo Garcia.
"""Reconciliacion completa administrativa limitada a categorias corporativas."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.schemas import AdminIngestRequest
from app.common.errors import MatrixError
from app.database.models import Document
from app.ingestion.reconciler import reconcile_with_lock
from tests.conftest import make_context
from tests.unit import test_knowledge_sync_new as fixtures

pytestmark = pytest.mark.unit
env = fixtures.env


def document(env, filename):
    return env.db.scalar(select(Document).where(Document.filename == filename))


def snapshot(row):
    return (row.sha256, row.active_generation, row.status, row.deleted_at, row.index_cleanup_pending)


def test_scoped_api_reconciles_present_modified_deleted_only_inside_categories(env, monkeypatch):
    from app.api.routes import admin
    from app.ingestion import reconciler

    allowed_changed = fixtures.write(env, "prestaciones/changed.md")
    allowed_deleted = fixtures.write(env, "prestaciones/deleted.md")
    outside_changed = fixtures.write(env, "nomina/outside_changed.md", "# Nomina\n\nconvenio veinte dias AJENO")
    outside_deleted = fixtures.write(env, "nomina/outside_deleted.md", "# Nomina\n\nconvenio veinte dias AJENO")
    reconcile_with_lock(env.db, new_only=True)
    private = env.service.ingest_conversation_attachment(
        env.db, data=b"# Convenio\n\nconvenio vacaciones veinte dias PRIVADO",
        display_name="privado.md", internal_filename="private-id.md", mime_type="text/markdown",
        owner_user_id="owner", conversation_id="chat",
    )
    env.db.commit()
    outside = {row.id: snapshot(row) for row in (
        document(env, outside_changed.name), document(env, outside_deleted.name),
        env.db.get(Document, private.document_id),
    )}
    before_allowed = document(env, allowed_changed.name).active_generation
    old_deleted_id = document(env, allowed_deleted.name).id
    allowed_changed.write_text("# Convenio\n\nconvenio vacaciones treinta dias autorizado", encoding="utf-8")
    allowed_deleted.unlink()
    outside_changed.write_text("# Nomina\n\nOTRO_CONTENIDO_PROHIBIDO", encoding="utf-8")
    outside_deleted.unlink()
    fixtures.write(env, "prestaciones/created.md")
    fixtures.write(env, "nomina/outside_new.md", "# Nomina\n\nNUEVO_AJENO")

    original_scan = reconciler.scan_knowledge

    def scan(**kwargs):
        inventory = original_scan(**kwargs)
        inventory.warnings.append({"code": "outside", "count": 1, "message": "METADATA_AJENA"})
        return inventory

    monkeypatch.setattr(reconciler, "scan_knowledge", scan)
    monkeypatch.setattr(admin, "get_policy_engine", lambda: SimpleNamespace(
        effective_categories=lambda _ctx: frozenset({"prestaciones"}),
    ))
    monkeypatch.setattr(admin, "get_audit_service", lambda: Mock())
    # Ninguna limpieza global de vectores debe acompañar una llamada parcial.
    monkeypatch.setattr(env.store, "cleanup_generations", Mock(side_effect=AssertionError("limpieza global")))
    calls_before = len(env.llm.calls)
    response = admin.reconcile(
        AdminIngestRequest(force=True), db=env.db,
        ctx=make_context(categories=frozenset({"prestaciones"}), wildcard=False),
    )
    stats = response.stats
    assert response.started
    assert (stats["new_documents"], stats["updated_documents"], stats["deleted_documents"]) == (1, 1, 1)
    assert stats["scanned_files"] == 2 and stats["private_scanned"] == stats["private_reindexed"] == 0
    assert stats["inventory_warnings"] == [] and not stats["failures"]
    for forbidden in ("nomina", "outside", "privado", "METADATA_AJENA"):
        assert forbidden not in str(response.model_dump())
    env.db.expire_all()
    for key, before in outside.items():
        assert snapshot(env.db.get(Document, key)) == before
    assert document(env, "outside_new.md") is None
    assert document(env, "created.md").status == "indexed"
    assert document(env, "changed.md").active_generation != before_allowed
    assert env.db.get(Document, old_deleted_id).status == "deleted"
    assert "PROHIBIDO" not in str(env.llm.calls[calls_before:])
    allowed_evidence = fixtures.retrieve(env)
    assert allowed_evidence and all(item.category == "prestaciones" for item in allowed_evidence)
    assert old_deleted_id not in {item.document_id for item in allowed_evidence}
    outside_evidence = fixtures.retrieve(env, categories=frozenset({"nomina"}))
    assert {item.document_id for item in outside_evidence} == set(outside) - {private.document_id}
    private_evidence = fixtures.retrieve(env, categories=frozenset(), conversation_id="chat")
    assert {item.document_id for item in private_evidence} == {private.document_id}
    assert not fixtures.retrieve(env, categories=frozenset(), owner="other", conversation_id="chat")


def test_empty_category_scope_cannot_open_inventory_or_modify_documents(env, monkeypatch):
    from app.ingestion import reconciler

    path = fixtures.write(env)
    reconcile_with_lock(env.db, new_only=True)
    row = document(env, path.name)
    previous = snapshot(row)
    path.unlink()
    fixtures.write(env, "prestaciones/unknown.md")
    monkeypatch.setattr(reconciler, "scan_knowledge", Mock(side_effect=AssertionError("inventario ajeno")))
    monkeypatch.setattr(reconciler, "IngestionService", Mock(side_effect=AssertionError("servicio innecesario")))
    stats = reconcile_with_lock(env.db, force=True, allowed_categories=frozenset())
    env.db.expire_all()
    assert snapshot(env.db.get(Document, row.id)) == previous
    assert document(env, "unknown.md") is None
    assert stats.scanned_files == stats.deleted_documents == stats.private_scanned == 0
    assert not stats.new_categories and not stats.inventory_warnings


@pytest.mark.parametrize("unavailable_flag", ("unavailable_sources", "incomplete_sources"))
def test_scoped_full_retains_complete_source_inventory_for_unavailable_roots(env, monkeypatch, unavailable_flag):
    from app.ingestion import reconciler

    path = fixtures.write(env)
    reconcile_with_lock(env.db, new_only=True)
    row = document(env, path.name)
    previous = snapshot(row)
    inventory = reconciler.scan_knowledge(knowledge_root=env.settings.knowledge_root_path)
    source_id = inventory.files[0].source_id
    inventory.files = []  # Una raiz inaccesible no prueba que sus archivos se borraron.
    getattr(inventory, unavailable_flag).add(source_id)
    monkeypatch.setattr(reconciler, "scan_knowledge", lambda **_: inventory)
    stats = reconcile_with_lock(env.db, allowed_categories=frozenset({"prestaciones"}))
    env.db.expire_all()
    assert snapshot(env.db.get(Document, row.id)) == previous
    assert stats.deleted_documents == 0 and stats.preserved_documents == 1
    assert any(source.source_id == source_id for source in inventory.sources)


def test_reconcile_route_requires_permission_and_csrf_before_scoped_work(env, monkeypatch):
    from app.api.deps import get_db, get_user_context
    from app.api.routes import admin

    app = FastAPI()
    app.include_router(admin.router)

    @app.exception_handler(MatrixError)
    async def handler(_request, exc):
        return JSONResponse(status_code=exc.status_code, content=exc.to_public_dict())

    app.dependency_overrides[get_db] = lambda: env.db
    app.dependency_overrides[get_user_context] = lambda: make_context(permissions=frozenset())
    monkeypatch.setattr("app.api.deps.resolve_session", lambda *_args: (SimpleNamespace(csrf_token="test-csrf"), None))
    reconcile = Mock()
    monkeypatch.setattr(admin, "reconcile_with_lock", reconcile)
    with TestClient(app) as client:
        assert client.post("/admin/knowledge/reconcile", json={}).status_code == 403
        assert client.post("/admin/knowledge/reconcile", json={}, headers={"X-CSRF-Token": "test-csrf"}).status_code == 403
        assert not reconcile.called
