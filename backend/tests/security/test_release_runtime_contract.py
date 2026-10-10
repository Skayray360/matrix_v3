# Creado por Aldo Garcia.
"""Contrato entre la entrega, sus cuatro entradas y el runtime nativo aislado."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from scripts.package_release import iter_package_files, verify_release_layout
from scripts.preflight import CORE_FILES, OPERATOR_BATCH_FILES
from scripts.uv_runtime import compatible_uv_version

ROOT = Path(__file__).resolve().parents[3]
CONTROLLER = ROOT / "backend/scripts/windows/MatrixRH.ps1"


def test_instalador_python_sincroniza_lock_congelado_en_entorno_propio():
    script = CONTROLLER.read_text(encoding="utf-8")
    command = next(line for line in script.splitlines() if "Invoke-Checked $uv @('sync'" in line)
    for flag in ("--frozen", "--no-dev", "--no-editable", "--managed-python", "--no-python-downloads"):
        assert f"'{flag}'" in command
    assert "UV_PROJECT_ENVIRONMENT=(Join-Path $script:Runtime 'venv')" in script
    assert "pip install" not in script.lower()
    # El equipo destino reutiliza el build entregado; no resuelve paquetes de Node.
    assert "frontend\\dist\\index.html" in script
    assert not re.search(r"\bnpm (?:install|ci|run build)\b", script)


def test_runtime_manifest_fija_descargas_windows_x64_con_checksum():
    manifest = json.loads((ROOT / "backend/config/runtime-manifest.json").read_text())
    assert manifest["platform"] == "windows-x86_64"
    for package in ("uv", "python"):
        entry = manifest[package]
        assert re.fullmatch("[0-9a-f]{64}", entry["sha256"])
        url = urlsplit(entry["url"])
        assert url.scheme == "https" and url.hostname == "github.com"
        assert "windows" in url.path and entry["version"] in url.path
    assert manifest["ollama"]["management"] == "external_local"
    assert manifest["ollama"]["downloads_allowed"] is False
    assert "url" not in manifest["ollama"]
    assert manifest["python"]["version"].startswith("3.12.")
    assert "backend/config/runtime-manifest.json" in CORE_FILES
    assert ".htaccess" in CORE_FILES


def test_uv_metadata_permite_versiones_soportadas_y_rechaza_plataforma_distinta():
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


def test_cada_bat_invoca_accion_valida_y_archivo_entregado_con_lineas_cmd_reales():
    script = CONTROLLER.read_text(encoding="utf-8")
    for name in OPERATOR_BATCH_FILES:
        raw = (ROOT / name).read_bytes()
        assert len(raw.splitlines()) > 10 and b"\\r\\n" not in raw
        content = raw.decode("utf-8")
        action = Path(name).stem
        assert f"-Action {action}" in content
        assert f"'{action}'" in script
        assert '%~dp0backend\\scripts\\windows\\MatrixRH.ps1' in content
        assert "%ERRORLEVEL%" in content and "exit /b %MATRIX_EXIT%" in content


def test_release_limpio_contiene_build_y_denegacion_wamp():
    assert (ROOT / "frontend/dist/index.html").is_file()
    protection = (ROOT / ".htaccess").read_text(encoding="utf-8").lower()
    assert "require all denied" in protection and "deny from all" in protection
    selected = iter_package_files(ROOT)
    assert not verify_release_layout(selected, ROOT)
    assert {path.relative_to(ROOT).parts[0] for path in selected if len(path.relative_to(ROOT).parts) > 1} == {
        "backend", "frontend", "knowledge-base",
    }


def test_native_installer_never_downloads_or_launches_ollama():
    script = CONTROLLER.read_text(encoding="utf-8")
    assert "Install-ZipRuntime 'ollama'" not in script
    assert "Start-Owned 'ollama'" not in script
    assert "Invoke-Ollama" not in script
    assert "/api/pull" not in script
    assert "OLLAMA_MODELS=" not in script
    assert "OLLAMA_HOST=" not in script
    assert "OLLAMA_BASE_URL=http://127.0.0.1:11434" in (ROOT / "backend/config/env.example").read_text()


def test_mysql_is_pinned_and_independent_of_wamp_database_components():
    manifest = json.loads((ROOT / "backend/config/runtime-manifest.json").read_text())
    mysql = manifest["mysql"]
    assert re.fullmatch(r"8\.4\.\d+", mysql["version"])
    assert mysql["archive_root"] == "mysql-" + mysql["version"] + "-winx64"
    assert mysql["archive_name"] == mysql["archive_root"] + ".zip"
    assert urlsplit(mysql["url"]).hostname == "cdn.mysql.com"
    assert mysql["url"].startswith("https://")
    assert re.fullmatch("[0-9a-f]{64}", mysql["sha256"])
    assert mysql["signature_verified_at_build"] is True
    assert mysql["signing_key_fingerprint"] == "BCA43417C3B485DD128EC6D4B7B3B788A8D3785C"
    assert set(mysql["binary_sha256"]) == {"mysqld.exe", "mysqladmin.exe"}
    assert all(re.fullmatch("[0-9a-f]{64}", value) for value in mysql["binary_sha256"].values())
    script = CONTROLLER.read_text(encoding="utf-8")
    assert "Join-Path $Wamp 'bin\\mysql'" not in script
    assert "Install-MySql $wamp" not in script
    assert "'cdn.mysql.com'" in script
