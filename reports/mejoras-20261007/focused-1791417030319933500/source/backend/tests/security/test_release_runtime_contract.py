# Creado por Aldo Garcia.
"""Contrato minimo del instalador y paquete Windows funcional."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.uv_runtime import compatible_uv_version

ROOT = Path(__file__).resolve().parents[3]


def test_instalador_python_usa_lock_congelado_sin_pip() -> None:
    script = (ROOT / "windows" / "Install-MatrixRH.ps1").read_text(encoding="utf-8")
    lowered = script.lower()

    assert "scripts.uv_runtime" in script
    assert '@("sync", "--frozen", "--no-dev")' in script
    assert "uv_project_environment" in lowered
    assert "pip install" not in lowered


def test_instalador_acepta_metadata_oficial_y_conserva_target_windows_x64() -> None:
    script = (ROOT / "windows" / "Install-MatrixRH.ps1").read_text(encoding="utf-8")
    assert '"--target", "x86_64-pc-windows-msvc"' in script
    for banner in (
        "uv 0.11.33 (fece32fc5 2026-07-28 x86_64-pc-windows-msvc)",
        "uv 0.12.8 (x86_64-pc-windows-msvc)",
        "uv 0.12.8 (fece32fc5 2026-07-28 x86_64-pc-windows-msvc)",
    ):
        assert compatible_uv_version(banner, expected_target="x86_64-pc-windows-msvc")
    with pytest.raises(ValueError, match="target"):
        compatible_uv_version("uv 0.12.8 (aarch64-pc-windows-msvc)", expected_target="x86_64-pc-windows-msvc")
    with pytest.raises(ValueError, match="invalida"):
        compatible_uv_version("uv 0.12.8\ncontenido inesperado")
    assert "$uvVersionLines.Count -ne 1" in script


def test_mensajes_operativos_no_recomiendan_npm_install() -> None:
    # Se veta el `npm install` sin flags (resuelve rangos y ejecuta scripts).
    # El limite de palabra evita un falso positivo con `pnpm install`, que SI es
    # el comando oficial (contiene "npm install" como subcadena).
    prohibido = re.compile(r"\bnpm install\b")
    for path in (ROOT / "windows").glob("*.ps1"):
        operational_lines = [
            line.lower()
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        assert not any(prohibido.search(line) for line in operational_lines)


def test_release_contiene_build_frontend_y_denegacion_wamp() -> None:
    assert (ROOT / "frontend" / "dist" / "index.html").is_file()
    protection = (ROOT / ".htaccess").read_text(encoding="utf-8").lower()
    assert "require all denied" in protection
    assert "deny from all" in protection
