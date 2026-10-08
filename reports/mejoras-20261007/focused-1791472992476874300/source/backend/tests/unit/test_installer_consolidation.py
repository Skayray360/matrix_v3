# Creado por Aldo Garcia.
"""Regresiones de instalacion; no conecta ni modifica servicios corporativos."""
from __future__ import annotations

import builtins
import hashlib
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import bootstrap, package_release, preflight, uv_runtime, verify_supply_chain

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


@pytest.fixture
def release_core(tmp_path):
    names = preflight.CORE_FILES | {
        "backend/app/__init__.py", "backend/app/main.py", "backend/scripts/preflight.py",
        "backend/scripts/bootstrap.py", "backend/scripts/__init__.py", "windows/Common-MatrixRH.ps1",
    }
    for name in names:
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    (tmp_path / "SHA256SUMS.txt").write_text("".join(
        f"{hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}  {name}\n" for name in sorted(names)
    ), encoding="utf-8")
    return tmp_path


def test_integridad_stdlib_detecta_init_cruzado_sin_ejecutar_codigo(release_core):
    target = release_core / "backend/app/__init__.py"
    target.write_text("raise RuntimeError('MARCADOR_PRIVADO')\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-S", "-m", "scripts.preflight", "--integrity-only", "--require-manifest", "--json"],
        cwd=release_core / "backend", capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr + result.stdout
    assert "MARCADOR_PRIVADO" not in result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["failed"] == ["integridad_codigo"]
    assert "no declara la version" in payload["checks"][0]["detail"]
    assert not (release_core / ".env").exists()
    assert not (release_core / "var").exists()


def test_integridad_rechaza_codigo_mezclado_permite_corpus_y_build_cliente(release_core):
    for name in ("data/knowledge/cliente.txt", "frontend/dist/index.html", ".env", "var/uploads/cliente.txt"):
        path = release_core / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("datos sinteticos modificables", encoding="utf-8")
    report = preflight.PreflightReport()
    preflight.check_integrity(report, root=release_core, require_manifest=True)
    assert report.ok
    (release_core / "backend/app/main.py").write_text("codigo distinto", encoding="utf-8")
    report = preflight.PreflightReport()
    preflight.check_integrity(report, root=release_core, require_manifest=True)
    assert not report.ok
    assert "backend/app/main.py" in report.checks[-1].detail


@pytest.mark.parametrize("line", ["../afuera.py", "/absoluto.py", "backend\\app\\main.py", "backend/app/main.py"])
def test_integridad_rechaza_rutas_y_duplicados_del_manifiesto(release_core, line):
    manifest = release_core / "SHA256SUMS.txt"
    manifest.write_text(manifest.read_text() + "a" * 64 + "  " + line + "\n", encoding="utf-8")
    report = preflight.PreflightReport()
    preflight.check_integrity(report, root=release_core, require_manifest=True)
    assert not report.ok
    assert "SHA256SUMS.txt no verificable" in report.checks[-1].detail


def test_settings_import_fallido_se_reporta_sin_payload_privado(monkeypatch):
    original = __import__

    def broken_import(name, *args, **kwargs):
        if name == "app.config":
            raise ImportError("MARCADOR_PRIVADO")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    report = preflight.PreflightReport()
    assert preflight.check_settings(report) is None
    assert report.as_dict()["failed"] == ["configuracion"]
    assert "MARCADOR_PRIVADO" not in preflight.render_text(report)


def test_aplicacion_asgi_import_fallido_es_accionable_y_no_inicia_lifespan(monkeypatch):
    def broken(name):
        raise ImportError("MARCADOR_PRIVADO")

    monkeypatch.setattr(preflight.importlib, "import_module", broken)
    report = preflight.PreflightReport()
    preflight.check_application(report)
    assert report.as_dict()["failed"] == ["aplicacion_asgi"]
    assert "MARCADOR_PRIVADO" not in preflight.render_text(report)


def _encryption_settings(tmp_path, **overrides):
    reference = tmp_path / "ti-evidencia.md"
    reference.write_text("Evidencia sintetica, sin claves de recuperacion", encoding="utf-8")
    values = {
        "app_env": "production", "encryption_attestation_reference": reference,
        "storage_encryption_attested": True, "database_encryption_attested": True, "backup_encryption_attested": True,
        "upload_storage_path": tmp_path / "uploads", "knowledge_root_path": tmp_path / "knowledge",
        "qdrant_storage_path": tmp_path / "qdrant", "qdrant_mode": "embedded",
        # secrets-scan: allow (fixture sintetico aislado; sin credenciales de un servicio real)
        "database_url": SimpleNamespace(get_secret_value=lambda: "mysql+pymysql://demo:fixture@127.0.0.1/demo"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_cifrado_produccion_linux_exige_archivo_y_declara_limite(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    settings = _encryption_settings(tmp_path)
    report = preflight.PreflightReport()
    preflight.check_storage_encryption(report, settings)
    assert report.ok
    assert "declaracion de infraestructura" in report.checks[-1].detail
    settings.encryption_attestation_reference.unlink()
    report = preflight.PreflightReport()
    preflight.check_storage_encryption(report, settings)
    assert report.as_dict()["failed"] == ["cifrado_reposo"]


@pytest.mark.parametrize(("status", "protection", "percentage", "ok"), [
    ("FullyEncrypted", "On", 100, True), ("FullyEncrypted", "Off", 100, False),
    ("EncryptionInProgress", "On", 50, False), ("FullyDecrypted", "Off", 0, False),
])
def test_bitlocker_real_exige_cifrado_completo_y_proteccion(tmp_path, monkeypatch, status, protection, percentage, ok):
    monkeypatch.setattr(preflight.platform, "system", lambda: "Windows")
    mysql_data = tmp_path / "mysql-datadir"
    mysql_data.mkdir()
    settings = _encryption_settings(tmp_path, database_datadir_path=mysql_data)
    observed_paths = []

    def probe(paths):
        observed_paths.extend(paths)
        return [{"path": str(path), "mount": "C:", "status": status, "protection": protection, "percentage": percentage}
                for path in paths]

    monkeypatch.setattr(preflight, "windows_encryption_status", probe)
    report = preflight.PreflightReport()
    preflight.check_storage_encryption(report, settings)
    assert report.ok is ok
    assert mysql_data in observed_paths
    assert settings.qdrant_storage_path in observed_paths
    assert preflight.PROJECT_ROOT in observed_paths


def test_bitlocker_no_disponible_falla_sin_presumir_cifrado(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight.platform, "system", lambda: "Windows")
    settings = _encryption_settings(tmp_path, database_datadir_path=tmp_path)
    monkeypatch.setattr(preflight, "windows_encryption_status", lambda paths: (_ for _ in ()).throw(OSError("PRIVADO")))
    report = preflight.PreflightReport()
    preflight.check_storage_encryption(report, settings)
    assert report.as_dict()["failed"] == ["cifrado_reposo"]
    assert "No se presume" in report.checks[-1].detail
    assert "PRIVADO" not in preflight.render_text(report)


@pytest.fixture
def model_pin_env(tmp_path, monkeypatch):
    for name in ("APP_ENV", "LLM_PROVIDER", "LLM_DEEP_PROVIDER", "LLM_EMBEDDING_PROVIDER", "OLLAMA_BASE_URL",
                 "LLM_LOCAL_HOSTS", "OLLAMA_FAST_MODEL", "OLLAMA_DEEP_MODEL", "OLLAMA_EMBEDDING_MODEL",
                 "LLM_FAST_DIGEST", "LLM_DEEP_DIGEST", "LLM_EMBEDDING_DIGEST"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("APP_ENV=development\nETIQUETA=Información México\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "PROJECT_ROOT", tmp_path)
    return env


def _mock_inventory(monkeypatch, **changes):
    import httpx

    models = {"gemma4:latest": "a" * 64, "embeddinggemma:latest": "c" * 64}
    models.update(changes)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            assert url == "http://127.0.0.1:11434/api/tags"
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"models": [{"name": name, "digest": value}
                                                            for name, value in models.items()]})

    monkeypatch.setattr(httpx, "Client", Client)


def test_pin_modelos_es_unico_preserva_digests_config_y_detecta_drift(model_pin_env, monkeypatch, capsys):
    _mock_inventory(monkeypatch)
    assert bootstrap.cmd_pin_models(SimpleNamespace()) == 0
    first = model_pin_env.read_bytes()
    assert "Información México" in first.decode()
    assert "LLM_FAST_DIGEST=" + "a" * 64 in first.decode()
    assert "LLM_DEEP_DIGEST=" + "a" * 64 in first.decode()
    assert bootstrap.cmd_pin_models(SimpleNamespace()) == 0
    assert model_pin_env.read_bytes() == first
    _mock_inventory(monkeypatch, **{"gemma4:latest": "d" * 64})
    assert bootstrap.cmd_pin_models(SimpleNamespace()) == 1
    assert model_pin_env.read_bytes() == first
    assert "No se reemplaza el digest" in capsys.readouterr().out


@pytest.mark.parametrize("digest", ["", "digest-invalido", "d" * 63])
@pytest.mark.parametrize("model", ["gemma4:latest", "embeddinggemma:latest"])
def test_pin_modelos_no_guarda_inventario_malo(model_pin_env, monkeypatch, digest, model):
    _mock_inventory(monkeypatch, **{model: digest})
    previous = model_pin_env.read_bytes()
    assert bootstrap.cmd_pin_models(SimpleNamespace()) == 1
    assert model_pin_env.read_bytes() == previous


def test_pin_modelos_no_sobrescribe_override_heredado_vacio(model_pin_env, monkeypatch):
    _mock_inventory(monkeypatch)
    monkeypatch.setenv("LLM_FAST_DIGEST", "")
    previous = model_pin_env.read_bytes()
    assert bootstrap.cmd_pin_models(SimpleNamespace()) == 1
    assert model_pin_env.read_bytes() == previous


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "::"])
def test_serve_cli_host_no_omite_guardia_con_skip_preflight(monkeypatch, host, capsys):
    import uvicorn

    from app import config

    settings = config.Settings(_env_file=None, app_env="test", auth_provider="local_test",
                               local_test_auth_enabled=True, local_test_seed_users_enabled=True)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: pytest.fail("No debe abrir un perfil local en red"))
    assert bootstrap.cmd_serve(SimpleNamespace(host=host, port=8000, reload=False, skip_preflight=True)) == 1
    assert "guardia de exposicion" in capsys.readouterr().out


def test_serve_proxy_confia_solo_ips_validadas_y_no_omite_cifrado(monkeypatch):
    import uvicorn

    from app import config

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    settings = config.Settings(_env_file=None, app_env="test", forwarded_allow_ips="127.0.0.1,172.30.0.0/24")
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    captured = []
    encryption_checks = []
    class FakeServer:
        def __init__(self, configuration):
            self.configuration = configuration
            self.should_exit = False

        def run(self):
            captured.append({
                "proxy_headers": self.configuration.proxy_headers,
                "forwarded_allow_ips": self.configuration.forwarded_allow_ips,
            })

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(bootstrap, "check_application", lambda report: None)
    monkeypatch.setattr(bootstrap, "check_storage_encryption", lambda report, settings: encryption_checks.append(True))
    assert bootstrap.cmd_serve(SimpleNamespace(host="127.0.0.1", port=port, reload=False, skip_preflight=True)) == 0
    assert encryption_checks == [True]
    assert captured[0]["proxy_headers"] is True
    assert captured[0]["forwarded_allow_ips"] == "127.0.0.1,172.30.0.0/24"


def test_paquete_conserva_readmes_y_excluye_historial_y_datos_runtime(tmp_path: Path):
    for name in (
        "README.md", "reports/README.md", "reports/historico/REVISION.md", "reports/tests/resultados.json",
        "var/README.md", "var/qdrant/README.md",
        "var/uploads/privado.txt", ".env", "frontend/dist/index.html", "source-history.bundle",
        ".venv.previous-20260910/lib/cache.dat",
        "offline-models/blobs/sha256-sintetico", "offline-models/manifests/registro/modelo",
        "weights/fixture.gguf", "weights/fixture.safetensors", "weights/fixture.onnx",
        "weights/fixture.pt", "weights/fixture.pth",
        "weights/PESOS.GGUF", "weights/pytorch_model-00001-of-00003.bin",
        "weights/ggml-model-f16.bin", ".ollama/models/blobs/sha256-sintetico",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sintetico", encoding="utf-8")
    external = tmp_path.parent / "externo.txt"
    external.write_text("sintetico", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(external)
    selected = {p.relative_to(tmp_path).as_posix() for p in package_release.iter_package_files(tmp_path)}
    assert selected == {
        "README.md", "reports/README.md", "frontend/dist/index.html", "var/README.md", "var/qdrant/README.md",
    }


def test_lockfile_debe_corresponder_al_gestor_fijado(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(verify_supply_chain, "FRONTEND", tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({"packageManager": "npm@10.9.0+sha512." + "a" * 128}))
    (tmp_path / ".npmrc").write_text("ignore-scripts=true\n")
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    report = verify_supply_chain.Report()
    verify_supply_chain.check_reproducible_install(report)
    assert not report.ok
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3}')
    report = verify_supply_chain.Report()
    verify_supply_chain.check_reproducible_install(report)
    assert report.ok


@pytest.mark.parametrize("pin", ["npm@10.9.0+sha512.abc123", "npm@10.9.0+sha0.abc", "npm@10.9.0"])
def test_hash_de_gestor_incompleto_no_satisface_gate(tmp_path: Path, pin: str):
    (tmp_path / "package.json").write_text(json.dumps({"packageManager": pin}))
    assert verify_supply_chain.package_manager_pin(tmp_path) == (pin, False)


def test_preflight_read_only_no_abre_qdrant_embebido(tmp_path: Path, monkeypatch):
    from app.config import QdrantMode
    from app.rag import vector_store

    def forbidden():
        pytest.fail("Un diagnostico de solo lectura no debe adquirir el lock ni crear archivos")

    monkeypatch.setattr(vector_store, "get_vector_store", forbidden)
    settings = SimpleNamespace(qdrant_mode=QdrantMode.EMBEDDED, qdrant_storage_path=tmp_path / "ausente")
    report = preflight.PreflightReport()
    preflight.check_qdrant(report, settings, read_only=True)
    assert report.ok
    assert report.checks[0].status == preflight.WARN
    assert not settings.qdrant_storage_path.exists()


def test_preflight_usa_adapter_configurado_y_cierra_cliente(monkeypatch):
    from app.llm import provider
    from app.llm.ollama_client import ModelInventory

    closed = []

    class FakeClient:
        def list_models(self):
            return ModelInventory(names=("modelo-local-a", "modelo-local-b", "embed-local"))

        def probe_embedding_dimension(self):
            return 1024

        def close(self):
            closed.append(True)

    monkeypatch.setattr(provider, "ModelClient", FakeClient)
    settings = SimpleNamespace(
        llm_provider="openai_compatible", llm_deep_provider="openai_compatible",
        llm_embedding_provider="openai_compatible", ollama_fast_model="modelo-local-a",
        ollama_deep_model="modelo-local-b", ollama_embedding_model="embed-local",
        ollama_embedding_dimension=1024,
    )
    report = preflight.PreflightReport()
    preflight.check_ollama(report, settings)
    assert report.ok
    assert closed == [True]
    assert {c.name for c in report.checks} >= {"modelo_fast", "modelo_deep", "modelo_embedding"}
    assert any("1024" in c.detail for c in report.checks)


@pytest.mark.parametrize("missing", ["pydantic", "pydantic_settings", "uvicorn", "python_multipart"])
def test_preflight_no_carga_configuracion_con_un_paquete_ausente(monkeypatch, missing):
    """Un venv parcialmente instalado debe producir un diagnostico, no otra importacion fallida."""
    original_import = __import__

    def import_without_package(name):
        if name == missing:
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return original_import(name)

    def unexpected_settings(_report):
        pytest.fail("La configuracion no debe cargarse con dependencias incompletas")

    monkeypatch.setattr(preflight, "__import__", import_without_package, raising=False)
    monkeypatch.setattr(preflight, "check_settings", unexpected_settings)
    report = preflight.run_preflight(read_only=True)
    assert not report.ok
    assert report.as_dict()["failed"] == ["dependencias"]
    assert missing in report.checks[-1].detail
    assert "INSTALAR_MATRIX_RH.bat -SkipFrontend" in preflight.render_text(report)


@pytest.mark.parametrize("scope", [[], ["--llm-only"], ["--dependencies-only"]])
def test_cli_preflight_sin_site_packages_devuelve_json_sin_traceback(scope):
    """Reproduce el fallo real con un proceso Python que solo dispone de la biblioteca estandar."""
    result = subprocess.run(
        [sys.executable, "-S", "-m", "scripts.preflight", "--read-only", "--json", *scope],
        cwd=ROOT / "backend", capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["failed"] == ["dependencias"]
    assert "pydantic" in payload["checks"][-1]["detail"]


def test_sonda_dependencias_funciona_sin_configuracion_ni_servicios(monkeypatch, capsys):
    def unexpected_probe(_report):
        pytest.fail("La sonda de paquetes no debe cargar Settings ni conectar un modelo")

    monkeypatch.setattr(preflight, "check_settings", unexpected_probe)
    monkeypatch.setattr(preflight, "check_ollama", unexpected_probe)
    assert preflight.main(["--dependencies-only", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {item["name"] for item in payload["checks"]} == {"python", "dependencias"}


@pytest.mark.parametrize("occupied", [False, True])
def test_cli_puertos_funciona_sin_paquetes_ni_settings_y_respeta_colision(occupied):
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        if not occupied:
            listener.close()
        result = subprocess.run(
            [sys.executable, "-S", "-m", "scripts.preflight", "--ports-only", "--host=127.0.0.1",
             f"--port={port}", "--json"],
            cwd=ROOT / "backend", capture_output=True, text=True, check=False, timeout=15,
        )
        assert result.returncode == int(occupied), result.stdout + result.stderr
        assert "Traceback" not in result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is not occupied
        assert len(payload["checks"]) == 1
        assert payload["checks"][0]["name"] == "puerto_backend"
    finally:
        listener.close()


def test_uv_distribuciones_sin_target_conservan_version_estable_compatible():
    assert uv_runtime.compatible_uv_version("uv 0.11.33") == "0.11.33"
    assert uv_runtime.compatible_uv_version("uv 0.12.8 (fece32fc5 2026-07-28)") == "0.12.8"
    assert uv_runtime.compatible_uv_version("uv 0.12.9") == "0.12.9"
    with pytest.raises(ValueError, match="encontrado 0.11.32"):
        uv_runtime.compatible_uv_version("uv 0.11.32")


def test_helper_mysql_windows_es_python_valido_y_no_expone_credenciales_en_argv():
    content = (ROOT / "windows/Common-MatrixRH.ps1").read_text()
    source = content.split('$codigo = @"\n', 1)[1].split('\n"@', 1)[0]
    compile(source, "dbcheck-sintetico.py", "exec")
    assert "sys.argv[2]" not in source
    assert "inspect_ownership(engine)" in source
    assert "SHOW DATABASES LIKE" not in source


@pytest.mark.skipif(sys.platform == "win32", reason="Contrato Bash ejecutado en Linux; Windows tiene job de PowerShell")
def test_detencion_shell_distingue_un_proceso_de_otra_carpeta(tmp_path: Path):
    backend = tmp_path / "backend"
    scripts = backend / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "__init__.py").write_text("")
    (scripts / "bootstrap.py").write_text("import sys\nsys.stdin.read()\n")
    python_link = tmp_path / ".venv/bin/python"
    python_link.parent.mkdir(parents=True)
    python_link.symlink_to(sys.executable)
    process = subprocess.Popen(
        [str(python_link), "-m", "scripts.bootstrap", "serve"], cwd=backend, stdin=subprocess.PIPE,
    )
    try:
        import psutil

        try:
            psutil.Process(process.pid).cwd()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            assert process.poll() is None
            pytest.skip("El sandbox no expone /proc del hijo vivo; el control real falla cerrado")
        command = [
            "bash", "-c", 'source "$1"; ROOT="$2"; VENV_PY="$3"; process_owned "$4"',
            "matrix-contract", str(ROOT / "scripts/matrixrh.sh"), str(tmp_path), sys.executable,
        ]
        assert subprocess.run([*command, str(process.pid)], check=False, timeout=10).returncode == 0
        assert subprocess.run([*command, str(__import__("os").getpid())], check=False, timeout=10).returncode == 1
    finally:
        process.communicate(timeout=5)
