# Creado por Aldo Garcia.
"""Cola SQL acotada y dispatcher local. Un unico proceso API posee el lease.

HTTP confirma tras commit y no espera inferencia. La cola guarda solo texto e ID
interno de sesion; el worker reconstruye permisos actuales antes de recuperar
informacion. Un trabajo running ambiguo expira: nunca se reproduce al reiniciar.
"""

from __future__ import annotations

from contextlib import ExitStack, suppress
from datetime import timedelta
from threading import Event, RLock, Thread
from time import monotonic

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.agents.chat_service import execute_chat
from app.agents.process_owner import new_owner, owner_state
from app.auth.sessions import resolve_session_id
from app.authorization.context import UserContext
from app.authorization.policy import get_policy_engine
from app.common.chat_failures import chat_failure_code
from app.common.errors import CapacityFullError, ForbiddenError, MatrixError, OllamaUnavailableError
from app.common.ids import utcnow_naive
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.common.logging import bind_request_context, get_logger, reset_request_context
from app.config import get_settings
from app.database.engine import get_sessionmaker
from app.database.models import ChatOperation, JobLock
from app.memory.service import MemoryService, authorization_fingerprint
from app.security.admission import admission, snapshot, wait_for_inference

logger = get_logger(__name__)
NOTICE = "Tu solicitud está en proceso. Puede demorar un poco porque tenemos muchas solicitudes en curso."
PENDING = ("queued", "running")
LEASE_NAME = "chat_dispatcher"
_memory = MemoryService()


def _failure_fields(error: Exception) -> dict[str, str | int]:
    """Solo tipo, codigo de dominio y numero SQL; nunca mensajes/parametros."""
    fields: dict[str, str | int] = {"error_type": type(error).__name__}
    if isinstance(error, MatrixError):
        fields["error_code"] = str(error.code)
    if isinstance(error, InferenceFailureError):
        with suppress(TypeError, ValueError):
            fields["failure_kind"] = str(InferenceFailureKind(error.failure_kind))
    if isinstance(error, OperationalError):
        args = getattr(error.orig, "args", ())
        if args and type(args[0]) is int:
            fields["database_error_number"] = args[0]
    return fields


def effective_limit() -> int:
    settings = get_settings()
    return min(settings.chat_max_inflight, settings.inference_max_inflight)


def expire_stalled(db: Session) -> None:
    settings = get_settings()
    now = utcnow_naive()
    queued_cutoff = now - timedelta(seconds=settings.chat_queue_timeout_seconds)
    running_cutoff = now - timedelta(seconds=settings.llm_request_deadline_seconds + 60)
    db.execute(
        update(ChatOperation)
        .where(
            or_(
                (ChatOperation.status == "queued") & (ChatOperation.created_at < queued_cutoff),
                (ChatOperation.status == "running")
                & (func.coalesce(ChatOperation.started_at, ChatOperation.created_at) < running_cutoff),
            )
        )
        .values(status="expired", message=None, session_id=None)
    )


def capacity_status(db: Session) -> dict:
    settings = get_settings()
    counts = snapshot()
    active = counts.get("chat", 0)
    inference_active = counts.get("inference", 0)
    queued = db.scalar(select(func.count()).select_from(ChatOperation).where(ChatOperation.status == "queued")) or 0
    limit = effective_limit()
    utilization = max(active / limit, inference_active / settings.inference_max_inflight) * 100
    return {
        "active": active,
        "limit": limit,
        "queued": queued,
        "queue_limit": settings.chat_queue_max_size,
        "inference_active": inference_active,
        "inference_limit": settings.inference_max_inflight,
        "utilization_pct": round(utilization, 2),
        "overloaded": (
            active * 100 >= limit * settings.chat_busy_threshold_percent
            or inference_active * 100 >= settings.inference_max_inflight * settings.chat_busy_threshold_percent
        ),
    }



def operation_status(db: Session, operation: ChatOperation) -> dict:
    capacity = capacity_status(db)
    return {
        "status": operation.status,
        "operation_id": operation.id,
        "error_code": operation.error_code if operation.status == "failed" else None,
        "conversation_id": operation.conversation_id,
        "response": operation.response if operation.status == "completed" else None,
        "capacity": capacity,
        "notice": NOTICE if operation.status in PENDING and capacity["overloaded"] else None,
        "poll_after_ms": 2000,
    }


def context_for_operation(db: Session, operation: ChatOperation) -> UserContext:
    record, user = resolve_session_id(db, operation.session_id)
    if user.id != operation.user_id:
        raise ForbiddenError()
    ctx = get_policy_engine().build_context(
        db, user=user, session_id=record.id, request_id=operation.request_id or operation.id
    )
    ctx.require_valid(get_settings().app_secret_key.get_secret_value())
    categories = get_policy_engine().effective_categories(ctx)
    if authorization_fingerprint(db, ctx, categories) != operation.authorization_scope:
        raise ForbiddenError("Sus permisos cambiaron durante la consulta. Vuelva a consultar.")
    return ctx


class ChatQueue:
    """Un dispatcher, ningun ejecutor con cola ilimitada y cupos compartidos."""

    def __init__(self) -> None:
        self.lock = RLock()
        self.owner = new_owner()
        self._wake = Event()
        self._stop = Event()
        self._thread: Thread | None = None
        self._workers: dict[str, Thread] = {}

    def ensure_owner(self, db: Session) -> None:
        """Falla cerrado si otro proceso atiende IA sobre esta base de datos.

        Un proceso local muerto se recupera sin cooldown. Un dueño vivo, remoto,
        desconocido o antiguo no se confunde con un proceso propio huérfano.
        El caller debe confirmar esta transaccion antes de iniciar IA.
        """
        if self._stop.is_set():
            raise OllamaUnavailableError("El servicio se esta deteniendo.")
        now = utcnow_naive()
        row = db.scalar(select(JobLock).where(JobLock.lock_name == LEASE_NAME).with_for_update())
        if row is None:
            try:
                with db.begin_nested():
                    row = JobLock(
                        lock_name=LEASE_NAME, locked_by=self.owner, locked_at=now, expires_at=now
                    )
                    db.add(row)
                    db.flush()
            except IntegrityError:
                row = db.scalar(select(JobLock).where(JobLock.lock_name == LEASE_NAME).with_for_update())
        previous_owner = row.locked_by if row is not None else ""
        state = owner_state(previous_owner) if previous_owner != self.owner else "alive"
        if row is None or (
            previous_owner != self.owner
            and (state == "alive" or (state != "dead" and row.expires_at > now))
        ):
            raise OllamaUnavailableError(
                "Este proceso no posee el servicio de IA. Se requiere un unico proceso API.", detail="chat_queue_owner"
            )
        if previous_owner != self.owner:
            # El único dueño anterior ya no puede publicar. No repetir una
            # inferencia ambigua que estaba running cuando murió el proceso.
            db.execute(
                update(ChatOperation).where(ChatOperation.status == "running")
                .values(status="expired", message=None, session_id=None)
            )
            logger.info("chat.queue_owner_recovered", extra={"previous_owner_state": state})
        row.locked_by = self.owner
        row.locked_at = now
        row.expires_at = now + timedelta(seconds=get_settings().llm_request_deadline_seconds + 60)
        db.flush()

    def require_no_pending(self, db: Session, ctx: UserContext, conversation_id: str | None) -> None:
        conditions = [ChatOperation.user_id == ctx.user_id]
        if conversation_id:
            conditions.append(ChatOperation.conversation_id == conversation_id)
        existing = db.scalar(
            select(ChatOperation.id).where(ChatOperation.status.in_(PENDING), or_(*conditions)).limit(1)
        )
        counts = snapshot()
        if existing or counts.get(f"user:{ctx.user_id}", 0):
            raise CapacityFullError(
                "Ya tiene una solicitud pendiente. Esta nueva solicitud no fue aceptada; espere su resultado."
            )

    def reserve(self, *, user_id: str, conversation_id: str) -> ExitStack:
        resources = ExitStack()
        try:
            resources.enter_context(admission("chat", effective_limit()))
            resources.enter_context(admission(f"conversation:{conversation_id}", 1))
            resources.enter_context(admission(f"user:{user_id}", 1))
            return resources
        except Exception:
            resources.close()
            raise

    def submit(self, db: Session, operation: ChatOperation) -> dict:
        """Caller posee lock, valida idempotencia/ACL y agrega conversacion en la misma TX."""
        operation_id = operation.id
        if self._stop.is_set():
            raise OllamaUnavailableError("El servicio se esta deteniendo.")
        self.ensure_owner(db)
        expire_stalled(db)
        capacity = capacity_status(db)
        resources = None
        if capacity["queued"] == 0 and capacity["active"] < capacity["limit"]:
            with suppress(OllamaUnavailableError):
                resources = self.reserve(user_id=operation.user_id, conversation_id=operation.conversation_id)
        if resources is None and capacity["queued"] >= get_settings().chat_queue_max_size:
            raise CapacityFullError()
        operation.status = "running" if resources else "queued"
        operation.started_at = utcnow_naive() if resources else None
        db.add(operation)
        try:
            db.commit()
        except Exception:
            if resources:
                resources.close()
            raise
        # Construir la aceptacion antes de ejecutar: no usa objetos ORM compartidos
        # con el worker y no retiene una conexion durante el procesamiento.
        try:
            accepted = operation_status(db, operation)
            db.commit()
        except Exception:
            db.rollback()
            if resources:
                resources.close()
            # El primer commit ya pudo confirmar la operacion. La clave sigue
            # siendo consultable, pero nunca dejar un cupo sin worker propietario.
            with suppress(Exception), get_sessionmaker()() as failure_db:
                failure_db.execute(
                    update(ChatOperation)
                    .where(ChatOperation.id == operation_id, ChatOperation.status.in_(PENDING))
                    .values(status="failed", message=None, session_id=None)
                )
                failure_db.commit()
            raise
        if resources:
            self._launch(operation_id, resources)
        self.start()
        self._wake.set()
        return accepted

    def start(self) -> None:
        with self.lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stop.is_set():
                raise OllamaUnavailableError("El servicio se esta deteniendo.")
            self._thread = Thread(target=self._dispatch_loop, name="matrix-chat-dispatcher", daemon=True)
            self._thread.start()

    def _launch(self, operation_id: str, resources: ExitStack) -> None:
        worker = Thread(target=self._run, args=(operation_id, resources), name="matrix-chat-worker", daemon=True)
        self._workers[operation_id] = worker
        try:
            worker.start()
        except Exception:
            self._workers.pop(operation_id, None)
            resources.close()
            with get_sessionmaker()() as db:
                db.execute(
                    update(ChatOperation).where(ChatOperation.id == operation_id)
                    .values(status="failed", message=None, session_id=None)
                )
                db.commit()
            raise

    def dispatch_once(self) -> None:
        """Transacciones cortas; ningun SQL permanece abierto mientras se espera cupo."""
        with self.lock, get_sessionmaker()() as db:
            self.ensure_owner(db)
            expire_stalled(db)
            db.commit()
            if self._stop.is_set():
                return
            while snapshot().get("chat", 0) < effective_limit():
                operation = db.scalar(
                    select(ChatOperation).where(ChatOperation.status == "queued")
                    .order_by(ChatOperation.created_at, ChatOperation.id).limit(1)
                )
                if operation is None:
                    break
                try:
                    resources = self.reserve(user_id=operation.user_id, conversation_id=operation.conversation_id)
                except OllamaUnavailableError:
                    break
                try:
                    changed = db.execute(
                        update(ChatOperation)
                        .where(ChatOperation.id == operation.id, ChatOperation.status == "queued")
                        .values(status="running", started_at=utcnow_naive())
                    )
                    db.commit()
                except Exception:
                    resources.close()
                    raise
                if changed.rowcount == 1:
                    self._launch(operation.id, resources)
                else:
                    resources.close()

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.dispatch_once()
            except Exception as exc:
                # No guardar prompt, credencial ni excepciones del proveedor.
                logger.warning("chat.queue_dispatch_unavailable", extra=_failure_fields(exc))
            self._wake.wait(timeout=2)
            self._wake.clear()

    def _run(self, operation_id: str, resources: ExitStack) -> None:
        # Los threads no heredan automaticamente el ContextVar del request HTTP.
        # Restaurarlo permite relacionar el fallo del proveedor con la solicitud.
        log_context = bind_request_context(operation_id=operation_id)
        try:
            with resources, get_sessionmaker()() as db:
                operation = db.get(ChatOperation, operation_id)
                if operation is None or operation.status != "running" or self._stop.is_set():
                    return
                # Consultar de nuevo sesion, usuario y RBAC; no usar ctx de submit.
                ctx = context_for_operation(db, operation)
                bind_request_context(request_id=ctx.request_id, user_opaque_id=ctx.user_id)
                conversation = _memory.get_owned_conversation(db, ctx, operation.conversation_id)
                message = operation.message
                if message is None:
                    raise ForbiddenError()
                expected_scope = operation.authorization_scope
                db.commit()

                def reauthorize(check_db: Session) -> UserContext:
                    owner = check_db.scalar(select(JobLock.locked_by).where(JobLock.lock_name == LEASE_NAME))
                    if owner != self.owner:
                        raise ForbiddenError("El proceso ya no posee esta solicitud.")
                    current = check_db.get(ChatOperation, operation_id)
                    if current is None or current.status != "running" or self._stop.is_set():
                        raise ForbiddenError("Solicitud cancelada o expirada.")
                    return context_for_operation(check_db, current)

                with wait_for_inference(get_settings().llm_request_deadline_seconds):
                    execute_chat(
                        db, ctx=ctx, conversation=conversation, message=message,
                        expected_scope=expected_scope, operation_id=operation_id, reauthorize=reauthorize,
                    )
        except Exception as exc:
            with suppress(Exception), get_sessionmaker()() as db:
                db.execute(
                    update(ChatOperation)
                    .where(ChatOperation.id == operation_id, ChatOperation.status == "running")
                    .values(status="failed", error_code=chat_failure_code(exc), message=None, session_id=None)
                )
                db.commit()
            logger.warning("chat.queue_job_failed", extra=_failure_fields(exc))
        finally:
            reset_request_context(log_context)
            with self.lock:
                self._workers.pop(operation_id, None)
            self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self.lock:
            workers = list(self._workers.values())
            operation_ids = tuple(self._workers)
        if operation_ids:
            with suppress(Exception), get_sessionmaker()() as db:
                db.execute(
                    update(ChatOperation)
                    .where(ChatOperation.id.in_(operation_ids), ChatOperation.status == "running")
                    .values(status="expired", message=None, session_id=None)
                )
                db.commit()
        # Cierra sólo las conexiones HTTP de ESTE backend. No apaga ni descarga
        # modelos de un Ollama compartido; la cancelación física depende del runtime.
        from app.llm.ollama_client import reset_ollama_client

        with suppress(Exception):
            reset_ollama_client()
        deadline = monotonic() + 3
        for worker in workers:
            worker.join(timeout=max(0, deadline - monotonic()))
        # Mientras el proceso siga vivo y haya workers no se cede la propiedad.
        # Tras terminar Python, el siguiente proceso verifica el PID/creation time
        # y recupera el lease inmediatamente, incluso si este cierre fue forzado.
        if not any(worker.is_alive() for worker in workers) and not (self._thread and self._thread.is_alive()):
            with suppress(Exception), get_sessionmaker()() as db:
                db.execute(
                    update(JobLock).where(JobLock.lock_name == LEASE_NAME, JobLock.locked_by == self.owner)
                    .values(locked_by="released:" + self.owner, expires_at=utcnow_naive())
                )
                db.commit()


_queue: ChatQueue | None = None
_queue_lock = RLock()


def get_chat_queue() -> ChatQueue:
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = ChatQueue()
        return _queue


def stop_chat_queue() -> None:
    global _queue
    if _queue is not None:
        _queue.stop()
        _queue = None
