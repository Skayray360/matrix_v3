# Creado por Aldo Garcia.
"""Verifica destinos de enlaces locales en la documentacion distribuible.

No realiza peticiones de red ni ejecuta comandos incluidos en las guias.
Los fragmentos #ancla no se validan; se comprueba el archivo de destino.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
_FENCES = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_LINKS = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[\"'][^\n]*?[\"'])?\s*\)")
_DATA_PREFIXES = (
    "knowledge-base/", "backend/tests/fixtures/", "backend/runtime/",
    "backend/logs/", "backend/config/secrets/", "data/",
)


def _project_markdown(root: Path, source: Path, *, workspace: bool = False) -> bool:
    relative = source.relative_to(root).as_posix()
    return (
        source.suffix.lower() == ".md"
        and not relative.startswith(_DATA_PREFIXES)
        and not (workspace and relative.startswith("reports/"))
    )


def check_readme_layout(root: Path, files: list[Path]) -> list[str]:
    """La entrega tiene una guia operativa unica, sin reatribuir documentos."""
    readmes = [
        path.relative_to(root).as_posix() for path in files
        if _project_markdown(root, path) and path.name.casefold() == "readme.md"
    ]
    errors = [] if "README.md" in readmes else ["Falta README.md en la raiz del paquete."]
    errors.extend(f"README adicional fuera de la guia unica: {path}" for path in readmes if path != "README.md")
    return errors


def check_links(root: Path, files: list[Path], *, workspace: bool = False) -> list[str]:
    """Comprueba enlaces Markdown simples contra el contenido seleccionado."""
    root = root.resolve()
    selected = {path.resolve() for path in files}
    errors: list[str] = []
    for source in files:
        if not _project_markdown(root, source, workspace=workspace):
            continue
        relative = source.relative_to(root).as_posix()
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            errors.append(f"{relative}: la guia debe ser un archivo local regular.")
            continue
        content = _FENCES.sub("", source.read_text(encoding="utf-8-sig"))
        for match in _LINKS.finditer(content):
            raw = match.group(1) or match.group(2)
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (source.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(root):
                errors.append(f"{relative}: enlace fuera del paquete: {raw}")
            elif workspace and target.is_relative_to(root / "reports") and target.is_file():
                # Un informe historico referenciado puede existir en Git sin
                # pertenecer al paquete distribuible (p. ej. JSON de pruebas).
                continue
            elif target not in selected and not (
                target.is_dir() and any(path.is_relative_to(target) for path in selected)
            ):
                errors.append(f"{relative}: destino no distribuido: {raw}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--workspace", action="store_true",
        help="valida guias activas del checkout y sus destinos; no audita el interior de snapshots reports",
    )
    args = parser.parse_args(argv)
    from scripts.package_release import iter_package_files

    root = args.root.resolve()
    files = iter_package_files(root)
    errors = check_readme_layout(root, files) + check_links(root, files, workspace=args.workspace)
    markdown_files = sum(
        _project_markdown(root, p, workspace=args.workspace) for p in files
    )
    print(json.dumps({"ok": not errors, "scope": "active_workspace" if args.workspace else "distributed_package",
                      "markdown_files": markdown_files,
                      "errors": errors}, indent=2, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
