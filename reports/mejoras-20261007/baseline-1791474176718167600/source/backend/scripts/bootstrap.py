# Creado por Aldo Garcia.
"""Bootstrap operativo compartido.

Los archivos ``.bat`` y ``.ps1`` de la raiz **no contienen logica de negocio**:
llaman a este modulo. De ese modo la instalacion, el arranque, el diagnostico y
la validacion se comportan igual desde Windows, desde shell y desde las pruebas.

Subcomandos:
    migrate    aplica migraciones idempotentes
    seed       crea las cuentas sinteticas (solo development/test)
    ingest     reconcilia el knowledge root
    setup      migrate + seed + ingest
    serve      arranca uvicorn (con preflight previo)
    status     resumen corto del estado del sistema
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.preflight import (
    PROJECT_ROOT,
    PreflightReport,
    check_application,
    check_integrity,
    check_settings,
    check_storage_encryption,
    render_text,
)


def cmd_pin_models(_: argparse.Namespace) -> int:
    """Fija una sola vez los digests vacios de Ollama; nunca acepta drift.

    Se ejecuta antes de Settings porque staging/production exige los digests.
    No crea usuarios, no descarga modelos y no toca la base de datos.
    """
    import httpx
    from dotenv import dotenv_values

    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        print("ERROR [CONFIGURATION_ERROR] Falta .env para fijar digests de los modelos locales.")
        return 1
    values = dict(dotenv_values(env_file))
    values.update(os.environ)
    base_url = values.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434"
    url = urlsplit(base_url)
    local_hosts = {
        host.strip().lower() for host in (values.get("LLM_LOCAL_HOSTS") or "127.0.0.1,localhost,::1").split(",")
    }
    if (url.scheme not in {"http", "https"} or not url.hostname or url.hostname.lower() not in local_hosts
            or url.username or url.password or url.query or url.fragment):
        print("ERROR [CONFIGURATION_ERROR] OLLAMA_BASE_URL debe usar un host local declarado en LLM_LOCAL_HOSTS.")
        return 1
    roles = [
        ("LLM_PROVIDER", "OLLAMA_FAST_MODEL", "LLM_FAST_DIGEST", "gemma4:latest"),
        ("LLM_DEEP_PROVIDER", "OLLAMA_DEEP_MODEL", "LLM_DEEP_DIGEST", "gemma4:latest"),
        ("LLM_EMBEDDING_PROVIDER", "OLLAMA_EMBEDDING_MODEL", "LLM_EMBEDDING_DIGEST", "embeddinggemma:latest"),
    ]
    enabled = [row for row in roles if (values.get(row[0]) or "ollama") == "ollama"]
    if not enabled:
        print("Digests: adapters distintos de Ollama; conserve las revisiones verificadas en .env.")
        return 0
    try:
        with httpx.Client(trust_env=False, timeout=15) as client:
            response = client.get(base_url.rstrip("/") + "/api/tags")
            response.raise_for_status()
            models = response.json()["models"]
        inventory: dict[str, str] = {}
        for model in models:
            name, digest = model["name"], model["digest"]
            if not isinstance(name, str) or not isinstance(digest, str):
                raise ValueError("InventarioInvalido")
            if name in inventory:
                raise ValueError("InventarioDuplicado")
            inventory[name] = digest.removeprefix("sha256:").lower()
        additions: dict[str, str] = {}
        for _provider, name_key, digest_key, default_model in enabled:
            model_name = values.get(name_key) or default_model
            observed = inventory.get(model_name)
            if observed is None or not re.fullmatch(r"[0-9a-f]{64}", observed):
                print(f"ERROR [CONFIGURATION_ERROR] {name_key}: modelo exacto o digest de Ollama no disponible; "
                      "instale el modelo configurado.")
                return 1
            expected = (values.get(digest_key) or "").removeprefix("sha256:").lower()
            if expected:
                if not re.fullmatch(r"[0-9a-f]{64}", expected) or expected != observed:
                    print(f"ERROR [CONFIGURATION_ERROR] {digest_key} no coincide con el modelo configurado. "
                          "No se reemplaza el digest; valide el cambio de modelo con TI.")
                    return 1
            elif digest_key in os.environ:
                print(f"ERROR [CONFIGURATION_ERROR] {digest_key} del entorno esta vacio. "
                      "Retire esa variable heredada antes de fijar el digest en .env.")
                return 1
            else:
                additions[digest_key] = observed
        if not additions:
            print("Digests de los modelos ya fijados y coincidentes; .env se conserva.")
            return 0
        original = env_file.read_text(encoding="utf-8-sig")
        lines = original.splitlines()
        written: set[str] = set()
        content = []
        for line in lines:
            match = re.match(r"^\s*(?:export\s+)?([A-Z_]+)\s*=", line)
            key = match.group(1) if match else ""
            if key in additions:
                content.append(f"{key}={additions[key]}")
                written.add(key)
            else:
                content.append(line)
        content.extend(f"{key}={digest}" for key, digest in additions.items() if key not in written)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".env-models-", dir=env_file.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(content) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, env_file)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR [CONFIGURATION_ERROR] No se pudieron verificar/fijar los digests ({type(exc).__name__}); "
              ".env no se reemplaza por un inventario invalido.")
        return 1
    print("Digests fijados una sola vez en .env: " + ", ".join(additions))
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    from app.database.migrator import run_migrations

    applied = run_migrations()
    print(f"Migraciones aplicadas ahora: {applied or '(ninguna, ya estaba al dia)'}")
    return 0


def cmd_seed(_: argparse.Namespace) -> int:
    from app.config import get_settings
    from seeds.identity_seed import run

    settings = get_settings()
    if str(settings.app_env) not in {"development", "test"} or not settings.local_test_seed_users_enabled:
        print("Seed omitido: el modo local de usuarios sinteticos no esta habilitado.")
        return 0
    created = run()
    print(f"Usuarios sinteticos creados: {created or '(ya existian)'}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from app.database.engine import session_scope
    from app.ingestion.reconciler import reconcile_with_lock

    with session_scope() as db:
        stats = reconcile_with_lock(db, trigger="cli", force=args.force)
    if stats is None:
        print("Ya hay una reconciliacion en curso. No se hizo nada.")
        return 0
    print(json.dumps(stats.as_dict(), indent=2, ensure_ascii=False))
    return 1 if stats.failures else 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Instalacion idempotente completa."""
    for step in (cmd_migrate, cmd_seed):
        code = step(args)
        if code != 0:
            return code
    if args.skip_ingest:
        print("Ingesta inicial omitida por --skip-ingest.")
        return 0
    return cmd_ingest(args)


def cmd_status(args: argparse.Namespace) -> int:
    from app.config import QdrantMode, get_settings
    from app.database.engine import check_database
    from app.llm.provider import ModelClient
    from app.rag.vector_store import get_vector_store

    settings = get_settings()
    db_ok, db_detail = check_database()
    ollama = ModelClient()
    try:
        ollama_ok = ollama.ping()
    finally:
        ollama.close()
    try:
        if getattr(args, "read_only", False) and settings.qdrant_mode is QdrantMode.EMBEDDED:
            qdrant_ok, qdrant_detail = None, "omitido en modo solo lectura; consultar /ready del servicio"
        else:
            qdrant_ok, qdrant_detail = get_vector_store().health()
    except Exception as exc:  # noqa: BLE001
        qdrant_ok, qdrant_detail = False, type(exc).__name__

    payload: dict[str, Any] = {
        "app_env": str(settings.app_env),
        "auth_provider": str(settings.auth_provider),
        "database": {"ok": db_ok, "detail": db_detail},
        "ollama": {"ok": ollama_ok, "base_url": settings.ollama_base_url},
        "qdrant": {"ok": qdrant_ok, "detail": qdrant_detail},
        "models": {
            "fast": settings.ollama_fast_model,
            "deep": settings.ollama_deep_model,
            "embedding": settings.ollama_embedding_model,
        },
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if (db_ok and ollama_ok and qdrant_ok is not False) else 1


def cmd_serve(args: argparse.Namespace) -> int:
    """Arranca el servidor tras un preflight obligatorio."""
    from app.config import Settings, get_settings
    from scripts.preflight import check_ports, run_preflight

    configured = get_settings()
    # model_copy(update=...) no valida. Revalidar el modelo impide que --host
    # exponga un perfil local de prueba que APP_HOST mantenia en loopback.
    actual = configured.model_dump()
    if args.host:
        actual["app_host"] = args.host
    if args.port:
        actual["app_port"] = args.port
    try:
        settings = Settings.model_validate(actual)
    except ValueError:
        print("ERROR [CONFIGURATION_ERROR] --host/--port no cumplen la guardia de exposicion de red. "
              "Use loopback para pruebas o configure identidad y usuario MySQL dedicados para red.")
        return 1

    mandatory = PreflightReport()
    check_integrity(mandatory)
    if mandatory.ok:
        check_application(mandatory)
    if mandatory.ok:
        check_storage_encryption(mandatory, settings)
    if mandatory.ok:
        check_ports(mandatory, settings, require_free=True)
    if not mandatory.ok:
        print(render_text(mandatory))
        return 1

    if not args.skip_preflight:
        report = run_preflight()
        print(render_text(report))
        if not report.ok:
            print("El backend NO se arranca porque el preflight fallo.")
            return 1

    import uvicorn

    server_options: dict[str, Any] = {
        "app": "app.main:app", "host": args.host or settings.app_host,
        "port": args.port or settings.app_port, "reload": args.reload,
        "log_config": None, "access_log": False, "proxy_headers": True,
        "forwarded_allow_ips": settings.forwarded_allow_ips, "timeout_graceful_shutdown": 20,
    }
    if args.reload:
        # Mantiene el modo de desarrollo existente; el arranque .bat usa un
        # solo proceso, sin reload, para ownership y parada cooperativa.
        uvicorn.run(**server_options)
        return 0
    from threading import Event, Thread

    from scripts.runtime_control import runtime_identity, watch_shutdown

    server = uvicorn.Server(uvicorn.Config(**server_options))
    finished = Event()
    with runtime_identity(PROJECT_ROOT) as identity:
        watcher = Thread(
            target=watch_shutdown,
            args=(server, PROJECT_ROOT / "var" / "matrixrh-backend.stop", finished),
            kwargs={"process_id": identity["pid"], "created_at": identity["created_at"]},
            name="matrix-shutdown-control", daemon=True,
        )
        watcher.start()
        try:
            server.run()
        finally:
            finished.set()
            watcher.join(timeout=1)
    return 0


def cmd_knowledge_status(_: argparse.Namespace) -> int:
    from app.ingestion.knowledge_layout import inventory_knowledge

    print(json.dumps(inventory_knowledge().as_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_upgrade_config(_: argparse.Namespace) -> int:
    from scripts.configuration_upgrade import upgrade_configuration

    result = upgrade_configuration(PROJECT_ROOT)
    changed = result["changed_keys"]
    assert isinstance(changed, list)
    print("Configuracion operativa: " + (", ".join(changed) or "ya actualizada"))
    if result["backup_created"]:
        print("Respaldo privado de .env creado en var/backups/configuration; no se muestran valores.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap operativo de Matrix RH")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="aplica migraciones").set_defaults(func=cmd_migrate)
    sub.add_parser("seed", help="crea usuarios sinteticos").set_defaults(func=cmd_seed)
    sub.add_parser("knowledge-status", help="inventario de archivos de conocimiento sin indexar").set_defaults(
        func=cmd_knowledge_status,
    )
    sub.add_parser("upgrade-config", help="actualiza limites operativos con respaldo de .env").set_defaults(
        func=cmd_upgrade_config,
    )
    sub.add_parser("pin-models", help="fija digests vacios de Ollama sin sustituir revisiones existentes").set_defaults(
        func=cmd_pin_models,
    )
    status = sub.add_parser("status", help="estado resumido")
    status.add_argument("--read-only", action="store_true", help="no abre ni crea Qdrant embebido")
    status.set_defaults(func=cmd_status)

    ingest = sub.add_parser("ingest", help="reconcilia el knowledge root")
    ingest.add_argument("--force", action="store_true", help="reindexa aunque el SHA no cambie")
    ingest.set_defaults(func=cmd_ingest)

    setup = sub.add_parser("setup", help="migrate + seed + ingest")
    setup.add_argument("--force", action="store_true")
    setup.add_argument("--skip-ingest", action="store_true")
    setup.set_defaults(func=cmd_setup)

    serve = sub.add_parser("serve", help="arranca el backend")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--skip-preflight", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada.

    Los errores tipados se presentan como un mensaje accionable y un codigo de
    salida, **nunca como un traceback**: el operador ejecuta esto desde un `.bat`,
    donde una traza de Python se convierte en ruido ilegible y el motivo real
    (por ejemplo, "la base pertenece a otra aplicacion") se pierde.
    """
    args = build_parser().parse_args(argv)
    integrity = PreflightReport()
    check_integrity(integrity)
    if not integrity.ok:
        print(render_text(integrity))
        return 1
    try:
        if args.command != "pin-models":
            settings = check_settings(integrity)
            if settings is not None:
                check_application(integrity)
                check_storage_encryption(integrity, settings)
            if not integrity.ok:
                print(render_text(integrity))
                return 1
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - tambien los imports fallidos deben ser accionables
        print("")
        code = getattr(exc, "code", "CONFIGURATION_ERROR")
        message = getattr(exc, "message",
                          "No se pudo cargar o ejecutar el backend; revise .env y la integridad del ZIP.")
        print(f"ERROR [{code}] {message}")
        print(f"  Tipo: {type(exc).__name__}")
        print("")
        return 1
    except KeyboardInterrupt:
        print("\nInterrumpido por el operador.")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
