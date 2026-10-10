# Creado por Aldo Garcia.
"""Preparacion Docker con configuracion sintetica: sin daemon, descargas ni inferencia."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from scripts import bootstrap
from scripts import docker_entrypoint as entry
from scripts import docker_prepare as prepare
from scripts import installation_state as state

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


@pytest.fixture()
def project(tmp_path, monkeypatch):
    (tmp_path / "backend/config").mkdir(parents=True)
    shutil.copyfile(ROOT / "backend/config/env.example", tmp_path / "backend/config/env.example")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/package.json").write_text("{}")
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setattr(prepare, "filesystem_type", lambda _: "ext4")
    monkeypatch.setattr(prepare, "prepare_certificates", lambda _: None)
    return tmp_path


def initialize(project):
    return prepare.prepare(project, uid=1000, gid=1000)


def test_prepare_idempotent_preserves_credentials_models_and_documents(project):
    first = initialize(project)
    path = project / "backend/config/docker.env"
    initial = path.read_bytes()
    secret_files = list((project / "backend/config/secrets/docker").iterdir())
    initial_secrets = {file.name: file.read_bytes() for file in secret_files}
    document = project / "knowledge-base/documents/test.txt"
    document.write_text("synthetic source")
    second = initialize(project)
    assert first["configuration_created"] and not second["configuration_created"]
    assert path.read_bytes() == initial
    assert initial_secrets == {file.name: file.read_bytes() for file in secret_files}
    assert document.read_text() == "synthetic source"
    assert path.stat().st_mode & 0o077 == 0
    values = prepare.read_values(path)
    assert "@mysql:3306/matrix_rh?" in values["DATABASE_URL"]
    assert values["MATRIX_DOCKER_MYSQL_PASSWORD"] in values["DATABASE_URL"]
    assert len(values["MATRIX_DOCKER_PROJECT_ID"]) == 16
    assert values["APP_SECRET_KEY"] not in json.dumps(second)
    assert all(values[key] == "" for key in prepare.PIN_KEYS)
    assert not (project / "backend/config/.env").exists()


def test_prepare_missing_project_identity_never_regenerates_over_existing_state(project):
    initialize(project)
    path = project / "backend/config/docker.env"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(line for line in lines if not line.startswith("MATRIX_DOCKER_PROJECT_ID=")) + "\n")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="configuracion existente"):
        initialize(project)
    assert path.read_bytes() == before


@pytest.mark.parametrize("native", ["backend/config/.env", "knowledge-base/state/mysql/data.bin",
                                    "knowledge-base/state/installation.json"])
def test_prepare_refuses_native_state_without_changing_it(project, native):
    path = project / native
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("existing native state")
    with pytest.raises(ValueError, match="nativ"):
        initialize(project)
    assert path.read_text() == "existing native state"
    assert not (project / "backend/config/docker.env").exists()


@pytest.mark.parametrize("filesystem", ["ntfs", "ntfs3", "drvfs", "9p", "cifs", "nfs", "virtiofs"])
def test_prepare_refuses_non_linux_bind_filesystems(project, monkeypatch, filesystem):
    monkeypatch.setattr(prepare, "filesystem_type", lambda _: filesystem)
    with pytest.raises(ValueError, match="filesystem Linux"):
        initialize(project)
    assert not (project / "backend/config/docker.env").exists()


def test_prepare_inspects_nested_state_mount_not_only_project_root(project, monkeypatch):
    target = project / "knowledge-base/state/docker/qdrant"
    target.mkdir(parents=True)
    monkeypatch.setattr(prepare, "filesystem_type", lambda path: "ntfs3" if path == target else "ext4")
    with pytest.raises(ValueError, match="filesystem Linux"):
        initialize(project)


def test_prepare_does_not_recreate_missing_secret_for_existing_database(project):
    initialize(project)
    secret = project / "backend/config/secrets/docker/mysql-root-password.txt"
    secret.unlink()
    before = (project / "backend/config/docker.env").read_bytes()
    with pytest.raises(ValueError, match="Restaure su respaldo"):
        initialize(project)
    assert not secret.exists()
    assert (project / "backend/config/docker.env").read_bytes() == before


def test_prepare_refuses_new_credentials_over_orphaned_data(project):
    target = project / "knowledge-base/state/docker/mysql/data.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"synthetic old database")
    with pytest.raises(ValueError, match="sin su configuracion"):
        initialize(project)
    assert target.read_bytes() == b"synthetic old database"


def test_prepare_rejects_project_and_secret_symlinks(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside-docker")
    (project / "backend/config/secrets").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="enlaces"):
        initialize(project)
    assert not list(outside.iterdir())


def test_mountinfo_resolves_deepest_mount_with_escaped_spaces(tmp_path):
    data = tmp_path / "mountinfo"
    data.write_text("20 1 0:1 / / rw - overlay overlay rw\n"
                    "21 20 0:2 / /project rw - ext4 /dev/sda rw\n"
                    "22 21 0:3 / /project/with\\040space rw - 9p windows rw\n")
    assert prepare.filesystem_type(Path("/project/state"), mountinfo=data) == "ext4"
    assert prepare.filesystem_type(Path("/project/with space/state"), mountinfo=data) == "9p"


def test_tls_local_pair_is_valid_and_preserved(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("OpenSSL no instalado en el entorno de pruebas")
    prepare.prepare_certificates(tmp_path)
    first = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    prepare.prepare_certificates(tmp_path)
    assert first == {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    assert (tmp_path / "matrixrh.key").stat().st_mode & 0o077 == 0


def test_operation_lock_blocks_second_process_until_release(tmp_path):
    with entry.operation_lock(tmp_path), pytest.raises(ValueError, match="ya esta activo"), entry.operation_lock(tmp_path):
        pytest.fail("No debe adquirir el bloqueo")
    with entry.operation_lock(tmp_path) as descriptor:
        assert descriptor >= 0


def runtime_environment(project):
    initialize(project)
    path = project / "backend/config/docker.env"
    with path.open("a") as output:
        for key in prepare.PIN_KEYS:
            output.write(f"{key}={'a' * 64}\n")
    return prepare.read_values(path)


@pytest.mark.parametrize("field,value", [("APP_BASE_URL", "http://127.0.0.1:8443"),
                                        ("AUTH_PROVIDER", "local_test"), ("APP_ENV", "production"),
                                        ("LLM_PROVIDER", "vertex"), ("LLM_FAST_DIGEST", "")])
def test_runtime_rejects_insecure_or_unprepared_profile(project, field, value):
    environment = runtime_environment(project)
    environment[field] = value
    with pytest.raises(ValueError):
        entry.runtime_configuration(environment)


def test_runtime_local_https_configuration_is_valid(project):
    entry.runtime_configuration(runtime_environment(project))


def test_prepare_runs_guard_before_any_bootstrap_and_failed_guard_does_not_mutate(project, monkeypatch):
    runtime_environment(project)
    directory = project / "knowledge-base/state/run"
    directory.mkdir(parents=True)
    marker = directory / "docker-setup.json"
    marker.write_text("old success")
    calls = []
    monkeypatch.setattr(entry, "wait_for_services", lambda environment: calls.append("services"))

    def run(module, *arguments, environment):
        calls.append((module, arguments))
        raise subprocess.CalledProcessError(1, [module])

    monkeypatch.setattr(entry, "_run", run)
    before = (project / "backend/config/docker.env").read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        entry.prepare_application(project)
    assert calls[0] == "services"
    assert calls[1][0] == "scripts.installation_state"
    assert "--guard-install" in calls[1][1] and "--verify-server-collections" in calls[1][1]
    assert len(calls) == 2 and not marker.exists()
    assert (project / "backend/config/docker.env").read_bytes() == before


def test_prepare_order_commits_success_only_after_ingest(project, monkeypatch):
    runtime_environment(project)
    directory = project / "knowledge-base/state/run"
    directory.mkdir(parents=True)
    monkeypatch.setattr(entry, "wait_for_services", lambda environment: None)
    calls = []
    monkeypatch.setattr(entry, "_run", lambda module, *args, **kwargs: calls.append((module, args)))
    entry.prepare_application(project)
    assert [module for module, _ in calls] == ["scripts.installation_state", "scripts.bootstrap",
                                             "scripts.preflight", "scripts.bootstrap",
                                             "scripts.local_identity", "scripts.bootstrap"]
    assert calls[-1][1] == ("ingest",)
    assert calls[3][1] == ("migrate",)
    marker = json.loads((directory / "docker-setup.json").read_text())
    assert marker["mode"] == "docker_local" and marker["digests"]["LLM_FAST_DIGEST"] == "a" * 64


def test_serve_refuses_missing_state_before_status_can_create_empty_collections(project, monkeypatch):
    environment = runtime_environment(project)
    directory = project / "knowledge-base/state/run"
    directory.mkdir(parents=True)
    (directory / "docker-setup.json").write_text(json.dumps(entry._marker(environment)))
    calls = []

    def run(module, *arguments, environment):
        calls.append((module, arguments))
        raise subprocess.CalledProcessError(1, [module])

    monkeypatch.setattr(entry, "_run", run)
    monkeypatch.setattr(entry.os, "execve", lambda *args: pytest.fail("No debe abrir el backend"))
    with pytest.raises(subprocess.CalledProcessError):
        entry.serve(lock_descriptor=999, root=project)
    assert len(calls) == 1 and calls[0][0] == "scripts.installation_state"
    assert "--guard-install" in calls[0][1] and "--verify-server-collections" in calls[0][1]


def test_pin_models_accepts_separate_config_and_preserves_native(project, monkeypatch):
    initialize(project)
    native = project / "backend/config/.env"
    native.write_text("native settings must stay intact")
    monkeypatch.setattr(bootstrap, "PROJECT_ROOT", project)
    for key in prepare.PIN_KEYS:
        monkeypatch.delenv(key, raising=False)
    inventory = {"gemma4:latest": "a" * 64, "embeddinggemma:latest": "b" * 64}
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "models": [{"name": key, "digest": value} for key, value in inventory.items()],
    })))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    path = Path("backend/config/docker.env")
    assert bootstrap.cmd_pin_models(SimpleNamespace(env_file=path)) == 0
    assert native.read_text() == "native settings must stay intact"
    assert prepare.read_values(project / path)["LLM_FAST_DIGEST"] == "a" * 64


@pytest.mark.parametrize("name", ["outside.env", "backend/config/../../outside.env"])
def test_pin_models_refuses_config_outside_owned_directory(project, monkeypatch, name):
    (project / "outside.env").write_text("private synthetic")
    monkeypatch.setattr(bootstrap, "PROJECT_ROOT", project)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("No network for rejected path"))
    assert bootstrap.cmd_pin_models(SimpleNamespace(env_file=Path(name))) == 1


def test_state_explicit_config_rejects_escape_before_import(project):
    settings_file = project / "backend/app/config/settings.py"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text("# fixture")
    outside = project / "outside.env"
    outside.write_text("private synthetic")
    with pytest.raises(state.StateCheckError, match="configuration_missing"):
        state.load_target_settings(project, env_file=outside)


@pytest.mark.parametrize("present", [False, True])
def test_server_guard_checks_required_collections_but_never_claims_vector_verification(monkeypatch, present):
    settings = SimpleNamespace(qdrant_url="http://qdrant:6333", qdrant_api_key=SecretStr("synthetic-key"),
                               rag_collection_corporate="corporate", rag_collection_private="private")
    report = {"qdrant": {"mode": "server", "required_scopes": ["corporate"]},
              "issues": [], "vectors_verified": False}
    observed = []

    def respond(request):
        observed.append(request)
        return httpx.Response(200, json={"result": {"collections": [{"name": "corporate"}] if present else []}})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    state.verify_server_collections(report, settings)
    assert len(observed) == 1 and str(observed[0].url) == "http://qdrant:6333/collections"
    assert observed[0].headers["api-key"] == "synthetic-key"
    assert report["vectors_verified"] is False
    assert bool(report["issues"]) is not present
    assert report["qdrant"]["collections_verified"] is True
    assert "synthetic-key" not in json.dumps(report)
