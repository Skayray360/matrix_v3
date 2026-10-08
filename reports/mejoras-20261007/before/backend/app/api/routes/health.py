# Creado por Aldo Garcia.
"""``/health`` y ``/ready`` con semantica distinta (seccion 19).

* ``health`` = el proceso esta vivo. Nunca toca dependencias: si dependiera de
  MySQL, un reinicio de la base tumbaria el contenedor entero.
* ``ready``  = todas las dependencias obligatorias estan operativas. Si Ollama,
  MySQL o Qdrant fallan, el sistema **no** se declara listo.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app import __version__
from app.api.schemas import HealthResponse, ReadyComponent, ReadyResponse
from app.common.logging import get_logger
from app.config import get_settings

logger = get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(status="ok", app=settings.app_name, version=__version__)


@router.get("/ready", response_model=ReadyResponse)
def ready(response: Response) -> ReadyResponse:
    """Comprueba las dependencias obligatorias."""
    components: list[ReadyComponent] = []

    # --- base de datos interna ---------------------------------------------
    from app.database.engine import check_database

    db_ok, db_detail = check_database()
    components.append(ReadyComponent(name="database", ok=db_ok, detail=db_detail))

    # --- migraciones aplicadas ---------------------------------------------
    if db_ok:
        try:
            from app.database.migrator import pending_migrations

            pending = pending_migrations()
            components.append(
                ReadyComponent(
                    name="migrations",
                    ok=not pending,
                    detail="pendientes: " + ", ".join(pending) if pending else "al dia",
                )
            )
        except Exception as exc:  # noqa: BLE001
            components.append(ReadyComponent(name="migrations", ok=False, detail=type(exc).__name__))

        # Solo el proceso propietario puede aceptar trabajos. Es una lectura:
        # el healthcheck no adquiere ni renueva leases del dispatcher.
        try:
            from app.agents.chat_queue import LEASE_NAME, get_chat_queue
            from app.agents.process_owner import owner_state
            from app.common.ids import utcnow_naive
            from app.database.engine import get_sessionmaker
            from app.database.models import JobLock

            with get_sessionmaker()() as db:
                lease = db.get(JobLock, LEASE_NAME)
                owner_ok = bool(
                    lease is not None
                    and lease.locked_by == get_chat_queue().owner
                    and lease.expires_at > utcnow_naive()
                )
                detail = "propietario activo" if owner_ok else "dispatcher sin propiedad vigente"
                if lease is not None and lease.locked_by != get_chat_queue().owner:
                    state = owner_state(lease.locked_by)
                    remaining = max(0, int((lease.expires_at - utcnow_naive()).total_seconds()) + 1)
                    if state == "alive":
                        detail = "bloqueo de inicio: otro proceso local sigue activo"
                    elif state == "unknown" and remaining:
                        detail = (
                            "bloqueo de inicio: reserva antigua o ajena sin identidad verificable; "
                            f"vence en {remaining} s"
                        )
                    elif state == "dead":
                        detail = "recuperando reserva de proceso local terminado"
            components.append(ReadyComponent(
                name="chat_dispatcher", ok=owner_ok,
                detail=detail,
            ))
        except Exception as exc:  # noqa: BLE001
            components.append(ReadyComponent(name="chat_dispatcher", ok=False, detail=type(exc).__name__))

    # --- Ollama y modelos ---------------------------------------------------
    settings = get_settings()
    try:
        from app.llm.ollama_client import get_ollama_client

        client = get_ollama_client()
        if not client.ping():
            components.append(ReadyComponent(name="ollama", ok=False, detail="no alcanzable"))
        else:
            inventory = client.list_models()
            required = (
                settings.ollama_fast_model,
                settings.ollama_deep_model,
                settings.ollama_embedding_model,
            )
            missing = [m for m in required if not inventory.has(m)]
            components.append(
                ReadyComponent(
                    name="ollama",
                    ok=not missing,
                    detail="faltan: " + ", ".join(missing) if missing else "modelos presentes",
                )
            )
    except Exception as exc:  # noqa: BLE001
        components.append(ReadyComponent(name="ollama", ok=False, detail=type(exc).__name__))

    # --- almacen vectorial --------------------------------------------------
    try:
        from app.rag.vector_store import get_vector_store

        qdrant_ok, qdrant_detail = get_vector_store().health()
        components.append(ReadyComponent(name="qdrant", ok=qdrant_ok, detail=qdrant_detail))
    except Exception as exc:  # noqa: BLE001
        components.append(ReadyComponent(name="qdrant", ok=False, detail=type(exc).__name__))

    # --- proveedor de identidad --------------------------------------------
    try:
        from app.auth.provider import get_identity_provider

        provider = get_identity_provider()
        status = str(provider.status())
        # El IdP activo debe responder; el IdP inactivo no se instancia.
        provider_ok = status in ("CONNECTED_AND_VALIDATED", "ENDPOINTS_REACHABLE")
        components.append(ReadyComponent(name=f"identity:{provider.name}", ok=provider_ok, detail=status))
    except Exception as exc:  # noqa: BLE001
        components.append(ReadyComponent(name="identity", ok=False, detail=type(exc).__name__))

    all_ok = all(c.ok for c in components)
    if not all_ok:
        response.status_code = 503
    return ReadyResponse(ready=all_ok, components=components)
