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


def check_links(root: Path, files: list[Path]) -> list[str]:
    """Comprueba enlaces Markdown simples contra el contenido seleccionado."""
    root = root.resolve()
    selected = {path.resolve() for path in files}
    errors: list[str] = []
    for source in files:
        if source.suffix.lower() != ".md":
            continue
        # Ejemplos de corpus son datos de prueba, no guias del operador.
        relative = source.relative_to(root).as_posix()
        if relative.startswith("data/") and source.name.casefold() != "readme.md":
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
            elif target not in selected and not (
                target.is_dir() and any(path.is_relative_to(target) for path in selected)
            ):
                errors.append(f"{relative}: destino no distribuido: {raw}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    from scripts.package_release import iter_package_files

    root = args.root.resolve()
    files = iter_package_files(root)
    errors = check_links(root, files)
    print(json.dumps({"ok": not errors, "markdown_files": sum(p.suffix.lower() == ".md" for p in files),
                      "errors": errors}, indent=2, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
