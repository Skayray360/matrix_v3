# Creado por Aldo Garcia.
"""Punto de entrada de la aplicacion Matrix RH.

Ensambla el API Gateway: middleware de contexto y seguridad, routers versionados
(``/api/v1``), manejadores de error tipados y, si existe, el build del frontend
servido same-origin (el patron BFF exige que UI y API compartan origen para que
la cookie de sesion sea ``SameSite`` efectiva).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.middleware import (
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    matrix_error_handler,
    unhandled_error_handler,
)
from app.api.routes import admin, auth, chat, conversations, health
from app.common.errors import ErrorCode, MatrixError
from app.common.logging import configure_logging, get_logger
from app.config import AppEnv, get_settings

logger = get_logger(__name__)

API_PREFIX = "/api/v1"
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranque y apagado ordenados."""
    settings = get_settings()
    configure_logging(settings.app_log_level)
    logger.info(
        "app.starting",
        extra={
            "app_env": str(settings.app_env),
            "auth_provider": str(settings.auth_provider),
            "fast_model": settings.ollama_fast_model,
            "deep_model": settings.ollama_deep_model,
            "embedding_model": settings.ollama_embedding_model,
        },
    )

    # Directorios de estado runtime. Se crean aqui y no en el instalador para que
    # la aplicacion arranque tambien en un despliegue limpio sin instalador.
    settings.upload_storage_path.mkdir(parents=True, exist_ok=True)
    settings.knowledge_root_path.mkdir(parents=True, exist_ok=True)

    if settings.scheduler_enabled:
        from app.jobs.scheduler import get_scheduler

        get_scheduler().start(run_immediately=False)

    from app.agents.chat_queue import get_chat_queue, stop_chat_queue

    get_chat_queue().start()
    try:
        yield
    finally:
        stop_chat_queue()
        if settings.scheduler_enabled:
            from app.jobs.scheduler import get_scheduler

            get_scheduler().stop()
        from app.database.engine import dispose_engine
        from app.rag.vector_store import reset_vector_store

        reset_vector_store()
        dispose_engine()
        logger.info("app.stopped")


def create_app() -> FastAPI:
    """Construye la aplicacion FastAPI."""
    settings = get_settings()
    configure_logging(settings.app_log_level)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        # La documentacion interactiva se expone solo fuera de produccion: en
        # produccion es superficie de reconocimiento innecesaria.
        docs_url="/api/docs" if settings.app_env is not AppEnv.PRODUCTION else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.app_env is not AppEnv.PRODUCTION else None,
    )

    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.session_cookie_secure)
    app.add_middleware(RequestContextMiddleware)

    app.add_exception_handler(MatrixError, matrix_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Errores de validacion sin eco del payload.

        El detalle de pydantic incluye los valores recibidos; devolverlos podria
        reflejar credenciales enviadas por error en un campo equivocado.
        """
        request_id = getattr(request.state, "request_id", None)
        logger.warning(
            "http.validation_error",
            extra={"request_id": request_id, "error_count": len(exc.errors())},
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": str(ErrorCode.VALIDATION_ERROR),
                "message": "La solicitud no es valida.",
                "request_id": request_id,
            },
        )

    from app.api.body_limit import UploadBodyLimitMiddleware

    app.add_middleware(UploadBodyLimitMiddleware)

    # --- routers -----------------------------------------------------------
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(chat.router, prefix=API_PREFIX)
    app.include_router(conversations.router, prefix=API_PREFIX)
    app.include_router(admin.router, prefix=API_PREFIX)

    # ``/health`` y ``/ready`` sin prefijo: los orquestadores y el script de
    # arranque los consultan en la raiz.
    app.include_router(health.router)

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Sirve el build del frontend si existe (despliegue same-origin)."""
    if not FRONTEND_DIST.is_dir():
        logger.warning("app.frontend_dist_missing", extra={"dist_path": str(FRONTEND_DIST)})
        return

    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    index_file = FRONTEND_DIST / "index.html"

    # response_model=None: la ruta devuelve dos tipos de Response y FastAPI no
    # debe intentar derivar un modelo de respuesta de la anotacion.
    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def spa_fallback(full_path: str) -> FileResponse | JSONResponse:
        """Fallback de la SPA.

        Cualquier ruta que no sea del API devuelve ``index.html`` para que el
        router del frontend resuelva la navegacion. Las rutas ``/api`` que llegan
        aqui son 404 reales y se responden como tales.
        """
        if full_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content={"code": str(ErrorCode.NOT_FOUND), "message": "Recurso no encontrado."},
            )
        return FileResponse(index_file)


app = create_app()
