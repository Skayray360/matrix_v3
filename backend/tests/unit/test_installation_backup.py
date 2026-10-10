# Creado por Aldo Garcia.
"""Continuidad de archivos nativos con datadir/binarios sinteticos.

No abre SQL ni modelos; no acredita restauracion/recovery de MySQL real.
Todos los procesos y listeners son dobles de prueba, incluidos metadatos CIM.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from scripts import installation_backup as backup

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_services(monkeypatch):
    for key in backup.ENV_SETTINGS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(backup.psutil, "process_iter", lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(backup.psutil, "net_connections", lambda **kwargs: [])

    def missing(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(backup.psutil, "Process", missing)


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "native"
    root.mkdir()
    # Layout instalado independiente de las constantes del verificador: un
    # cambio de contrato incompatible debe fallar, no recrear el mismo error.
    for relative in (
        "backend/config", "backend/runtime/venv", "backend/runtime/python", "backend/app",
        "knowledge-base/documents", "knowledge-base/unclassified",
        "knowledge-base/state/mysql", "knowledge-base/state/qdrant", "knowledge-base/state/uploads",
        "knowledge-base/state/run", "backend/scripts/windows", "backend/config/apache", "backend/release",
        "frontend/dist",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in (
        "backend/config/.env", "backend/config/mysql.ini", "backend/pyproject.toml", "backend/uv.lock",
        "backend/runtime/mysql/bin/mysqld.exe", "backend/runtime/mysql/.matrix-package.json",
        "backend/runtime/venv/Scripts/python.exe",
        "frontend/package-lock.json", "frontend/dist/index.html", "README.md", ".htaccess",
        "instalar.bat", "iniciar.bat", "detener.bat", "diagnosticar.bat",
        "backend/scripts/windows/MatrixRH.ps1", "backend/scripts/windows/launch_process.py",
        "backend/scripts/installation_backup.py", "backend/config/runtime-manifest.json",
        "backend/config/apache/matrix-rh.conf.template", "backend/release/BUILD_INFO.json",
        "backend/release/SHA256SUMS.txt", "backend/release/layout-migration.json",
        "knowledge-base/state/mysql/auto.cnf",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"SYNTHETIC {relative}\n", encoding="utf-8")
    env = {
        **backup.PATH_SETTINGS, "APP_HOST": "127.0.0.1", "APP_PORT": "8000",
        # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
        "DATABASE_URL": "mysql+pymysql://fixture:PRIVATE_FIXTURE@127.0.0.1:3308/matrix_fixture",
        "MATRIX_MYSQL_PORT": "3308", "QDRANT_MODE": "embedded",
        "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
    }
    (root / "backend/config/.env").write_text("\n".join(f"{key}={value}" for key, value in env.items()), encoding="utf-8")
    (root / "backend/config/mysql.ini").write_text(
        f'[mysqld]\nbasedir="{root / "backend/runtime/mysql"}"\n'
        f'datadir="{root / "knowledge-base/state/mysql"}"\nport=3308\nbind-address=127.0.0.1\n'
        'mysqlx=0\nskip-name-resolve\n', encoding="utf-8")
    receipt = {"version": "mysqld synthetic-8.x-fixture", "source": "synthetic-donor",
               "executable_sha256": hashlib.sha256((root / backup.MYSQL_EXE).read_bytes()).hexdigest().upper()}
    (root / backup.MYSQL_RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
    sample_files = {
        "knowledge-base/state/mysql/mysql.ibd": b"SYNTHETIC MYSQL DATADIR, NOT A REAL DB",
        "knowledge-base/state/qdrant/collection/storage.sqlite": b"SYNTHETIC VECTOR DATA",
        "knowledge-base/state/uploads/owner-a/private.txt": b"PRIVATE_DOCUMENT_FIXTURE",
        "knowledge-base/state/run/installation-state.json": b'{"installed":true}',
        "knowledge-base/documents/prestaciones/politica.md": b"DOCUMENT_FIXTURE",
        "knowledge-base/unclassified/sample.md": b"UNCLASSIFIED_FIXTURE",
        "backend/config/secrets/local-account.txt": b"PRIVATE_ACCOUNT_FIXTURE",
        "backend/runtime/mysql/lib/dependency.dll": b"MYSQL_RUNTIME_DEPENDENCY_FIXTURE",
        "backend/runtime/python/python.exe": b"PYTHON_FIXTURE",
        "backend/runtime/venv/pyvenv.cfg": b"home = original/local/path",
        "backend/runtime/uv/uv.exe": b"UV_FIXTURE",
        "backend/app/main.py": b"# synthetic code",
        "backend/scripts/bootstrap.py": b"# synthetic launcher",
    }
    for relative, content in sample_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def test_backup_contract_matches_the_actual_native_delivery_layout():
    project = Path(__file__).resolve().parents[3]
    batches = {"instalar.bat", "iniciar.bat", "detener.bat", "diagnosticar.bat"}
    assert {path.name for path in project.glob("*.bat")} == batches
    shipped = batches | {
        ".htaccess", "backend/scripts/windows/MatrixRH.ps1", "backend/scripts/windows/launch_process.py",
        "backend/scripts/installation_backup.py", "backend/config/runtime-manifest.json",
        "backend/config/apache/matrix-rh.conf.template", "backend/release/BUILD_INFO.json",
        "backend/release/SHA256SUMS.txt", "backend/release/layout-migration.json",
    }
    assert shipped <= set(backup.REQUIRED_FILES)
    assert all((project / relative).is_file() for relative in shipped)
    assert "backend/scripts/windows" in backup.REQUIRED_DIRS and "windows" not in backup.REQUIRED_DIRS
    # Ningun archivo requerido de codigo puede depender de un nombre retirado.
    for relative in backup.REQUIRED_FILES:
        if (relative not in {"backend/config/.env", "backend/config/mysql.ini"}
                and not relative.startswith(("backend/runtime/", "knowledge-base/state/"))):
            assert (project / relative).is_file(), relative


def change_env(root, key, value):
    path = root / "backend/config/.env"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(f"{key}={value}" if line.startswith(key + "=") else line for line in lines), encoding="utf-8")


def test_default_backup_includes_datadir_secrets_and_owned_runtimes_without_external_ollama(installation):
    root = installation
    previous = root / "knowledge-base/backups/older/should-not-recurse.txt"
    previous.parent.mkdir(parents=True)
    previous.write_text("older backup fixture", encoding="utf-8")
    source_before = backup._inventory(root, source=True)
    result = backup.create_backup(root)
    destination = Path(result["backup_path"])
    assert destination.parent == root / "knowledge-base/backups"
    assert not (destination / backup.INCOMPLETE).exists()
    verified = backup.verify_backup(destination)
    assert (verified["directories"], verified["files"]) == source_before
    assert backup._inventory(root, source=True) == source_before
    assert verified["mysql_recovery_tested"] is False
    assert verified["automatic_restore_supported"] is False
    assert verified["runtime_binaries_included"] is True
    for relative in ("backend/config/secrets/local-account.txt", "backend/runtime/mysql/lib/dependency.dll",
                     "backend/runtime/python/python.exe", "backend/runtime/uv/uv.exe", "backend/runtime/venv/pyvenv.cfg",
                     "knowledge-base/state/mysql/mysql.ibd",
                     "knowledge-base/state/run/installation-state.json", "backend/uv.lock"):
        assert (destination / backup.PAYLOAD / relative).read_bytes() == (root / relative).read_bytes()
    assert not (destination / backup.PAYLOAD / "knowledge-base/backups").exists()


def test_explicit_new_local_destination_is_supported(installation, tmp_path):
    destination = tmp_path / "backup"
    assert backup.create_backup(installation, destination)["backup_path"] == str(destination)
    assert backup.verify_backup(destination)["state"] == "complete"


def test_existing_destination_and_origin_are_never_overwritten(installation, tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "private.txt").write_bytes(b"KEEP")
    with pytest.raises(FileExistsError):
        backup.create_backup(installation, destination)
    assert (destination / "private.txt").read_bytes() == b"KEEP"
    with pytest.raises(backup.BackupError, match="contener el origen"):
        backup.create_backup(installation, installation)


@pytest.mark.parametrize("setting", list(backup.PATH_SETTINGS))
def test_configured_data_outside_the_project_blocks_backup(installation, tmp_path, setting):
    external = tmp_path / "external"
    external.mkdir()
    change_env(installation, setting, str(external))
    with pytest.raises(backup.BackupError, match="fuera del almacenamiento"):
        backup.create_backup(installation, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_environment_override_is_rejected(installation, tmp_path, monkeypatch):
    # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://foreign:PRIVATE_OTHER@127.0.0.1:3306/foreign")
    with pytest.raises(backup.BackupError, match="variable de entorno"):
        backup.create_backup(installation, tmp_path / "rejected")


def test_no_empty_database_or_unmatched_mysql_binary_is_accepted(installation, tmp_path):
    (installation / "knowledge-base/state/mysql/mysql.ibd").unlink()
    with pytest.raises(backup.BackupError, match="datadir MySQL"):
        backup.create_backup(installation, tmp_path / "missing-datadir")
    (installation / backup.MYSQL_EXE).write_bytes(b"CHANGED MYSQL BINARY")
    with pytest.raises(backup.BackupError, match="identidad del runtime MySQL"):
        backup.create_backup(installation, tmp_path / "changed-runtime")
    assert not (tmp_path / "changed-runtime").exists()


@pytest.mark.parametrize("relative", ["knowledge-base/state/uploads/link", "backend/runtime/mysql/link"])
def test_source_symbolic_links_never_enter_backup(installation, tmp_path, relative):
    target = tmp_path / "outside.txt"
    target.write_bytes(b"PRIVATE_EXTERNAL_FIXTURE")
    link = installation / relative
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("El sistema no permite crear enlaces de prueba")
    with pytest.raises(backup.BackupError, match="enlaces"):
        backup.create_backup(installation, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_reparse_point_metadata_is_rejected_without_following_windows_junction(tmp_path, monkeypatch):
    path = tmp_path / "junction"
    path.mkdir()
    original = Path.lstat

    def lstat(candidate):
        value = original(candidate)
        if candidate == path:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(backup.BackupError, match="junctions"):
        backup._safe_path(path)


def test_destination_parent_links_traversal_and_project_data_paths_are_rejected(installation, tmp_path):
    with pytest.raises(backup.BackupError, match="traversal"):
        backup.create_backup(installation, tmp_path / "unused/../rejected")
    with pytest.raises(backup.BackupError, match="solo se permite"):
        backup.create_backup(installation, installation / "knowledge-base/documents/rejected")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(installation / "knowledge-base/documents", target_is_directory=True)
    except OSError:
        pytest.skip("El sistema no permite crear enlaces de prueba")
    with pytest.raises(backup.BackupError, match="enlaces"):
        backup.create_backup(installation, linked / "rejected")


@pytest.mark.parametrize("relative,name", [(backup.MYSQL_EXE, "mysqld.exe"), (backup.OLLAMA_EXE, "ollama.exe"),
                                          ("backend/runtime/python/python.exe", "python.exe")])
def test_live_owned_service_blocks_even_without_pid_record_or_listener(installation, tmp_path, monkeypatch, relative, name):
    info = {"pid": 123456, "name": name, "exe": str(installation / relative), "cwd": str(tmp_path), "cmdline": [name]}
    monkeypatch.setattr(backup.psutil, "process_iter", lambda *args, **kwargs: iter([SimpleNamespace(pid=123456, info=info)]))
    with pytest.raises(backup.BackupError, match="sigue vivo"):
        backup.create_backup(installation, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_reused_pid_and_unrelated_wamp_process_do_not_authorize_termination(installation, tmp_path, monkeypatch):
    record = {"pid": 123456, "executable": str(installation / backup.MYSQL_EXE),
              "created_ticks": str(621355968000000000 + 1_000_000_000 * 10_000_000)}
    (installation / "knowledge-base/state/run/mysql.json").write_text(json.dumps(record), encoding="utf-8")
    info = {"pid": 123456, "name": "mysqld.exe", "exe": str(tmp_path / "wamp/mysql/bin/mysqld.exe"),
            "cwd": str(tmp_path / "wamp"), "cmdline": ["mysqld.exe"]}
    process = SimpleNamespace(pid=123456, info=info, create_time=lambda: 1_100_000_000,
                              terminate=lambda: pytest.fail("No se debe detener ningun proceso"))
    monkeypatch.setattr(backup.psutil, "Process", lambda pid: process)
    monkeypatch.setattr(backup.psutil, "process_iter", lambda *args, **kwargs: iter([process]))
    assert backup.create_backup(installation, tmp_path / "snapshot")["state"] == "complete"


@pytest.mark.parametrize("service", ["backend", "mysql", "ollama"])
def test_live_epoch_and_cim_identities_block_backup(installation, tmp_path, monkeypatch, service):
    created = 1_700_000_000.125
    if service == "backend":
        name = "matrixrh-backend.identity.json"
        record = {"pid": 123456, "created_at": created, "root": str(installation), "nonce": "0" * 32}
    else:
        name = f"{service}.json"
        executable = backup.MYSQL_EXE if service == "mysql" else backup.OLLAMA_EXE
        record = {"pid": 123456, "executable": str(installation / executable),
                  "created_ticks": str(621355968000000000 + int(created * 10_000_000))}
    (installation / "knowledge-base/state/run" / name).write_text(json.dumps(record), encoding="utf-8")
    process = SimpleNamespace(create_time=lambda: created, terminate=lambda: pytest.fail("No se debe detener"))
    monkeypatch.setattr(backup.psutil, "Process", lambda pid: process)
    with pytest.raises(backup.BackupError, match="registrado vivo"):
        backup.create_backup(installation, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_listener_collision_blocks_even_when_no_process_can_be_attributed(installation, tmp_path, monkeypatch):
    connection = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=3308), pid=None)
    monkeypatch.setattr(backup.psutil, "net_connections", lambda **kwargs: [connection])
    with pytest.raises(backup.BackupError, match="puerto configurado"):
        backup.create_backup(installation, tmp_path / "rejected")


def test_opaque_process_table_fails_closed(installation, tmp_path, monkeypatch):
    def unavailable(**kwargs):
        raise psutil.AccessDenied()

    monkeypatch.setattr(backup.psutil, "net_connections", unavailable)
    with pytest.raises(backup.BackupError, match="inspeccionar procesos"):
        backup.create_backup(installation, tmp_path / "rejected")


def test_changed_source_leaves_partial_copy_without_valid_manifest(installation, tmp_path, monkeypatch):
    original_copy = backup._copy_file
    changed = False

    def copy_and_change(source, destination, entry, checkpoint):
        nonlocal changed
        original_copy(source, destination, entry, checkpoint)
        if not changed:
            source.write_bytes(b"MODIFIED DURING BACKUP")
            changed = True

    monkeypatch.setattr(backup, "_copy_file", copy_and_change)
    destination = tmp_path / "interrupted"
    with pytest.raises(backup.BackupError, match="cambio"):
        backup.create_backup(installation, destination)
    assert (destination / backup.INCOMPLETE).is_file()
    assert not (destination / backup.MANIFEST).exists()
    with pytest.raises(backup.BackupError, match="incompleto"):
        backup.verify_backup(destination)


def test_process_restart_at_final_check_never_publishes_a_valid_backup(installation, tmp_path, monkeypatch):
    calls = 0

    def stopped(root, configuration):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise backup.BackupError("Servicio reiniciado; copia incompleta.")

    monkeypatch.setattr(backup, "assert_project_stopped", stopped)
    monkeypatch.setattr(backup.time, "monotonic", lambda: 0)
    destination = tmp_path / "interrupted"
    with pytest.raises(backup.BackupError, match="reiniciado"):
        backup.create_backup(installation, destination)
    assert (destination / backup.MANIFEST).exists()
    assert (destination / backup.INCOMPLETE).exists()
    with pytest.raises(backup.BackupError, match="incompleto"):
        backup.verify_backup(destination)


@pytest.mark.parametrize("damage", ["changed-data", "orphan-payload", "orphan-root", "traversal", "duplicate"])
def test_verification_rejects_corruption_orphan_files_and_manifest_attacks(installation, tmp_path, damage):
    destination = tmp_path / "snapshot"
    backup.create_backup(installation, destination)
    if damage == "changed-data":
        (destination / backup.PAYLOAD / "knowledge-base/state/mysql/mysql.ibd").write_bytes(b"ALTERED")
    elif damage == "orphan-payload":
        (destination / backup.PAYLOAD / "orphan.txt").write_bytes(b"ORPHAN")
    elif damage == "orphan-root":
        (destination / "orphan.txt").write_bytes(b"ORPHAN")
    else:
        path = destination / backup.MANIFEST
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if damage == "traversal":
            manifest["files"][0]["path"] = "../outside.txt"
        else:
            manifest["files"].append(dict(manifest["files"][0]))
        path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(backup.BackupError):
        backup.verify_backup(destination)


def test_cli_reports_only_safe_summary_and_cold_sql_limitation(installation, tmp_path, capsys):
    destination = tmp_path / "snapshot"
    assert backup.main(["backup", "--project-root", str(installation), "--destination", str(destination)]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["mysql_recovery_tested"] is False
    assert result["activation"] == "not_performed"
    assert "PRIVATE_" not in output
    assert backup.main(["verify", "--backup", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    # secrets-scan: allow (valor sintetico aislado; ninguna credencial operativa)
    change_env(installation, "DATABASE_URL", "mysql+pymysql://fixture:PRIVATE_FIXTURE@127.0.0.1:NOT_A_PORT/db")
    assert backup.main(["backup", "--project-root", str(installation), "--destination", str(tmp_path / "bad")]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is False
    assert "PRIVATE_" not in output


def test_external_ollama_can_stay_running_while_backup_is_created(installation, tmp_path, monkeypatch):
    info = {"pid": 123456, "name": "ollama.exe", "exe": str(tmp_path / "user/ollama/ollama.exe"),
            "cwd": str(installation), "cmdline": ["ollama", "serve"]}
    process = SimpleNamespace(pid=123456, info=info, terminate=lambda: pytest.fail("External service must remain alive"))
    monkeypatch.setattr(backup.psutil, "process_iter", lambda *args, **kwargs: iter([process]))
    listener = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=11434), pid=123456)
    monkeypatch.setattr(backup.psutil, "net_connections", lambda **kwargs: [listener])
    result = backup.create_backup(installation, tmp_path / "snapshot")
    assert result["state"] == "complete"
    assert result["configured_service_ports"] == {"backend": 8000, "mysql": 3308}
    assert result["external_services"]["ollama"] == {
        "port": 11434, "management": "external_local", "runtime_and_weights_included": False,
    }
    assert not (Path(result["backup_path"]) / backup.PAYLOAD / "backend/runtime/ollama").exists()


def test_backup_preserves_legacy_local_weights_if_present(installation, tmp_path):
    model = installation / "knowledge-base/models/blobs/synthetic-model"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"LEGACY SYNTHETIC WEIGHTS - DO NOT DELETE")
    result = backup.create_backup(installation, tmp_path / "snapshot")
    assert (Path(result["backup_path"]) / backup.PAYLOAD / model.relative_to(installation)).read_bytes() == model.read_bytes()
