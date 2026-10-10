# Creado por Aldo Garcia.
"""Contratos de enlaces del paquete, sin red ni datos de usuario."""

from pathlib import Path

import pytest

from scripts.verify_documentation import check_links, check_readme_layout
from scripts.verify_headers import check as check_headers


def sample(root: Path, relative: str, content: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_documented_destinations_must_be_in_package(tmp_path):
    readme = sample(tmp_path, "README.md", "[guide](docs/guide.md) ![map](docs/assets/map.svg)")
    guide = sample(tmp_path, "docs/guide.md", "[root](../README.md#start)")
    image = sample(tmp_path, "docs/assets/map.svg", "<svg/>")
    assert check_links(tmp_path, [readme, guide, image]) == []
    assert "map.svg" in check_links(tmp_path, [readme, guide])[0]


def test_network_anchors_and_code_examples_are_not_executed(tmp_path):
    readme = sample(tmp_path, "README.md", """[external](https://example.test/absent)
[anchor](#example) [email](mailto:test@example.test)
```markdown
[example](missing.md)
```
""")
    assert check_links(tmp_path, [readme]) == []


def test_link_outside_package_is_rejected(tmp_path):
    readme = sample(tmp_path, "README.md", "[outside](../other.md)")
    assert "fuera del paquete" in check_links(tmp_path, [readme])[0]


def test_encoded_paths_and_links_with_titles(tmp_path):
    readme = sample(tmp_path, "README.md", '[guide](docs/My%20Guide.md "Read this")')
    guide = sample(tmp_path, "docs/My Guide.md")
    assert check_links(tmp_path, [readme, guide]) == []


@pytest.mark.parametrize("directory", [
    "knowledge-base/documents", "knowledge-base/unclassified", "knowledge-base/state",
    "knowledge-base/models", "knowledge-base/backups", "backend/tests/fixtures/knowledge",
    "backend/runtime", "backend/logs", "backend/config/secrets",
])
def test_data_and_runtime_are_neither_read_nor_relabelled_as_project_docs(tmp_path, monkeypatch, directory):
    private = sample(tmp_path, f"{directory}/README.md", "Documento privado: [source](missing.md)")
    readme = sample(tmp_path, "README.md", "Creado por Aldo Garcia.")
    original_read = Path.read_text
    original_open = Path.open

    def read_checked(path, *args, **kwargs):
        assert path != private, "La auditoria de codigo no debe leer documentos o secretos."
        return original_read(path, *args, **kwargs)

    def open_checked(path, *args, **kwargs):
        assert path != private, "La auditoria de autoria no debe abrir documentos o secretos."
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_checked)
    monkeypatch.setattr(Path, "open", open_checked)
    assert check_links(tmp_path, [private, readme]) == []
    assert check_readme_layout(tmp_path, [private, readme]) == []
    assert check_headers(tmp_path) == ([], 1)


def test_delivery_requires_one_project_readme(tmp_path):
    root_readme = sample(tmp_path, "README.md")
    extra = sample(tmp_path, "backend/README.md")
    assert "Falta README.md" in check_readme_layout(tmp_path, [extra])[0]
    assert check_readme_layout(tmp_path, [root_readme]) == []
    assert check_readme_layout(tmp_path, [root_readme, extra]) == [
        "README adicional fuera de la guia unica: backend/README.md",
    ]


def test_empty_directory_is_not_a_distributed_destination(tmp_path):
    readme = sample(tmp_path, "README.md", "[directory](docs/)")
    (tmp_path / "docs").mkdir()
    assert check_links(tmp_path, [readme])
    guide = sample(tmp_path, "docs/guide.md")
    assert check_links(tmp_path, [readme, guide]) == []


def test_workspace_checks_active_guides_without_rewriting_historical_snapshots(tmp_path):
    readme = sample(tmp_path, "README.md", "[result](reports/old/result.json) [bad](missing.md)")
    historical = sample(tmp_path, "reports/old/source/README.md", "[old](missing-old.md)")
    result = sample(tmp_path, "reports/old/result.json", '{"historical":true}')
    errors = check_links(tmp_path, [readme, historical], workspace=True)
    assert len(errors) == 1 and "missing.md" in errors[0]
    # Distribucion sigue exigiendo incluir el archivo; no se relaja ese contrato.
    assert len(check_links(tmp_path, [readme, historical])) == 3
    result.unlink()
    assert len(check_links(tmp_path, [readme, historical], workspace=True)) == 2
