# Creado por Aldo Garcia.
"""El gate no aprueba pasos obligatorios omitidos ni usa evidencia obsoleta."""

import json
from types import SimpleNamespace

import pytest

from scripts import run_quality_gate as gate


@pytest.mark.parametrize(
    ("status", "blocking", "expected", "code"),
    [
        (gate.SKIPPED, True, gate.INCOMPLETE, 2),
        (gate.INCOMPLETE, True, gate.INCOMPLETE, 2),
        (gate.FAIL, True, gate.FAIL, 1),
        (gate.SKIPPED, False, gate.PASS, 0),
        (gate.PASS, True, gate.PASS, 0),
    ],
)
def test_verdict_distinguishes_missing_required_evidence(tmp_path, monkeypatch, status, blocking, expected, code):
    monkeypatch.setattr(gate, "build_gates", lambda _args: [gate.GateResult("synthetic", status, blocking=blocking)])
    output = tmp_path / "result.json"
    assert gate.main(["--output", str(output)]) == code
    assert json.loads(output.read_text())["overall"] == expected


@pytest.mark.parametrize(("count", "skipped", "expected"), [(10, 2, gate.INCOMPLETE), (0, 0, gate.INCOMPLETE), (10, 0, gate.PASS)])
def test_junit_zero_exit_does_not_hide_skipped_cases(tmp_path, monkeypatch, count, skipped, expected):
    report = tmp_path / "junit.xml"

    def run(*_args, **_kwargs):
        report.write_text(f'<testsuites><testsuite tests="{count}" skipped="{skipped}"/></testsuites>')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate.subprocess, "run", run)
    result = gate._run(["synthetic"], cwd=tmp_path, name="integration", require_complete_junit=report)
    assert result.status == expected


def test_never_accepts_a_previous_junit_report(tmp_path, monkeypatch):
    report = tmp_path / "junit.xml"
    report.write_text('<testsuites><testsuite tests="10" skipped="0"/></testsuites>')
    monkeypatch.setattr(gate.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    result = gate._run(["synthetic"], cwd=tmp_path, name="integration", require_complete_junit=report)
    assert result.status == gate.FAIL


def test_failure_takes_precedence_over_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "build_gates", lambda _args: [
        gate.GateResult("missing", gate.SKIPPED), gate.GateResult("broken", gate.FAIL),
    ])
    assert gate.main(["--output", str(tmp_path / "result.json")]) == 1


def test_dependency_vulnerability_blocks_release_even_with_other_checks_passing(tmp_path, monkeypatch):
    """Un pip-audit fallido nunca puede reducirse a una advertencia informativa."""
    monkeypatch.setattr(gate, "TEST_REPORTS", tmp_path / "tests")
    monkeypatch.setattr(gate, "SECURITY_REPORTS", tmp_path / "security")
    monkeypatch.setattr(gate, "FRONTEND_ROOT", tmp_path / "frontend")
    monkeypatch.setattr(gate, "_launcher", lambda _name: None)

    def run(command, *, name, blocking=True, **_kwargs):
        status = gate.FAIL if "pip_audit" in command else gate.PASS
        return gate.GateResult(name, status, blocking=blocking)

    monkeypatch.setattr(gate, "_run", run)
    results = gate.build_gates(SimpleNamespace(with_e2e=False, fast=True))
    monkeypatch.setattr(gate, "build_gates", lambda _args: results)
    output = tmp_path / "result.json"
    assert gate.main(["--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert "auditoria de dependencias (pip-audit)" in report["blocking_failures"]


def test_audit_inventory_excludes_only_private_app_and_keeps_all_installed_third_parties(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.importlib.metadata, "distributions", lambda: [
        SimpleNamespace(metadata={"Name": "matrix-rh-backend"}, version="1.3.0"),
        SimpleNamespace(metadata={"Name": "fastapi"}, version="0.141.1"),
        SimpleNamespace(metadata={"Name": "PyYAML"}, version="6.0.2"),
        SimpleNamespace(metadata={"Name": "pip-audit"}, version="2.7.3"),
    ])
    inventory = tmp_path / "requirements.txt"
    gate._write_installed_dependencies(inventory)
    assert set(inventory.read_text().splitlines()) == {"fastapi==0.141.1", "pyyaml==6.0.2", "pip-audit==2.7.3"}


@pytest.mark.parametrize("distributions", [
    [],
    [SimpleNamespace(metadata={"Name": "bad\n-r other.txt"}, version="1")],
    [SimpleNamespace(metadata={"Name": "normal"}, version="1\n--index-url example.test")],
    [SimpleNamespace(metadata={"Name": "normal"}, version="1"),
     SimpleNamespace(metadata={"Name": "normal"}, version="2")],
])
def test_invalid_or_empty_audit_inventory_fails_closed(tmp_path, monkeypatch, distributions):
    monkeypatch.setattr(gate.importlib.metadata, "distributions", lambda: distributions)
    monkeypatch.setattr(gate, "SECURITY_REPORTS", tmp_path)
    result = gate._audit_dependencies("python")
    assert result.status == gate.FAIL
    assert result.blocking
