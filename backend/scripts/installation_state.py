# Creado por Aldo Garcia.
"""Compara referencias SQL y estado local sin abrir Qdrant ni documentos.

Puede ejecutarse desde una entrega nueva con el Python de una instalacion
anterior: ``python -B installation_state.py --root RUTA --previous-root RUTA``.
La configuracion y el inventario se importan de --root, nunca del ZIP que
contiene este script. No imprime rutas, identificadores, secretos ni textos.
La presencia de metadatos no demuestra integridad o contenido de vectores.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path, PureWindowsPath

sys.dont_write_bytecode = True

ISSUES = {
    "invalid_project_root": "La raiz no contiene la configuracion de Matrix RH.",
    "configuration_missing": "Falta .env en la instalacion indicada; no se puede comparar su estado.",
    "configuration_unavailable": "No se pudo cargar la configuracion de la instalacion indicada.",
    "wrong_configuration_origin": "Python tiene cargado otro proyecto; ejecute este script en un proceso nuevo.",
    "database_unavailable": "No se pudo leer MySQL; revise servicio y DATABASE_URL sin compartir credenciales.",
    "database_not_matrix": "El registro de migraciones no corresponde a esta entrega de Matrix RH.",
    "database_schema_unverified": "No se pudo verificar el esquema documental de Matrix RH.",
    "document_scope_unverified": "SQL contiene un scope documental no reconocido; requiere revision.",
    "qdrant_metadata_unavailable": "No se pudo leer meta.json del QDRANT_PATH efectivo.",
    "qdrant_collection_missing": "SQL conserva indices publicados, pero falta su coleccion configurada.",
    "qdrant_server_unavailable": "No se pudieron comprobar las colecciones del servidor Qdrant por HTTP.",
    "qdrant_storage_missing": "La coleccion figura en meta.json, pero falta su archivo de almacenamiento local.",
    "attachment_files_missing": "SQL conserva adjuntos cuyo archivo no esta en UPLOAD_STORAGE_ROOT.",
    "attachment_paths_invalid": "Hay referencias de adjuntos que no forman una ruta privada valida.",
    "corporate_files_missing": "Faltan archivos corporativos referidos por SQL en el inventario configurado.",
    "corporate_inventory_unavailable": "No se pudo completar el inventario corporativo de solo lectura.",
    "local_state_unavailable": "No se pudo comprobar el estado local por un error de acceso.",
}
RECOVERY = (
    "Detenga los servicios con detener.bat y conserve la carpeta completa. Si faltan indices o adjuntos, "
    "restaure un respaldo coherente de la misma instalacion antes de continuar. El respaldo frio de "
    "backend/scripts/installation_backup.py conserva configuracion, SQL y estado local juntos; "
    "no reconstruye archivos que ya faltan. Para una instalacion nueva, extraiga la entrega completa "
    "en una carpeta vacia y ejecute instalar.bat sin adoptar bases ni estado de otras carpetas. "
    "Si las bajas corporativas fueron intencionales, reviselas mediante el procedimiento de reconciliacion. "
    "No fuerce la ingesta ni elimine colecciones o indicadores SQL para omitir esta comprobacion."
)


class StateCheckError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def select_only_guard(_conn, _cursor, statement, _parameters, _context, _executemany):
    if not statement.lstrip().upper().startswith("SELECT "):
        raise StateCheckError("database_schema_unverified")


def load_target_settings(root: Path, *, env_file: Path | None = None):
    expected = root / "backend" / "app"
    if not (expected / "config" / "settings.py").is_file():
        raise StateCheckError("invalid_project_root")
    target_env = env_file if env_file is not None else root / "backend/config/.env"
    if not target_env.is_absolute():
        target_env = root / target_env
    if (target_env.is_symlink() or target_env.absolute() != target_env.resolve()
            or not target_env.resolve().is_relative_to((root / "backend/config").resolve())
            or not target_env.is_file()):
        raise StateCheckError("configuration_missing")
    # No borrar/reimportar modulos de otro proyecto para ocultar un origen mixto.
    for name, module in tuple(sys.modules.items()):
        if name == "app" or name.startswith("app."):
            origin = getattr(module, "__file__", None)
            if not origin or not Path(origin).resolve().is_relative_to(expected):
                raise StateCheckError("wrong_configuration_origin")
    sys.path.insert(0, str(root / "backend"))
    config = importlib.import_module("app.config")
    module = importlib.import_module("app.config.settings")
    if not Path(module.__file__).resolve().is_relative_to(expected):
        raise StateCheckError("wrong_configuration_origin")
    if env_file is None:
        return config.get_settings()
    from dotenv import dotenv_values

    # El archivo explicito gana a .env y al entorno heredado para esta lectura.
    values = {key.lower(): value for key, value in dotenv_values(target_env).items() if value is not None}
    return config.Settings(_env_file=None, **values)


def _migration_checksums(root: Path) -> dict[str, str]:
    return {
        path.name[:4]: hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        for path in (root / "backend" / "migrations").glob("*.sql")
        if re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", path.name)
    }


def read_document_metadata(conn, *, table: str, columns: set[str]) -> list[dict]:
    """Selecciona solo referencias y contadores; no cuerpos, hashes ni errores."""
    from sqlalchemy import text

    required = {
        "scope", "relative_path", "storage_path", "owner_user_id", "conversation_id", "deleted_at", "chunk_count",
    }
    if not required <= columns or not re.fullmatch(r"`[A-Za-z0-9_]{1,64}`\.`documents`|documents", table):
        raise StateCheckError("database_schema_unverified")
    generation = (
        "(active_generation IS NOT NULL AND active_generation <> '')" if "active_generation" in columns else "0"
    )
    cleanup = "index_cleanup_pending" if "index_cleanup_pending" in columns else "0"
    query = (
        "SELECT scope, relative_path, storage_path, owner_user_id, conversation_id, "  # noqa: S608 - closed/validated names
        "deleted_at IS NOT NULL AS deleted, chunk_count, "
        f"{generation} AS has_active_generation, {cleanup} AS cleanup_pending FROM {table}"
    )
    return [dict(row) for row in conn.execute(text(query)).mappings()]


def database_snapshot(settings, root: Path) -> tuple[str, list[dict]]:
    """SELECT a INFORMATION_SCHEMA y al esquema validado; nunca ORM/migrador."""
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url.get_secret_value())
    database = url.database or ""
    if url.get_backend_name() not in {"mysql", "mariadb"} or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database):
        raise StateCheckError("configuration_unavailable")
    engine = create_engine(
        url.set(database=""), echo=False, hide_parameters=True, connect_args={"connect_timeout": 5},
    )
    try:
        with engine.connect() as conn:
            event.listen(conn, "before_cursor_execute", select_only_guard)
            if not conn.execute(text(
                "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = :name"
            ), {"name": database}).first():
                return "absent", []
            tables = set(conn.execute(text(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = :name"
            ), {"name": database}).scalars())
            if not tables:
                return "empty", []
            if "schema_migrations" not in tables:
                raise StateCheckError("database_not_matrix")
            registered = dict(conn.execute(text(
                f"SELECT version, checksum FROM `{database}`.`schema_migrations`"  # noqa: S608 - database validated above
            )).all())
            known = _migration_checksums(root)
            if "0001" not in registered or any(
                known.get(version) != checksum for version, checksum in registered.items()
            ):
                raise StateCheckError("database_not_matrix")
            if "documents" not in tables:
                raise StateCheckError("database_schema_unverified")
            columns = set(conn.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = :name AND TABLE_NAME = 'documents'"
            ), {"name": database}).scalars())
            return "matrix", read_document_metadata(conn, table=f"`{database}`.`documents`", columns=columns)
    finally:
        engine.dispose()


def _safe_part(part) -> bool:
    return (
        isinstance(part, str) and bool(part) and part not in {".", ".."}
        and not any(character in part for character in '/\\:\x00')
        and not any(ord(character) < 32 for character in part)
        and PureWindowsPath(part).name == part
    )


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _safe_file(path: Path, base: Path) -> bool:
    if not path.is_relative_to(base):
        return False
    cursor = path
    while cursor != base:
        if _linked(cursor):
            return False
        cursor = cursor.parent
    return not _linked(base) and path.is_file()


def qdrant_metadata(path: Path, collections: dict[str, str]) -> dict:
    """Lee un JSON acotado y existencia de storage.sqlite, sin abrir el indice."""
    result = {"status": "missing", "collection_count": 0, "collections": {}}
    meta = path / "meta.json"
    try:
        if not meta.exists():
            return result
        if not _safe_file(meta, path) or meta.stat().st_size > 1024 * 1024:
            result["status"] = "unreadable"
            return result
        with meta.open("rb") as handle:
            payload = json.loads(handle.read(1024 * 1024 + 1))
        items = payload.get("collections") if isinstance(payload, dict) else None
        aliases = payload.get("aliases", {}) if isinstance(payload, dict) else None
        if not isinstance(items, dict) or not isinstance(aliases, dict):
            result["status"] = "unreadable"
            return result
        result.update(status="readable", collection_count=len(items))
        for scope, configured in collections.items():
            name = configured if configured in items else aliases.get(configured)
            present = isinstance(name, str) and _safe_part(name) and isinstance(items.get(name), dict)
            storage = bool(present and any(
                _safe_file(path / "collection" / name / filename, path)
                for filename in ("storage.sqlite", "storage.dbm")
            ))
            result["collections"][scope] = {"metadata_present": bool(present), "storage_file_present": storage}
    except (OSError, ValueError, TypeError):
        result["status"] = "unreadable"
    return result


def attachment_inventory(documents: list[dict], root: Path) -> dict:
    counts = Counter(referenced=0, present=0, missing=0, invalid=0)
    for row in documents:
        if row["scope"] != "conversation" or row["deleted"]:
            continue
        counts["referenced"] += 1
        stored = row.get("storage_path")
        filename = PureWindowsPath(stored).name if isinstance(stored, str) else ""
        parts = (row.get("owner_user_id"), row.get("conversation_id"), filename)
        if not all(_safe_part(part) for part in parts):
            counts["invalid"] += 1
            continue
        counts["present" if _safe_file(root.joinpath(*parts), root) else "missing"] += 1
    return dict(counts)


def corporate_inventory(documents: list[dict], settings, root: Path) -> dict:
    rows = [row for row in documents if row["scope"] == "corporate" and not row["deleted"]]
    counts = {"referenced": len(rows), "present": 0, "missing": 0, "scan_complete": True}
    if not rows:
        return counts
    from app.ingestion.knowledge_layout import scan_knowledge

    scan = scan_knowledge(knowledge_root=settings.knowledge_root_path, project_root=root)
    def key(value):
        normalized = str(value or "").replace("\\", "/")
        return normalized.casefold() if os.name == "nt" else normalized
    canonical = {key(item.relative_path) for item in scan.files}
    physical = {key(item.absolute_path) for item in scan.files}
    legacy = {
        key(value): "/data/" + value.casefold()
        for item in scan.files for value in item.legacy_relative_paths
    }

    def present(row):
        relative = key(row.get("relative_path"))
        storage = row.get("storage_path")
        if relative in canonical or (storage and key(storage) in physical):
            return True
        # Igual que ingest_corporate_file: un alias legacy sin su ubicacion
        # original no demuestra identidad. data/knowledge/x y data/x pueden
        # contener documentos diferentes aunque compartan relative_path.
        return bool(
            relative in legacy and storage
            and str(storage).replace("\\", "/").casefold().endswith(legacy[relative])
        )

    counts["present"] = sum(present(row) for row in rows)
    counts["missing"] = len(rows) - counts["present"]
    # Optional absent alias roots alone are harmless; any unaccounted SQL
    # reference still blocks, including a source now absent/unmapped.
    counts["scan_complete"] = not bool(scan.incomplete_sources)
    return counts


def previous_inventory(root: Path | None) -> dict:
    if root is None:
        return {"provided": False}
    return {
        "provided": True, "standard_locations_only": True,
        "directory_present": root.is_dir(), "configuration_present": (root / "backend" / "config" / ".env").is_file(),
        "data_present": (root / "knowledge-base" / "documents").is_dir(),
        "uploads_present": (root / "knowledge-base" / "state" / "uploads").is_dir(),
        "qdrant_metadata": qdrant_metadata(root / "knowledge-base" / "state" / "qdrant", {}),
    }


def evaluate_state(settings, root: Path, database_state: str, documents: list[dict]) -> dict:
    report = {
        "read_only": True, "qdrant_opened": False, "vectors_verified": False,
        "document_contents_read": False, "credentials_changed": False,
        "database_state": database_state, "sql_documents": [], "issues": [],
    }
    codes = []
    required_scopes = set()
    for scope in ("corporate", "conversation"):
        rows = [row for row in documents if row["scope"] == scope]
        report["sql_documents"].append({
            "scope": scope, "documents": len(rows), "not_deleted": sum(not row["deleted"] for row in rows),
            "chunks": sum(max(0, int(row["chunk_count"] or 0)) for row in rows),
            "has_active_generation": sum(bool(row["has_active_generation"]) for row in rows),
            "cleanup_pending": sum(bool(row["cleanup_pending"]) for row in rows),
            "cleanup_pending_active_generation": sum(
                bool(row["cleanup_pending"] and row["has_active_generation"]) for row in rows
            ),
        })
        if any(row["has_active_generation"] or int(row["chunk_count"] or 0) > 0 for row in rows):
            required_scopes.add(scope)
    if any(row["scope"] not in {"corporate", "conversation"} for row in documents):
        codes.append("document_scope_unverified")
    if str(settings.qdrant_mode) == "embedded":
        metadata = qdrant_metadata(settings.qdrant_storage_path, {
            "corporate": settings.rag_collection_corporate, "conversation": settings.rag_collection_private,
        })
        report["qdrant"] = {"mode": "embedded", "required_scopes": sorted(required_scopes), **metadata}
        if required_scopes and metadata["status"] != "readable":
            codes.append("qdrant_metadata_unavailable")
        for scope in sorted(required_scopes):
            collection = metadata["collections"].get(scope, {})
            if metadata["status"] == "readable" and not collection.get("metadata_present"):
                codes.append("qdrant_collection_missing")
            elif collection.get("metadata_present") and not collection.get("storage_file_present"):
                codes.append("qdrant_storage_missing")
    else:
        report["qdrant"] = {
            "mode": "server", "status": "server_not_verified", "required_scopes": sorted(required_scopes),
        }
    attachments = attachment_inventory(documents, settings.upload_storage_path)
    report["attachments"] = attachments
    if attachments["missing"]:
        codes.append("attachment_files_missing")
    if attachments["invalid"]:
        codes.append("attachment_paths_invalid")
    try:
        inventory = corporate_inventory(documents, settings, root)
        report["corporate_files"] = inventory
        if inventory["missing"]:
            codes.append("corporate_files_missing")
        if not inventory["scan_complete"]:
            codes.append("corporate_inventory_unavailable")
    except Exception:
        codes.append("corporate_inventory_unavailable")
    report["issues"] = [{"code": code, "detail": ISSUES[code]} for code in dict.fromkeys(codes)]
    return report


def verify_server_collections(report: dict, settings) -> None:
    """Solo metadatos HTTP; no descarga vectores ni declara que coinciden con SQL."""
    import httpx

    qdrant = report["qdrant"]
    if qdrant["mode"] != "server":
        return
    required = {
        "corporate": settings.rag_collection_corporate, "conversation": settings.rag_collection_private,
    }
    try:
        if qdrant["required_scopes"]:
            with httpx.Client(trust_env=False, timeout=5) as client:
                response = client.get(settings.qdrant_url.rstrip("/") + "/collections",
                                      headers={"api-key": settings.qdrant_api_key.get_secret_value()})
                response.raise_for_status()
                collections = response.json()["result"]["collections"]
            if not isinstance(collections, list) or any(not isinstance(row.get("name"), str) for row in collections):
                raise ValueError("InventarioInvalido")
            names = {row["name"] for row in collections}
        else:
            names = set()
        missing = [scope for scope in qdrant["required_scopes"] if required[scope] not in names]
        qdrant.update({"status": "collections_checked", "collections_verified": True,
                       "missing_scopes": missing})
        if missing:
            report["issues"].append({
                "code": "qdrant_collection_missing", "detail": ISSUES["qdrant_collection_missing"],
            })
    except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
        qdrant.update({"status": "server_unavailable", "collections_verified": False})
        report["issues"].append({"code": "qdrant_server_unavailable", "detail": ISSUES["qdrant_server_unavailable"]})


def collect(root: Path, *, previous_root: Path | None = None, env_file: Path | None = None,
            check_server_collections: bool = False) -> dict:
    report = {
        "read_only": True, "qdrant_opened": False, "vectors_verified": False,
        "document_contents_read": False, "credentials_changed": False, "issues": [],
    }
    phase = "configuration_unavailable"
    try:
        root = root.resolve()
        settings = load_target_settings(root, env_file=env_file) if env_file is not None else load_target_settings(root)
        phase = "database_unavailable"
        database_state, documents = database_snapshot(settings, root)
        phase = "local_state_unavailable"
        report = evaluate_state(settings, root, database_state, documents)
        if check_server_collections:
            verify_server_collections(report, settings)
    except Exception as exc:
        code = exc.code if isinstance(exc, StateCheckError) and exc.code in ISSUES else phase
        report["issues"].append({"code": code, "detail": ISSUES[code]})
    try:
        report["previous_installation"] = previous_inventory(previous_root)
    except OSError:
        report["previous_installation"] = {"provided": previous_root is not None, "status": "unreadable"}
    report["guard_allowed"] = not report["issues"]
    report["status"] = "metadata_only" if report["guard_allowed"] else "blocked"
    report["next_action"] = RECOVERY if report["issues"] else (
        "No se detectaron referencias locales ausentes. Es una comprobacion de metadatos; "
        "no verifica vectores, claves de cifrado ni consistencia de un respaldo."
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--previous-root", type=Path)
    parser.add_argument("--env-file", type=Path, help="archivo explicito dentro de backend/config (Docker)")
    parser.add_argument("--verify-server-collections", action="store_true",
                        help="comprueba por HTTP colecciones requeridas; no verifica sus vectores")
    parser.add_argument(
        "--guard-install", action="store_true", help="Devuelve 1 si no es seguro continuar la instalacion.",
    )
    args = parser.parse_args(argv)
    # Los drivers y Settings no deben volcar DSN, errores libres o rutas privadas.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        report = collect(args.root, previous_root=args.previous_root, env_file=args.env_file,
                         check_server_collections=args.verify_server_collections)
        print(json.dumps(report, ensure_ascii=True, indent=2))
    finally:
        logging.disable(previous_logging)
    return 1 if args.guard_install and not report["guard_allowed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
