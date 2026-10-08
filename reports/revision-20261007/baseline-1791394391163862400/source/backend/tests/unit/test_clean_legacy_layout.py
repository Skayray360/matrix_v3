# Creado por Aldo Garcia.
"""Limpieza acotada sobre instalaciones superpuestas, solo datos sinteticos."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import clean_legacy_layout as cleanup


def _write(root: Path, relative: str, content: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def synthetic_catalog(monkeypatch):
    document = b"Guia sintetica retirada.\r\n"
    asset = b"console.log('asset sintetico anterior');\n"
    monkeypatch.setattr(cleanup, "LEGACY_DOCUMENT_HASHES", {
        "docs/old-guide.md": hashlib.sha256(document).hexdigest(),
    })
    monkeypatch.setattr(cleanup, "LEGACY_ASSET_SHA256", hashlib.sha256(asset).hexdigest())
    return document, asset


def test_catalog_is_fixed_and_never_includes_state_or_current_launchers():
    assert len(cleanup.LEGACY_DOCUMENT_HASHES) == 64
    for path, digest in cleanup.LEGACY_DOCUMENT_HASHES.items():
        assert path.endswith(".md")
        assert not path.startswith(("data/", "var/"))
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    assert set(cleanup.LEGACY_LAUNCHERS) == {
        "ACTUALIZAR_MATRIX_RH.bat", "PROBAR_MODELOS_MATRIX_RH.bat", "install.bat",
    }


def test_retire_exact_files_backup_all_bytes_and_repeat_safely(tmp_path, synthetic_catalog):
    document, asset = synthetic_catalog
    originals = {name: b"@echo CUSTOM\r\nrem operador\r\n" for name in cleanup.LEGACY_LAUNCHERS}
    originals.update({"docs/old-guide.md": document, cleanup.LEGACY_ASSET: asset})
    for relative, content in originals.items():
        _write(tmp_path, relative, content)
    preserved = {
        ".env": b"CONFIGURACION_PRIVADA=valor-sintetico\n",
        "data/prestaciones/politica.pdf": b"PDF sintetico",
        "var/qdrant/state": b"indice sintetico",
        "docs/guia-personal.md": b"Notas propias",
        "frontend/dist/assets/new.js": b"app vigente",
        "frontend/dist/index.html": b'<script src="assets/new.js"></script>',
        "INSTALAR_MATRIX_RH.bat": b"instalador vigente",
        "INICIAR_MATRIX_RH.bat": b"inicio vigente",
        "DETENER_MATRIX_RH.bat": b"detener vigente",
        "DIAGNOSTICAR_MATRIX_RH.bat": b"diagnostico vigente",
    }
    for relative, content in preserved.items():
        _write(tmp_path, relative, content)
    result = cleanup.clean(tmp_path)
    assert result["retired"] == 5 and result["preserved_modified"] == 0
    backup = tmp_path / result["backup"]
    for relative, content in originals.items():
        assert not (tmp_path / relative).exists()
        assert (backup / (relative + ".retired")).read_bytes() == content
    assert not list(backup.rglob("*.bat"))
    for relative, content in preserved.items():
        assert (tmp_path / relative).read_bytes() == content
    assert "CUSTOM" not in json.dumps(result)
    repeated = cleanup.clean(tmp_path)
    assert repeated["retired"] == 0 and repeated["backup"] is None
    assert len(list((tmp_path / "var/backups").iterdir())) == 1


def test_custom_document_and_asset_are_preserved(tmp_path, synthetic_catalog):
    _write(tmp_path, "docs/old-guide.md", b"Guia personalizada")
    _write(tmp_path, cleanup.LEGACY_ASSET, b"asset personalizado")
    result = cleanup.clean(tmp_path)
    assert result["preserved_modified"] == 2 and result["retired"] == 0
    assert (tmp_path / "docs/old-guide.md").read_bytes() == b"Guia personalizada"
    assert (tmp_path / cleanup.LEGACY_ASSET).read_bytes() == b"asset personalizado"
    assert not (tmp_path / "var").exists()


@pytest.mark.parametrize("index", [
    None,
    b'<script src="assets/index-B6hzhSCJ.js"></script>',
    b'<script src="assets/index-B6hzhSCJ&#46;js"></script>',
    b'<script src="assets/index-B6hzhSCJ%2Ejs"></script>',
    b'<script src="assets/INDEX-B6HZHSCJ.JS"></script>',
])
def test_asset_requires_present_index_without_reference(tmp_path, synthetic_catalog, index):
    _, asset = synthetic_catalog
    _write(tmp_path, cleanup.LEGACY_ASSET, asset)
    if index is not None:
        _write(tmp_path, cleanup.FRONTEND_INDEX, index)
    result = cleanup.clean(tmp_path)
    assert result["retired"] == 0 and result["backup"] is None
    key = "preserved_unchecked_asset" if index is None else "preserved_referenced"
    assert result[key] == 1
    assert (tmp_path / cleanup.LEGACY_ASSET).read_bytes() == asset


@pytest.mark.parametrize("redirect", ["root", "docs", "document", "var", "backups", "launcher", "index"])
def test_redirects_abort_before_first_backup_or_delete(tmp_path, synthetic_catalog, redirect):
    document, asset = synthetic_catalog
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    launcher = _write(root, cleanup.LEGACY_LAUNCHERS[0], b"custom launcher")
    try:
        if redirect == "root":
            target = tmp_path / "root-link"
            target.symlink_to(root, target_is_directory=True)
            root = target
        elif redirect in {"docs", "var"}:
            (root / redirect).symlink_to(outside, target_is_directory=True)
        elif redirect == "backups":
            (root / "var").mkdir()
            (root / "var/backups").symlink_to(outside, target_is_directory=True)
        elif redirect == "document":
            target = _write(outside, "guide.md", document)
            (root / "docs").mkdir()
            (root / "docs/old-guide.md").symlink_to(target)
        elif redirect == "launcher":
            target = _write(outside, "external.bat", b"external script")
            (root / cleanup.LEGACY_LAUNCHERS[1]).symlink_to(target)
        else:
            _write(root, cleanup.LEGACY_ASSET, asset)
            target = _write(outside, "index.html", b"frontend")
            (root / cleanup.FRONTEND_INDEX).symlink_to(target)
    except OSError:
        pytest.skip("El sistema no permite crear enlaces simbolicos para esta prueba.")
    with pytest.raises(cleanup.UnsafeLayoutError):
        cleanup.clean(root)
    assert launcher.read_bytes() == b"custom launcher"
    assert not list(outside.glob("layout-*"))
    assert not list(root.glob("var/backups/layout-*"))


def test_windows_reparse_directory_is_rejected(tmp_path, synthetic_catalog, monkeypatch):
    launcher = _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], b"custom")
    var = tmp_path / "var"
    var.mkdir()
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == var:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(cleanup.UnsafeLayoutError):
        cleanup.clean(tmp_path)
    assert launcher.exists()


def test_full_plan_rejects_unexpected_directory(tmp_path, synthetic_catalog):
    launcher = _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], b"custom")
    (tmp_path / "docs/old-guide.md").mkdir(parents=True)
    with pytest.raises(cleanup.UnsafeLayoutError):
        cleanup.clean(tmp_path)
    assert launcher.exists() and not (tmp_path / "var").exists()


def test_backup_failure_never_deletes_sources(tmp_path, synthetic_catalog, monkeypatch):
    document, _ = synthetic_catalog
    launcher = _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], b"custom")
    guide = _write(tmp_path, "docs/old-guide.md", document)
    original = cleanup._write_backup
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Sin espacio")
        return original(*args)

    monkeypatch.setattr(cleanup, "_write_backup", fail_second)
    with pytest.raises(OSError):
        cleanup.clean(tmp_path)
    assert launcher.read_bytes() == b"custom"
    assert guide.read_bytes() == document


def test_changed_file_after_backup_is_preserved(tmp_path, synthetic_catalog, monkeypatch):
    launcher = _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], b"original")
    original = cleanup._publish_backup

    def change_after_backup(*args):
        result = original(*args)
        launcher.write_bytes(b"nueva edicion")
        return result

    monkeypatch.setattr(cleanup, "_publish_backup", change_after_backup)
    with pytest.raises(cleanup.UnsafeLayoutError):
        cleanup.clean(tmp_path)
    assert launcher.read_bytes() == b"nueva edicion"
    assert next((tmp_path / "var/backups").glob("layout-*/*.retired")).read_bytes() == b"original"


def test_modified_backup_never_allows_deletion(tmp_path, synthetic_catalog, monkeypatch):
    launcher = _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], b"original")
    original = cleanup._publish_backup

    def damage_after_backup(*args):
        result = original(*args)
        (result / (launcher.name + ".retired")).write_bytes(b"corrupto")
        return result

    monkeypatch.setattr(cleanup, "_publish_backup", damage_after_backup)
    with pytest.raises(cleanup.UnsafeLayoutError):
        cleanup.clean(tmp_path)
    assert launcher.read_bytes() == b"original"


def test_redirected_backup_after_creation_never_allows_deletion(tmp_path, synthetic_catalog, monkeypatch):
    launcher = _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], b"original")
    original = cleanup._publish_backup

    def redirect_after_backup(*args):
        result = original(*args)
        target = result.with_name(result.name + "-moved")
        result.rename(target)
        try:
            result.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("El sistema no permite enlaces simbolicos.")
        return result

    monkeypatch.setattr(cleanup, "_publish_backup", redirect_after_backup)
    with pytest.raises(cleanup.UnsafeLayoutError):
        cleanup.clean(tmp_path)
    assert launcher.read_bytes() == b"original"


def test_changed_index_never_removes_asset(tmp_path, synthetic_catalog, monkeypatch):
    _, asset = synthetic_catalog
    target = _write(tmp_path, cleanup.LEGACY_ASSET, asset)
    index = _write(tmp_path, cleanup.FRONTEND_INDEX, b'<script src="new.js"></script>')
    original = cleanup._publish_backup

    def change_after_backup(*args):
        result = original(*args)
        index.write_bytes(b'<script src="index-B6hzhSCJ.js"></script>')
        return result

    monkeypatch.setattr(cleanup, "_publish_backup", change_after_backup)
    with pytest.raises(cleanup.UnsafeLayoutError):
        cleanup.clean(tmp_path)
    assert target.read_bytes() == asset


def test_delete_failure_leaves_every_retired_byte_backed_up(tmp_path, synthetic_catalog, monkeypatch):
    paths = [_write(tmp_path, name, name.encode()) for name in cleanup.LEGACY_LAUNCHERS]
    original = Path.unlink

    def fail_second(path, *args, **kwargs):
        if path == paths[1]:
            raise OSError("Archivo ocupado")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second)
    with pytest.raises(OSError):
        cleanup.clean(tmp_path)
    backup = next((tmp_path / "var/backups").glob("layout-*"))
    assert not paths[0].exists() and paths[1].exists() and paths[2].exists()
    for path in paths:
        assert (backup / (path.name + ".retired")).read_bytes() == path.name.encode()


def test_cli_reports_counts_without_contents(tmp_path, synthetic_catalog, capsys):
    _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], b"sensitive synthetic launcher")
    assert cleanup.main(["--root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["retired"] == 1
    assert "sensitive synthetic launcher" not in output


def test_cli_failure_is_nonzero_and_does_not_print_exception_data(tmp_path, monkeypatch, capsys):
    def fail(_):
        raise OSError("Un dato que no debe aparecer")

    monkeypatch.setattr(cleanup, "clean", fail)
    assert cleanup.main(["--root", os.fspath(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is False
    assert "Un dato que no debe aparecer" not in output


def _stat_variant(info, **changes):
    values = {
        key: getattr(info, key) for key in (
            "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns",
        )
    }
    for optional in ("st_birthtime_ns", "st_file_attributes"):
        if hasattr(info, optional):
            values[optional] = getattr(info, optional)
    return SimpleNamespace(**(values | changes))


@pytest.mark.parametrize("variation", ["execute_bits", "ctime", "both"])
def test_windows_stat_differences_do_not_block_backup_or_retirement(tmp_path, monkeypatch, variation):
    """Windows puede agregar ejecucion por extension y tener distinto ctime en fstat."""
    content = b"@echo off\r\nrem launcher sintetico\r\n"
    source = _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], content)
    source.chmod(0o755)
    original = os.fstat

    def windows_fstat(fd):
        info = original(fd)
        changes = {}
        if variation in {"execute_bits", "both"}:
            changes["st_mode"] = info.st_mode & ~0o111
        if variation in {"ctime", "both"}:
            changes["st_ctime_ns"] = info.st_ctime_ns + 100_000
        return _stat_variant(info, **changes)

    monkeypatch.setattr(os, "fstat", windows_fstat)
    result = cleanup.clean(tmp_path)
    assert result["retired"] == 1
    assert not source.exists()
    assert (tmp_path / result["backup"] / (source.name + ".retired")).read_bytes() == content
    assert cleanup.clean(tmp_path)["retired"] == 0


@pytest.mark.parametrize("attribute", ["st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_file_attributes"])
def test_open_handle_must_still_match_file_identity_and_content_metadata(tmp_path, monkeypatch, attribute):
    source = _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], b"original")
    original = os.fstat

    def wrong_file(fd):
        info = original(fd)
        value = {
            "st_mode": stat.S_IFDIR | 0o755,
            "st_file_attributes": cleanup._REPARSE_POINT,
        }.get(attribute, getattr(info, attribute, 0) + 1)
        return _stat_variant(info, **{attribute: value})

    monkeypatch.setattr(os, "fstat", wrong_file)
    with pytest.raises(cleanup.UnsafeLayoutError):
        cleanup.clean(tmp_path)
    assert source.read_bytes() == b"original"
    assert not (tmp_path / "var/backups").exists()


def test_descriptor_change_during_read_is_still_rejected(tmp_path, monkeypatch):
    source = _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], b"original")
    original = os.fstat
    calls = 0

    def changed_descriptor(fd):
        nonlocal calls
        calls += 1
        info = original(fd)
        return _stat_variant(info, st_ctime_ns=info.st_ctime_ns + calls * 100_000)

    monkeypatch.setattr(os, "fstat", changed_descriptor)
    with pytest.raises(cleanup.UnsafeLayoutError):
        cleanup.clean(tmp_path)
    assert source.read_bytes() == b"original"


def test_cli_reports_relative_file_and_reason_without_external_path(tmp_path, capsys):
    (tmp_path / cleanup.LEGACY_LAUNCHERS[0]).mkdir()
    assert cleanup.main(["--root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    error = json.loads(output)
    assert error["code"] == "not_regular_file"
    assert error["path"] == cleanup.LEGACY_LAUNCHERS[0]
    assert error["phase"] == "read"
    assert str(tmp_path) not in output


def test_cli_reports_backup_permission_error_without_deleting_source(tmp_path, monkeypatch, capsys):
    import errno

    source = _write(tmp_path, cleanup.LEGACY_LAUNCHERS[0], b"original")

    def denied(*_):
        raise PermissionError(errno.EACCES, "dato externo privado", "/ruta/privada")

    monkeypatch.setattr(cleanup, "_publish_backup", denied)
    assert cleanup.main(["--root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    error = json.loads(output)
    assert error["code"] == "permission_denied"
    assert error["phase"] == "backup"
    assert error["path"] == "var/backups"
    assert error["errno"] == errno.EACCES
    assert "privad" not in output and str(tmp_path) not in output
    assert source.read_bytes() == b"original"
