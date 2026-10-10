# Creado por Aldo Garcia.
"""Actualizacion de .env y parada cooperativa, con archivos/PID sinteticos."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from scripts.configuration_upgrade import ADDITIONS, upgrade_configuration
from scripts.runtime_control import runtime_identity, watch_shutdown

pytestmark = pytest.mark.unit

def env_path(root):
    path = root / "backend" / "config" / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


SYNTHETIC_CONFIG = (
    "# Configuracion sintetica: no contiene credenciales reales.\r\n"
    "APP_ENV=development\r\n"
    "APP_SECRET_KEY='synthetic-original-session-key-0123456789'\r\n"
    "MATRIX_SEED_PASSWORD=synthetic-original-seed-1234567890\r\n"
    "DATABASE_URL='mysql+pymysql://synthetic:replace_me@127.0.0.1/synthetic'\r\n"
    "OLLAMA_FAST_MODEL=gemma4:latest\r\n"
    "OLLAMA_DEEP_MODEL=qwen3.6:latest\r\n"
    "LLM_FAST_DIGEST=" + "a" * 64 + "\r\n"
    "LLM_DEEP_DIGEST=" + "b" * 64 + "\r\n"
    "LLM_EMBEDDING_DIGEST=" + "c" * 64 + "\r\n"
    "LLM_REQUEST_DEADLINE_SECONDS=120\r\n"
)


def backups(root: Path) -> list[Path]:
    return list((root / "knowledge-base" / "backups" / "configuration").glob("*.bak"))


def test_legacy_upgrade_backs_up_exact_bytes_preserves_secrets_models_and_pins(tmp_path, capsys):
    original = b"\xef\xbb\xbf" + SYNTHETIC_CONFIG.encode("utf-8")
    target = env_path(tmp_path)
    target.write_bytes(original)
    result = upgrade_configuration(tmp_path)
    copies = backups(tmp_path)
    assert len(copies) == 1
    assert copies[0].read_bytes() == original
    assert result["backup_created"] is True
    assert set(result["changed_keys"]) == {*ADDITIONS, "LLM_REQUEST_DEADLINE_SECONDS"}
    updated = target.read_text(encoding="utf-8-sig")
    for line in SYNTHETIC_CONFIG.splitlines():
        if not line.startswith("LLM_REQUEST_DEADLINE_SECONDS="):
            assert line in updated.splitlines()
    assert "LLM_REQUEST_DEADLINE_SECONDS=600" in updated.splitlines()
    assert all(f"{key}={value}" in updated.splitlines() for key, value in ADDITIONS.items())
    assert not capsys.readouterr().out
    assert {path.name for path in tmp_path.iterdir()} == {"backend", "knowledge-base"}
    assert {path.name for path in target.parent.iterdir()} == {".env"}


@pytest.mark.parametrize(
    "legacy",
    ("120", "120.0", "120.000", "'120'", '"120.0" # presupuesto anterior'),
)
def test_only_legacy_120_budget_is_migrated(tmp_path, legacy):
    target = env_path(tmp_path)
    target.write_text(f"LLM_REQUEST_DEADLINE_SECONDS={legacy}\n", encoding="utf-8")
    upgrade_configuration(tmp_path)
    assert target.read_text(encoding="utf-8").splitlines()[0].startswith("LLM_REQUEST_DEADLINE_SECONDS=600")


@pytest.mark.parametrize("custom", ("30", "90", "180", "300", "600", "120.5", '"240" # ajuste de TI'))
def test_custom_deadline_and_operational_choices_are_preserved(tmp_path, custom):
    lines = [
        f"LLM_REQUEST_DEADLINE_SECONDS={custom}",
        "LLM_FAST_TIMEOUT_SECONDS=80", "LLM_DEEP_TIMEOUT_SECONDS=240",
        "ANSWER_ALLOW_GENERAL_KNOWLEDGE=false", "ANSWER_EVIDENCE_MODE=extractive",
        "LLM_FAST_THINKING=enabled", "LLM_DEEP_THINKING=disabled", "LLM_STRUCTURED_THINKING=auto",
        "LLM_COMPLETION_RETRIES=0", "OLLAMA_FAST_MAX_TOKENS=900", "OLLAMA_DEEP_MAX_TOKENS=3000",
    ]
    target = env_path(tmp_path)
    original = ("\n".join(lines) + "\n").encode("utf-8")
    target.write_bytes(original)
    result = upgrade_configuration(tmp_path)
    assert result == {"changed_keys": [], "backup_created": False}
    assert target.read_bytes() == original
    assert backups(tmp_path) == []


@pytest.mark.parametrize("custom", ("90", "300", '"240" # ajuste de TI'))
def test_custom_deadline_is_preserved_when_missing_settings_require_a_write(tmp_path, custom):
    target = env_path(tmp_path)
    line = f"export LLM_REQUEST_DEADLINE_SECONDS = {custom}"
    original = (line + "\nAPP_SECRET_KEY=synthetic-existing-key\n").encode()
    target.write_bytes(original)
    result = upgrade_configuration(tmp_path)
    updated = target.read_text(encoding="utf-8")
    assert line in updated.splitlines()
    assert set(result["changed_keys"]) == set(ADDITIONS)
    assert backups(tmp_path)[0].read_bytes() == original


def test_missing_new_settings_are_added_without_generating_any_credentials_or_digest(tmp_path):
    target = env_path(tmp_path)
    target.write_text("APP_ENV=development\nOLLAMA_FAST_MODEL=custom-local\n", encoding="utf-8")
    result = upgrade_configuration(tmp_path)
    keys = {
        line.split("=", 1)[0]
        for line in target.read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    assert keys == {"APP_ENV", "OLLAMA_FAST_MODEL", "LLM_REQUEST_DEADLINE_SECONDS", *ADDITIONS}
    assert set(result["changed_keys"]) == {*ADDITIONS, "LLM_REQUEST_DEADLINE_SECONDS"}
    assert not any("SECRET" in key or "PASSWORD" in key or "DIGEST" in key for key in keys)


def test_second_upgrade_is_idempotent_without_another_backup(tmp_path):
    target = env_path(tmp_path)
    target.write_text(SYNTHETIC_CONFIG, encoding="utf-8")
    first = upgrade_configuration(tmp_path)
    assert first["backup_created"] is True
    current = target.read_bytes()
    copies = backups(tmp_path)
    second = upgrade_configuration(tmp_path)
    assert second == {"changed_keys": [], "backup_created": False}
    assert target.read_bytes() == current
    assert backups(tmp_path) == copies


def test_atomic_replace_failure_retains_original_env_backup_and_removes_temporary_file(tmp_path, monkeypatch):
    target = env_path(tmp_path)
    original = SYNTHETIC_CONFIG.encode("utf-8")
    target.write_bytes(original)

    def denied_replace(_temporary, _target):
        raise PermissionError("Reemplazo sintetico bloqueado")

    monkeypatch.setattr(Path, "replace", denied_replace)
    with pytest.raises(PermissionError, match="bloqueado"):
        upgrade_configuration(tmp_path)
    assert target.read_bytes() == original
    assert len(backups(tmp_path)) == 1
    assert backups(tmp_path)[0].read_bytes() == original
    assert {path.name for path in tmp_path.iterdir()} == {"backend", "knowledge-base"}
    assert {path.name for path in target.parent.iterdir()} == {".env"}


@pytest.mark.parametrize("key", ("LLM_REQUEST_DEADLINE_SECONDS", *ADDITIONS))
def test_duplicate_operational_keys_are_rejected_before_backup_or_mutation(tmp_path, key):
    target = env_path(tmp_path)
    original = (
        f"{key}=120\nexport {key.lower()} = 600\n"
        "APP_SECRET_KEY=synthetic-do-not-replace\n"
    ).encode()
    target.write_bytes(original)
    with pytest.raises(ValueError, match="duplicadas"):
        upgrade_configuration(tmp_path)
    assert target.read_bytes() == original
    assert backups(tmp_path) == []
    assert {path.name for path in tmp_path.iterdir()} == {"backend"}
    assert {path.name for path in target.parent.iterdir()} == {".env"}


@pytest.mark.parametrize("kind", ("missing", "directory", "symlink"))
def test_configuration_upgrade_requires_regular_local_env(tmp_path, kind):
    target = env_path(tmp_path)
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        original = tmp_path / "external-config"
        original.write_text("APP_SECRET_KEY=synthetic-original\n", encoding="utf-8")
        target.symlink_to(original)
    with pytest.raises(ValueError, match="local regular"):
        upgrade_configuration(tmp_path)
    assert backups(tmp_path) == []
    if kind == "symlink":
        assert original.read_text(encoding="utf-8") == "APP_SECRET_KEY=synthetic-original\n"


class OneIterationThenFinished:
    """Ejercita exactamente una inspeccion sin esperas ni temporizadores fragiles."""

    def __init__(self):
        self.calls = 0

    def wait(self, timeout):
        assert timeout == 0.25
        self.calls += 1
        assert self.calls <= 2, "El watcher debe atender la senal finished."
        return self.calls == 2


def test_shutdown_request_for_this_pid_is_consumed_once(tmp_path):
    request = tmp_path / "backend.stop"
    request.write_text(json.dumps({"pid": 4242, "created_at": 123.5}), encoding="ascii")
    server = SimpleNamespace(should_exit=False)
    finished = OneIterationThenFinished()
    watch_shutdown(server, request, finished, process_id=4242, created_at=123.5)
    assert server.should_exit is True
    assert not request.exists()
    assert finished.calls == 1


def test_shutdown_without_override_targets_current_process_pid(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.runtime_control.os.getpid", lambda: 8383)
    request = tmp_path / "backend.stop"
    request.write_text(json.dumps({"pid": 8383, "created_at": 123.5}), encoding="ascii")
    monkeypatch.setattr("scripts.runtime_control.psutil.Process", lambda _pid: SimpleNamespace(create_time=lambda: 123.5))
    server = SimpleNamespace(should_exit=False)
    watch_shutdown(server, request, OneIterationThenFinished())
    assert server.should_exit is True
    assert not request.exists()


@pytest.mark.parametrize("content", (b"4243", b"4242 extra", b"-4242", b"\xff", b"4242" + b" " * 509))
def test_wrong_pid_invalid_encoding_and_oversize_requests_are_ignored(tmp_path, content):
    request = tmp_path / "backend.stop"
    request.write_bytes(content)
    server = SimpleNamespace(should_exit=False)
    finished = OneIterationThenFinished()
    watch_shutdown(server, request, finished, process_id=4242, created_at=123.5)
    assert server.should_exit is False
    assert request.read_bytes() == content
    assert finished.calls == 2


def test_shutdown_rejects_symlink_without_deleting_target_or_link(tmp_path):
    target = tmp_path / "original-stop-request"
    target.write_text("4242", encoding="ascii")
    request = tmp_path / "backend.stop"
    request.symlink_to(target)
    server = SimpleNamespace(should_exit=False)
    watch_shutdown(server, request, OneIterationThenFinished(), process_id=4242, created_at=123.5)
    assert server.should_exit is False
    assert request.is_symlink()
    assert target.read_text(encoding="ascii") == "4242"


def test_shutdown_accepts_size_boundary_but_rejects_larger_request(tmp_path):
    request = tmp_path / "backend.stop"
    payload = json.dumps({"pid": 4242, "created_at": 123.5}).encode()
    request.write_bytes(payload + b" " * (512 - len(payload)))
    assert request.stat().st_size == 512
    server = SimpleNamespace(should_exit=False)
    watch_shutdown(server, request, OneIterationThenFinished(), process_id=4242, created_at=123.5)
    assert server.should_exit is True
    assert not request.exists()


def test_finished_event_stops_idle_watcher_without_request_or_server_exit(tmp_path):
    finished = Event()
    server = SimpleNamespace(should_exit=False)
    request = tmp_path / "backend.stop"
    worker = Thread(target=watch_shutdown, args=(server, request, finished), kwargs={"process_id": 4242, "created_at": 123.5})
    worker.start()
    finished.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert server.should_exit is False
    assert not request.exists()


def test_already_finished_event_does_not_consume_request(tmp_path):
    finished = Event()
    finished.set()
    request = tmp_path / "backend.stop"
    request.write_text("4242", encoding="ascii")
    server = SimpleNamespace(should_exit=False)
    watch_shutdown(server, request, finished, process_id=4242, created_at=123.5)
    assert server.should_exit is False
    assert request.read_text(encoding="ascii") == "4242"


@pytest.mark.parametrize("created_at", [122.5, None, True, "123.5", float("nan"), float("inf")])
def test_shutdown_refuses_reused_pid_and_invalid_creation_time(tmp_path, created_at):
    request = tmp_path / "backend.stop"
    request.write_text(json.dumps({"pid": 4242, "created_at": created_at}), encoding="ascii")
    server = SimpleNamespace(should_exit=False)
    watch_shutdown(server, request, OneIterationThenFinished(), process_id=4242, created_at=123.5)
    assert server.should_exit is False
    assert request.exists()


def test_runtime_identity_registers_real_backend_and_removes_only_own_record(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.runtime_control.psutil.Process", lambda _pid: SimpleNamespace(pid=4242, create_time=lambda: 123.5))
    path = tmp_path / "knowledge-base/state/run/matrixrh-backend.identity.json"
    with runtime_identity(tmp_path) as identity:
        assert identity["pid"] == 4242
        assert identity["created_at"] == 123.5
        assert identity["root"] == str(tmp_path.resolve())
        assert json.loads(path.read_text()) == identity
    assert not path.exists()


def test_old_runtime_exit_never_deletes_replacement_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.runtime_control.psutil.Process", lambda _pid: SimpleNamespace(pid=4242, create_time=lambda: 123.5))
    path = tmp_path / "knowledge-base/state/run/matrixrh-backend.identity.json"
    with runtime_identity(tmp_path):
        replacement = {"pid": 4343, "created_at": 555.5, "root": str(tmp_path), "nonce": "a" * 32}
        path.write_text(json.dumps(replacement))
    assert json.loads(path.read_text()) == replacement
