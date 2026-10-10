# Creado por Aldo Garcia.
"""Respaldo verificable de la instalacion Windows nativa, estrictamente en frio.

Primero ejecute detener.bat. Desde backend:
    runtime\\venv\\Scripts\\python.exe -m scripts.installation_backup backup
    runtime\\venv\\Scripts\\python.exe -m scripts.installation_backup verify --backup RUTA

Copia el datadir MySQL real junto con SU runtime, configuracion privada,
documentos, uploads, Qdrant, estado y codigo. Ollama externo no se copia. No abre SQL, no detiene procesos,
no descarga, no restaura sobre una instalacion y no acredita recuperacion MySQL.
Los hashes detectan alteraciones accidentales; no son una firma de autenticidad.
"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import hashlib
import io
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import psutil
from dotenv import dotenv_values

MANIFEST = "installation-backup.json"
INCOMPLETE = "BACKUP-INCOMPLETE.txt"
PAYLOAD = "installation"
SCHEMA = "matrix-rh-native-cold-backup-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MYSQL_EXE = "backend/runtime/mysql/bin/mysqld.exe"
MYSQL_RECEIPT = "backend/runtime/mysql/.matrix-package.json"
OLLAMA_EXE = "backend/runtime/ollama/ollama.exe"
REQUIRED_DIRS = (
    "backend/config", "backend/runtime", "backend/runtime/venv", "backend/runtime/python",
    "knowledge-base/documents", "knowledge-base/unclassified",
    "knowledge-base/state/mysql", "knowledge-base/state/qdrant",
    "knowledge-base/state/uploads", "knowledge-base/state/run",
    "backend/app", "backend/scripts", "backend/scripts/windows", "backend/release",
    "backend/config/apache", "frontend/dist",
)
REQUIRED_FILES = (
    "backend/config/.env", "backend/config/mysql.ini", "backend/pyproject.toml", "backend/uv.lock",
    MYSQL_EXE, MYSQL_RECEIPT, "backend/runtime/venv/Scripts/python.exe",
    "frontend/package-lock.json", "frontend/dist/index.html", "README.md",
    "instalar.bat", "iniciar.bat", "detener.bat", "diagnosticar.bat", ".htaccess",
    "backend/scripts/windows/MatrixRH.ps1", "backend/scripts/windows/launch_process.py",
    "backend/scripts/installation_backup.py", "backend/config/runtime-manifest.json",
    "backend/config/apache/matrix-rh.conf.template", "backend/release/BUILD_INFO.json",
    "backend/release/SHA256SUMS.txt", "backend/release/layout-migration.json",
    "knowledge-base/state/mysql/auto.cnf",
)
EXCLUDED_TREES = (
    ".git", "knowledge-base/backups", "frontend/node_modules", "frontend/playwright-report",
    "frontend/test-results", "backend/.venv",
)
CODE_CACHES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
PATH_SETTINGS = {
    "DATABASE_DATADIR_PATH": "knowledge-base/state/mysql",
    "RAG_KNOWLEDGE_ROOT": "knowledge-base/documents",
    "QDRANT_PATH": "knowledge-base/state/qdrant",
    "UPLOAD_STORAGE_ROOT": "knowledge-base/state/uploads",
}
ENV_SETTINGS = frozenset({
    *PATH_SETTINGS, "APP_HOST", "APP_PORT", "DATABASE_URL", "MATRIX_MYSQL_PORT", "QDRANT_MODE",
    "OLLAMA_BASE_URL", "LLM_PROVIDER", "LLM_DEEP_PROVIDER", "LLM_EMBEDDING_PROVIDER",
})
RESTORE_INSTRUCTIONS = [
    "Conserve el origen detenido durante toda la copia y la verificacion; proteja las credenciales del respaldo.",
    "Verifique el respaldo antes de copiar installation completo a una carpeta NUEVA y vacia; "
    "nunca sobre datos existentes.",
    "Conserve juntos datadir, runtime MySQL y .matrix-package.json; "
    "no inicialice ni actualice MySQL sobre ese datadir.",
    "La copia conserva los binarios propios y venv; "
    "un venv y mysql.ini pueden contener rutas absolutas no relocatables.",
    "Ollama y sus pesos externos no se copian: conserve o restaure esa instalacion por separado "
    "y compruebe sus digests antes de iniciar Matrix.",
    "Antes de activar una copia reubicada, TI debe adaptar rutas locales de .env/mysql.ini "
    "y recrear el venv con uv.lock.",
    "Los registros state/run son evidencia historica: no autorizan detener procesos "
    "ni arrancar con PIDs de otra instalacion.",
    "Compruebe puertos libres, permisos, cuentas, MySQL y Qdrant en el destino antes de permitir acceso de usuarios.",
    "No se ha ejecutado una recuperacion MySQL, restauracion SQL ni activacion mediante este comando.",
]


class BackupError(ValueError):
    """Error operativo cuyo mensaje nunca contiene configuracion privada."""


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\\:\x00"):
        raise BackupError("Ruta no portable o fuera del conjunto.")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {".", ".."} for part in path.parts):
        raise BackupError("Ruta no portable o fuera del conjunto.")
    for part in path.parts:
        if part.endswith((".", " ")) or any(ord(char) < 32 or char in '<>\"|?*' for char in part):
            raise BackupError("Nombre de archivo no portable a Windows.")
        if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part):
            raise BackupError("Nombre de dispositivo Windows no permitido.")
    return value


def _regular(path: Path, *, directory: bool | None = None) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise BackupError("No se admiten enlaces simbolicos, junctions ni puntos de reanalisis.")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise BackupError("No se admiten archivos especiales.")
    if directory is not None and stat.S_ISDIR(info.st_mode) != directory:
        raise BackupError("Un componente obligatorio tiene un tipo incorrecto.")
    return info


def _safe_path(path: Path, *, exists: bool = True) -> Path:
    raw = str(path)
    if raw.startswith(("\\\\", "//")) or ".." in raw.replace("\\", "/").split("/"):
        raise BackupError("Use una ruta local directa, sin red ni traversal.")
    result = path.absolute()
    for ancestor in reversed((result, *result.parents)):
        try:
            _regular(ancestor)
        except FileNotFoundError:
            if ancestor != result or exists:
                raise BackupError("El directorio padre debe existir y ser local.") from None
    if os.name == "nt" and ctypes.windll.kernel32.GetDriveTypeW(str(result.anchor)) != 3:
        raise BackupError("El respaldo requiere un volumen local fijo; no se admite una unidad de red.")
    return result


def _read_json(path: Path, *, limit: int = 1_048_576) -> dict:
    if _regular(path, directory=False).st_size > limit:
        raise BackupError("Metadatos demasiado grandes o invalidos.")
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise BackupError("Metadatos invalidos.")
    return result


def _hash(path: Path, checkpoint: Callable[[], None] = lambda: None) -> str:
    _safe_path(path)
    before = _regular(path, directory=False)
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise BackupError("El origen cambio durante la lectura.")
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
            checkpoint()
    after = _regular(path, directory=False)
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise BackupError("El origen cambio durante la lectura.")
    return digest.hexdigest()


def _excluded(relative: str) -> bool:
    if any(relative == tree or relative.startswith(tree + "/") for tree in EXCLUDED_TREES):
        return True
    # Los documentos y el runtime se copian completos, incluso si un archivo
    # tiene el nombre de una cache de desarrollo.
    return (relative.startswith(("backend/", "frontend/")) and not relative.startswith("backend/runtime/")
            and any(part in CODE_CACHES for part in PurePosixPath(relative).parts))


def _inventory(root: Path, *, source: bool = False,
               checkpoint: Callable[[], None] = lambda: None) -> tuple[list[str], list[dict]]:
    directories, files, names_seen = [], [], set()

    def visit(directory: Path) -> None:
        _regular(directory, directory=True)
        for path in sorted(directory.iterdir()):
            relative = _relative(path.relative_to(root).as_posix())
            info = _regular(path)  # Tambien rechaza enlaces en una exclusion.
            if source and _excluded(relative):
                continue
            folded = relative.casefold()
            if folded in names_seen:
                raise BackupError("Hay nombres que colisionarian en Windows.")
            names_seen.add(folded)
            checkpoint()
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                visit(path)
            else:
                files.append({"path": relative, "size": info.st_size, "sha256": _hash(path, checkpoint)})

    visit(root)
    return sorted(directories), sorted(files, key=lambda entry: entry["path"])


def _configured_path(root: Path, value: str, expected: str) -> None:
    path = Path(value)
    candidate = _safe_path(path if path.is_absolute() else root / path)
    if candidate != root / expected:
        raise BackupError("La configuracion apunta fuera del almacenamiento propio previsto.")


def _loopback(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _port(value: str | int) -> int:
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise BackupError("Puerto de servicio invalido.") from None
    if not 1 <= number <= 65535:
        raise BackupError("Puerto de servicio invalido.")
    return number


def _receipt_hash(receipt: dict) -> str:
    digest = receipt.get("executable_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise BackupError("La identidad del runtime MySQL no contiene un SHA256 valido.")
    # Get-FileHash de PowerShell guarda el mismo digest en mayusculas.
    return digest.lower()


def _configuration(root: Path) -> dict:
    env_path = root / "backend/config/.env"
    if _regular(env_path, directory=False).st_size > 1_048_576:
        raise BackupError("Configuracion privada invalida.")
    env = dict(dotenv_values(stream=io.StringIO(env_path.read_text(encoding="utf-8-sig")), interpolate=False))
    for key in ENV_SETTINGS:
        if key in os.environ and os.environ[key] != env.get(key):
            raise BackupError("Una variable de entorno altera la configuracion local; use una consola limpia.")
    for key, relative in PATH_SETTINGS.items():
        if not env.get(key):
            raise BackupError("Falta una ruta local obligatoria en backend/config/.env.")
        _configured_path(root, env[key], relative)
    if env.get("QDRANT_MODE", "embedded") != "embedded":
        raise BackupError("Este respaldo requiere Qdrant embedded detenido con el backend.")
    if any(env.get(key, "ollama") != "ollama" for key in
           ("LLM_PROVIDER", "LLM_DEEP_PROVIDER", "LLM_EMBEDDING_PROVIDER")):
        raise BackupError("Este respaldo admite la instalacion nativa con Ollama local.")
    database = urlsplit(env.get("DATABASE_URL", ""))
    ollama = urlsplit(env.get("OLLAMA_BASE_URL", ""))
    if database.scheme != "mysql+pymysql" or not _loopback(database.hostname or "") or not database.port:
        raise BackupError("MySQL debe usar un puerto local explicito de esta instalacion.")
    if (ollama.scheme != "http" or not _loopback(ollama.hostname or "") or not ollama.port
            or ollama.username or ollama.password or ollama.path not in {"", "/"} or ollama.query or ollama.fragment):
        raise BackupError("Ollama debe usar un endpoint local explicito.")
    if not _loopback(env.get("APP_HOST", "127.0.0.1")):
        raise BackupError("El backend nativo debe estar configurado en loopback.")
    ports = {"backend": _port(env.get("APP_PORT", "8000")), "mysql": _port(database.port),
             "ollama": _port(ollama.port)}
    if len(set(ports.values())) != 3 or _port(env.get("MATRIX_MYSQL_PORT", database.port)) != database.port:
        raise BackupError("Los puertos de los servicios propios no son coherentes.")
    ini_path = root / "backend/config/mysql.ini"
    _regular(ini_path, directory=False)
    parser = configparser.ConfigParser(interpolation=None, allow_no_value=True)
    parser.read_string(ini_path.read_text(encoding="utf-8-sig"))
    if not parser.has_section("mysqld"):
        raise BackupError("mysql.ini no identifica el MySQL propio.")
    for key, expected in (("basedir", "backend/runtime/mysql"), ("datadir", PATH_SETTINGS["DATABASE_DATADIR_PATH"])):
        _configured_path(root, (parser.get("mysqld", key, fallback="") or "").strip('"\''), expected)
    if _port(parser.get("mysqld", "port", fallback="")) != ports["mysql"]:
        raise BackupError("mysql.ini no corresponde al puerto configurado.")
    receipt = _read_json(root / MYSQL_RECEIPT)
    if (not isinstance(receipt.get("version"), str) or not receipt["version"].strip()
            or _receipt_hash(receipt) != _hash(root / MYSQL_EXE)):
        raise BackupError("La identidad del runtime MySQL no coincide con su binario; no se acredita el datadir.")
    return {"ports": {key: value for key, value in ports.items() if key != "ollama"},
            "external_services": {"ollama": {"port": ports["ollama"], "management": "external_local",
                                             "runtime_and_weights_included": False}},
            "mysql_runtime_version": receipt["version"]}


def _under(value: str, parent: Path) -> bool:
    if not value:
        return False
    try:
        return Path(value).absolute().is_relative_to(parent)
    except (ValueError, OSError):
        return False


def _own_process(info: dict, root: Path) -> bool:
    command = info.get("cmdline") or []
    executable = info.get("exe") or ""
    name = (info.get("name") or Path(executable).name).lower()
    if "python" in name and (command[1:3] == ["-m", "scripts.installation_backup"] or (
            len(command) > 1 and command[1].replace("\\", "/").endswith("/installation_backup.py"))):
        return False  # Python del propio comando, incluido launcher venv Windows.
    if _under(executable, root / "backend/runtime"):
        return True
    if "ollama" in name and executable:
        return False  # Servicio externo: no comparte los archivos copiados ni debe detenerse.
    relevant = any(item in name for item in ("python", "uvicorn", "mysqld", "ollama", "qdrant"))
    if relevant and _under(info.get("cwd") or "", root):
        return True
    if relevant and any(str(root).replace("\\", "/").casefold() in argument.replace("\\", "/").casefold()
                        for argument in command):
        return True
    if relevant and not executable and not command:
        raise BackupError("No se puede atribuir un proceso de servicio; no se acredita una copia en frio.")
    return False


def _identities(root: Path) -> None:
    run = root / "knowledge-base/state/run"
    for name, executable in (("mysql.json", MYSQL_EXE), ("ollama.json", OLLAMA_EXE),
                             ("matrixrh-backend.identity.json", None)):
        path = run / name
        if not path.exists() and not path.is_symlink():
            continue
        record = _read_json(path, limit=16_384)
        pid = record.get("pid")
        if type(pid) is not int or pid <= 0:
            raise BackupError("Una identidad de proceso es invalida; ejecute detener.bat.")
        if executable:
            if record.get("executable") != str(root / executable):
                raise BackupError("Una identidad de servicio no pertenece a esta instalacion.")
            ticks = record.get("created_ticks")
            if not isinstance(ticks, str) or not ticks.isdigit():
                raise BackupError("Una identidad de proceso es invalida.")
            created = (int(ticks) - 621355968000000000) / 10_000_000
        else:
            if record.get("root") != str(root):
                raise BackupError("Una identidad de backend no pertenece a esta instalacion.")
            created = record.get("created_at")
        if type(created) not in (int, float) or not math.isfinite(created) or created <= 0:
            raise BackupError("Una identidad de proceso es invalida.")
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - created) < 0.001:
                # Un PID/tiempo coincidente bloquea, pero NUNCA autoriza detenerlo.
                raise BackupError("Hay un proceso registrado vivo; ejecute detener.bat primero.")
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            raise BackupError("No se puede comprobar la identidad de un servicio.") from None


def assert_project_stopped(root: Path, configuration: dict) -> None:
    """Solo inspecciona. Un puerto ocupado por terceros tambien bloquea la prueba."""
    _identities(root)
    try:
        for process in psutil.process_iter(["pid", "name", "exe", "cwd", "cmdline", "create_time"], ad_value=None):
            if process.pid != os.getpid() and _own_process(process.info, root):
                raise BackupError("Un servicio propio sigue vivo; ejecute detener.bat primero.")
        ports = set(configuration["ports"].values())
        for connection in psutil.net_connections(kind="tcp"):
            if (connection.status == psutil.CONN_LISTEN and connection.laddr
                    and connection.laddr.port in ports):
                raise BackupError("Un puerto configurado sigue ocupado; no se acredita una copia en frio.")
    except psutil.Error:
        raise BackupError("No se pudo inspeccionar procesos y puertos; no se acredita una copia en frio.") from None


def _validate_set(directories: list[str], files: list[dict]) -> None:
    by_path = {entry["path"]: entry for entry in files}
    if any(relative not in directories for relative in REQUIRED_DIRS):
        raise BackupError("Faltan directorios de una instalacion nativa completa.")
    if any(relative not in by_path for relative in REQUIRED_FILES):
        raise BackupError("Faltan configuracion, codigo, runtime o archivos del datadir MySQL propio.")
    if not any(f"knowledge-base/state/mysql/{name}" in by_path for name in ("mysql.ibd", "ibdata1")):
        raise BackupError("El datadir MySQL propio no contiene sus archivos de datos inicializados.")


def _copy_file(source: Path, destination: Path, entry: dict, checkpoint: Callable[[], None]) -> None:
    _safe_path(source)
    _safe_path(destination, exists=False)
    before = _regular(source, directory=False)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    with os.fdopen(os.open(source, flags), "rb") as original, destination.open("xb") as output:
        opened = os.fstat(original.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise BackupError("El origen cambio durante la copia.")
        for chunk in iter(lambda: original.read(1_048_576), b""):
            output.write(chunk)
            digest.update(chunk)
            checkpoint()
        output.flush()
        os.fsync(output.fileno())
    after = _regular(source, directory=False)
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise BackupError("El origen cambio durante la copia; el respaldo permanece incompleto.")
    os.chmod(destination, stat.S_IMODE(before.st_mode) & 0o700)
    if digest.hexdigest() != entry["sha256"] or _regular(destination).st_size != entry["size"]:
        raise BackupError("El origen cambio durante la copia; el respaldo permanece incompleto.")


def _manifest_inventory(manifest: dict) -> tuple[list[str], list[dict]]:
    directories, files = manifest.get("directories"), manifest.get("files")
    if not isinstance(directories, list) or not isinstance(files, list):
        raise BackupError("El manifiesto no contiene un inventario valido.")
    seen = set()
    for value in directories:
        relative = _relative(value)
        if relative.casefold() in seen:
            raise BackupError("El manifiesto tiene rutas duplicadas.")
        seen.add(relative.casefold())
    for entry in files:
        if (not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}
                or type(entry["size"]) is not int or entry["size"] < 0
                or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise BackupError("El manifiesto contiene archivos invalidos.")
        relative = _relative(entry["path"])
        if relative.casefold() in seen:
            raise BackupError("El manifiesto tiene rutas duplicadas.")
        seen.add(relative.casefold())
    _validate_set(directories, files)
    return directories, files


def _verify(backup: Path, *, incomplete: bool = False) -> dict:
    backup = _safe_path(backup)
    _regular(backup, directory=True)
    allowed = {MANIFEST, PAYLOAD, INCOMPLETE} if incomplete else {MANIFEST, PAYLOAD}
    if {path.name for path in backup.iterdir()} != allowed:
        raise BackupError("Respaldo incompleto o con archivos huerfanos; no debe restaurarse.")
    manifest = _read_json(backup / MANIFEST, limit=128_000_000)
    if (manifest.get("schema") != SCHEMA or manifest.get("state") != "complete"
            or manifest.get("mysql_recovery_tested") is not False
            or manifest.get("runtime_binaries_included") is not True):
        raise BackupError("Formato de respaldo no compatible.")
    expected = _manifest_inventory(manifest)
    payload = _safe_path(backup / PAYLOAD)
    if _inventory(payload) != expected:
        raise BackupError("La integridad del respaldo no coincide con el manifiesto.")
    by_path = {entry["path"]: entry for entry in expected[1]}
    receipt = _read_json(payload / MYSQL_RECEIPT)
    if (_receipt_hash(receipt) != by_path[MYSQL_EXE]["sha256"]
            or receipt.get("version") != manifest.get("mysql_runtime_version")):
        raise BackupError("El runtime MySQL del respaldo no concuerda con su identidad.")
    return manifest


def verify_backup(backup: Path) -> dict:
    """Verifica exactamente el inventario, incluidos archivos privados, sin abrir SQL."""
    return _verify(backup)


def create_backup(project_root: Path = PROJECT_ROOT, destination: Path | None = None) -> dict:
    root = _safe_path(project_root)
    _regular(root, directory=True)
    configuration = _configuration(root)
    assert_project_stopped(root, configuration)
    last_check = time.monotonic()

    def checkpoint() -> None:
        nonlocal last_check
        if time.monotonic() - last_check >= 2:
            assert_project_stopped(root, configuration)
            last_check = time.monotonic()

    directories, files = _inventory(root, source=True, checkpoint=checkpoint)
    _validate_set(directories, files)
    backup_root = root / "knowledge-base/backups"
    if destination is None:
        if not backup_root.exists():
            _safe_path(backup_root, exists=False).mkdir(mode=0o700)
        destination = backup_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = _safe_path(destination, exists=False)
    if destination == root or destination in root.parents:
        raise BackupError("El destino no puede contener el origen.")
    if destination.is_relative_to(root) and destination.parent != backup_root:
        raise BackupError("Dentro del proyecto solo se permite knowledge-base/backups/NombreNuevo.")
    if destination.exists():
        raise FileExistsError("El destino debe ser una carpeta nueva.")
    assert_project_stopped(root, configuration)
    destination.mkdir(mode=0o700)
    marker = destination / INCOMPLETE
    with marker.open("x", encoding="utf-8") as output:
        output.write("Respaldo parcial: no restaurar ni activar. Solo un finalizado correcto elimina este marcador.\n")
    payload = destination / PAYLOAD
    payload.mkdir(mode=0o700)
    for relative in directories:
        _safe_path(payload / relative, exists=False).mkdir(mode=0o700)
    for entry in files:
        _copy_file(root / entry["path"], payload / entry["path"], entry, checkpoint)
    assert_project_stopped(root, configuration)
    if _inventory(root, source=True, checkpoint=checkpoint) != (directories, files):
        raise BackupError("La instalacion cambio durante la copia; el respaldo permanece incompleto.")
    manifest = {
        "schema": SCHEMA, "state": "complete", "created_at_utc": datetime.now(UTC).isoformat(),
        "source_root": str(root), "directories": directories, "files": files,
        "excluded_trees": list(EXCLUDED_TREES), "excluded_code_cache_names": sorted(CODE_CACHES),
        "consistency": "cold_native_datadir_and_matching_runtime; source_and_copy_sha256_verified",
        "mysql_runtime_version": configuration["mysql_runtime_version"], "mysql_recovery_tested": False,
        "runtime_binaries_included": True, "automatic_restore_supported": False,
        "configured_service_ports": configuration["ports"], "external_services": configuration["external_services"],
        "restore_instructions": RESTORE_INSTRUCTIONS,
    }
    with (destination / MANIFEST).open("x", encoding="utf-8") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(destination / MANIFEST, 0o600)
    _verify(destination, incomplete=True)
    assert_project_stopped(root, configuration)
    # El unico borrado es nuestro marcador nuevo; no se modifica ningun dato de origen.
    marker.unlink()
    return {**manifest, "backup_path": str(destination)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    backup = commands.add_parser("backup", help="Copiar una instalacion nativa completamente detenida.")
    backup.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    backup.add_argument("--destination", type=Path)
    verify = commands.add_parser("verify", help="Verificar integridad sin iniciar servicios ni abrir SQL.")
    verify.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = (create_backup(args.project_root, args.destination) if args.action == "backup"
                  else verify_backup(args.backup))
        print(json.dumps({"ok": True, "action": args.action, "file_count": len(result["files"]),
                          "backup_path": result.get("backup_path", str(args.backup) if args.action == "verify" else ""),
                          "mysql_recovery_tested": False, "activation": "not_performed",
                          "restore": "manual_new_folder_only; see restore_instructions in installation-backup.json"}))
        return 0
    except (OSError, ValueError, TypeError, configparser.Error, psutil.Error) as error:
        message = (str(error) if isinstance(error, BackupError)
                   else "Respaldo rechazado: ruta, integridad o metadatos invalidos.")
        print(json.dumps({"ok": False, "action": args.action, "message": message,
                          "activation": "not_performed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
