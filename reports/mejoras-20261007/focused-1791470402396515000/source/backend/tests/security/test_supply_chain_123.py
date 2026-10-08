# Creado por Aldo Garcia.
"""Gates de instalacion con IOC sinteticos; nunca descargan malware."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import run_quality_gate as gate
from scripts import verify_supply_chain as vsc

pytestmark = [pytest.mark.security, pytest.mark.unit]
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def npm_tree(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "node_modules").mkdir()
    (frontend / "package.json").write_text(json.dumps({
        "packageManager": "npm@11.9.0+sha512." + "a" * 128,
        "dependencies": {"normal": "1.0.0"},
    }))
    (frontend / ".npmrc").write_text("ignore-scripts=true\n")
    (frontend / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3, "packages": {
            "": {"dependencies": {"normal": "1.0.0"}},
            "node_modules/normal": {
                "version": "1.0.0", "resolved": "https://registry.npmjs.org/normal/-/normal-1.0.0.tgz",
                "integrity": "sha512-" + "a" * 86 + "==",
            },
        },
    }))
    package = frontend / "node_modules/normal"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"name": "normal", "version": "1.0.0"}))
    deny = tmp_path / "deny.yaml"
    deny.write_text(yaml.safe_dump({
        "incidents": [{"id": "synthetic", "packages": [{"name": "blocked", "versions": ["1.2.3"]}]}],
        "indicators": {"content_patterns": [r"webhook\.site"], "filenames": ["shai-hulud*"]},
    }))
    monkeypatch.setattr(vsc, "FRONTEND", frontend)
    monkeypatch.setattr(vsc, "NODE_MODULES", frontend / "node_modules")
    monkeypatch.setattr(vsc, "DENYLIST_PATH", deny)
    return frontend


def change_lock(frontend, callback):
    path = frontend / "package-lock.json"
    data = json.loads(path.read_text())
    callback(data)
    path.write_text(json.dumps(data))


def kinds(report):
    return {f.kind for f in report.blocking}


def test_clean_lock_and_installed_tree_pass(npm_tree):
    report = vsc.run()
    assert report.ok, report.as_dict()
    assert report.checked_packages == 1


def test_blocked_optional_package_rejected_before_download(npm_tree):
    def update(data):
        data["packages"]["node_modules/blocked"] = {
            **data["packages"]["node_modules/normal"], "version": "1.2.3", "optional": True,
        }
    change_lock(npm_tree, update)
    assert "paquete_en_lista_de_bloqueo" in kinds(vsc.run(lock_only=True))
    assert not (npm_tree / "node_modules/blocked").exists()


def test_lock_only_explicitly_does_not_claim_installed_tree(npm_tree):
    report = vsc.run(lock_only=True)
    assert report.ok
    assert report.checked_packages == 0
    assert "pendiente" in report.checks["arbol_instalado"]


@pytest.mark.parametrize("field,value", [("resolved", "http://example.test/pkg.tgz"), ("integrity", ""), ("link", True)])
def test_lock_rejects_non_reproducible_download(npm_tree, field, value):
    change_lock(npm_tree, lambda d: d["packages"]["node_modules/normal"].update({field: value}))
    assert "lock_sin_integridad" in kinds(vsc.run(lock_only=True))


def test_lock_manifest_dependency_mismatch_fails_before_download(npm_tree):
    change_lock(npm_tree, lambda d: d["packages"][""].update({"dependencies": {"normal": "2.0.0"}}))
    assert "lock_invalido" in kinds(vsc.run(lock_only=True))


@pytest.mark.parametrize("value", ["{}", "not JSON"])
def test_invalid_lock_cannot_pass(npm_tree, value):
    (npm_tree / "package-lock.json").write_text(value)
    assert not vsc.run(lock_only=True).ok


def test_missing_installed_tree_is_not_a_success(npm_tree):
    (npm_tree / "node_modules/normal/package.json").unlink()
    assert {"arbol_instalado_ausente", "paquete_del_lock_ausente"} <= kinds(vsc.run())


def test_different_installed_version_is_blocked(npm_tree):
    (npm_tree / "node_modules/normal/package.json").write_text('{"name":"normal","version":"2.0.0"}')
    assert "identidad_difiere_del_lock" in kinds(vsc.run())


def test_malformed_installed_manifest_is_not_silently_skipped(npm_tree):
    (npm_tree / "node_modules/normal/package.json").write_text("invalid")
    assert "manifiesto_instalado_invalido" in kinds(vsc.run())


def test_extra_package_outside_lock_is_blocked(npm_tree):
    extra = npm_tree / "node_modules/extra"
    extra.mkdir()
    (extra / "package.json").write_text('{"name":"extra","version":"1.0.0"}')
    assert "paquete_fuera_del_lock" in kinds(vsc.run())


def test_optional_other_platform_package_can_be_absent(npm_tree):
    change_lock(npm_tree, lambda d: d["packages"].update({
        "node_modules/platform-other": {**d["packages"]["node_modules/normal"], "optional": True},
    }))
    assert vsc.run().ok


@pytest.mark.parametrize("name", ["test", "lib", "src", "dist"])
def test_real_package_named_like_source_directory_is_scanned(npm_tree, name):
    target = npm_tree / "node_modules" / name
    target.mkdir()
    (target / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0"}))
    assert name in {row[0] for row in vsc.iter_installed_packages(npm_tree / "node_modules")}


@pytest.mark.parametrize("payload", ["ignore-scripts=true\nignore-scripts=false\n", "ignore-scripts=true-but-not-really\n"])
def test_npmrc_override_cannot_disguise_enabled_lifecycle(npm_tree, payload):
    (npm_tree / ".npmrc").write_text(payload)
    assert "scripts_de_instalacion_habilitados" in kinds(vsc.run())


@pytest.mark.parametrize("suffix", [".js", ".json", ".ps1", ".sh", ".py", ""])
def test_large_file_ioc_across_block_boundary_is_detected(npm_tree, suffix):
    payload = " " * (vsc.MAX_SCAN_BYTES - 4) + "webhook.site" + " " * 200
    (npm_tree / "node_modules/normal" / ("payload" + suffix)).write_text(payload)
    assert "ioc_contenido" in kinds(vsc.run())


def test_external_executable_link_is_blocked(npm_tree, tmp_path):
    external = tmp_path / "external.js"
    external.write_text("// synthetic")
    (npm_tree / "node_modules/normal/bin.js").symlink_to(external)
    assert "enlace_fuera_del_arbol" in kinds(vsc.run())


@pytest.mark.parametrize("failure", ["cadena de suministro (npm fijado)", "npm audit (incluye dev)", "npm audit (produccion)"])
def test_quality_gate_never_executes_node_tools_after_security_failure(tmp_path, monkeypatch, failure):
    frontend = tmp_path / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(gate, "FRONTEND_ROOT", frontend)
    monkeypatch.setattr(gate, "TEST_REPORTS", tmp_path / "tests")
    monkeypatch.setattr(gate, "SECURITY_REPORTS", tmp_path / "security")
    monkeypatch.setattr(gate, "_launcher", lambda name: [name] if name == "corepack" else None)
    executed = []

    def fake_run(command, *, name, **kwargs):
        executed.append(command)
        return gate.GateResult(name, gate.FAIL if name == failure else gate.PASS)

    monkeypatch.setattr(gate, "_run", fake_run)
    results = gate.build_gates(SimpleNamespace(with_e2e=True, fast=True))
    assert not any("corepack" in command and ("run" in command or "exec" in command) for command in executed)
    assert next(r for r in results if r.name == failure).blocking


@pytest.mark.parametrize("path", ["backend/Dockerfile", ".github/workflows/implementation-v2.yml", "scripts/matrixrh.sh", "windows/Install-MatrixRH.ps1"])
def test_all_installers_gate_before_download_and_build(path):
    content = (ROOT / path).read_text()
    # Ignore prose comments; require actual commands in the right order.
    command_lines = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith("#"))
    before = command_lines.index("--lock-only")
    audit = command_lines.index("audit --audit-level=low")
    ci = command_lines.index('npmArgs = @("ci"') if path.endswith(".ps1") else command_lines.index("npm ci --ignore-scripts")
    post = command_lines.index("scripts.verify_supply_chain", ci) if not path.endswith("Dockerfile") else command_lines.index("verify_supply_chain.py --frontend /build/frontend", ci)
    build = command_lines.index("npm run build", post)
    assert before < audit < ci < post < build
