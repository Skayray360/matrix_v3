# Creado por Aldo Garcia.
"""Traslado de estado local entre entregas completas, sin copiar codigo viejo."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from scripts.preflight import PROJECT_ROOT, PreflightReport, check_integrity

PATH_KEYS = {"RAG_KNOWLEDGE_ROOT", "QDRANT_PATH", "UPLOAD_STORAGE_ROOT"}
TRANSIENT_FILES = {"matrixrh-backend.pid", "matrixrh-backend.stop"}


def _redirected(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _manifest(root: Path) -> dict[str, str]:
    manifest = root / "SHA256SUMS.txt"
    if not manifest.is_file() or _redirected(manifest):
        raise ValueError("La entrega requiere SHA256SUMS.txt regular.")
    result = {}
    for line in manifest.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        digest, relative = line.split("  ", 1)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest) or relative in result:
            raise ValueError("Manifiesto invalido o duplicado.")
        normalized = Path(relative.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("Ruta invalida en manifiesto.")
        result[normalized.as_posix()] = digest.lower()
    return result


def _files(root: Path, folder: str) -> list[tuple[Path, str]]:
    base = root / folder
    if _redirected(base):
        raise ValueError("El estado no debe redirigirse mediante enlaces o junctions.")
    if not base.exists():
        return []
    if not base.is_dir():
        raise ValueError("Las carpetas de estado deben ser directorios locales regulares.")
    found = []
    for directory, dirs, names in os.walk(base, followlinks=False):
        for name in dirs + names:
            if _redirected(Path(directory) / name):
                raise ValueError("El estado contiene enlaces; revise el traslado manual de esas rutas.")
        dirs[:] = [name for name in dirs if name not in {"__pycache__", ".pytest_cache"}]
        for name in names:
            path = Path(directory) / name
            if path.name not in TRANSIENT_FILES:
                found.append((path, path.relative_to(root).as_posix()))
    return found


def rebase_environment(original: str, previous: Path, current: Path) -> tuple[str, list[str]]:
    lines = original.splitlines(keepends=True)
    changed = []
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*(?:export\s+)?([A-Z_]+)\s*=\s*)(.*?)(\r?\n)?$", line, re.IGNORECASE)
        if not match or match[2].upper() not in PATH_KEYS:
            continue
        raw = match[3].strip()
        quote = ""
        comment = ""
        if raw.startswith(("'", '"')):
            quoted = re.fullmatch(r"(['\"])(.*?)\1(\s*(?:#.*)?)", raw)
            if not quoted:
                raise ValueError("Una ruta de almacenamiento tiene comillas incompletas.")
            quote, value, comment = quoted.groups()
        else:
            unquoted = re.fullmatch(r"(.*?)(\s+#.*)?", raw)
            assert unquoted is not None
            value, comment = unquoted[1].strip(), unquoted[2] or ""
        if not os.path.isabs(value):
            continue
        old = os.path.abspath(previous)
        configured = os.path.abspath(value)
        try:
            belongs = os.path.normcase(os.path.commonpath((old, configured))) == os.path.normcase(old)
        except ValueError:
            belongs = False
        if belongs:
            mapped = str(current / os.path.relpath(configured, old))
            lines[index] = f"{match[1]}{quote}{mapped}{quote}{comment}{match[4] or ''}"
            changed.append(match[2].upper())
    return "".join(lines), changed


def transfer_state(previous: Path, current: Path) -> dict[str, object]:
    if _redirected(previous) or _redirected(current):
        raise ValueError("Las raices de entrega no deben ser enlaces.")
    previous, current = previous.resolve(), current.resolve()
    if previous == current or previous.is_relative_to(current) or current.is_relative_to(previous):
        raise ValueError("Use dos carpetas independientes; no mezcle entregas.")
    report = PreflightReport()
    check_integrity(report, root=current, require_manifest=True)
    if not report.ok:
        raise ValueError("La entrega nueva no pasa integridad; extraiga el ZIP completo.")
    old_manifest, new_manifest = _manifest(previous), _manifest(current)
    source_env, target_env = previous / ".env", current / ".env"
    if not source_env.is_file() or _redirected(source_env):
        raise ValueError("La entrega anterior debe contener .env regular.")
    if target_env.exists() or _redirected(target_env):
        raise ValueError("La carpeta nueva ya contiene .env; no se sobrescribe estado existente.")
    # Primero comprobar TODO el destino; no descubrir una colision a media copia.
    for folder in ("data", "var", "config"):
        for path, relative in _files(current, folder):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if new_manifest.get(relative) != digest:
                raise ValueError("El destino ya tiene estado o configuracion modificada; use una extraccion vacia.")
    copies = []
    preserved_readmes = []
    readme_backup = f"var/backups/transfer-readmes/1.2.8-{uuid4().hex}"
    for source, relative in _files(previous, "data") + _files(previous, "var"):
        # Los README incluidos en ambas entregas son documentacion distribuida,
        # no corpus: el codigo nuevo conserva sus instrucciones. Una nota local
        # personalizada se respalda byte por byte en vez de perderse.
        if source.name.casefold() == "readme.md" and relative in old_manifest and relative in new_manifest:
            if hashlib.sha256(source.read_bytes()).hexdigest() != old_manifest[relative]:
                copies.append((source, f"{readme_backup}/{relative}"))
                preserved_readmes.append(relative)
            continue
        copies.append((source, relative))
    custom_config = []
    for path, relative in _files(previous, "config"):
        if path.suffix.lower() in {".yaml", ".yml"}:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if old_manifest.get(relative) != digest:
                copies.append((path, relative))
                custom_config.append(relative)
    content, rebased = rebase_environment(source_env.read_bytes().decode("utf-8-sig"), previous, current)
    for source, relative in copies:
        destination = current / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copy2(source_env, target_env)
    if rebased:
        backup = current / "var" / "backups" / "configuration" / "env-before-path-transfer-1.2.8.bak"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_env, backup)
        with target_env.open("w", encoding="utf-8", newline="") as stream:
            stream.write(content)
    # No se copian PID/stop de la entrega anterior, incluso si estaban en el ZIP.
    for name in TRANSIENT_FILES:
        (current / "var" / name).unlink(missing_ok=True)
    return {"copied_files": len(copies), "custom_config": custom_config, "rebased_keys": rebased,
            "preserved_readmes": preserved_readmes}


def main() -> int:
    parser = argparse.ArgumentParser(description="Traslada estado local a una entrega completa nueva.")
    parser.add_argument("--previous-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = transfer_state(args.previous_root, PROJECT_ROOT)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Traslado detenido ({type(error).__name__}); revise carpetas, permisos e integridad. "
              "La carpeta anterior se conserva.")
        return 1
    print(f"Estado trasladado: {result['copied_files']} archivos. No se copio codigo ni entorno virtual.")
    custom_config, rebased_keys = result["custom_config"], result["rebased_keys"]
    assert isinstance(custom_config, list) and isinstance(rebased_keys, list)
    print("YAML personalizados conservados: " + str(len(custom_config)))
    preserved_readmes = result["preserved_readmes"]
    assert isinstance(preserved_readmes, list)
    print("README personalizados respaldados: " + str(len(preserved_readmes)))
    print("Rutas locales adaptadas: " + (", ".join(rebased_keys) or "ninguna"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
