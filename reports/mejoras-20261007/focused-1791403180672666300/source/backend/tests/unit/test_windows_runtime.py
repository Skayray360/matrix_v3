# Creado por Aldo Garcia.
"""Contratos ejecutados en PowerShell; no manipulan tareas o servicios del equipo.

En Linux requieren pwsh. En Windows usan powershell/pwsh y los cmdlets del
sistema se sustituyen solamente en los escenarios aislados de tareas/instalacion.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import preflight

ROOT = Path(__file__).resolve().parents[3]
PWSH = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.unit


@pytest.fixture
def windows_project(tmp_path: Path):
    root = tmp_path / "Matrix RH con espacios"
    shutil.copytree(ROOT / "windows", root / "windows")
    backend = root / "backend/scripts"
    backend.mkdir(parents=True)
    (backend / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "backend/scripts/preflight.py", backend / "preflight.py")
    for name in preflight.CORE_FILES | {"backend/app/__init__.py", "backend/app/main.py", "backend/scripts/bootstrap.py"}:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    files = [path for path in root.rglob("*") if path.is_file()]
    (root / "SHA256SUMS.txt").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
        for path in files
    ), encoding="utf-8")
    return root


def run_powershell(root: Path, source: str, *, extra_env: dict[str, str] | None = None):
    if PWSH is None:
        pytest.skip("PowerShell no instalado; este contrato se ejecuta en el job de Windows o con pwsh")
    env = dict(os.environ)
    for name in (
        "APP_HOST", "APP_PORT", "DATABASE_URL", "OLLAMA_BASE_URL", "QDRANT_MODE", "QDRANT_URL",
        "LLM_PROVIDER", "LLM_DEEP_PROVIDER", "LLM_EMBEDDING_PROVIDER", "LLM_API_BASE_URL",
        "APP_ENV", "MATRIX_SEED_PASSWORD", "LOCAL_TEST_SEED_USERS_ENABLED",
        "OLLAMA_HOST", "OLLAMA_NO_CLOUD",
    ):
        env.pop(name, None)
    env["MATRIX_HARNESS_ROOT"] = str(root)
    env["MATRIX_HARNESS_PYTHON"] = sys.executable
    if extra_env:
        env.update(extra_env)
    harness = root / "harness.ps1"
    harness.write_text(
        "$ErrorActionPreference='Stop'\n[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)\n"
        ". (Join-Path $env:MATRIX_HARNESS_ROOT 'windows/Common-MatrixRH.ps1')\n"
        + source,
        encoding="utf-8-sig",
    )
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True, text=True, check=False, timeout=30, env=env, cwd=root,
    )


def last_json(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_scripts_powershell_se_pueden_analizar(windows_project):
    result = run_powershell(windows_project, """
$parseFailures = @()
Get-ChildItem (Join-Path $env:MATRIX_HARNESS_ROOT 'windows/*.ps1') | ForEach-Object {
    $tokens=$null; $errors=$null
    $null=[System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$tokens,[ref]$errors)
    $parseFailures += @($errors | ForEach-Object { $_.Message })
}
@{ errors=@($parseFailures) } | ConvertTo-Json -Compress
""")
    assert last_json(result)["errors"] == []


def test_lee_env_utf8_comillas_y_prioridad_del_proceso(windows_project):
    env_file = windows_project / ".env"
    env_file.write_text(
        'APP_HOST="0.0.0.0" # LAN\nAPP_PORT="8192" # servidor\n'
        'export ETIQUETA="Información México"\n', encoding="utf-8",
    )
    previous = env_file.read_bytes()
    result = run_powershell(windows_project, """
@{ url=(Get-MatrixBackendUrl); etiqueta=(Get-MatrixEnvValue -Key 'ETIQUETA') } | ConvertTo-Json -Compress
""", extra_env={"APP_PORT": "8193"})
    assert last_json(result) == {"url": "http://127.0.0.1:8193", "etiqueta": "Información México"}
    assert env_file.read_bytes() == previous


def test_actualizacion_env_conserva_unicode_y_dolares_literales(windows_project):
    env_file = windows_project / ".env"
    env_file.write_text('ETIQUETA=Información México\nexport DATABASE_URL = "anterior"\n', encoding="utf-8")
    result = run_powershell(windows_project, """
$ok = Set-MatrixEnvValue -Key 'DATABASE_URL' -Value 'mysql+pymysql://demo:$1$&@localhost/matrix_rh_app' # secrets-scan: allow (DSN sintetico que comprueba dolares literales)
@{ ok=$ok; etiqueta=(Get-MatrixEnvValue -Key 'ETIQUETA'); url=(Get-MatrixEnvValue -Key 'DATABASE_URL') } | ConvertTo-Json -Compress
""")
    assert last_json(result) == {
        "ok": True, "etiqueta": "Información México",
        "url": "mysql+pymysql://demo:$1$&@localhost/matrix_rh_app",  # secrets-scan: allow (DSN sintetico esperado de la prueba)
    }
    assert not env_file.read_bytes().startswith(b"\xef\xbb\xbf")


def test_creacion_de_env_genera_clave_y_preserva_archivo_existente(windows_project):
    (windows_project / ".env.example").write_text("APP_SECRET_KEY=\nETIQUETA=Información México\n", encoding="utf-8")
    result = run_powershell(windows_project, "@{ ok=(Test-MatrixEnvFile) } | ConvertTo-Json -Compress\n")
    assert last_json(result) == {"ok": True}
    env_file = windows_project / ".env"
    previous = env_file.read_bytes()
    content = previous.decode("utf-8")
    secret = content.split("APP_SECRET_KEY=", 1)[1].splitlines()[0]
    assert len(secret) == 64 and all(char in "0123456789abcdef" for char in secret)
    assert "Información México" in content
    again = run_powershell(windows_project, "@{ ok=(Test-MatrixEnvFile) } | ConvertTo-Json -Compress\n")
    assert last_json(again) == {"ok": True}
    assert env_file.read_bytes() == previous


def test_seed_password_se_genera_una_vez_no_se_publica_y_no_cambia_env(windows_project):
    env_file = windows_project / ".env"
    env_file.write_text("APP_ENV=development\nLOCAL_TEST_SEED_USERS_ENABLED=true\nETIQUETA=Información\n", encoding="utf-8")
    first = run_powershell(windows_project, "@{ ok=(Initialize-MatrixSeedPassword) } | ConvertTo-Json -Compress\n")
    assert last_json(first) == {"ok": True}
    previous = env_file.read_bytes()
    password = previous.decode().split("MATRIX_SEED_PASSWORD=", 1)[1].splitlines()[0]
    assert len(password) == 64
    assert password not in first.stdout + first.stderr
    again = run_powershell(windows_project, "@{ ok=(Initialize-MatrixSeedPassword) } | ConvertTo-Json -Compress\n")
    assert last_json(again) == {"ok": True}
    assert env_file.read_bytes() == previous
    assert password not in again.stdout + again.stderr


@pytest.mark.parametrize("operation", ["Install", "Start"])
def test_paquete_cruzado_no_modifica_instalacion_ni_intenta_ingesta(windows_project, operation):
    (windows_project / "backend/app/__init__.py").write_text(
        '"""Acceso controlado a bases de datos."""\nfrom app.structured_data.tool import StructuredDataTool\n',
        encoding="utf-8",
    )
    option = "-SkipFrontend" if operation == "Install" else "-NoBrowser"
    result = run_powershell(windows_project, f"& (Join-Path $env:MATRIX_HARNESS_ROOT 'windows/{operation}-MatrixRH.ps1') {option}\nexit $LASTEXITCODE\n")
    assert result.returncode == 1
    assert "no declara la version" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert not (windows_project / ".env").exists()
    assert not (windows_project / ".venv").exists()
    assert not (windows_project / "var").exists()


def test_url_ipv6_usa_corchetes_y_direccion_local_para_escucha_total(windows_project):
    result = run_powershell(windows_project, "@{ url=(Get-MatrixBackendUrl) } | ConvertTo-Json -Compress\n",
                            extra_env={"APP_HOST": "::"})
    assert last_json(result)["url"] == "http://[::1]:8000"


def test_sonda_de_puerto_distingue_listener_y_libera_el_puerto(windows_project):
    result = run_powershell(windows_project, """
$listener=New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback,0)
$listener.Start()
$port=$listener.LocalEndpoint.Port
$env:APP_PORT=[string]$port
$occupied=Test-MatrixBackendPortFree
$listener.Stop()
$free=Test-MatrixBackendPortFree
$verifyListener=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,$port)
$verifyListener.Server.ExclusiveAddressUse=$true
$verifyListener.Start()
$released=$verifyListener.Server.IsBound
$verifyListener.Stop()
@{ occupied=$occupied; free=$free; released=$released } | ConvertTo-Json -Compress
""")
    assert last_json(result) == {"occupied": False, "free": True, "released": True}


@pytest.mark.parametrize(("app_host", "listen_address"), [
    ("127.0.0.1", "127.0.0.1"),
    ("0.0.0.0", "127.0.0.1"),
    ("localhost", "127.0.0.1"),
    ("::1", "::1"),
    ("::", "::1"),
])
def test_sonda_comprueba_host_real_ipv4_ipv6_y_hostname(
    windows_project, app_host, listen_address,
):
    if ":" in listen_address:
        try:
            with socket.socket(socket.AF_INET6) as probe:
                probe.bind(("::1", 0))
        except OSError:
            pytest.skip("El sistema no permite escucha IPv6 local")
    result = run_powershell(windows_project, """
$listener=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse($env:MATRIX_LISTEN_ADDRESS),0)
$listener.Server.ExclusiveAddressUse=$true
if ($listener.Server.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) {
    $listener.Server.DualMode=$false
}
$listener.Start()
$port=$listener.LocalEndpoint.Port
$env:APP_PORT=[string]$port
try { $occupied=Test-MatrixBackendPortFree } finally { $listener.Stop() }
$free=Test-MatrixBackendPortFree
$verifyListener=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse($env:MATRIX_LISTEN_ADDRESS),$port)
$verifyListener.Server.ExclusiveAddressUse=$true
if ($verifyListener.Server.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) {
    $verifyListener.Server.DualMode=$false
}
$verifyListener.Start()
$released=$verifyListener.Server.IsBound
$verifyListener.Stop()
@{ occupied=$occupied; free=$free; released=$released } | ConvertTo-Json -Compress
""", extra_env={"APP_HOST": app_host, "MATRIX_LISTEN_ADDRESS": listen_address})
    assert last_json(result) == {"occupied": False, "free": True, "released": True}


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_sonda_rechaza_puerto_invalido_sin_traceback(windows_project, port):
    result = run_powershell(windows_project, """
@{ free=(Test-MatrixBackendPortFree) } | ConvertTo-Json -Compress
""", extra_env={"APP_PORT": port})
    assert last_json(result) == {"free": False}
    assert "APP_HOST o APP_PORT no son validos" in result.stdout


@pytest.mark.parametrize("host", [" ", "[", "[::1]"])
def test_sonda_rechaza_host_invalido_sin_traceback(windows_project, host):
    result = run_powershell(windows_project, """
@{ free=(Test-MatrixBackendPortFree) } | ConvertTo-Json -Compress
""", extra_env={"APP_HOST": host})
    assert last_json(result) == {"free": False}
    assert "APP_HOST o APP_PORT no son validos" in result.stdout


def test_sonda_rechaza_host_no_local_con_error_identificable(windows_project):
    result = run_powershell(windows_project, """
@{ free=(Test-MatrixBackendPortFree) } | ConvertTo-Json -Compress
""", extra_env={"APP_HOST": "192.0.2.1"})
    assert last_json(result) == {"free": False}
    assert "no verificable" in result.stdout


def test_escucha_total_rechaza_colision_fuera_de_127_0_0_1(windows_project):
    try:
        with socket.socket(socket.AF_INET) as probe:
            probe.bind(("127.0.0.2", 0))
    except OSError:
        pytest.skip("El sistema no permite la segunda direccion loopback")
    result = run_powershell(windows_project, """
$listener=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse('127.0.0.2'),0)
$listener.Server.ExclusiveAddressUse=$true
$listener.Start()
$env:APP_PORT=[string]$listener.LocalEndpoint.Port
try { $occupied=Test-MatrixBackendPortFree } finally { $listener.Stop() }
@{ occupied=$occupied; free=(Test-MatrixBackendPortFree) } | ConvertTo-Json -Compress
""", extra_env={"APP_HOST": "0.0.0.0"})
    assert last_json(result) == {"occupied": False, "free": True}


def test_guardia_inicial_usa_stdlib_del_sistema_con_venv_incompatible(windows_project):
    executable = windows_project / ".venv/Scripts/python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture incompatible", encoding="utf-8")
    previous = executable.read_bytes()
    result = run_powershell(windows_project, """
function Get-Command {
    param($Name,$ErrorAction)
    if ($Name -eq 'py') { return $null }
    if ($Name -eq 'python') { return @{Source=$env:MATRIX_HARNESS_PYTHON} }
    return Microsoft.PowerShell.Core\\Get-Command $Name -ErrorAction $ErrorAction
}
$listener=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,0)
$listener.Start()
$env:APP_PORT=[string]$listener.LocalEndpoint.Port
$listener.Stop()
@{ free=(Test-MatrixBackendPortFree) } | ConvertTo-Json -Compress
""")
    assert last_json(result) == {"free": True}
    assert executable.read_bytes() == previous


@pytest.mark.parametrize("qdrant_mode", ["embedded", "server"])
def test_diagnostico_usa_endpoints_reales_y_no_expone_credenciales(windows_project, qdrant_mode):
    (windows_project / ".env").write_text(
        'APP_PORT=8112\nDATABASE_URL="mysql+pymysql://demo:synthetic-value@db.interno:3319/matrix_rh"\n'  # secrets-scan: allow (DSN sintetico que verifica redaccion de credenciales)
        'LLM_PROVIDER=openai_compatible\nLLM_DEEP_PROVIDER=openai_compatible\n'
        'LLM_EMBEDDING_PROVIDER=openai_compatible\nLLM_API_BASE_URL=http://llm.interno:8181/v1\n'
        f'QDRANT_MODE={qdrant_mode}\nQDRANT_URL=http://vector.interno:6339\n', encoding="utf-8",
    )
    result = run_powershell(windows_project, "Get-MatrixServiceEndpoints | ConvertTo-Json -Compress\n")
    endpoints = last_json(result)
    observed = {item["Name"]: (item["HostName"], item["Port"]) for item in endpoints}
    expected = {"Backend": ("127.0.0.1", 8112), "MySQL": ("db.interno", 3319),
                "Inferencia compatible": ("llm.interno", 8181)}
    if qdrant_mode == "server":
        expected["Qdrant server"] = ("vector.interno", 6339)
    assert observed == expected
    assert "synthetic-value" not in result.stdout + result.stderr


def test_invocacion_python_devuelve_solo_codigo_y_restaura_cwd_y_pythonpath(windows_project):
    scripts = windows_project / ".venv/Scripts"
    if sys.platform == "win32":
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(windows_project / ".venv")],
                       check=True, timeout=30)
    else:
        scripts.mkdir(parents=True)
        (scripts / "python.exe").symlink_to(sys.executable)
    result = run_powershell(windows_project, """
$before=(Get-Location).Path
$env:PYTHONPATH='valor-preservado'
$values=@(Invoke-MatrixPython -Arguments @('-S','-m','scripts.preflight','--dependencies-only','--json'))
@{ count=$values.Count; code=$values[0]; path=$env:PYTHONPATH; cwdRestored=((Get-Location).Path -eq $before) } | ConvertTo-Json -Compress
""")
    assert last_json(result) == {"count": 1, "code": 1, "path": "valor-preservado", "cwdRestored": True}
    assert "Traceback" not in result.stdout + result.stderr


def test_control_de_procesos_distingue_carpeta_y_modulo(windows_project):
    result = run_powershell(windows_project, """
$mine=[pscustomobject]@{ ExecutablePath=$script:VenvPython; CommandLine='python.exe -m scripts.bootstrap serve --skip-preflight' }
$otherFolder=[pscustomobject]@{ ExecutablePath=($script:VenvPython + '.other'); CommandLine=$mine.CommandLine }
$otherCommand=[pscustomobject]@{ ExecutablePath=$script:VenvPython; CommandLine='python.exe -m scripts.bootstrap ingest' }
@{ mine=(Test-MatrixOwnedProcess $mine); otherFolder=(Test-MatrixOwnedProcess $otherFolder); otherCommand=(Test-MatrixOwnedProcess $otherCommand) } | ConvertTo-Json -Compress
""")
    assert last_json(result) == {"mine": True, "otherFolder": False, "otherCommand": False}


@pytest.fixture
def foreign_http():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"app":"Otra aplicacion"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_puerto_ajeno_es_un_fallo_obligatorio_del_preflight(foreign_http):
    report = preflight.PreflightReport()
    preflight.check_ports(report, SimpleNamespace(app_host="127.0.0.1", app_port=foreign_http, app_name="Matrix RH"))
    assert not report.ok
    assert report.as_dict()["failed"] == ["puerto_backend"]


def test_preflight_sonda_escucha_total_en_loopback(foreign_http):
    report = preflight.PreflightReport()
    preflight.check_ports(report, SimpleNamespace(app_host="0.0.0.0", app_port=foreign_http, app_name="Matrix RH"))
    assert report.as_dict()["failed"] == ["puerto_backend"]
    assert report.checks[0].detail == "ocupado por otro proceso"


def test_preflight_reporta_error_de_socket_sin_traceback(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("fixture de resolucion DNS")

    monkeypatch.setattr(preflight.socket, "getaddrinfo", unavailable)
    report = preflight.PreflightReport()
    preflight.check_ports(report, SimpleNamespace(app_host="fixture", app_port=8000, app_name="Matrix RH"))
    assert report.as_dict()["failed"] == ["puerto_backend"]
    assert report.checks[0].detail == "no verificable: OSError"


def test_preflight_puerto_libre_no_depende_de_conectar_y_lo_libera(monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError("conexion saliente lenta del caso Windows")

    monkeypatch.setattr(preflight.socket, "create_connection", timeout)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    report = preflight.PreflightReport()
    preflight.check_ports(report, SimpleNamespace(app_host="127.0.0.1", app_port=port, app_name="Matrix RH"))
    assert report.ok
    assert "libre (bind verificado)" in report.checks[0].detail
    with socket.socket() as reopen:
        reopen.bind(("127.0.0.1", port))
        reopen.listen(1)


def test_preflight_escucha_total_rechaza_colision_en_otra_interfaz():
    with socket.socket() as foreign:
        try:
            foreign.bind(("127.0.0.2", 0))
        except OSError:
            pytest.skip("El sistema no permite la segunda direccion loopback")
        foreign.listen(1)
        report = preflight.PreflightReport()
        preflight.check_ports(
            report, SimpleNamespace(app_host="0.0.0.0", app_port=foreign.getsockname()[1]), require_free=True,
        )
        assert report.as_dict()["failed"] == ["puerto_backend"]


def test_diagnostico_reconoce_matrix_activo_pero_instalacion_exige_puerto_libre(monkeypatch):
    requests = []

    class MatrixHealth(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            requests.append(self.path)
            body = b'{"app":"Matrix RH"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), MatrixHealth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    settings = SimpleNamespace(app_host="127.0.0.1", app_port=server.server_port, app_name="Matrix RH")
    try:
        report = preflight.PreflightReport()
        preflight.check_ports(report, settings)
        assert report.ok
        assert report.checks[0].detail == "ocupado por Matrix RH"
        strict = preflight.PreflightReport()
        preflight.check_ports(strict, settings, require_free=True)
        assert strict.as_dict()["failed"] == ["puerto_backend"]
        assert requests == ["/health"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("code", [errno.EADDRINUSE, 10048, 10013, errno.EADDRNOTAVAIL])
def test_preflight_no_oculta_error_de_bind_ni_filtra_excepcion_privada(monkeypatch, code):
    closed = []

    class RejectedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def setsockopt(self, *args):
            return

        def bind(self, *args):
            raise OSError(code, "MARCADOR_PRIVADO_ERROR_SOCKET")

    monkeypatch.setattr(preflight.socket, "socket", lambda *args: RejectedSocket())
    report = preflight.PreflightReport()
    preflight.check_ports(report, SimpleNamespace(app_host="127.0.0.1", app_port=8000), require_free=True)
    assert report.as_dict()["failed"] == ["puerto_backend"]
    assert f"codigo {code}" in report.checks[0].detail
    assert "MARCADOR_PRIVADO_ERROR_SOCKET" not in preflight.render_text(report)
    assert closed == [True]


def test_arranque_rechaza_puerto_ajeno_antes_de_crear_runtime(windows_project, foreign_http):
    (windows_project / ".env").write_text(f"APP_PORT={foreign_http}\n", encoding="utf-8")
    result = run_powershell(windows_project, "& (Join-Path $env:MATRIX_HARNESS_ROOT 'windows/Start-MatrixRH.ps1') -NoBrowser\nexit $LASTEXITCODE\n")
    assert result.returncode == 1
    assert "puerto configurado esta ocupado" in result.stdout
    assert not (windows_project / "var").exists()


def test_instalador_no_modifica_env_ni_runtime_con_puerto_ocupado(windows_project, foreign_http):
    env_file = windows_project / ".env"
    env_file.write_text(f"APP_PORT={foreign_http}\n", encoding="utf-8")
    previous = env_file.read_bytes()
    result = run_powershell(windows_project, "& (Join-Path $env:MATRIX_HARNESS_ROOT 'windows/Install-MatrixRH.ps1') -SkipFrontend\nexit $LASTEXITCODE\n")
    assert result.returncode == 1
    assert "La instalacion no se modifica" in result.stdout
    assert env_file.read_bytes() == previous
    assert not (windows_project / "var").exists()
    assert not (windows_project / ".venv").exists()


def test_autoarranque_ejecuta_script_sin_bat_interactivo(windows_project):
    result = run_powershell(windows_project, """
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory); return @{ Execute=$Execute; Argument=$Argument; WorkingDirectory=$WorkingDirectory } }
function New-ScheduledTaskTrigger { param([switch]$AtLogOn,$User); return @{} }
function New-ScheduledTaskSettingsSet { param([switch]$StartWhenAvailable,[switch]$DontStopIfGoingOnBatteries,[switch]$AllowStartIfOnBatteries,$ExecutionTimeLimit); return @{} }
function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Settings,$Description,[switch]$Force); $Action | ConvertTo-Json -Compress | Set-Content (Join-Path $env:MATRIX_HARNESS_ROOT 'action.json') }
& (Join-Path $env:MATRIX_HARNESS_ROOT 'windows/Install-Autostart.ps1')
Get-Content (Join-Path $env:MATRIX_HARNESS_ROOT 'action.json')
""", extra_env={"USERNAME": "synthetic-user"})
    action = last_json(result)
    assert action["Execute"] == "powershell.exe"
    assert '-File "' in action["Argument"]
    assert "Start-MatrixRH.ps1\" -NoBrowser" in action["Argument"]
    assert ".bat" not in action["Argument"]
    assert action["WorkingDirectory"] == str(windows_project)


@pytest.fixture
def isolated_installer(windows_project):
    scripts = windows_project / ".venv/Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("fixture", encoding="utf-8")
    (windows_project / ".venv/matrix-root.txt").write_text(str(windows_project), encoding="utf-8")
    dist = windows_project / "frontend/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("fixture", encoding="utf-8")
    (windows_project / ".env").write_text(
        "DATABASE_URL=mysql+pymysql://demo:@127.0.0.1:3306/matrix_rh_130\n", encoding="utf-8"
    )
    common = windows_project / "windows/Common-MatrixRH.ps1"
    with common.open("a", encoding="utf-8") as handle:
        handle.write("""
function Get-Command { param($Name,$ErrorAction); if($Name -eq 'py'){return $null}; if($Name -eq 'python'){return @{Source=$env:MATRIX_HARNESS_PYTHON}}; if($Name -eq 'uv'){return @{Source='Matrix-UvFixture'}}; return Microsoft.PowerShell.Core\\Get-Command $Name -ErrorAction $ErrorAction }
function Matrix-UvFixture { if($args[0] -eq '--version'){Write-Output 'uv 0.11.33'}; $global:LASTEXITCODE=0 }
function Assert-Python312 { param($PythonPath); return $true }
function Test-MatrixEnvFile { return $true }
function Test-MatrixDependencies { return $true }
function Test-MatrixIntegrity { return $true }
function Test-MatrixApplication { return $true }
function Test-MatrixSafety { return $true }
function Initialize-MatrixSeedPassword { return $true }
function Test-MatrixBackendPortFree { return $true }
function Find-WampMySql { return $null }
function Test-OllamaModels { return $true }
function Start-MatrixOllamaIfNeeded { return $true }
function Test-MatrixDatabaseFree { param($DatabaseUrl,$DatabaseName); if($DatabaseName -eq 'matrix_precreada'){return @{Libre=$false;Detalle='EXISTE_VACIA'}}; return @{Libre=$true;Detalle='LIBRE'} }
function Invoke-MatrixPython { param([string[]]$Arguments); $command=$Arguments -join ' '; Add-Content (Join-Path $env:MATRIX_HARNESS_ROOT 'called-modules.txt') $command; if($command -eq '-m scripts.preflight'){return 1}; return 0 }
""")
    return windows_project


def test_instalador_no_ingiere_tras_preflight_fallido(isolated_installer):
    windows_project = isolated_installer
    result = run_powershell(windows_project, "& (Join-Path $env:MATRIX_HARNESS_ROOT 'windows/Install-MatrixRH.ps1') -SkipFrontend\nexit $LASTEXITCODE\n")
    assert result.returncode == 1, result.stdout + result.stderr
    calls = (windows_project / "called-modules.txt").read_text(encoding="utf-8")
    assert "scripts.bootstrap migrate" in calls
    assert "-m scripts.preflight" in calls
    assert "scripts.bootstrap ingest" not in calls
    assert "Ingesta inicial omitida porque el preflight fallo" in result.stdout


@pytest.mark.parametrize(("adopt", "inherited", "expected_database", "migrates"), [
    (True, True, "matrix_precreada", True),
    (False, False, "matrix_precreada_app", True),
    (False, True, "matrix_precreada", False),
])
def test_instalador_respeta_adopcion_explicita_y_variable_heredada(
    isolated_installer, adopt, inherited, expected_database, migrates,
):
    root = isolated_installer
    url = "mysql+pymysql://demo:@127.0.0.1:3306/matrix_precreada"
    env_file = root / ".env"
    env_file.write_text(f"DATABASE_URL={url}\n", encoding="utf-8")
    extra = {"MATRIX_ADOPT_EXISTING_DATABASE": "true" if adopt else "false"}
    if inherited:
        extra["DATABASE_URL"] = url
    result = run_powershell(root, "& (Join-Path $env:MATRIX_HARNESS_ROOT 'windows/Install-MatrixRH.ps1') -SkipFrontend\nexit $LASTEXITCODE\n", extra_env=extra)
    # El preflight aislado devuelve 1 cuando el flujo consigue llegar a el.
    assert result.returncode == 1, result.stdout + result.stderr
    calls = (root / "called-modules.txt").read_text(encoding="utf-8")
    assert ("scripts.bootstrap migrate" in calls) is migrates
    assert env_file.read_text(encoding="utf-8").strip() == f"DATABASE_URL=mysql+pymysql://demo:@127.0.0.1:3306/{expected_database}"
    if adopt:
        assert "Adopcion de la base vacia" in result.stdout


def test_helper_de_bd_no_clasifica_registro_incompatible_como_vacio(monkeypatch, capsys):
    import sqlalchemy

    from app.database import migrator

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, *args):
            return SimpleNamespace(first=lambda: ("matrix_precreada",))

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            pass

    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(migrator, "inspect_ownership", lambda engine: (False, {"schema_migrations"}))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(json.dumps({
        "url": "mysql+pymysql://demo:@127.0.0.1:3306/matrix_precreada", "name": "matrix_precreada",
    }).encode("utf-8"))))
    content = (ROOT / "windows/Common-MatrixRH.ps1").read_text(encoding="utf-8")
    source = content.split('$codigo = @"\n', 1)[1].split('\n"@', 1)[0]
    exec(compile(source, "matrix-dbcheck-fixture.py", "exec"), {})
    assert capsys.readouterr().out.strip() == "OCUPADA:schema_migrations"


def test_instalador_usa_build_incluido_sin_buscar_node(isolated_installer):
    """Una instalacion normal avanza sin Node; el preflight sintetico falla despues."""
    common = isolated_installer / "windows/Common-MatrixRH.ps1"
    with common.open("a", encoding="utf-8") as handle:
        handle.write("""
function Get-Command { param($Name,$ErrorAction)
    if ($Name -in @('node','corepack.cmd')) { throw 'NO_DEBE_REQUERIR_NODE' }
    if ($Name -eq 'py') { return $null }
    if ($Name -eq 'python') { return @{Source=$env:MATRIX_HARNESS_PYTHON} }
    if ($Name -eq 'uv') { return @{Source='Matrix-UvFixture'} }
    return Microsoft.PowerShell.Core\\Get-Command $Name -ErrorAction $ErrorAction
}
""")
    result = run_powershell(isolated_installer, """
& (Join-Path $env:MATRIX_HARNESS_ROOT 'windows/Install-MatrixRH.ps1')
exit $LASTEXITCODE
""")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "NO_DEBE_REQUERIR_NODE" not in result.stdout + result.stderr
    assert "Interfaz compilada incluida" in result.stdout
    calls = (isolated_installer / "called-modules.txt").read_text()
    assert "scripts.bootstrap migrate" in calls


def test_instalador_no_migra_si_mysql_no_puede_verificarse(isolated_installer):
    common = isolated_installer / "windows/Common-MatrixRH.ps1"
    with common.open("a", encoding="utf-8") as handle:
        handle.write("""
function Test-MatrixDatabaseFree { param($DatabaseUrl,$DatabaseName); return @{Libre=$false;Detalle='DESCONOCIDO:OperationalError'} }
""")
    result = run_powershell(isolated_installer, """
& (Join-Path $env:MATRIX_HARNESS_ROOT 'windows/Install-MatrixRH.ps1')
exit $LASTEXITCODE
""")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Abra WampServer" in result.stdout
    assert "scripts.bootstrap migrate" not in (isolated_installer / "called-modules.txt").read_text()


def test_uv_ausente_offline_no_intenta_instalar_ni_crea_archivos(windows_project):
    result = run_powershell(windows_project, """
function Find-MatrixUv { param($PythonPath); return $null }
$uv = Initialize-MatrixUv -PythonPath $env:MATRIX_HARNESS_PYTHON -Offline
@{ missing=($null -eq $uv); writes=(Test-Path (Join-Path $env:MATRIX_HARNESS_ROOT 'var')) } | ConvertTo-Json -Compress
""")
    assert last_json(result) == {"missing": True, "writes": False}


def test_ollama_abierto_se_reutiliza_sin_arrancar_procesos(windows_project):
    result = run_powershell(windows_project, """
function Test-MatrixOllamaUp { param($Endpoint); return $true }
function Start-Process { throw 'NO_DUPLICAR_OLLAMA' }
@{ ok=(Start-MatrixOllamaIfNeeded); writes=(Test-Path (Join-Path $env:MATRIX_HARNESS_ROOT 'var')) } | ConvertTo-Json -Compress
""")
    assert last_json(result) == {"ok": True, "writes": False}


def test_inicio_ollama_acota_host_local_y_restaura_entorno(windows_project):
    result = run_powershell(windows_project, """
$script:probe = 0
function Test-MatrixOllamaUp { param($Endpoint); $script:probe++; return ($script:probe -gt 1) }
function Find-MatrixOllama { return 'ollama-fixture.exe' }
function Start-Process {
    param($FilePath,$ArgumentList,$WindowStyle,$RedirectStandardOutput,$RedirectStandardError,[switch]$PassThru)
    $script:observed = @{ exe=$FilePath; args=@($ArgumentList); host=$env:OLLAMA_HOST; cloud=$env:OLLAMA_NO_CLOUD }
    return [pscustomobject]@{ HasExited=$false }
}
$ok = Start-MatrixOllamaIfNeeded -TimeoutSeconds 1
@{ok=$ok; started=$script:observed; restoredHost=$env:OLLAMA_HOST; restoredCloud=$env:OLLAMA_NO_CLOUD} | ConvertTo-Json -Compress -Depth 5
""", extra_env={"OLLAMA_HOST": "0.0.0.0:9999", "OLLAMA_NO_CLOUD": "0"})
    payload = last_json(result)
    assert payload["ok"] is True
    assert payload["started"] == {
        "exe": "ollama-fixture.exe", "args": ["serve"], "host": "127.0.0.1:11434", "cloud": "1"
    }
    assert payload["restoredHost"] == "0.0.0.0:9999"
    assert payload["restoredCloud"] == "0"


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:11434/otra-ruta", "http://demo:secreto@127.0.0.1:11434",
    "file:///fixture", "http://127.0.0.1:11434/?token=secreto",
])
def test_inicio_ollama_rechaza_endpoint_invalido_sin_procesos(windows_project, endpoint):
    result = run_powershell(windows_project, """
function Test-MatrixOllamaUp { param($Endpoint); throw 'NO_CONECTAR' }
function Start-Process { throw 'NO_ARRANCAR' }
@{ok=(Start-MatrixOllamaIfNeeded)} | ConvertTo-Json -Compress
""", extra_env={"OLLAMA_BASE_URL": endpoint})
    assert last_json(result) == {"ok": False}
    assert "secreto" not in result.stdout + result.stderr
