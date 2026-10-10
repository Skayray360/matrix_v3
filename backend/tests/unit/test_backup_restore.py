# Creado por Aldo Garcia.
"""Restauracion aislada de archivos, SQLite sustituto y Qdrant embedded reales.

SQLite es solo fixture; estas pruebas NO acreditan exportacion/restauracion MySQL.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from qdrant_client import QdrantClient, models

from scripts.backup_restore import COMPONENTS, create_backup, restore_backup, verify_backup

pytestmark = pytest.mark.unit


def prepared_set(tmp_path):
    source = tmp_path / "prepared"
    source.mkdir()
    for component in COMPONENTS:
        (source / component).mkdir()
    (source / "configuration/.env").write_text("APP_ENV=test\n", encoding="utf-8")
    (source / "knowledge/policy.md").write_text("Documento sintetico; requiere autorizacion.", encoding="utf-8")
    (source / "models/model-manifest.json").write_text('{"fixture":true}', encoding="utf-8")
    with sqlite3.connect(source / "database/fixture.sqlite") as database:
        database.execute("CREATE TABLE owners (user_id TEXT, document_id TEXT)")
        database.execute("INSERT INTO owners VALUES (?, ?)", ("owner-a", "document-a"))
    return source


def test_restore_reopens_sqlite_fixture_and_qdrant_with_same_owner_metadata(tmp_path):
    source = prepared_set(tmp_path)
    vectors = QdrantClient(path=str(source / "vector/index"))
    vectors.create_collection("fixture", vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
    vectors.upsert("fixture", [models.PointStruct(id=1, vector=[1.0, 0.0, 0.0],
                                                   payload={"user_id": "owner-a", "document_id": "document-a"})])
    vectors.close()  # La copia se prepara cerrada, nunca sobre un indice abierto.
    backup, restored = tmp_path / "backup", tmp_path / "isolated-restore"
    manifest = create_backup(source, backup)
    assert manifest["consistency"].endswith("database_restore_not_verified_by_copy")
    assert restore_backup(backup, restored)["activation"] == "not_performed"
    with sqlite3.connect(restored / "database/fixture.sqlite") as database:
        assert database.execute("SELECT user_id, document_id FROM owners").fetchall() == [("owner-a", "document-a")]
    reopened = QdrantClient(path=str(restored / "vector/index"))
    try:
        points = reopened.retrieve("fixture", ids=[1], with_vectors=True)
        assert points[0].payload == {"user_id": "owner-a", "document_id": "document-a"}
        assert points[0].vector == [1.0, 0.0, 0.0]
    finally:
        reopened.close()
    assert (restored / "knowledge/policy.md").read_bytes() == (source / "knowledge/policy.md").read_bytes()
    assert (restored / "uploads").is_dir()


def test_corruption_blocks_restore_before_destination_is_created(tmp_path):
    source = prepared_set(tmp_path)
    backup, destination = tmp_path / "backup", tmp_path / "restore"
    create_backup(source, backup)
    (backup / "knowledge/policy.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="integridad"):
        restore_backup(backup, destination)
    assert not destination.exists()


def test_existing_destination_is_preserved(tmp_path):
    source = prepared_set(tmp_path)
    backup, destination = tmp_path / "backup", tmp_path / "restore"
    create_backup(source, backup)
    destination.mkdir()
    (destination / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        restore_backup(backup, destination)
    assert (destination / "existing.txt").read_text(encoding="utf-8") == "keep"


def test_linked_source_and_manifest_traversal_are_rejected(tmp_path):
    source = prepared_set(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("must stay private", encoding="utf-8")
    link = source / "uploads/link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("El sistema no permite crear enlaces para este fixture")
    with pytest.raises(ValueError, match="enlaces"):
        create_backup(source, tmp_path / "rejected")
    link.unlink()
    backup = tmp_path / "backup"
    create_backup(source, backup)
    path = backup / "backup-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../outside.txt"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="integridad"):
        verify_backup(backup)
    assert outside.read_text(encoding="utf-8") == "must stay private"


def test_missing_database_never_creates_a_valid_backup(tmp_path):
    source = prepared_set(tmp_path)
    (source / "database/fixture.sqlite").unlink()
    with pytest.raises(ValueError, match="database"):
        create_backup(source, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()
