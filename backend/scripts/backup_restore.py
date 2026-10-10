# Creado por Aldo Garcia.
"""Copia y verifica un conjunto preparado en frio; restaura solo a un destino nuevo.

No exporta/importa MySQL, no detiene servicios y no acredita consistencia SQL
por copiar archivos. TI prepara el conjunto con Matrix/Qdrant detenidos y un
dump SQL consistente. No se aceptan enlaces ni se sobrescribe una instalacion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

COMPONENTS = ("configuration", "database", "knowledge", "uploads", "vector", "models")
MANIFEST = "backup-manifest.json"


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _root(path: Path) -> Path:
    if path.is_symlink() or path.absolute() != path.resolve():
        raise ValueError("No se admiten rutas mediante enlaces simbolicos.")
    return path.resolve()


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("El manifiesto contiene una ruta no portable.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"..", "."} for part in path.parts) or str(path) != value:
        raise ValueError("El manifiesto contiene una ruta fuera del conjunto.")
    return value


def _inventory(root: Path) -> tuple[list[str], list[dict]]:
    directories, files = [], []
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in sorted(names + filenames):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            relative = _relative(path.relative_to(root).as_posix())
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("El conjunto contiene enlaces o archivos especiales.")
            if stat.S_ISDIR(mode):
                directories.append(relative)
            elif relative != MANIFEST:
                files.append({"path": relative, "size": path.stat().st_size, "sha256": _hash(path)})
    return sorted(directories), sorted(files, key=lambda item: item["path"])


def verify_backup(backup: Path) -> dict:
    backup = _root(backup)
    manifest_file = backup / MANIFEST
    if manifest_file.is_symlink() or not manifest_file.is_file() or manifest_file.stat().st_size > 32_000_000:
        raise ValueError("El respaldo no tiene un manifiesto regular valido.")
    manifest = json.loads(manifest_file.read_bytes())
    if manifest.get("schema_version") != 1 or manifest.get("components") != list(COMPONENTS):
        raise ValueError("Formato o componentes del respaldo no compatibles.")
    directories, files = _inventory(backup)
    if manifest.get("files") != files or manifest.get("directories") != directories:
        raise ValueError("La integridad del respaldo no coincide; no se restaura.")
    if any(component not in directories for component in COMPONENTS):
        raise ValueError("Falta un componente obligatorio del conjunto.")
    return manifest


def _copy(source: Path, destination: Path, directories: list[str], files: list[dict]) -> None:
    destination.mkdir(mode=0o700)  # Existente significa rechazo, incluso vacio.
    for relative in directories:
        (destination / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in files:
        relative = entry["path"]
        path, output = source / relative, destination / relative
        if path.is_symlink() or _hash(path) != entry["sha256"]:
            raise ValueError("El origen cambio durante la copia; el destino parcial no se acredita.")
        shutil.copyfile(path, output, follow_symlinks=False)
        if output.is_symlink():
            raise ValueError("El origen cambio a un enlace durante la copia.")
        os.chmod(output, 0o600)
        if output.is_symlink() or output.stat().st_size != entry["size"] or _hash(output) != entry["sha256"]:
            raise ValueError("La copia no coincide; el destino parcial no se acredita.")


def create_backup(prepared: Path, backup: Path) -> dict:
    prepared, backup = _root(prepared), _root(backup)
    if not prepared.is_dir() or backup == prepared or prepared in backup.parents or backup in prepared.parents:
        raise ValueError("Origen y respaldo deben ser directorios separados.")
    if (prepared / MANIFEST).exists():
        raise ValueError("El origen preparado no debe incluir un manifiesto de respaldo anterior.")
    directories, files = _inventory(prepared)
    if any(component not in directories for component in COMPONENTS):
        raise ValueError("Prepare configuration, database, knowledge, uploads, vector y models.")
    if not any(entry["path"].startswith("database/") for entry in files):
        raise ValueError("El componente database requiere un respaldo consistente preparado por TI.")
    manifest = {"schema_version": 1, "created_at_utc": datetime.now(UTC).isoformat(),
                "components": list(COMPONENTS), "directories": directories, "files": files,
                "consistency": "operator_prepared_cold_set; database_restore_not_verified_by_copy"}
    _copy(prepared, backup, directories, files)
    # Si el origen se modifico, conservar copia parcial pero nunca emitir manifiesto valido.
    if _inventory(prepared) != (directories, files):
        raise ValueError("El conjunto preparado cambio durante el respaldo.")
    with (backup / MANIFEST).open("x", encoding="utf-8") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
    os.chmod(backup / MANIFEST, 0o600)
    return verify_backup(backup)


def restore_backup(backup: Path, destination: Path) -> dict:
    backup, destination = _root(backup), _root(destination)
    if destination == backup or backup in destination.parents or destination in backup.parents:
        raise ValueError("El destino debe estar separado del respaldo.")
    manifest = verify_backup(backup)
    _copy(backup, destination, manifest["directories"], manifest["files"])
    if _inventory(destination) != (manifest["directories"], manifest["files"]):
        raise ValueError("El conjunto restaurado no coincide; no debe activarse.")
    return {"restored": True, "file_count": len(manifest["files"]), "activation": "not_performed"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create")
    create.add_argument("--prepared", type=Path, required=True)
    create.add_argument("--backup", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "create":
            result = create_backup(args.prepared, args.backup)
        elif args.action == "verify":
            result = verify_backup(args.backup)
        else:
            result = restore_backup(args.backup, args.destination)
        print(json.dumps({"ok": True, "action": args.action,
                          "file_count": len(result["files"]) if "files" in result else result["file_count"],
                          "database_restore": "not_performed", "activation": "not_performed"}))
        return 0
    except (OSError, ValueError, TypeError):
        print(json.dumps({"ok": False, "action": args.action,
                          "message": "Conjunto incompleto, alterado o ruta insegura/existente; "
                                     "no se activa el destino."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
