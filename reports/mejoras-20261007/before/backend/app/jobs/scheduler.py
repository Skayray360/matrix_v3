# Creado por Aldo Garcia.
"""Scheduler de reconciliacion cada 24 horas.

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
        while not self._stop.wait(self.interval.total_seconds()):
            self.run_once()

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
            self.state.last_status = "skipped_locked" if stats is None else "completed"
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
