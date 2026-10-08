# Creado por Aldo Garcia.
"""Traslado entre entregas sinteticas completas con manifiestos reales."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from scripts import release_transfer
from scripts.preflight import CORE_FILES, PreflightReport, check_integrity

pytestmark = pytest.mark.unit


def write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def manifest(root: Path) -> None:
    entries = [
        (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*")
        if path.is_file() and path.name not in {"SHA256SUMS.txt", ".env"}
    ]
    write(root, "SHA256SUMS.txt", "".join(f"{digest}  {relative}\n" for relative, digest in sorted(entries)))


def make_release(root: Path, version: str) -> Path:
    root.mkdir()
    for relative in CORE_FILES:
        write(root, relative, f"release sintetica {version}: {relative}\n")
    write(root, "backend/pyproject.toml", f'[project]\nname="synthetic-matrix"\nversion="{version}"\n')
    write(root, "backend/app/__init__.py", f'__version__ = "{version}"\n')
    for relative in (
        "backend/app/main.py", "backend/scripts/preflight.py", "backend/scripts/bootstrap.py",
        "windows/Common-MatrixRH.ps1",
    ):
        write(root, relative, f"# Codigo sintetico {version}\n")
    write(root, "config/categories.yaml", f"version: {version}\ncategories: [prestaciones]\n")
    write(root, "config/access.yaml", f"version: {version}\naccess: baseline\n")
    write(root, "data/README.md", f"Corpus de entrega {version}\n")
    write(root, "var/README.md", f"Estado de entrega {version}\n")
    manifest(root)
    report = PreflightReport()
    check_integrity(report, root=root, require_manifest=True)
    assert report.ok, report.as_dict()
    return root


@pytest.fixture
def releases(tmp_path):
    previous = make_release(tmp_path / "previous", "1.2.6")
    current = make_release(tmp_path / "current", "1.2.7")
    write(
        previous, ".env",
        "APP_ENV=development\r\n"
        "APP_SECRET_KEY=synthetic-original-secret\r\n"
        "MATRIX_SEED_PASSWORD=synthetic-original-password\r\n"
        "LLM_FAST_DIGEST=" + "a" * 64 + "\r\n"
        "RAG_KNOWLEDGE_ROOT=./data/knowledge\r\n"
        "QDRANT_PATH=./var/qdrant\r\n"
        "UPLOAD_STORAGE_ROOT=./var/uploads\r\n",
    )
    return previous, current


def snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("link", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        else:
            result[relative] = ("directory", "")
    return result


def assert_rejected_without_writes(previous: Path, current: Path, *, match: str | None = None) -> None:
    before_previous, before_current = snapshot(previous), snapshot(current)
    with pytest.raises(ValueError, match=match):
        release_transfer.transfer_state(previous, current)
    assert snapshot(previous) == before_previous
    assert snapshot(current) == before_current


def test_transfer_preserves_state_custom_yaml_and_new_code_without_source_mutation(releases, capsys):
    previous, current = releases
    write(previous, "data/knowledge/prestaciones/reglas.pdf", b"synthetic PDF corpus bytes\x00\xff")
    write(previous, "var/qdrant/storage.bin", b"synthetic vector state\x00\xfe")
    write(previous, "var/uploads/owned-upload.txt", "Adjunto sintetico del usuario")
    write(previous, "var/logs/backend.log", "Log sintetico")
    write(previous, "config/access.yaml", "access: customized\nroles: [prestaciones_reader]\n")
    write(previous, "config/custom-source.yml", "source: synthetic-customer\n")
    write(previous, "config/local-extra.json", '{"old_source_config": true}')
    write(previous, "backend/app/old_only.py", "# NO DEBE COPIARSE\n")
    write(previous, ".venv/Scripts/python.exe", b"synthetic old virtual environment")
    write(previous, "var/matrixrh-backend.pid", "1111")
    write(previous, "var/matrixrh-backend.stop", "1111")
    write(previous, "data/__pycache__/ignored.pyc", b"cache not state")
    write(previous, "var/.pytest_cache/ignored", b"cache not state")
    before_previous = snapshot(previous)
    current_code = (current / "backend/app/__init__.py").read_bytes()
    new_categories = (current / "config/categories.yaml").read_bytes()
    result = release_transfer.transfer_state(previous, current)
    assert snapshot(previous) == before_previous
    assert (current / "backend/app/__init__.py").read_bytes() == current_code
    assert not (current / "backend/app/old_only.py").exists()
    assert not (current / ".venv").exists()
    assert not (current / "config/local-extra.json").exists()
    assert not (current / "data/__pycache__").exists()
    assert not (current / "var/.pytest_cache").exists()
    for relative in (
        "data/knowledge/prestaciones/reglas.pdf", "var/qdrant/storage.bin",
        "var/uploads/owned-upload.txt", "var/logs/backend.log", "config/access.yaml", "config/custom-source.yml",
    ):
        assert (current / relative).read_bytes() == (previous / relative).read_bytes()
    assert (current / "config/categories.yaml").read_bytes() == new_categories
    assert (current / ".env").read_bytes() == (previous / ".env").read_bytes()
    assert not (current / "var/matrixrh-backend.pid").exists()
    assert not (current / "var/matrixrh-backend.stop").exists()
    assert set(result["custom_config"]) == {"config/access.yaml", "config/custom-source.yml"}
    assert result["rebased_keys"] == []
    assert not capsys.readouterr().out


@pytest.mark.parametrize("collision", (".env", "data/knowledge/existing.pdf", "var/uploads/existing.txt", "config/access.yaml"))
def test_destination_collisions_are_rejected_before_writes(releases, collision):
    previous, current = releases
    write(previous, "data/knowledge/prestaciones/corpus.pdf", "Debe quedarse solo en la entrega anterior")
    write(current, collision, "Estado previo sintetico del destino")
    assert_rejected_without_writes(previous, current)


@pytest.mark.parametrize("broken", ("manifest_missing", "changed_code", "missing_version", "wrong_version"))
def test_invalid_new_release_is_rejected_before_copy(releases, broken):
    previous, current = releases
    if broken == "manifest_missing":
        (current / "SHA256SUMS.txt").unlink()
    elif broken == "changed_code":
        write(current, "backend/app/main.py", "# Codigo alterado sin manifiesto\n")
    elif broken == "missing_version":
        write(current, "backend/app/__init__.py", '"""Archivo incorrecto."""\n')
    else:
        write(current, "backend/app/__init__.py", '__version__ = "1.2.4"\n')
    assert_rejected_without_writes(previous, current, match="integridad")


@pytest.mark.parametrize("nesting", ("same", "new_inside_old", "old_inside_new"))
def test_nested_or_identical_release_roots_are_rejected_without_writes(tmp_path, nesting):
    root = tmp_path / "release"
    root.mkdir()
    write(root, "marker", "synthetic marker")
    nested = root / "nested"
    nested.mkdir()
    previous, current = (root, root) if nesting == "same" else (
        (root, nested) if nesting == "new_inside_old" else (nested, root)
    )
    assert_rejected_without_writes(previous, current, match="independientes")


@pytest.mark.parametrize("linked", ("previous_root", "current_root", "source_env", "target_env", "old_data_file", "new_var_dir"))
def test_links_are_rejected_before_any_transfer(releases, tmp_path, linked):
    previous, current = releases
    outside = tmp_path / "outside"
    outside.mkdir()
    external = write(outside, "marker", "synthetic outside value")
    if linked in {"previous_root", "current_root"}:
        alias = tmp_path / f"alias-{linked}"
        alias.symlink_to(previous if linked == "previous_root" else current, target_is_directory=True)
        if linked == "previous_root":
            previous = alias
        else:
            current = alias
    elif linked == "source_env":
        (previous / ".env").unlink()
        (previous / ".env").symlink_to(external)
    elif linked == "target_env":
        (current / ".env").symlink_to(external)
    elif linked == "old_data_file":
        (previous / "data/linked-corpus").symlink_to(external)
    else:
        (current / "var/linked-storage").symlink_to(outside, target_is_directory=True)
    before_outside = snapshot(outside)
    assert_rejected_without_writes(previous, current)
    assert snapshot(outside) == before_outside


def test_three_internal_absolute_paths_are_rebased_with_original_env_backup(releases):
    previous, current = releases
    original = (
        "\ufeffAPP_SECRET_KEY=synthetic-original-secret\r\n"
        f'RAG_KNOWLEDGE_ROOT="{previous / "data/knowledge"}" # corpus aprobado\r\n'
        f"QDRANT_PATH='{previous / 'var/qdrant'}'\r\n"
        f"export UPLOAD_STORAGE_ROOT={previous / 'var/uploads'} # adjuntos\r\n"
        "OLLAMA_FAST_MODEL=gemma4:latest\r\n"
    ).encode()
    (previous / ".env").write_bytes(original)
    write(previous, "data/knowledge/prestaciones/source.pdf", b"synthetic corpus")
    before = snapshot(previous)
    result = release_transfer.transfer_state(previous, current)
    text = (current / ".env").read_text(encoding="utf-8")
    assert set(result["rebased_keys"]) == release_transfer.PATH_KEYS
    assert f'RAG_KNOWLEDGE_ROOT="{current / "data/knowledge"}" # corpus aprobado' in text
    assert f"QDRANT_PATH='{current / 'var/qdrant'}'" in text
    assert f"export UPLOAD_STORAGE_ROOT={current / 'var/uploads'} # adjuntos" in text
    assert "APP_SECRET_KEY=synthetic-original-secret" in text
    assert "OLLAMA_FAST_MODEL=gemma4:latest" in text
    assert (current / "var/backups/configuration/env-before-path-transfer-1.2.8.bak").read_bytes() == original
    assert snapshot(previous) == before


def test_external_absolute_path_and_relative_paths_are_respected(releases, tmp_path):
    previous, current = releases
    external = tmp_path / "shared-corpus"
    original = (
        f"RAG_KNOWLEDGE_ROOT={external}\r\n"
        "QDRANT_PATH=./var/qdrant\r\n"
        "UPLOAD_STORAGE_ROOT=./var/uploads\r\n"
        "APP_SECRET_KEY=synthetic-original-secret\r\n"
    ).encode()
    (previous / ".env").write_bytes(original)
    result = release_transfer.transfer_state(previous, current)
    assert result["rebased_keys"] == []
    assert (current / ".env").read_bytes() == original
    assert not external.exists()


def test_paths_with_lowercase_and_mixed_case_keys_are_rebased(releases):
    previous, current = releases
    original = (
        f"qdrant_path={previous / 'var/qdrant'}\n"
        f"Rag_Knowledge_Root={previous / 'data/knowledge'}\n"
        f"export upload_storage_root={previous / 'var/uploads'}\n"
    )
    (previous / ".env").write_text(original)
    result = release_transfer.transfer_state(previous, current)
    assert set(result["rebased_keys"]) == release_transfer.PATH_KEYS
    assert (current / ".env").read_text() == original.replace(str(previous), str(current))
    assert (previous / ".env").read_text() == original


def test_release_readmes_stay_current_and_custom_notes_are_backed_up(releases):
    previous, current = releases
    current_docs = {relative: (current / relative).read_bytes() for relative in ("data/README.md", "var/README.md")}
    note = b"Customized synthetic corpus notes\n"
    (previous / "data/README.md").write_bytes(note)
    before = snapshot(previous)
    result = release_transfer.transfer_state(previous, current)
    assert snapshot(previous) == before
    assert result["preserved_readmes"] == ["data/README.md"]
    for relative, contents in current_docs.items():
        assert (current / relative).read_bytes() == contents
    notes = list((current / "var/backups/transfer-readmes").glob("*/data/README.md"))
    assert len(notes) == 1 and notes[0].read_bytes() == note


def test_shared_prefix_does_not_make_external_path_internal(releases, tmp_path):
    previous, current = releases
    unrelated = tmp_path / "previous-other" / "data"
    original = f"RAG_KNOWLEDGE_ROOT={unrelated}\n"
    mapped, changed = release_transfer.rebase_environment(original, previous, current)
    assert mapped == original
    assert changed == []


def test_invalid_path_quotes_fail_before_state_is_copied(releases):
    previous, current = releases
    write(previous, "data/knowledge/to-copy.pdf", "synthetic payload")
    write(previous, ".env", 'RAG_KNOWLEDGE_ROOT="incomplete-value\n')
    assert_rejected_without_writes(previous, current, match="comillas")


@pytest.mark.parametrize("failed", (False, True))
def test_command_output_never_includes_secret_values(releases, monkeypatch, capsys, failed):
    previous, current = releases
    monkeypatch.setattr(release_transfer, "PROJECT_ROOT", current)
    monkeypatch.setattr("sys.argv", ["release_transfer", "--previous-root", str(previous)])
    if failed:
        write(current, ".env", "APP_SECRET_KEY=synthetic-destination-secret\n")
    exit_code = release_transfer.main()
    captured = capsys.readouterr()
    assert exit_code == int(failed)
    assert "synthetic-original-secret" not in captured.out + captured.err
    assert "synthetic-original-password" not in captured.out + captured.err
    assert "synthetic-destination-secret" not in captured.out + captured.err
    assert "LLM_FAST_DIGEST" not in captured.out + captured.err


def test_windows_junction_is_rejected_before_writing_outside_destination(releases, tmp_path, monkeypatch):
    """Simula una junction Windows; su is_symlink es False y is_junction True."""
    previous, current = releases
    outside = tmp_path / "outside-storage"
    outside.mkdir()
    write(previous, "data/customer.pdf", b"synthetic customer corpus")
    (current / "data/README.md").unlink()
    (current / "data").rmdir()
    junction = current / "data"
    junction.symlink_to(outside, target_is_directory=True)
    original_is_symlink = Path.is_symlink
    original_is_junction = Path.is_junction
    monkeypatch.setattr(Path, "is_symlink", lambda path: False if path == junction else original_is_symlink(path))
    monkeypatch.setattr(Path, "is_junction", lambda path: True if path == junction else original_is_junction(path))
    before_previous, before_current, before_outside = snapshot(previous), snapshot(current), snapshot(outside)
    with pytest.raises(ValueError, match="enlaces|regulares|junction"):
        release_transfer.transfer_state(previous, current)
    assert snapshot(previous) == before_previous
    assert snapshot(current) == before_current
    assert snapshot(outside) == before_outside
