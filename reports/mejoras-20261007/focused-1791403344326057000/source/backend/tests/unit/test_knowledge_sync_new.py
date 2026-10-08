"""Sincronizacion real SQL/Qdrant sinteticos, sin corpus ni servicios operativos."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

from app.common.errors import MatrixError
from app.common.ids import utcnow_naive
from app.config.settings import Settings
from app.database.models import Base, Conversation, Document, DocumentVersion, IngestionJob, JobLock, User
from app.ingestion.loaders import extract_document
from app.ingestion.reconciler import (
    RECONCILE_LOCK,
    LockNotAcquired,
    ReconcileLeaseLost,
    ReconcileStats,
    reconcile_knowledge,
    reconcile_lock,
    reconcile_with_lock,
    renew_reconcile_lease,
)
from app.ingestion.service import IngestionService
from app.rag.retriever import Retriever
from app.rag.schemas import SCOPE_CONVERSATION, SCOPE_CORPORATE
from app.rag.vector_store import VectorStore
from tests.conftest import make_context
from tests.unit.test_rag_pipeline_isolated import FakeEmbeddingClient

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, rag_knowledge_root=str(tmp_path / "data" / "knowledge"),
                        upload_storage_root=str(tmp_path / "uploads"), rag_sync_stability_seconds=0,
                        rag_min_similarity=0)
    settings.knowledge_root_path.mkdir(parents=True)
    monkeypatch.setattr("app.ingestion.knowledge_layout.PROJECT_ROOT", tmp_path)
    for module in ("app.ingestion.service", "app.ingestion.reconciler", "app.ingestion.knowledge_layout",
                   "app.rag.index_manifest", "app.rag.retriever", "app.rag.vector_store", "app.jobs.scheduler",
                   "app.api.routes.admin"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    monkeypatch.setattr("app.ingestion.service.extract_document", extract_document)
    engine = create_engine(f"sqlite:///{tmp_path / 'manifest.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def scope():
        with factory() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr("app.rag.index_manifest.session_scope", scope)
    monkeypatch.setattr("app.database.engine.session_scope", scope)
    store = VectorStore(client=QdrantClient(location=":memory:"))
    llm = FakeEmbeddingClient()
    monkeypatch.setattr("app.ingestion.service.get_vector_store", lambda: store)
    monkeypatch.setattr("app.ingestion.service.get_ollama_client", lambda: llm)
    monkeypatch.setattr("app.rag.vector_store.get_vector_store", lambda: store)
    with factory() as db:
        db.add(User(id="owner", username="synthetic", display_name="synthetic", auth_source="local_test"))
        db.flush()
        db.add(Conversation(id="chat", user_id="owner", title="Sintetico"))
        db.commit()
        yield SimpleNamespace(db=db, factory=factory, store=store, llm=llm, settings=settings,
                              root=tmp_path, service=IngestionService(store=store, llm=llm))
    store.close()
    engine.dispose()


def write(env, name="prestaciones/nuevo.md", text="# Vacaciones\n\nEl convenio sintetico otorga veinte dias."):
    path = env.settings.knowledge_root_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def retrieve(env, categories=frozenset({"prestaciones"}), **kwargs):
    ctx = make_context(user_id=kwargs.pop("owner", "owner"), wildcard=False, categories=categories)
    return Retriever(store=env.store, llm=env.llm).retrieve(
        question="convenio vacaciones veinte dias", ctx=ctx, authorized_categories=categories, **kwargs,
    ).evidences


def test_new_source_is_retrievable_immediately_without_restart_and_idempotent(env):
    retriever = Retriever(store=env.store, llm=env.llm)
    ctx = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    assert not retriever.retrieve(question="vacaciones", ctx=ctx, authorized_categories=ctx.allowed_categories).evidences
    write(env)
    first = reconcile_with_lock(env.db, new_only=True)
    assert first.new_documents == 1 and first.pending_documents == 0 and not first.failures
    result = retriever.retrieve(question="convenio vacaciones veinte dias", ctx=ctx,
                                authorized_categories=ctx.allowed_categories)
    assert len(result.evidences) == 1 and "veinte dias" in result.evidences[0].text
    generation = env.db.scalar(select(Document.active_generation))
    calls = len(env.llm.calls)
    second = reconcile_with_lock(env.db, new_only=True)
    assert second.unchanged_documents == 1 and second.new_documents == 0
    assert len(env.llm.calls) == calls
    assert env.db.scalar(select(Document.active_generation)) == generation
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == 1
    assert env.db.scalar(select(func.count()).select_from(IngestionJob)) == 1


def test_sync_preserves_existing_modified_missing_and_deleted_documents(env):
    changed = write(env, "prestaciones/changed.md")
    missing = write(env, "prestaciones/missing.md")
    deleted = write(env, "prestaciones/deleted.md")
    reconcile_with_lock(env.db, new_only=True)
    before = {row.id: (row.sha256, row.active_generation) for row in env.db.scalars(select(Document))}
    document = env.db.scalar(select(Document).where(Document.filename == deleted.name))
    env.service.delete_document(env.db, document)
    env.db.commit()
    before[document.id] = (document.sha256, document.active_generation)
    missing.unlink()
    changed.write_text("# Nuevo contenido que requiere reconciliacion completa", encoding="utf-8")
    write(env, "prestaciones/otro.md")
    result = reconcile_with_lock(env.db, new_only=True)
    assert result.new_documents == 1 and result.updated_documents == result.deleted_documents == 0
    for key, value in before.items():
        env.db.expire_all()
        row = env.db.get(Document, key)
        assert (row.sha256, row.active_generation) == value
    assert env.db.get(Document, document.id).deleted_at is not None


def test_sync_retrieval_enforces_category_acl(env):
    write(env)
    write(env, "nomina/restringido.md", "# Nomina\n\nconvenio vacaciones veinte dias SALARIO SECRETO")
    stats = reconcile_with_lock(env.db, new_only=True)
    assert stats.new_documents == 2
    assert retrieve(env)
    assert all(item.category == "prestaciones" and "SECRETO" not in item.text for item in retrieve(env))


def test_private_upload_is_never_promoted_or_reindexed_by_fast_sync(env):
    private = env.service.ingest_conversation_attachment(
        env.db, data=b"# Convenio\n\nconvenio vacaciones veinte dias privado",
        display_name="privado.md", internal_filename="id.md", mime_type="text/markdown",
        owner_user_id="owner", conversation_id="chat",
    )
    env.db.commit()
    write(env)
    document = env.db.get(Document, private.document_id)
    generation = document.active_generation
    stats = reconcile_with_lock(env.db, new_only=True)
    assert stats.private_scanned == stats.private_reindexed == 0
    env.db.expire_all()
    assert (document.scope, document.category, document.active_generation) == (SCOPE_CONVERSATION, None, generation)
    assert all(item.scope == SCOPE_CORPORATE for item in retrieve(env))
    assert any(item.scope == SCOPE_CONVERSATION for item in retrieve(env, conversation_id="chat"))
    assert all(item.scope == SCOPE_CORPORATE for item in retrieve(env, owner="other", conversation_id="chat"))


def test_recent_copy_is_deferred_without_embeddings_then_indexed(env, monkeypatch):
    path = write(env)
    env.settings.rag_sync_stability_seconds = 10
    info = path.stat()
    monkeypatch.setattr("app.ingestion.reconciler.time.time", lambda: max(info.st_ctime, info.st_mtime) + 3)
    first = reconcile_with_lock(env.db, new_only=True)
    assert first.deferred_documents == first.pending_documents == 1 and not env.llm.calls
    monkeypatch.setattr("app.ingestion.reconciler.time.time", lambda: max(info.st_ctime, info.st_mtime) + 15)
    second = reconcile_with_lock(env.db, new_only=True)
    assert second.new_documents == 1 and not second.pending_documents


def test_temporary_copy_is_not_a_candidate_until_renamed(env):
    pending = write(env, "prestaciones/politica.md.part")
    assert reconcile_with_lock(env.db, new_only=True).new_documents == 0
    pending.rename(pending.with_suffix(""))
    assert reconcile_with_lock(env.db, new_only=True).new_documents == 1


def test_change_during_embeddings_never_publishes_partial_source_and_retries(env, monkeypatch):
    path = write(env)
    original = env.llm.embed

    def change(texts):
        vectors = original(texts)
        path.write_text("# Convenio\n\nconvenio vacaciones treinta dias nuevo", encoding="utf-8")
        return vectors

    monkeypatch.setattr(env.llm, "embed", change)
    first = reconcile_with_lock(env.db, new_only=True)
    assert first.new_documents == 0 and first.deferred_documents == first.pending_documents == 1
    assert env.db.scalar(select(func.count()).select_from(Document)) == 0
    monkeypatch.setattr(env.llm, "embed", original)
    assert not retrieve(env)
    assert reconcile_with_lock(env.db, new_only=True).new_documents == 1
    assert "treinta dias" in retrieve(env)[0].text


def test_failed_embeddings_retry_on_next_pass_without_losing_other_new_sources(env, monkeypatch):
    write(env, "prestaciones/a.md")
    write(env, "prestaciones/b.md")
    original = env.llm.embed
    calls = 0

    def fail_once(texts):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fallo sintetico")
        return original(texts)

    monkeypatch.setattr(env.llm, "embed", fail_once)
    first = reconcile_with_lock(env.db, new_only=True)
    assert first.new_documents == len(first.failures) == first.pending_documents == 1
    assert env.db.scalar(select(func.count()).select_from(Document)) == 1
    second = reconcile_with_lock(env.db, new_only=True)
    assert second.new_documents == 1 and not second.failures and second.pending_documents == 0
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == 2


def test_batch_rotates_past_failed_files_so_later_files_are_not_starved(env, monkeypatch):
    write(env, "prestaciones/a.md", "# A\n\nSIEMPRE FALLA")
    write(env, "prestaciones/b.md")
    original = env.llm.embed

    def fail_a(texts):
        if any("SIEMPRE FALLA" in text for text in texts):
            raise RuntimeError("sintetico")
        return original(texts)

    monkeypatch.setattr(env.llm, "embed", fail_a)
    first = reconcile_with_lock(env.db, new_only=True, max_documents=1)
    assert first.pending_documents == 2 and len(first.failures) == 1
    second = reconcile_with_lock(env.db, new_only=True, max_documents=1)
    assert second.new_documents == second.pending_documents == 1
    assert env.db.scalar(select(Document.filename)) == "b.md"


def test_no_candidates_does_not_initialize_provider_or_vector_store(env, monkeypatch):
    def forbidden():
        pytest.fail("No se deben abrir servicios sin candidatos")

    monkeypatch.setattr("app.ingestion.service.get_vector_store", forbidden)
    monkeypatch.setattr("app.ingestion.service.get_ollama_client", forbidden)
    monkeypatch.setattr("app.rag.vector_store.get_vector_store", forbidden)
    assert reconcile_with_lock(env.db, new_only=True).new_documents == 0


def test_new_only_rejects_force(env):
    with pytest.raises(ValueError, match="incompatibles"):
        reconcile_knowledge(env.db, new_only=True, force=True)


def test_sync_admin_scope_does_not_publish_other_categories(env):
    write(env)
    write(env, "nomina/secret.md")
    stats = reconcile_with_lock(env.db, new_only=True, allowed_categories=frozenset({"prestaciones"}))
    assert stats.new_documents == 1 and stats.pending_documents == 0
    assert set(env.db.scalars(select(Document.category))) == {"prestaciones"}


def test_lock_rejects_competitor_and_does_not_leave_lock_after_error(env):
    with pytest.raises(RuntimeError, match="sintetico"), reconcile_lock(env.db, owner="one"):
        with env.factory() as second, pytest.raises(LockNotAcquired), reconcile_lock(second, owner="two"):
            pass
        raise RuntimeError("sintetico")
    assert env.db.get(JobLock, RECONCILE_LOCK) is None


def test_stale_owner_cannot_release_replacement_lease_or_publish(env):
    with reconcile_lock(env.db, owner="one"):
        env.db.execute(update(JobLock).where(JobLock.lock_name == RECONCILE_LOCK)
                       .values(expires_at=utcnow_naive() - timedelta(seconds=1)))
        env.db.commit()
        with env.factory() as second, reconcile_lock(second, owner="two"):
            with pytest.raises(ReconcileLeaseLost):
                renew_reconcile_lease(env.db)
            with env.factory() as observer:
                token = observer.get(JobLock, RECONCILE_LOCK).locked_by
                assert token.startswith("two:")
        assert env.db.get(JobLock, RECONCILE_LOCK) is None


def test_lock_exception_rolls_back_pending_document_and_preserves_other_owner(env):
    with pytest.raises(RuntimeError), reconcile_lock(env.db, owner="one"):
        env.db.execute(update(JobLock).where(JobLock.lock_name == RECONCILE_LOCK).values(locked_by="replacement"))
        env.db.commit()
        env.db.add(Document(id="not-published", scope="corporate", filename="synthetic.md", mime_type="text/markdown",
                            sha256="0" * 64, size_bytes=3, category="prestaciones"))
        raise RuntimeError("sintetico")
    assert env.db.get(JobLock, RECONCILE_LOCK).locked_by == "replacement"
    assert env.db.get(Document, "not-published") is None


def test_lease_lost_after_upsert_keeps_generation_invisible(env, monkeypatch):
    write(env)
    original = env.store.upsert_chunks

    def upsert_then_lose(chunks, vectors):
        result = original(chunks, vectors)
        env.db.execute(update(JobLock).where(JobLock.lock_name == RECONCILE_LOCK).values(locked_by="replacement"))
        return result

    monkeypatch.setattr(env.store, "upsert_chunks", upsert_then_lose)
    with pytest.raises(ReconcileLeaseLost):
        reconcile_with_lock(env.db, new_only=True)
    assert env.db.scalar(select(func.count()).select_from(Document)) == 0
    assert not retrieve(env)


def test_scheduler_prioritizes_chat_and_eventually_attempts_one_document(env, monkeypatch):
    from app.jobs.scheduler import ReconcileScheduler

    monkeypatch.setattr("app.security.admission.snapshot", lambda: {"chat": 1})
    reconcile = Mock(return_value=ReconcileStats(mode="new_only"))
    monkeypatch.setattr("app.ingestion.reconciler.reconcile_with_lock", reconcile)
    scheduler = ReconcileScheduler()
    for _ in range(5):
        scheduler.run_sync_once()
    assert scheduler.state.sync_last_status == "deferred_busy" and not reconcile.called
    scheduler.run_sync_once()
    assert reconcile.call_args.kwargs == {"trigger": "scheduler_sync", "new_only": True, "max_documents": 1}
    assert scheduler.state.sync_last_status == "completed"


def test_scheduler_reports_pending_failures_and_survives_exception(env, monkeypatch):
    from app.jobs.scheduler import ReconcileScheduler

    scheduler = ReconcileScheduler()
    reconcile = Mock(side_effect=[ReconcileStats(pending_documents=1),
                                  ReconcileStats(failures=[{"path": "hidden", "error": "RuntimeError"}]),
                                  RuntimeError("sintetico"), ReconcileStats()])
    monkeypatch.setattr("app.ingestion.reconciler.reconcile_with_lock", reconcile)
    for expected in ("pending", "partial_failure", "error:RuntimeError", "completed"):
        scheduler.run_sync_once()
        assert scheduler.state.sync_last_status == expected
    assert scheduler.state.sync_last_run_at


def test_scheduler_initial_sync_does_not_trigger_full_reindex(env, monkeypatch):
    from app.jobs.scheduler import ReconcileScheduler

    scheduler = ReconcileScheduler()
    monkeypatch.setattr(scheduler, "run_once", Mock(side_effect=AssertionError("full no solicitado")))
    monkeypatch.setattr(scheduler, "run_sync_once", lambda: scheduler._stop.set())
    scheduler._loop(immediate=False)
    assert scheduler._stop.is_set()


@pytest.mark.parametrize("path,method", [("/admin/knowledge/sync", "post"),
                                        ("/admin/knowledge/sync/status", "get")])
def test_sync_routes_require_explicit_permissions(env, monkeypatch, path, method):
    from app.api.deps import get_db, get_user_context, require_csrf
    from app.api.routes.admin import router

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(MatrixError)
    async def handler(_request, exc):
        return JSONResponse(status_code=exc.status_code, content=exc.to_public_dict())

    app.dependency_overrides[get_db] = lambda: env.db
    app.dependency_overrides[get_user_context] = lambda: make_context(permissions=frozenset())
    app.dependency_overrides[require_csrf] = lambda: None
    with TestClient(app) as client:
        assert getattr(client, method)(path).status_code == 403


def test_sync_status_omits_document_paths(env, monkeypatch):
    from app.api.routes.admin import sync_status
    from app.jobs.scheduler import ReconcileScheduler

    scheduler = ReconcileScheduler()
    scheduler.state.sync_stats = ReconcileStats(
        failures=[{"path": "SECRET_PATH", "error": "RuntimeError"}], last_scanned_path="SECRET_PATH",
    ).as_dict()
    monkeypatch.setattr("app.jobs.scheduler.get_scheduler", lambda: scheduler)
    response = sync_status(ctx=make_context())
    assert "SECRET_PATH" not in str(response) and response["sync_stats"]["failure_count"] == 1


def test_cli_new_only_and_force_are_mutually_exclusive():
    from scripts.bootstrap import build_parser

    parser = build_parser()
    args = parser.parse_args(["ingest", "--new-only"])
    assert args.new_only and not args.force
    with pytest.raises(SystemExit):
        parser.parse_args(["ingest", "--new-only", "--force"])
