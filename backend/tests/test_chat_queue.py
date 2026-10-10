# Creado por Aldo Garcia.
"""Admisión y cola HTTP con usuarios y sesiones sintéticas, sin inferencia real."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_db
from app.api.middleware import matrix_error_handler
from app.auth.sessions import create_session
from app.common.errors import MatrixError, OllamaUnavailableError
from app.common.ids import new_id, sha256_text, utcnow_naive
from app.config import get_settings
from app.database.models import Base, ChatOperation, Conversation, JobLock, Role, SessionRecord, User, UserRole
from app.security.admission import admission, snapshot, wait_for_inference
from app.security.rate_limit import get_rate_limiter

pytestmark = pytest.mark.unit


@compiles(MEDIUMTEXT, "sqlite")
def compile_queue_mediumtext(_type, _compiler, **_kwargs):
    return "TEXT"


@dataclass
class QueueHarness:
    client: TestClient
    factory: object
    session_id: str
    user_id: str
    csrf: str

    def operation(self, request_id: str):
        with self.factory() as db:
            return db.get(ChatOperation, sha256_text(f"{self.user_id}|{request_id}"))


@pytest.fixture
def queue_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'chat-queue.db'}",
        connect_args={"check_same_thread": False},
        pool_size=8,
        max_overflow=0,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def controlled_queue(queue_db, monkeypatch):
    """Controla cuándo comienza cada worker, manteniendo SQL y admisión reales."""
    from app.agents import chat_queue, chat_service

    manager = chat_queue.ChatQueue()
    launches = {}
    settings = get_settings()
    monkeypatch.setattr(settings, "chat_max_inflight", 2)
    monkeypatch.setattr(settings, "inference_max_inflight", 2)
    monkeypatch.setattr(settings, "chat_queue_max_size", 2)
    monkeypatch.setattr(settings, "chat_busy_threshold_percent", 90)
    monkeypatch.setattr(chat_queue, "get_sessionmaker", lambda: queue_db)
    monkeypatch.setattr(chat_service, "get_sessionmaker", lambda: queue_db)
    monkeypatch.setattr(chat_queue, "_queue", manager)
    monkeypatch.setattr(manager, "start", lambda: None)
    monkeypatch.setattr(manager, "_launch", lambda operation_id, resources: launches.update({operation_id: resources}))
    calls = []

    def handle_chat(db, *, ctx, conversation, message):
        db.commit()
        calls.append((ctx.user_id, message))
        return outcome(conversation)

    monkeypatch.setattr(chat_service, "get_orchestrator", lambda: SimpleNamespace(handle_chat=handle_chat))
    get_rate_limiter().reset()
    yield SimpleNamespace(manager=manager, launches=launches, calls=calls, settings=settings)
    for resources in launches.values():
        resources.close()
    get_rate_limiter().reset()


@contextmanager
def authenticated_client(factory):
    """Las rutas resuelven cookies y permisos reales: no se inyecta un rol HTTP."""
    from app.api.routes.chat import router

    with factory() as db:
        user = User(
            id=new_id(),
            username=f"queue-{new_id()}",
            display_name="Usuario sintético de cola",
            auth_source="local_test",
            is_active=True,
        )
        db.add(user)
        issued = create_session(db, user=user, auth_source="local_test")
        db.commit()

    def isolated_db():
        with factory() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.add_exception_handler(MatrixError, matrix_error_handler)
    app.dependency_overrides[get_db] = isolated_db
    with TestClient(app) as client:
        client.cookies.set(get_settings().session_cookie_name, issued.session_token)
        client.headers["X-CSRF-Token"] = issued.csrf_token
        yield QueueHarness(client, factory, issued.session_id, user.id, issued.csrf_token)


def submit(harness: QueueHarness, request_id: str, **overrides):
    return harness.client.post(
        "/api/chat/submit",
        json={"message": "Hola", "client_request_id": request_id, **overrides},
    )


def status(harness: QueueHarness, request_id: str):
    return harness.client.get(f"/api/chat/requests/{request_id}")


def outcome(conversation):
    return SimpleNamespace(
        conversation_id=conversation.id,
        message_id=new_id(),
        answer="Respuesta sintética autorizada.",
        public_sources=lambda: [],
        intent="general",
        grounded=False,
        answer_basis="general",
        latency_ms=1,
    )


def test_submit_requires_idempotency_key(queue_db):
    with authenticated_client(queue_db) as harness:
        response = harness.client.post("/api/chat/submit", json={"message": "Hola"})
    assert response.status_code == 422
    with queue_db() as db:
        assert db.scalar(select(ChatOperation.id)) is None


def test_submit_requires_browser_csrf_and_session(queue_db):
    with authenticated_client(queue_db) as harness:
        del harness.client.headers["X-CSRF-Token"]
        rejected = submit(harness, "request-csrf-123456")
        assert rejected.status_code == 403
        harness.client.cookies.clear()
        anonymous = submit(harness, "request-anon-123456")
        assert anonymous.status_code == 401
    with queue_db() as db:
        assert db.scalar(select(ChatOperation.id)) is None


def test_worker_waits_for_inference_without_rejecting_accepted_work():
    entering = Event()
    acquired = Event()

    def worker():
        with wait_for_inference(2):
            entering.set()
            with admission("inference", 1):
                acquired.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with admission("inference", 1):
            future = pool.submit(worker)
            assert entering.wait(1)
            assert not acquired.wait(0.02)
            # Un request legado no hereda el permiso de espera del worker.
            with pytest.raises(OllamaUnavailableError), admission("inference", 1):
                pytest.fail("No existe un segundo cupo")
        future.result(timeout=2)
    assert acquired.is_set()


def test_worker_inference_wait_has_finite_deadline():
    with (
        admission("inference", 1),
        wait_for_inference(0.01),
        pytest.raises(OllamaUnavailableError),
        admission("inference", 1),
    ):
        pytest.fail("Un worker sin cupo debe agotar su plazo")


def test_cancel_route_interrupts_the_active_turn_and_releases_its_slots(queue_db, controlled_queue, monkeypatch):
    from app.agents import chat_service
    from app.llm.request_control import check_inference_control

    entered, finished = Event(), Event()

    def controlled_generation(db, **kwargs):
        db.commit()
        entered.set()
        try:
            while not finished.wait(0.01):
                check_inference_control()
            raise AssertionError("La cancelacion debe interrumpir el turno")
        finally:
            finished.set()

    monkeypatch.setattr(chat_service, "get_orchestrator", lambda: SimpleNamespace(handle_chat=controlled_generation))
    manager = controlled_queue.manager
    with authenticated_client(queue_db) as harness, ThreadPoolExecutor(max_workers=1) as pool:
        assert submit(harness, "request-cancel-active-123").status_code == 202
        operation = harness.operation("request-cancel-active-123")
        resources = controlled_queue.launches.pop(operation.id)
        worker = pool.submit(manager._run, operation.id, resources)
        assert entered.wait(1)
        response = harness.client.post("/api/chat/requests/request-cancel-active-123/cancel")
        assert response.status_code == 200
        worker.result(timeout=2)
        assert finished.is_set()
        assert status(harness, "request-cancel-active-123").json()["status"] == "cancelled"
        assert harness.operation("request-cancel-active-123").response is None
    assert snapshot() == {}
    assert manager._cancellations == {}


def test_cancelled_inference_wait_exits_without_waiting_for_another_user():
    from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
    from app.llm.request_control import inference_control

    entered, cancelled = Event(), Event()

    def waiting_turn():
        with inference_control(10, cancel_event=cancelled), wait_for_inference(10):
            entered.set()
            with admission("inference", 1):
                pytest.fail("La cancelacion no debe adquirir el cupo de otro usuario")

    with ThreadPoolExecutor(max_workers=1) as pool, admission("inference", 1):
        future = pool.submit(waiting_turn)
        assert entered.wait(1)
        cancelled.set()
        with pytest.raises(InferenceFailureError) as caught:
            future.result(timeout=1)
        assert caught.value.failure_kind is InferenceFailureKind.CANCELLED
        assert snapshot() == {"inference": 1}
    assert snapshot() == {}


def test_cancellation_events_are_isolated_and_not_reused_after_completion():
    from app.agents.chat_queue import ChatQueue

    manager = ChatQueue()
    with manager.request_control("a") as first, manager.request_control("b") as other:
        manager.cancel("a")
        assert first.is_set()
        assert not other.is_set()
    with manager.request_control("a") as next_turn:
        assert not next_turn.is_set()


@pytest.mark.parametrize(
    ("active", "limit", "busy"),
    [(0, 50, False), (44, 50, False), (45, 50, True), (50, 50, True), (57, 64, False)],
)
def test_capacity_notice_uses_actual_admission_occupancy(queue_db, controlled_queue, active, limit, busy):
    from app.agents.chat_queue import operation_status

    controlled_queue.settings.chat_max_inflight = limit
    controlled_queue.settings.inference_max_inflight = limit
    with ExitStack() as slots, queue_db() as db:
        for _ in range(active):
            slots.enter_context(admission("chat", limit))
        operation = ChatOperation(status="queued", conversation_id=new_id())
        result = operation_status(db, operation)
        assert result["capacity"]["active"] == active
        assert result["capacity"]["limit"] == limit
        assert result["capacity"]["overloaded"] is busy
        assert result["capacity"]["utilization_pct"] == round(active * 100 / limit, 2)
        assert bool(result["notice"]) is busy
        assert result["poll_after_ms"] == 2000


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled", "expired"])
def test_terminal_request_never_claims_to_be_processing(queue_db, controlled_queue, terminal_status):
    from app.agents.chat_queue import operation_status

    with admission("inference", 1), queue_db() as db:
        controlled_queue.settings.inference_max_inflight = 1
        result = operation_status(db, ChatOperation(status=terminal_status, conversation_id=new_id()))
    assert result["capacity"]["overloaded"] is True
    assert result["notice"] is None


def test_embedding_occupancy_uses_its_own_configured_limit(queue_db, controlled_queue):
    from app.agents.chat_queue import capacity_status

    controlled_queue.settings.chat_max_inflight = 1
    controlled_queue.settings.inference_max_inflight = 4
    with admission("inference", 4), queue_db() as db:
        capacity = capacity_status(db)
    assert capacity["active"] == 0
    assert capacity["inference_active"] == 1
    assert capacity["inference_limit"] == 4
    assert capacity["utilization_pct"] == 25
    assert capacity["overloaded"] is False


def test_51st_request_is_accepted_queued_then_runs_when_capacity_is_released(queue_db, controlled_queue):
    controlled_queue.settings.chat_max_inflight = 50
    controlled_queue.settings.inference_max_inflight = 50
    request_id = "request-51st-123456"
    with authenticated_client(queue_db) as harness:
        with ExitStack() as occupied:
            for _ in range(50):
                occupied.enter_context(admission("chat", 50))
            response = submit(harness, request_id)
            assert response.status_code == 202, response.text
            data = response.json()
            assert data["status"] == "queued"
            assert data["capacity"]["active"] == 50
            assert data["capacity"]["queued"] == 1
            assert data["notice"]
            assert not controlled_queue.launches
            assert not controlled_queue.calls
            # La espera HTTP no mantiene una conexión SQL reservada.
            assert queue_db.kw["bind"].pool.checkedout() == 0
        controlled_queue.manager.dispatch_once()
        operation = harness.operation(request_id)
        assert operation.status == "running"
        controlled_queue.manager._run(operation.id, controlled_queue.launches[operation.id])
        completed = status(harness, request_id)
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"
        assert completed.json()["response"]["answer"] == "Respuesta sintética autorizada."
        assert completed.json()["notice"] is None
    assert len(controlled_queue.calls) == 1


def test_full_queue_rejects_without_false_acceptance_or_persisting_work(queue_db, controlled_queue):
    controlled_queue.settings.chat_max_inflight = 1
    controlled_queue.settings.inference_max_inflight = 1
    controlled_queue.settings.chat_queue_max_size = 1
    with authenticated_client(queue_db) as first, authenticated_client(queue_db) as second, admission("chat", 1):
        accepted = submit(first, "request-queued-123456")
        assert accepted.status_code == 202
        rejected = submit(second, "request-rejected-123456")
        assert rejected.status_code == 503, rejected.text
        assert rejected.json()["code"] == "chat_capacity_full"
        assert "no" in rejected.json()["message"].lower()
        assert "status" not in rejected.json()
        assert "notice" not in rejected.json()
        assert rejected.headers["Retry-After"]
        assert second.operation("request-rejected-123456") is None
        assert not controlled_queue.calls
    with queue_db() as db:
        assert len(db.scalars(select(ChatOperation)).all()) == 1


def test_retry_of_accepted_request_is_idempotent_and_owned(queue_db, controlled_queue):
    request_id = "request-idempotent-123456"
    with authenticated_client(queue_db) as owner, authenticated_client(queue_db) as other, ExitStack() as occupied:
        occupied.enter_context(admission("chat", 2))
        occupied.enter_context(admission("chat", 2))
        first = submit(owner, request_id)
        duplicate = submit(owner, request_id)
        assert first.status_code == duplicate.status_code == 202
        assert first.json()["conversation_id"] == duplicate.json()["conversation_id"]
        assert duplicate.json()["status"] == "queued"
        changed = submit(owner, request_id, message="Otro contenido")
        assert changed.status_code == 422
        second_pending = submit(owner, "request-second-123456")
        assert second_pending.status_code == 503
        assert status(other, request_id).status_code == 404
        assert other.client.post(f"/api/chat/requests/{request_id}/cancel").status_code == 404
    with queue_db() as db:
        operations = db.scalars(select(ChatOperation)).all()
        assert len(operations) == 1


def test_cancelling_queued_work_never_calls_model_and_drops_pending_text(queue_db, controlled_queue):
    request_id = "request-cancel-123456"
    with authenticated_client(queue_db) as harness:
        with ExitStack() as occupied:
            occupied.enter_context(admission("chat", 2))
            occupied.enter_context(admission("chat", 2))
            response = submit(harness, request_id)
            assert response.status_code == 202
            cancelled = harness.client.post(f"/api/chat/requests/{request_id}/cancel")
            assert cancelled.status_code == 200
        controlled_queue.manager.dispatch_once()
        result = status(harness, request_id)
        assert result.json()["status"] == "cancelled"
        assert result.json()["notice"] is None
        operation = harness.operation(request_id)
        assert operation.message is None
        assert operation.session_id is None
    assert not controlled_queue.launches
    assert not controlled_queue.calls


@pytest.mark.parametrize("revocation", ["session", "expiry", "user", "role"])
def test_worker_revalidates_session_and_current_roles_before_model_access(queue_db, controlled_queue, revocation):
    request_id = f"request-revoked-{revocation}-123456"
    with authenticated_client(queue_db) as harness:
        with ExitStack() as occupied:
            occupied.enter_context(admission("chat", 2))
            occupied.enter_context(admission("chat", 2))
            assert submit(harness, request_id).status_code == 202
            with queue_db() as db:
                session = db.get(SessionRecord, harness.session_id)
                if revocation == "session":
                    session.revoked_at = utcnow_naive()
                elif revocation == "expiry":
                    session.expires_at = utcnow_naive() - timedelta(seconds=1)
                elif revocation == "user":
                    db.get(User, harness.user_id).is_active = False
                else:
                    role = Role(id=new_id(), name="changed-role")
                    db.add(role)
                    db.flush()
                    db.add(UserRole(user_id=harness.user_id, role_id=role.id))
                db.commit()
        controlled_queue.manager.dispatch_once()
        operation = harness.operation(request_id)
        controlled_queue.manager._run(operation.id, controlled_queue.launches[operation.id])
        failed = harness.operation(request_id)
        assert failed.status == "failed"
        assert failed.response is None
        assert failed.message is None
        assert failed.session_id is None
    assert not controlled_queue.calls


def test_expired_queue_entry_releases_capacity_without_replaying_it(queue_db, controlled_queue):
    from app.agents.chat_queue import expire_stalled

    request_id = "request-expired-123456"
    with authenticated_client(queue_db) as harness:
        with ExitStack() as occupied:
            occupied.enter_context(admission("chat", 2))
            occupied.enter_context(admission("chat", 2))
            assert submit(harness, request_id).status_code == 202
            with queue_db() as db:
                operation = db.get(ChatOperation, harness.operation(request_id).id)
                operation.created_at = utcnow_naive() - timedelta(hours=2)
                db.commit()
                expire_stalled(db)
                db.commit()
        controlled_queue.manager.dispatch_once()
        operation = harness.operation(request_id)
        assert operation.status == "expired"
        assert operation.message is None
        assert operation.session_id is None
    assert not controlled_queue.launches
    assert not controlled_queue.calls


def test_second_dispatcher_cannot_multiply_the_capacity_limit(queue_db, controlled_queue):
    from app.agents.chat_queue import ChatQueue

    with queue_db() as db:
        controlled_queue.manager.ensure_owner(db)
        db.commit()
    second_process = ChatQueue()
    with queue_db() as db, pytest.raises(OllamaUnavailableError, match="unico proceso"):
        second_process.ensure_owner(db)


def test_alive_owner_cannot_be_taken_over_even_if_lease_expired(queue_db, controlled_queue, monkeypatch):
    from app.agents import process_owner
    from app.agents.chat_queue import LEASE_NAME, ChatQueue

    monkeypatch.setattr(process_owner.psutil, "Process", lambda _pid: SimpleNamespace(pid=4242, create_time=lambda: 123.5))
    controlled_queue.manager.owner = process_owner.new_owner()
    assert process_owner.owner_state(controlled_queue.manager.owner) == "alive"

    with queue_db() as db:
        controlled_queue.manager.ensure_owner(db)
        db.get(JobLock, LEASE_NAME).expires_at = utcnow_naive() - timedelta(minutes=1)
        db.commit()
    with queue_db() as db, pytest.raises(OllamaUnavailableError):
        ChatQueue().ensure_owner(db)


def test_unknown_legacy_owner_is_not_stolen_before_expiry(queue_db, controlled_queue):
    from app.agents.chat_queue import LEASE_NAME

    now = utcnow_naive()
    with queue_db() as db:
        db.add(JobLock(lock_name=LEASE_NAME, locked_by="legacy-uuid", locked_at=now,
                       expires_at=now + timedelta(minutes=10)))
        db.commit()
    with queue_db() as db, pytest.raises(OllamaUnavailableError):
        controlled_queue.manager.ensure_owner(db)


def test_fencing_rejects_answer_if_dispatcher_ownership_changed(queue_db, controlled_queue, monkeypatch):
    from app.agents import chat_service
    from app.agents.chat_queue import LEASE_NAME

    with authenticated_client(queue_db) as harness:
        request_id = "request-fencing-123456"
        assert submit(harness, request_id).status_code == 202
        operation = harness.operation(request_id)

        def changed_owner(db, *, ctx, conversation, message):
            db.commit()
            with queue_db() as replacement_db:
                replacement_db.get(JobLock, LEASE_NAME).locked_by = "replacement-owner"
                replacement_db.commit()
            return outcome(conversation)

        monkeypatch.setattr(chat_service, "get_orchestrator", lambda: SimpleNamespace(handle_chat=changed_owner))
        controlled_queue.manager._run(operation.id, controlled_queue.launches[operation.id])
        result = harness.operation(request_id)
        assert result.status == "failed"
        assert result.response is None
        assert result.message is None


def test_cancellation_during_generation_prevents_late_publication(queue_db, controlled_queue, monkeypatch):
    from app.agents import chat_service

    request_id = "request-late-cancel-123456"
    with authenticated_client(queue_db) as harness:
        accepted = submit(harness, request_id)
        assert accepted.status_code == 202
        operation = harness.operation(request_id)
        assert operation.status == "running"

        def cancelled_generation(db, *, ctx, conversation, message):
            db.commit()
            assert harness.client.post(f"/api/chat/requests/{request_id}/cancel").status_code == 200
            return outcome(conversation)

        monkeypatch.setattr(chat_service, "get_orchestrator", lambda: SimpleNamespace(handle_chat=cancelled_generation))
        controlled_queue.manager._run(operation.id, controlled_queue.launches[operation.id])
        actual = status(harness, request_id)
        assert actual.json()["status"] == "cancelled"
        assert actual.json()["response"] is None
        assert actual.json()["notice"] is None
        assert harness.operation(request_id).response is None


def test_shutdown_preserves_queued_work_for_next_dispatcher(queue_db, controlled_queue, monkeypatch):
    from app.agents.chat_queue import ChatQueue

    request_id = "request-restart-queued-123456"
    with authenticated_client(queue_db) as harness:
        with ExitStack() as occupied:
            occupied.enter_context(admission("chat", 2))
            occupied.enter_context(admission("chat", 2))
            accepted = submit(harness, request_id)
            assert accepted.json()["status"] == "queued"
        controlled_queue.manager.stop()
        preserved = harness.operation(request_id)
        assert preserved.status == "queued"
        assert preserved.message == "Hola"
        assert preserved.session_id == harness.session_id

        replacement = ChatQueue()
        monkeypatch.setattr(
            replacement,
            "_launch",
            lambda operation_id, resources: controlled_queue.launches.update({operation_id: resources}),
        )
        replacement.dispatch_once()
        assert harness.operation(request_id).status == "running"
        replacement._run(preserved.id, controlled_queue.launches[preserved.id])
        assert harness.operation(request_id).status == "completed"
        assert len(controlled_queue.calls) == 1


@pytest.mark.parametrize("worker_still_alive", [False, True])
def test_shutdown_expires_running_work_without_replay_and_retains_live_worker_lease(
    queue_db, controlled_queue, monkeypatch, worker_still_alive
):
    from app.agents.chat_queue import ChatQueue

    request_id = "request-restart-running-123456"
    with authenticated_client(queue_db) as harness:
        assert submit(harness, request_id).json()["status"] == "running"
        operation = harness.operation(request_id)
        controlled_queue.manager._workers[operation.id] = SimpleNamespace(
            join=lambda timeout: None, is_alive=lambda: worker_still_alive
        )
        controlled_queue.manager.stop()
        expired = harness.operation(request_id)
        assert expired.status == "expired"
        assert expired.message is None
        assert expired.session_id is None
        assert expired.response is None

        replacement = ChatQueue()
        if worker_still_alive:
            with queue_db() as db, pytest.raises(OllamaUnavailableError, match="unico proceso"):
                replacement.ensure_owner(db)
            # El proceso ha terminado: el reemplazo comprueba su identidad y
            # recupera la reserva SIN adelantar el reloj 660 segundos.
            monkeypatch.setattr("app.agents.chat_queue.owner_state", lambda _owner: "dead")
        controlled_queue.launches[operation.id].close()
        relaunched = []
        monkeypatch.setattr(replacement, "_launch", lambda *args: relaunched.append(args))
        replacement.dispatch_once()
        assert not relaunched
        assert not controlled_queue.calls
        assert harness.operation(request_id).status == "expired"


def test_running_expiration_uses_start_time_instead_of_time_spent_queued(queue_db, controlled_queue):
    from app.agents.chat_queue import expire_stalled

    request_id = "request-recent-start-123456"
    with authenticated_client(queue_db) as harness:
        assert submit(harness, request_id).json()["status"] == "running"
        operation_id = harness.operation(request_id).id
        with queue_db() as db:
            operation = db.get(ChatOperation, operation_id)
            operation.created_at = utcnow_naive() - timedelta(hours=2)
            operation.started_at = utcnow_naive()
            db.commit()
            expire_stalled(db)
            db.commit()
        current = harness.operation(request_id)
        assert current.status == "running"
        assert current.message == "Hola"
        assert current.session_id == harness.session_id
        with queue_db() as db:
            db.get(ChatOperation, operation_id).started_at = utcnow_naive() - timedelta(hours=2)
            db.commit()
            expire_stalled(db)
            db.commit()
        assert harness.operation(request_id).status == "expired"


def test_acceptance_serialization_failure_releases_reserved_slots_and_purges_work(
    queue_db, controlled_queue, monkeypatch
):
    from app.agents import chat_queue

    request_id = "request-status-failure-123456"

    def unavailable_status(db, operation):
        raise RuntimeError("Fallo sintético al construir la aceptación")

    monkeypatch.setattr(chat_queue, "operation_status", unavailable_status)
    with authenticated_client(queue_db) as harness:
        with pytest.raises(RuntimeError, match="Fallo sintético"):
            submit(harness, request_id)
        operation = harness.operation(request_id)
        assert operation is not None
        assert operation.status == "failed"
        assert operation.message is None
        assert operation.session_id is None
        assert operation.response is None
        counts = snapshot()
        assert counts.get("chat", 0) == 0
        assert counts.get(f"user:{harness.user_id}", 0) == 0
        assert counts.get(f"conversation:{operation.conversation_id}", 0) == 0
    assert not controlled_queue.launches
    assert not controlled_queue.calls


def test_concurrent_retries_reserve_one_slot_and_one_operation(queue_db, controlled_queue):
    request_id = "request-concurrent-123456"
    simultaneous = Barrier(2)
    with authenticated_client(queue_db) as harness, ThreadPoolExecutor(max_workers=2) as workers:
        def send_same_request():
            simultaneous.wait(timeout=2)
            return submit(harness, request_id)

        requests = [workers.submit(send_same_request) for _ in range(2)]
        responses = [request.result(timeout=5) for request in requests]
        assert [response.status_code for response in responses] == [202, 202]
        assert responses[0].json()["conversation_id"] == responses[1].json()["conversation_id"]
        assert all(response.json()["status"] == "running" for response in responses)
        assert all(response.json()["capacity"]["active"] == 1 for response in responses)
        assert len(controlled_queue.launches) == 1
        with queue_db() as db:
            operations = db.scalars(select(ChatOperation)).all()
            assert len(operations) == 1
        assert snapshot().get("chat", 0) == 1
    assert not controlled_queue.calls


@pytest.mark.parametrize("occupied", [False, True])
def test_zero_queue_capacity_accepts_only_work_that_can_start_immediately(queue_db, controlled_queue, occupied):
    controlled_queue.settings.chat_queue_max_size = 0
    request_id = "request-no-queue-123456"
    with authenticated_client(queue_db) as harness, ExitStack() as reservations:
        if occupied:
            reservations.enter_context(admission("chat", 2))
            reservations.enter_context(admission("chat", 2))
        response = submit(harness, request_id)
        if occupied:
            assert response.status_code == 503
            assert "notice" not in response.json()
            assert harness.operation(request_id) is None
            assert not controlled_queue.launches
        else:
            assert response.status_code == 202
            assert response.json()["status"] == "running"
            assert response.json()["capacity"]["queued"] == 0
            assert response.json()["capacity"]["queue_limit"] == 0
            assert len(controlled_queue.launches) == 1


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/submit"])
def test_foreign_pending_conversation_is_indistinguishable_from_unknown_conversation(
    queue_db, controlled_queue, endpoint
):
    with authenticated_client(queue_db) as owner, authenticated_client(queue_db) as other, ExitStack() as occupied:
        occupied.enter_context(admission("chat", 2))
        occupied.enter_context(admission("chat", 2))
        accepted = submit(owner, "request-owner-private-123456")
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "queued"
        foreign_id = accepted.json()["conversation_id"]
        foreign = other.client.post(
            endpoint,
            json={
                "message": "Hola",
                "client_request_id": "request-foreign-private-123456",
                "conversation_id": foreign_id,
            },
        )
        unknown = other.client.post(
            endpoint,
            json={
                "message": "Hola",
                "client_request_id": "request-unknown-private-123456",
                "conversation_id": new_id(),
            },
        )
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.json() == unknown.json()
        assert other.operation("request-foreign-private-123456") is None
        assert other.operation("request-unknown-private-123456") is None
    with queue_db() as db:
        assert len(db.scalars(select(ChatOperation)).all()) == 1
        assert len(db.scalars(select(Conversation)).all()) == 1
