# Creado por Aldo Garcia.
"""La entrega conserva arquitectura y evidencia sin distribuir estado privado."""
from pathlib import Path

from scripts.package_release import build_zip, iter_package_files


def test_zip_incluye_guias_e_informes_y_excluye_copias_de_datos(tmp_path: Path):
    import hashlib
    import zipfile

    source = tmp_path / "source"
    names = (
        "docs/ARCHITECTURE.md", "docs/RUNBOOK.md", "docs/integrations/IDENTITY_PROVIDER_DESIGN.md",
        "reports/AUDITOR_MAESTRO.md", "reports/FINAL_SOURCE_COMPARISON.md",
        "reports/historico/anterior.md", "reports/tests/junit.xml", "var/qdrant/storage.sqlite",
        "data/respaldo.sqlite", "copia.zip", ".env", ".env.example", "frontend/dist/index.html",
        "backend/build/lib/app/main.py", "backend/other.egg-info/PKG-INFO",
    )
    for name in names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    archive = tmp_path / "release.zip"
    metadata = source / "backend/pyproject.toml"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text('[project]\nversion="1.2.6"\n', encoding="utf-8")
    build_zip(source, archive, iter_package_files(source))
    expected = {
        "docs/ARCHITECTURE.md", "docs/RUNBOOK.md", "docs/integrations/IDENTITY_PROVIDER_DESIGN.md",
        "reports/AUDITOR_MAESTRO.md", "reports/FINAL_SOURCE_COMPARISON.md", ".env.example",
        "frontend/dist/index.html",
        "backend/pyproject.toml",
    }
    with zipfile.ZipFile(archive) as result:
        root = result.namelist()[0].split("/")[0] + "/"
        assert {name.removeprefix(root) for name in result.namelist()} == expected | {"SHA256SUMS.txt"}
        manifest = result.read(root + "SHA256SUMS.txt").decode().splitlines()
        for line in manifest:
            digest, name = line.split("  ", 1)
            assert digest == hashlib.sha256(result.read(root + name)).hexdigest()
