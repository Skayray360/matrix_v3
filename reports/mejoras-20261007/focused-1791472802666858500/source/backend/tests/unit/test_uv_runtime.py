# Creado por Aldo Garcia.
"""Regresiones de la version real de uv y el chequeo ejecutable antes de instalar paquetes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.uv_runtime import compatible_uv_version

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("banner", [
    "uv 0.11.33 (fece32fc5 2026-07-28 x86_64-pc-windows-msvc)",
    "uv 0.11.33",
    "uv 0.11.34 (x86_64-pc-windows-msvc)",
    "uv 0.12.0",
    "uv 0.12.8 (fece32fc5 2026-07-28)",
    "uv 0.12.18 (x86_64-pc-windows-msvc)",
])
def test_acepta_versiones_estables_y_su_metadata_oficial(banner):
    assert compatible_uv_version(banner, expected_target="x86_64-pc-windows-msvc") == banner.split()[1]


@pytest.mark.parametrize("banner", [
    "uv 0.11.32", "uv 0.13.0", "uv 1.0.0", "uv 0.12.8rc1", "uv 0.12.8+custom",
    "uv 0.11.33\ncontenido inesperado", "uv 0.11.33 (metadata desconocida)",
    "uv 0.11.33 (aarch64-pc-windows-msvc)", "uv 0.11.33 (x86_64-unknown-linux-gnu)",
])
def test_rechaza_ramas_no_validadas_formato_ambiguo_y_targets_ajenos(banner):
    with pytest.raises(ValueError):
        compatible_uv_version(banner, expected_target="x86_64-pc-windows-msvc")


@pytest.mark.parametrize(("banner", "code"), [
    ("uv 0.11.33 (fece32fc5 2026-07-28 x86_64-pc-windows-msvc)", 0),
    ("uv 0.11.32", 1),
    ("uv 0.11.33 (aarch64-pc-windows-msvc)", 1),
])
def test_cli_sin_paquetes_valida_el_banner_del_operador_y_devuelve_codigo(banner, code):
    result = subprocess.run(
        [sys.executable, "-S", "-m", "scripts.uv_runtime", "--banner", banner,
         "--target", "x86_64-pc-windows-msvc"],
        cwd=ROOT / "backend", capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == code
    assert "Traceback" not in result.stderr
    assert ("[ OK ]" if code == 0 else "[FAIL]") in result.stdout
