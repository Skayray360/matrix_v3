# Creado por Aldo Garcia.
"""Execute pure supervisor flows with fake runtimes; never start Windows services."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CONTROLLER = ROOT / "backend/scripts/windows/MatrixRH.ps1"


@pytest.mark.parametrize("fixture", [
    "native_supervisor_synthetic.ps1", "native_supervisor_apache_synthetic.ps1",
    "native_supervisor_identity_synthetic.ps1", "native_supervisor_ollama_synthetic.ps1",
    "native_supervisor_mysql_synthetic.ps1", "native_supervisor_incomplete_synthetic.ps1",
])
def test_native_controller_pure_powershell_flows(fixture: str):
    powershell = os.environ.get("MATRIX_TEST_POWERSHELL") or shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is required to execute isolated supervisor functions")
    # The fixture loads function AST nodes only, mocks every runtime/service call,
    # and owns its temporary directory. It never evaluates the controller body.
    script = Path(__file__).parent / "fixtures" / fixture
    result = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-File", str(script), "-Controller", str(CONTROLLER)],
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SYNTHETIC PASS" in result.stdout


def test_four_batch_launchers_have_real_crlf_and_relative_entrypoints():
    expected = {"instalar.bat", "iniciar.bat", "detener.bat", "diagnosticar.bat"}
    assert {path.name for path in ROOT.glob("*.bat")} == expected
    for name in expected:
        data = (ROOT / name).read_bytes()
        assert b"\r\n" in data and b"\\r\\n" not in data
        assert b'%~dp0backend\\scripts\\windows\\MatrixRH.ps1' in data
        assert b'%ERRORLEVEL%' in data
        assert b'C:\\' not in data
