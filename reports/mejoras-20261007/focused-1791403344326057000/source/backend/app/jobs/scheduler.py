# Creado por Aldo Garcia.
"""Sincronizacion ligera de nuevos documentos y reconciliacion completa diaria.

Se implementa con un hilo demonio y un ``Event`` en lugar de una libreria de
scheduling: la unica tarea periodica del sistema es esta, y un hilo con
``Event.wait`` es interrumpible al apagar el proceso, no requiere dependencias y
no introduce un segundo modelo de concurrencia en el backend.

La proteccion frente a ejecuciones simultaneas no vive aqui sino en el lock de
base de datos (``reconcile_with_lock``): asi tambien queda cubierto el caso de
dos procesos (por ejemplo, el servidor y una ejecucion manual del script).
"""

from __future__ import annotations

import threading
from time import monotonic
from dataclasses import dataclass
from datetime import timedelta

from app.common.ids import utcnow_naive
from app.common.logging import get_logger
from app.config import get_settings

logger = get_logger(__name__)


@dataclass
class SchedulerState:
    running: bool = False
    last_run_at: str | None = None
    last_status: str = "never_run"
    run_count: int = 0
    maintenance_status: str = "never_run"
    maintenance_stats: dict[str, int] | None = None
    sync_last_run_at: str | None = None
    sync_last_status: str = "never_run"
    sync_run_count: int = 0
    sync_stats: dict[str, object] | None = None


class ReconcileScheduler:
    """Ejecuta la reconciliacion del knowledge root periodicamente."""

    def __init__(self, *, interval_hours: int | None = None) -> None:
        settings = get_settings()
        self.interval = timedelta(
            hours=interval_hours if interval_hours is not None else settings.rag_reindex_interval_hours
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = SchedulerState()
        self.sync_interval = settings.rag_sync_interval_seconds
        self._busy_deferrals = 0
        self._run_lock = threading.Lock()

    def start(self, *, run_immediately: bool = False) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="matrixrh-reconcile", daemon=True, kwargs={"immediate": run_immediately}
        )
        self._thread.start()
        self.state.running = True
        logger.info(
            "scheduler.started", extra={"interval_hours": self.interval.total_seconds() / 3600}
        )

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self.state.running = False
        logger.info("scheduler.stopped")

    def _loop(self, *, immediate: bool) -> None:
        if immediate:
            self.run_once()
        next_full = monotonic() + self.interval.total_seconds()
        next_sync = monotonic()  # Arranque: incorporar pendientes sin reindexar todo.
        while not self._stop.is_set():
            now = monotonic()
            if now >= next_full:
                self.run_once()
                next_full = monotonic() + self.interval.total_seconds()
            if now >= next_sync and not self._stop.is_set():
                self.run_sync_once()
                next_sync = monotonic() + self.sync_interval
            self._stop.wait(max(0, min(next_sync, next_full) - monotonic()))

    def run_sync_once(self) -> None:
        """Nuevos estables, con prioridad al chat y reintento automatico acotado."""
        from sqlalchemy import select

        from app.database.engine import session_scope
        from app.database.models import ChatOperation
        from app.ingestion.reconciler import reconcile_with_lock
        from app.security.admission import snapshot

        if not self._run_lock.acquire(blocking=False):
            return
        try:
            with session_scope() as db:
                active = snapshot()
                busy = bool(active.get("chat") or active.get("inference") or db.scalar(
                    select(ChatOperation.id).where(ChatOperation.status == "queued").limit(1)
                ))
                if busy and self._busy_deferrals < 5:
                    self._busy_deferrals += 1
                    self.state.sync_last_status = "deferred_busy"
                    return
                # Tras cinco aplazamientos intentar un documento; el cupo de
                # inferencia existente sigue siendo obligatorio, sin otra cola.
                stats = reconcile_with_lock(db, trigger="scheduler_sync", new_only=True,
                                            max_documents=1 if busy else 8)
                self._busy_deferrals = 0
            self.state.sync_run_count += 1
            self.state.sync_stats = stats.as_dict() if stats is not None else None
            self.state.sync_last_status = (
                "skipped_locked" if stats is None else "partial_failure" if stats.failures
                else "pending" if stats.pending_documents else "completed"
            )
        except Exception as exc:  # noqa: BLE001 - reintentar en la siguiente pasada
            self.state.sync_last_status = f"error:{type(exc).__name__}"
            logger.error("scheduler.sync_failed", extra={"error_type": type(exc).__name__})
        finally:
            self.state.sync_last_run_at = utcnow_naive().isoformat()
            self._run_lock.release()

    def run_once(self) -> None:
        """Una pasada de reconciliacion. Nunca propaga excepciones al hilo."""
        from app.database.engine import session_scope
        from app.ingestion.reconciler import reconcile_with_lock
        from app.jobs.retention import run_maintenance

        # Expiracion de sesiones y bajas reintentables no dependen de que Ollama
        # o el corpus corporativo esten disponibles para la reconciliacion.
        try:
            with session_scope() as db:
                maintenance = run_maintenance(db)
            self.state.maintenance_stats = maintenance.to_dict()
            self.state.maintenance_status = "pending_cleanup" if maintenance.pending_cleanup else "completed"
        except Exception as exc:  # noqa: BLE001 - una tarea no mata la otra
            self.state.maintenance_status = f"error:{type(exc).__name__}"
            logger.error("scheduler.maintenance_failed", extra={"error_type": type(exc).__name__})

        try:
            with session_scope() as db:
                stats = reconcile_with_lock(db, trigger="scheduler")
            self.state.last_status = (
                "skipped_locked" if stats is None else "partial_failure" if stats.failures else "completed"
            )
            self.state.run_count += 1
        except Exception as exc:  # noqa: BLE001 - un fallo no debe matar el hilo
            self.state.last_status = f"error:{type(exc).__name__}"
            logger.error("scheduler.run_failed", extra={"error_type": type(exc).__name__})
        finally:
            self.state.last_run_at = utcnow_naive().isoformat()


_scheduler: ReconcileScheduler | None = None


def get_scheduler() -> ReconcileScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ReconcileScheduler()
    return _scheduler
