# Creado por Aldo Garcia.
"""Preflight y diagnostico de Matrix RH (seccion 29).

Modulo unico de diagnostico. Los scripts de Windows (BAT/PowerShell) y los de
shell **invocan este modulo**; no duplican logica. Asi el diagnostico que ve el
operador en Windows es exactamente el que ejecutan las pruebas.

Uso:
    python -m scripts.preflight              # comprobacion completa
    python -m scripts.preflight --json       # salida JSON para automatizacion
    python -m scripts.preflight --read-only  # no crea directorios ni conecta a escritura
    python -m scripts.preflight --dependencies-only  # valida Python/paquetes, sin cargar .env
    python -m scripts.preflight --ports-only --host=127.0.0.1 --port=8000  # solo stdlib

Codigos de salida:
    0 -> todo correcto (puede haber avisos)
    1 -> al menos una comprobacion obligatoria fallo
"""

from __future__ import annotations

import argparse
import ast
import errno
import hashlib
import importlib
import json
import os
import platform
import socket
import subprocess
import sys
import tomllib
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORE_PREFIXES = ("backend/app/", "backend/scripts/", "backend/seeds/", "backend/migrations/", "windows/")
OPERATOR_BATCH_FILES = frozenset({
    "INSTALAR_MATRIX_RH.bat", "INICIAR_MATRIX_RH.bat",
    "DETENER_MATRIX_RH.bat", "DIAGNOSTICO_MATRIX_RH.bat",
})
CORE_FILES = frozenset({
    "backend/pyproject.toml", "backend/uv.lock", "backend/Dockerfile", "docker-compose.yml",
    "frontend/package.json", "frontend/package-lock.json",
}) | OPERATOR_BATCH_FILES


@dataclass
class Check:
    """Resultado de una comprobacion individual."""

    name: str
    status: str
    detail: str = ""
    #: Un check no obligatorio nunca provoca codigo de salida distinto de cero.
    required: bool = True

    @property
    def failed(self) -> bool:
        return self.required and self.status == FAIL


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", *, required: bool = True) -> None:
        self.checks.append(Check(name=name, status=status, detail=detail, required=required))

    @property
    def ok(self) -> bool:
        return not any(c.failed for c in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "failed": [c.name for c in self.checks if c.failed],
            "warnings": [c.name for c in self.checks if c.status == WARN],
            "checks": [asdict(c) for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Comprobaciones
# ---------------------------------------------------------------------------
def _core_file(relative: str) -> bool:
    return relative in CORE_FILES or (
        relative.startswith(CORE_PREFIXES) and Path(relative).suffix.lower() in {".py", ".ps1", ".sql"}
    )


def project_version(root: Path = PROJECT_ROOT) -> str:
    """Lee la version sin importar el paquete que se esta verificando."""
    metadata = tomllib.loads((root / "backend/pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    if not isinstance(version, str) or not version:
        raise ValueError("VersionAusente")
    return version


def check_integrity(report: PreflightReport, *, root: Path = PROJECT_ROOT, require_manifest: bool = False) -> None:
    """Detiene archivos cruzados antes de imports, .env, migraciones o ingesta.

    El manifiesto comprueba la coherencia del paquete, no autentica a su autor.
    Corpus, configuracion de cliente y build recompilado no se comparan.
    """
    try:
        expected_version = project_version(root)
        tree = ast.parse((root / "backend/app/__init__.py").read_text(encoding="utf-8-sig"))
        versions = [
            node.value.value for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
        ]
        if versions != [expected_version]:
            report.add(
                "integridad_codigo", FAIL,
                "backend/app/__init__.py no declara la version del proyecto. "
                "Extraiga el ZIP completo en una carpeta vacia; no mezcle archivos de otra entrega.",
            )
            return
    except (OSError, SyntaxError, ValueError, KeyError, TypeError) as exc:
        report.add("integridad_codigo", FAIL,
                   f"Paquete incompleto o ilegible ({type(exc).__name__}); extraiga nuevamente el ZIP completo.")
        return
    manifest = root / "SHA256SUMS.txt"
    if not manifest.is_file():
        report.add(
            "integridad_codigo", FAIL if require_manifest else WARN,
            "Falta SHA256SUMS.txt; extraiga el ZIP completo en una carpeta vacia."
            if require_manifest else f"version {expected_version}; checkout sin manifiesto de release",
            required=require_manifest,
        )
        return
    try:
        entries: dict[str, str] = {}
        for line in manifest.read_text(encoding="utf-8-sig").splitlines():
            digest, relative = line.split("  ", 1)
            parts = relative.split("/")
            if (
                len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
                or not relative or "\\" in relative or ":" in relative or relative.startswith("/")
                or any(part in {"", ".", ".."} for part in parts) or relative in entries
            ):
                raise ValueError("ManifiestoInvalido")
            entries[relative] = digest
        essential = CORE_FILES | {"backend/app/__init__.py", "backend/app/main.py",
                                  "backend/scripts/preflight.py", "backend/scripts/bootstrap.py",
                                  "windows/Common-MatrixRH.ps1"}
        missing = sorted(essential - entries.keys())
        for prefix in CORE_PREFIXES:
            directory = root / prefix
            if directory.is_dir():
                missing.extend(
                    path.relative_to(root).as_posix() for path in directory.rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts
                    and _core_file(path.relative_to(root).as_posix())
                    and path.relative_to(root).as_posix() not in entries
                )
        changed = []
        for relative, digest in entries.items():
            if not _core_file(relative):
                continue
            path = root / relative
            if (path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                changed.append(relative)
        if missing or changed:
            details = ", ".join(sorted(set(missing + changed))[:8])
            report.add("integridad_codigo", FAIL,
                       f"Codigo distinto, ausente o fuera del manifiesto: {details}. "
                       "Extraiga el ZIP completo en una carpeta vacia y conserve .env, corpus y var por separado.")
            return
    except (OSError, ValueError, UnicodeError) as exc:
        report.add("integridad_codigo", FAIL,
                   f"SHA256SUMS.txt no verificable ({type(exc).__name__}); descargue el ZIP completo nuevamente.")
        return
    report.add("integridad_codigo", OK, f"version {expected_version}; fuente operativa conforme a SHA256SUMS.txt")


def check_python(report: PreflightReport) -> None:
    version = sys.version_info
    is_64bit = platform.architecture()[0] == "64bit"
    if version[:2] == (3, 12) and is_64bit:
        report.add("python", OK, f"{platform.python_version()} x64")
    elif version[:2] == (3, 12):
        report.add("python", FAIL, f"{platform.python_version()} no es x64")
    else:
        report.add(
            "python",
            FAIL,
            f"Se requiere Python 3.12 x64; encontrado {platform.python_version()}",
        )


def check_dependencies(report: PreflightReport) -> None:
    required = (
        "fastapi",
        "pydantic",
        "pydantic_settings",
        "sqlalchemy",
        "pymysql",
        "cryptography",
        "httpx",
        "argon2",
        "sqlglot",
        "qdrant_client",
        "docx",
        "pypdf",
        "openpyxl",
        "yaml",
        "jwt",
        "jsonschema",
        "psutil",
        "uvicorn",
        "python_multipart",
        "starlette",
    )
    missing: list[str] = []
    for module in required:
        try:
            __import__(module)
        except (ImportError, OSError):
            missing.append(module)
    if missing:
        report.add("dependencias", FAIL, "no se pueden importar: " + ", ".join(missing))
    else:
        report.add("dependencias", OK, f"{len(required)} paquetes presentes")


def check_settings(report: PreflightReport):  # noqa: ANN201
    try:
        from app.config import get_settings

        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - imports/configuracion tambien pueden estar incompletos
        diagnostic = getattr(exc, "detail", "")
        report.add("configuracion", FAIL,
                   f"No se pudo cargar .env o el backend ({type(exc).__name__}). "
                   "Revise .env.example y la integridad del ZIP; no se modifico la configuracion."
                   + (f" {diagnostic}" if diagnostic else ""))
        return None
    report.add(
        "configuracion",
        OK,
        f"APP_ENV={settings.app_env} AUTH_PROVIDER={settings.auth_provider}",
    )
    return settings


def check_application(report: PreflightReport) -> None:
    """Carga el mismo objeto ASGI que Uvicorn sin ejecutar su lifespan."""
    try:
        from fastapi import FastAPI

        application = importlib.import_module("app.main").app
        if not isinstance(application, FastAPI) or application.version != project_version():
            raise ValueError("AplicacionOVersionInvalida")
    except Exception as exc:  # noqa: BLE001 - diagnostico operativo, sin payloads ni secretos
        report.add("aplicacion_asgi", FAIL,
                   f"app.main:app no puede cargarse ({type(exc).__name__}). "
                   "Restaure el ZIP completo y repita la instalacion; no se ejecutan migraciones ni ingesta.")
        return
    report.add("aplicacion_asgi", OK, "app.main:app importable; version coherente (sin iniciar servicios)")


def check_environment_safety(report: PreflightReport, settings) -> None:  # noqa: ANN001
    """Verifica que el modo local no pueda vivir en produccion."""
    from app.config import AppEnv, AuthProvider

    if settings.app_env is AppEnv.PRODUCTION:
        if settings.local_test_auth_enabled or settings.auth_provider is AuthProvider.LOCAL_TEST:
            report.add(
                "modo_local_en_produccion",
                FAIL,
                "LOCAL_TEST_AUTH_ENABLED/AUTH_PROVIDER=local_test en production",
            )
        else:
            report.add("modo_local_en_produccion", OK, "deshabilitado correctamente")
    else:
        report.add(
            "modo_local_en_produccion",
            OK,
            f"APP_ENV={settings.app_env}: modo local permitido",
        )

    if settings.auth_provider is AuthProvider.ENTRA:
        placeholders = {"", "replace_me"}
        missing = [
            name
            for name, value in (
                ("ENTRA_TENANT_ID", settings.entra_tenant_id),
                ("ENTRA_CLIENT_ID", settings.entra_client_id),
                ("ENTRA_REDIRECT_URI", settings.entra_redirect_uri),
            )
            if value in placeholders
        ]
        report.add(
            "entra_configuracion",
            FAIL if missing else OK,
            "faltan: " + ", ".join(missing) if missing else "configurado",
        )
    else:
        report.add(
            "entra_configuracion",
            WARN,
            f"AUTH_PROVIDER={settings.auth_provider}: Entra ID permanece inactivo",
            required=False,
        )


def check_ollama(report: PreflightReport, settings) -> None:  # noqa: ANN001
    """Verifica los roles configurados mediante el adapter; nombre historico conservado."""
    import httpx

    from app.common.errors import MatrixError
    from app.llm.provider import ModelClient

    client = None
    providers = ", ".join(dict.fromkeys((
        settings.llm_provider, settings.llm_deep_provider, settings.llm_embedding_provider,
    )))
    try:
        client = ModelClient()
        inventory = client.list_models()
        report.add("ollama", OK, f"adapters configurados: {providers}")
        for role, model in (
            ("fast", settings.ollama_fast_model),
            ("deep", settings.ollama_deep_model),
            ("embedding", settings.ollama_embedding_model),
        ):
            present = inventory.has(model)
            report.add(f"modelo_{role}", OK if present else FAIL,
                       model if present else f"{model} NO disponible en su runtime")
        dimension = client.probe_embedding_dimension()
        report.add("ollama_embeddings", OK, f"adapter {settings.llm_embedding_provider} responde")
        expected = settings.ollama_embedding_dimension
        report.add(
            "ollama_dimension", OK if dimension == expected else FAIL,
            f"real={dimension} esperada={expected}"
            + ("" if dimension == expected else " -- no se trunca ni se rellena el vector"),
        )
    except (MatrixError, httpx.HTTPError, ValueError) as exc:
        detail = exc.message if isinstance(exc, MatrixError) else type(exc).__name__
        report.add("ollama", FAIL, f"adapters {providers}: {detail}")
        report.add("ollama_dimension", FAIL, "no verificable")
    finally:
        if client is not None:
            client.close()


def check_database(report: PreflightReport, *, read_only: bool) -> bool:
    """Comprueba la base interna. Devuelve True si es utilizable por Matrix RH.

    Distingue tres situaciones que antes se confundian en un unico fallo
    generico: servidor inalcanzable, base que **pertenece a otra aplicacion**, y
    migraciones pendientes.
    """
    from app.database.engine import check_database as ping_database

    ok, detail = ping_database()
    report.add("mysql", OK if ok else FAIL, detail)
    if not ok:
        report.add("migraciones", FAIL, "no verificable")
        return False

    # Propiedad de la base ANTES que nada: si es ajena, todo lo demas fallara
    # con errores que no explican la causa.
    try:
        from app.database.migrator import database_ownership_problem

        problema = database_ownership_problem()
    except Exception as exc:  # noqa: BLE001
        problema = f"no verificable ({type(exc).__name__})"

    if problema:
        from sqlalchemy.engine import make_url

        from app.config import get_settings

        nombre = make_url(get_settings().database_url.get_secret_value()).database
        report.add(
            "base_de_datos_propia",
            FAIL,
            f"la base '{nombre}' {problema}. "
            f"Edite DATABASE_URL en .env y use un nombre libre (por ejemplo {nombre}_app).",
        )
        report.add("migraciones", FAIL, "no aplicables sobre una base ajena")
        return False

    report.add("base_de_datos_propia", OK, "la base pertenece a Matrix RH")

    try:
        from app.database.migrator import pending_migrations

        pending = pending_migrations()
        if pending and read_only:
            report.add("migraciones", WARN, "pendientes: " + ", ".join(pending), required=False)
        elif pending:
            report.add(
                "migraciones",
                FAIL,
                "pendientes: " + ", ".join(pending) + ". Ejecute INSTALAR_MATRIX_RH.bat",
            )
        else:
            report.add("migraciones", OK, "al dia")
    except Exception as exc:  # noqa: BLE001
        report.add("migraciones", FAIL, type(exc).__name__)
    return True


def check_seed_users(report: PreflightReport, settings, *, database_ok: bool = True) -> None:  # noqa: ANN001
    """Verifica las cuentas sinteticas y que su hash sea Argon2id."""
    if not settings.is_local_auth_allowed:
        report.add("usuarios_prueba", OK, "modo local deshabilitado: no aplica", required=False)
        return
    if not database_ok:
        # Sin una base utilizable, consultar los usuarios produce un error de SQL
        # que no explica nada. Se reporta la dependencia real.
        report.add(
            "usuarios_prueba",
            WARN,
            "no verificable: resuelva antes el problema de la base de datos",
            required=False,
        )
        return
    try:
        from sqlalchemy import select

        from app.auth.passwords import is_argon2id
        from app.database.engine import session_scope
        from app.database.models import LocalCredential, User

        with session_scope() as db:
            problems: list[str] = []
            for username in ("Matrix", "MatrixR1"):
                user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
                if user is None:
                    problems.append(f"{username}: no existe")
                    continue
                credential = db.get(LocalCredential, user.id)
                if credential is None:
                    problems.append(f"{username}: sin credencial local")
                elif not is_argon2id(credential.password_hash_argon2id):
                    problems.append(f"{username}: el hash no es Argon2id")
        if problems:
            report.add("usuarios_prueba", FAIL, "; ".join(problems))
        else:
            report.add("usuarios_prueba", OK, "Matrix y MatrixR1 con hash Argon2id")
    except Exception as exc:  # noqa: BLE001
        report.add("usuarios_prueba", FAIL, type(exc).__name__)


def check_qdrant(report: PreflightReport, settings, *, read_only: bool = False) -> None:  # noqa: ANN001
    if read_only:
        from app.config import QdrantMode

        if settings.qdrant_mode is QdrantMode.EMBEDDED:
            report.add(
                "qdrant", WARN,
                "modo embedded: se omite la apertura para no crear archivos ni competir por el lock; "
                "compruebe /ready del backend activo o ejecute preflight con el backend detenido",
                required=False,
            )
            return
        try:
            from qdrant_client import QdrantClient

            client = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key.get_secret_value() or None,
                timeout=10,
            )
            try:
                client.get_collections()
            finally:
                client.close()
            report.add("qdrant", OK, "servidor alcanzable (consulta de colecciones sin escritura)")
        except Exception as exc:  # noqa: BLE001
            report.add("qdrant", FAIL, type(exc).__name__)
        return
    try:
        from app.rag.vector_store import get_vector_store

        ok, detail = get_vector_store().health()
        report.add("qdrant", OK if ok else FAIL, detail)
    except Exception as exc:  # noqa: BLE001
        report.add("qdrant", FAIL, type(exc).__name__)


def check_paths(report: PreflightReport, settings, *, read_only: bool) -> None:  # noqa: ANN001
    knowledge = settings.knowledge_root_path
    report.add(
        "knowledge_root",
        OK if knowledge.is_dir() else FAIL,
        str(knowledge) if knowledge.is_dir() else f"no existe: {knowledge}",
    )
    try:
        from app.ingestion.knowledge_layout import inventory_knowledge

        inventory = inventory_knowledge(knowledge_root=knowledge)
        summary = inventory.as_dict()
        files_by_category = summary["files_by_category"]
        assert isinstance(files_by_category, dict)
        categories = ", ".join(f"{name}={count}" for name, count in files_by_category.items())
        report.add(
            "conocimiento_documental", WARN if inventory.warnings else OK,
            f"{summary['indexable_files']} archivos indexables; {summary['pdf_files']} PDF; "
            f"{len(inventory.sources) - len(inventory.unavailable_sources)} raices disponibles"
            + (f"; categorias: {categories}" if categories else "")
            + ". Solo inventario: texto de PDF se valida durante ingesta; no hay OCR.",
            required=False,
        )
        for warning in inventory.warnings[:10]:
            report.add("conocimiento_aviso", WARN, str(warning["message"]), required=False)
    except Exception as exc:  # noqa: BLE001 - una politica invalida falla cerrado sin exponerla
        report.add("conocimiento_documental", FAIL,
                   f"No se pudo validar el inventario: {type(exc).__name__}. Revise config/knowledge-layout.yaml.")

    storage = settings.upload_storage_path
    if read_only:
        report.add(
            "almacenamiento_runtime",
            OK if storage.parent.exists() else WARN,
            str(storage),
            required=False,
        )
        return
    try:
        storage.mkdir(parents=True, exist_ok=True)
        probe = storage / ".preflight_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.add("almacenamiento_runtime", OK, f"escritura verificada en {storage}")
    except Exception as exc:  # noqa: BLE001
        report.add("almacenamiento_runtime", FAIL, f"{type(exc).__name__}: {storage}")


def windows_encryption_status(paths: list[Path]) -> list[dict[str, object]]:
    """Consulta BitLocker sin modificar cifrado, protectores ni archivos.

    Get-Volume resuelve el volumen real del camino (incluidas unidades montadas),
    no se infiere que una carpeta esta cifrada por la letra de su padre.
    """
    script = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
try {
    $paths=ConvertFrom-Json ([Console]::In.ReadToEnd())
    $result=@(foreach($path in $paths) {
        $existing=[string]$path
        while(-not (Test-Path -LiteralPath $existing)) {
            $parent=Split-Path -Parent $existing
            if(-not $parent -or $parent -eq $existing) { throw 'RutaNoVerificable' }
            $existing=$parent
        }
        $volumes=@(Get-Volume -FilePath $existing -ErrorAction Stop)
        if($volumes.Count -ne 1 -or -not $volumes[0].DriveLetter) { throw 'VolumenNoVerificable' }
        $mount=[string]$volumes[0].DriveLetter + ':'
        $items=@(Get-BitLockerVolume -MountPoint $mount -ErrorAction Stop)
        if($items.Count -ne 1) { throw 'BitLockerNoVerificable' }
        $item=$items[0]
        @{ path=[string]$path; mount=$mount; status=[string]$item.VolumeStatus;
           protection=[string]$item.ProtectionStatus; percentage=[int]$item.EncryptionPercentage }
    })
    ConvertTo-Json -InputObject $result -Compress
}
catch { Write-Output 'BITLOCKER_NO_VERIFICABLE'; exit 1 }
"""
    powershell = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    completed = subprocess.run(  # noqa: S603 - programa fijo; rutas via stdin, sin credenciales
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
        input=json.dumps([str(path) for path in paths]), capture_output=True, text=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise OSError("BitLockerNoVerificable")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, list) or len(payload) != len(paths):
        raise ValueError("BitLockerRespuestaInvalida")
    if any(not isinstance(item, dict) or item.get("path") != str(path)
           for item, path in zip(payload, paths, strict=True)):
        raise ValueError("BitLockerRespuestaInvalida")
    return payload


def _mysql_datadir(settings) -> Path:  # noqa: ANN001
    configured = getattr(settings, "database_datadir_path", None)
    if configured is not None:
        path = Path(configured)
    else:
        from sqlalchemy import text

        from app.database.engine import get_engine

        with get_engine().connect() as connection:
            path = Path(str(connection.execute(text("SELECT @@datadir")).scalar_one()))
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("DatadirNoVerificable")
    return path


def check_storage_encryption(report: PreflightReport, settings, *, database_ok: bool = True) -> None:  # noqa: ANN001
    """Exige evidencia de infraestructura; no activa cifrado por codigo."""
    required = str(settings.app_env) in {"staging", "production"}
    if not required:
        report.add("cifrado_reposo", WARN,
                   "no exigido en development/test; TI debe validar discos, MySQL y respaldos antes de produccion",
                   required=False)
        return
    reference = getattr(settings, "encryption_attestation_reference", None)
    reference_path = Path(reference) if reference is not None else None
    if reference_path is not None and not reference_path.is_absolute():
        reference_path = PROJECT_ROOT / reference_path
    attestations = all(bool(getattr(settings, key, False)) for key in (
        "storage_encryption_attested", "database_encryption_attested", "backup_encryption_attested",
    ))
    if not attestations or reference_path is None or not reference_path.is_file():
        report.add("cifrado_reposo", FAIL,
                   "Falta evidencia local de TI: STORAGE/DATABASE/BACKUP_ENCRYPTION_ATTESTED y "
                   "ENCRYPTION_ATTESTATION_REFERENCE deben identificar el documento de cifrado del host y respaldos.")
        return
    if platform.system() != "Windows":
        report.add("cifrado_reposo", OK,
                   "declaracion de infraestructura con referencia local existente; Linux/Docker no mide BitLocker. "
                   "TI conserva la evidencia de host, MySQL y respaldos (no se ha activado cifrado desde Matrix).")
        return
    try:
        paths = [PROJECT_ROOT, settings.upload_storage_path, settings.knowledge_root_path]
        if str(settings.qdrant_mode) == "embedded":
            paths.append(settings.qdrant_storage_path)
        database_host = urlsplit(settings.database_url.get_secret_value()).hostname
        local_database = database_host in {"127.0.0.1", "localhost", "::1"}
        if local_database:
            if not database_ok:
                raise ValueError("MySQLNoDisponible")
            paths.append(_mysql_datadir(settings))
        status = windows_encryption_status(paths)
        unprotected = [str(item.get("mount", "?")) for item in status if not (
            item.get("status") == "FullyEncrypted" and item.get("protection") == "On"
            and item.get("percentage") == 100
        )]
        if unprotected:
            report.add("cifrado_reposo", FAIL,
                       "BitLocker no protege completamente los volumenes " + ", ".join(sorted(set(unprotected)))
                       + "; TI debe resolver cifrado/proteccion antes de instalar o arrancar.")
            return
    except Exception as exc:  # noqa: BLE001 - consulta SQL/Windows devuelve solo tipo, nunca credenciales
        report.add("cifrado_reposo", FAIL,
                   f"BitLocker/datadir no verificable ({type(exc).__name__}). "
                   "Solicite a TI acceso de lectura a Get-Volume/Get-BitLockerVolume y el datadir local de MySQL. "
                   "No se presume que el disco esta cifrado.")
        return
    report.add("cifrado_reposo", OK,
               f"BitLocker completo y proteccion On en {len(paths)} rutas locales; respaldos declarados por TI"
               + ("; MySQL remoto cubierto solo por la evidencia del servidor"
                  if not local_database else "; datadir MySQL incluido"))


def check_config_files(report: PreflightReport, settings) -> None:  # noqa: ANN001
    from app.authorization.categories import load_category_policy_file
    from app.structured_data.sources import load_sources

    try:
        policy = load_category_policy_file()
        report.add("politica_categorias", OK, f"{len(policy.categories)} categorias declaradas")
    except Exception as exc:  # noqa: BLE001
        report.add("politica_categorias", FAIL, str(exc)[:200])

    try:
        catalog = load_sources()
        statuses = catalog.status_report()
        connected = [s for s in statuses if s["status"] == "CONNECTED_AND_VALIDATED"]
        report.add(
            "fuentes_estructuradas",
            OK,
            f"{len(statuses)} declaradas, {len(connected)} conectadas",
            required=settings.matrix_external_connectors_required,
        )
        for source in statuses:
            report.add(
                f"fuente_{source['name']}",
                OK if source["status"] != "ERROR" else FAIL,
                source["status"],
                required=settings.matrix_external_connectors_required,
            )
    except Exception as exc:  # noqa: BLE001
        report.add("fuentes_estructuradas", FAIL, str(exc)[:200])


def check_frontend(report: PreflightReport) -> None:
    from app.config import PROJECT_ROOT

    dist = PROJECT_ROOT / "frontend" / "dist" / "index.html"
    report.add(
        "frontend_build",
        OK if dist.exists() else WARN,
        str(dist.parent) if dist.exists() else "no compilado (corepack npm run build)",
        required=False,
    )


def check_supply_chain(report: PreflightReport) -> None:
    """Cadena de suministro del frontend (seccion 13 de docs/README.md).

    Es una comprobacion **obligatoria**: un paquete comprometido en el arbol
    ejecuta codigo con los privilegios del operador. Si no hay arbol instalado
    se degrada a aviso, porque el backend funciona sin el frontend compilado.
    """
    try:
        from scripts.verify_supply_chain import NODE_MODULES, run

        if not NODE_MODULES.is_dir():
            report.add(
                "cadena_de_suministro",
                WARN,
                "frontend/node_modules ausente (ejecute 'corepack npm ci --ignore-scripts')",
                required=False,
            )
            return

        result = run()
        if result.ok:
            avisos = [f for f in result.findings if f.severity == "warning"]
            detalle = f"{result.checked_packages} paquetes verificados"
            if avisos:
                detalle += f", {len(avisos)} con script de instalacion (no ejecutados)"
            report.add("cadena_de_suministro", OK, detalle)
        else:
            report.add(
                "cadena_de_suministro",
                FAIL,
                "; ".join(f"{f.kind}: {f.detail}" for f in result.blocking[:3]),
            )
    except Exception as exc:  # noqa: BLE001
        report.add("cadena_de_suministro", FAIL, type(exc).__name__)


def check_ports(report: PreflightReport, settings, *, require_free: bool = False) -> None:  # noqa: ANN001
    """Comprueba bind/listen; el modo estricto no importa paquetes ni consulta salud."""
    host = settings.app_host
    try:
        port = int(settings.app_port)
    except (TypeError, ValueError):
        port = 0
    if (
        not isinstance(host, str) or not host.strip() or host != host.strip()
        or "[" in host or "]" in host or not 1 <= port <= 65535
    ):
        report.add("puerto_backend", FAIL, "APP_HOST o APP_PORT no son validos; puerto requerido: 1 a 65535")
        return
    try:
        addresses = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP, flags=socket.AI_PASSIVE,
        )
        if not addresses:
            report.add("puerto_backend", FAIL, "APP_HOST no resuelve a una direccion comprobable")
            return
        # Mantener todas las escuchas hasta terminar detecta tambien colisiones
        # parciales de hostname. ExitStack cierra cada socket ante cualquier fallo.
        with ExitStack() as opened:
            seen = set()
            for family, kind, proto, _, address in addresses:
                if (family, address) in seen:
                    continue
                seen.add((family, address))
                listener = opened.enter_context(socket.socket(family, kind, proto))
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                elif os.name == "posix" and sys.platform != "cygwin":
                    # Mismo reinicio tras TIME_WAIT que asyncio/uvicorn en Unix.
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.bind(address)
                listener.listen(1)
    except OSError as exc:
        code = getattr(exc, "winerror", None) or exc.errno
        detail = f"no verificable: {type(exc).__name__}"
        if code is not None:
            detail += f" (codigo {code})"
        busy = code in {errno.EADDRINUSE, 10048}
        denied = code in {errno.EACCES, 10013}
        if require_free or not (busy or denied):
            report.add("puerto_backend", FAIL, f"ocupado (codigo {code})" if busy else detail)
            return
    else:
        report.add("puerto_backend", OK, f"{host}:{port} libre (bind verificado)")
        return
    # Solo tras un bind rechazado: el diagnostico puede reconocer Matrix activo.
    # La instalacion exige puerto libre y nunca entra en esta consulta HTTP.
    try:
        import httpx

        probe_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)  # noqa: S104
        url_host = f"[{probe_host}]" if ":" in probe_host else probe_host
        response = httpx.get(f"http://{url_host}:{port}/health", timeout=3.0, trust_env=False)
        mine = response.status_code == 200 and response.json().get("app") == settings.app_name
    except Exception:  # noqa: BLE001
        mine = False
    report.add(
        "puerto_backend",
        OK if mine else FAIL,
        "ocupado por Matrix RH" if mine else ("ocupado por otro proceso" if busy else detail),
        required=not mine,
    )


def check_windows_scripts(report: PreflightReport) -> None:
    """Comprueba que los BAT apunten a rutas reales del proyecto."""
    from app.config import PROJECT_ROOT

    expected = (
        "INSTALAR_MATRIX_RH.bat",
        "INICIAR_MATRIX_RH.bat",
        "DETENER_MATRIX_RH.bat",
        "DIAGNOSTICO_MATRIX_RH.bat",
    )
    missing = [name for name in expected if not (PROJECT_ROOT / name).exists()]
    report.add(
        "scripts_windows",
        WARN if missing else OK,
        "faltan: " + ", ".join(missing) if missing else f"{len(expected)} scripts presentes",
        required=False,
    )


# ---------------------------------------------------------------------------
def run_preflight(*, read_only: bool = False) -> PreflightReport:
    """Ejecuta todas las comprobaciones."""
    report = PreflightReport()
    check_integrity(report)
    if not report.ok:
        return report
    check_python(report)
    check_dependencies(report)
    # Un python.exe presente no acredita una instalacion completa. La sonda
    # usa solo la biblioteca estandar hasta aqui; cargar Settings con paquetes
    # ausentes volveria a convertir el fallo detectado en un traceback.
    if not report.ok:
        return report
    settings = check_settings(report)
    if settings is None:
        return report

    check_application(report)
    if not report.ok:
        return report
    check_environment_safety(report, settings)
    check_ollama(report, settings)
    database_ok = check_database(report, read_only=read_only)
    check_storage_encryption(report, settings, database_ok=database_ok)
    check_seed_users(report, settings, database_ok=database_ok)
    check_qdrant(report, settings, read_only=read_only)
    check_paths(report, settings, read_only=read_only)
    check_config_files(report, settings)
    check_frontend(report)
    check_supply_chain(report)
    check_ports(report, settings)
    check_windows_scripts(report)
    return report


def render_text(report: PreflightReport) -> str:
    symbols = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
    lines = ["", "=== PREFLIGHT MATRIX RH ===", ""]
    for check in report.checks:
        lines.append(f"{symbols[check.status]} {check.name:26s} {check.detail}")
    lines.append("")
    lines.append("RESULTADO: " + ("PREFLIGHT OK" if report.ok else "PREFLIGHT CON FALLOS"))
    if not report.ok:
        lines.append("Fallos obligatorios: " + ", ".join(c.name for c in report.checks if c.failed))
    if any(c.name == "dependencias" and c.failed for c in report.checks):
        lines.append("Accion: desde la raiz del proyecto ejecute INSTALAR_MATRIX_RH.bat -SkipFrontend.")
        lines.append("El instalador sincroniza todos los paquetes desde backend/uv.lock en .venv.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight y diagnostico de Matrix RH")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--llm-only", action="store_true", help="solo valida modelos y embeddings desde .env")
    scope.add_argument(
        "--dependencies-only", action="store_true",
        help="solo valida Python y paquetes; no carga configuracion ni conecta servicios",
    )
    scope.add_argument("--ports-only", action="store_true", help="solo bind del backend, sin dependencias ni .env")
    scope.add_argument("--integrity-only", action="store_true",
                       help="valida version y manifiesto con stdlib, sin imports de app")
    scope.add_argument("--app-only", action="store_true", help="carga app.main:app sin iniciar lifespan ni servicios")
    scope.add_argument("--safety-only", action="store_true",
                       help="configuracion y cifrado sin escribir ni migrar la base")
    parser.add_argument("--require-manifest", action="store_true", help="exige SHA256SUMS.txt de la entrega")
    parser.add_argument("--host", default="127.0.0.1", help="host para --ports-only")
    parser.add_argument("--port", default="8000", help="puerto para --ports-only")
    parser.add_argument("--json", action="store_true", help="salida JSON")
    parser.add_argument("--read-only", action="store_true", help="no crea directorios ni exige migraciones aplicadas")
    parser.add_argument("--output", default="", help="ruta de archivo donde escribir el JSON")
    args = parser.parse_args(argv)

    # El logging JSON estorba en una salida pensada para un operador.
    os.environ.setdefault("APP_LOG_LEVEL", "WARNING")

    if args.integrity_only:
        report = PreflightReport()
        check_integrity(report, require_manifest=args.require_manifest)
    elif args.ports_only:
        report = PreflightReport()
        check_ports(report, SimpleNamespace(app_host=args.host, app_port=args.port), require_free=True)
    elif args.dependencies_only or args.llm_only or args.app_only or args.safety_only:
        report = PreflightReport()
        check_python(report)
        check_dependencies(report)
        if (args.llm_only or args.app_only or args.safety_only) and report.ok:
            check_integrity(report)
            settings = check_settings(report) if report.ok else None
            if settings is not None and report.ok:
                if args.llm_only:
                    check_ollama(report, settings)
                elif args.app_only:
                    check_application(report)
                else:
                    check_storage_encryption(report, settings)
    else:
        report = run_preflight(read_only=args.read_only)
    payload = report.as_dict()

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else render_text(report))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
