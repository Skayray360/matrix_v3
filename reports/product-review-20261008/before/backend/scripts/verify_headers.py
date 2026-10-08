# Creado por Aldo Garcia.
"""Verifica el encabezado de autoria obligatorio (requisito 16).

Cada archivo de codigo propio debe llevar la leyenda exacta correspondiente a su
lenguaje dentro de las primeras lineas del archivo.

Se excluyen las carpetas generadas o de terceros: ``.git``, ``.venv``,
``node_modules``, ``dist``, ``build``, caches, cobertura, temporales,
almacenamiento runtime y volumenes de Qdrant (requisito 19).

Uso:
    python -m scripts.verify_headers
    python -m scripts.verify_headers --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import PROJECT_ROOT

#: Leyenda por lenguaje, tal como exige la especificacion.
EXPECTED: dict[str, str] = {
    ".py": "# Creado por Aldo Garcia.",
    ".ts": "/* Creado por Aldo Garcia. */",
    ".tsx": "/* Creado por Aldo Garcia. */",
    ".js": "/* Creado por Aldo Garcia. */",
    ".jsx": "/* Creado por Aldo Garcia. */",
    ".css": "/* Creado por Aldo Garcia. */",
    ".sql": "-- Creado por Aldo Garcia.",
    ".ps1": "# Creado por Aldo Garcia.",
    ".bat": "REM Creado por Aldo Garcia.",
    ".sh": "# Creado por Aldo Garcia.",
    ".yml": "# Creado por Aldo Garcia.",
    ".yaml": "# Creado por Aldo Garcia.",
    ".conf": "# Creado por Aldo Garcia.",
    ".md": "Creado por Aldo Garcia.",
    ".html": "Creado por Aldo Garcia.",
}

#: Archivos sin extension que tambien llevan encabezado.
BY_NAME: dict[str, str] = {
    "Dockerfile": "# Creado por Aldo Garcia.",
    ".env.example": "# Creado por Aldo Garcia.",
    ".gitignore": "# Creado por Aldo Garcia.",
}

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
        "var",
        "qdrant",
        "playwright-report",
        "test-results",
        "matrix_rh_backend.egg-info",
        ".playwright",
    }
)

#: Archivos generados o de terceros que no son codigo propio.
EXCLUDED_NAMES = frozenset(
    {"package-lock.json", "pnpm-lock.yaml", "package.json", "tsconfig.json", "tsconfig.node.json"}
)

#: Rutas excluidas por naturaleza del contenido, no por ser generadas.
#:
#: `data/knowledge` contiene el CORPUS documental, no codigo. Insertar una linea
#: de autoria en una politica de RH seria doblemente incorrecto: atribuiria a un
#: desarrollador la autoria de un documento corporativo, y esa linea acabaria
#: indexada como un chunk mas del RAG. Los README de esas carpetas si llevan
#: encabezado, porque esos si son documentacion del proyecto.
# El corpus sintetico conserva sus bytes/anotaciones al separarse del corpus activo.
EXCLUDED_PREFIXES = ("data/knowledge/", "data/synthetic_test_data/knowledge/")

#: Numero de lineas iniciales en las que se busca la leyenda. Es holgado para
#: admitir shebangs, `@echo off`, `<!doctype html>` y directivas de formato.
HEADER_WINDOW = 8


def iter_candidates(root: Path):  # noqa: ANN201
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.name in EXCLUDED_NAMES:
            continue
        relative = path.relative_to(root).as_posix()
        # Los alias corporativos bajo data/ tambien contienen documentos, no
        # codigo escrito por el autor del proyecto. Nunca modificar su autoria.
        if path.suffix.lower() == ".md" and path.name.lower() != "readme.md" and relative.startswith("data/"):
            continue
        if path.name.lower() != "readme.md" and relative.startswith(EXCLUDED_PREFIXES):
            continue
        if path.name in BY_NAME or path.suffix.lower() in EXPECTED:
            yield path


def expected_legend(path: Path) -> str:
    return BY_NAME.get(path.name) or EXPECTED[path.suffix.lower()]


def check(root: Path | None = None) -> tuple[list[str], int]:
    """Devuelve (archivos sin encabezado, total revisado)."""
    base = root or PROJECT_ROOT
    missing: list[str] = []
    total = 0

    for path in iter_candidates(base):
        total += 1
        legend = expected_legend(path)
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                head = "".join(next(handle, "") for _ in range(HEADER_WINDOW))
        except OSError:
            missing.append(path.relative_to(base).as_posix())
            continue
        if legend not in head:
            missing.append(path.relative_to(base).as_posix())
    return missing, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verifica los encabezados de autoria")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", default="")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else PROJECT_ROOT
    missing, total = check(root)

    if args.json:
        print(json.dumps({"total": total, "missing": missing, "ok": not missing}, indent=2))
    else:
        print(f"Archivos revisados: {total}")
        if missing:
            print(f"Sin encabezado ({len(missing)}):")
            for item in missing:
                print(f"  - {item}")
        print("ENCABEZADOS DE AUTORIA:", "OK" if not missing else "FALTAN")
    return 0 if not missing else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
