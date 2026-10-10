# Creado por Aldo Garcia.
"""Contratos de la entrega actual: carpeta estable y cuatro entradas operativas."""

from pathlib import Path
from zipfile import ZipFile

from scripts.package_release import build_zip, iter_package_files
from scripts.preflight import OPERATOR_BATCH_FILES
from scripts.verify_headers import check

ROOT = Path(__file__).resolve().parents[3]


def test_delivery_has_exactly_four_operator_batches():
    selected = iter_package_files(ROOT)
    assert {p.relative_to(ROOT).as_posix() for p in selected if p.suffix.lower() == ".bat"} == OPERATOR_BATCH_FILES


def test_packaging_keeps_installation_folder_when_internal_revision_differs(tmp_path):
    root = tmp_path / "matrix-rh-1.3.0"
    metadata = root / "backend/pyproject.toml"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('[project]\nversion="1.3.1"\n', encoding="utf-8")
    archive = tmp_path / "delivery.zip"
    build_zip(root, archive, [metadata])
    with ZipFile(archive) as package:
        assert set(package.namelist()) == {
            "matrix-rh-1.3.0/backend/pyproject.toml", "matrix-rh-1.3.0/backend/release/SHA256SUMS.txt",
        }


def test_corporate_alias_document_is_not_relabelled_as_project_authorship(tmp_path):
    corpus = tmp_path / "knowledge-base/documents/analisis-1.md"
    corpus.parent.mkdir(parents=True)
    corpus.write_text("Documento corporativo con su autoria original.", encoding="utf-8")
    guide = tmp_path / "README.md"
    guide.write_text("Guia del proyecto sin encabezado.", encoding="utf-8")
    original = corpus.read_bytes()
    missing, total = check(tmp_path)
    assert missing == ["README.md"] and total == 1
    assert corpus.read_bytes() == original
