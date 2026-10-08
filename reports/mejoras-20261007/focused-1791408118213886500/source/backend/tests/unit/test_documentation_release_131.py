# Creado por Aldo Garcia.
"""Contratos de enlaces del paquete, sin red ni datos de usuario."""

from pathlib import Path

from scripts.verify_documentation import check_links


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


def test_corpus_text_is_not_treated_as_operator_documentation(tmp_path):
    corpus = sample(tmp_path, "data/synthetic_test_data/knowledge/example.md", "[fixture](missing.md)")
    assert check_links(tmp_path, [corpus]) == []


def test_empty_directory_is_not_a_distributed_destination(tmp_path):
    readme = sample(tmp_path, "README.md", "[directory](docs/)")
    (tmp_path / "docs").mkdir()
    assert check_links(tmp_path, [readme])
    guide = sample(tmp_path, "docs/guide.md")
    assert check_links(tmp_path, [readme, guide]) == []
