# Creado por Aldo Garcia.
"""Contrato del arbol documental oficial y compatibilidad no destructiva."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.authorization.categories import discover_filesystem_categories
from app.ingestion.reconciler import category_from_relative_path, iter_knowledge_files

pytestmark = pytest.mark.unit


def test_category_from_relative_path_aplica_las_tres_reglas() -> None:
    assert category_from_relative_path("general/guias/empleado.md") == "general"
    assert (
        category_from_relative_path("especializadas/nomina_confidencial/cierre.md")
        == "nomina_confidencial"
    )
    assert category_from_relative_path("prestaciones/politica.md") == "prestaciones"


@pytest.mark.parametrize(
    "relative",
    (
        "especializadas/suelto.md",
        "especializadas/general/incorrecto.md",
        "especializadas/Area Invalida/incorrecto.md",
    ),
)
def test_category_from_relative_path_rechaza_rutas_sin_dominio_valido(relative: str) -> None:
    assert category_from_relative_path(relative) is None


def test_iter_knowledge_files_clasifica_y_excluye_readme(tmp_path: Path) -> None:
    (tmp_path / "general" / "guias").mkdir(parents=True)
    (tmp_path / "general" / "guias" / "empleado.md").write_text("# Guia", encoding="utf-8")
    (tmp_path / "especializadas" / "talento").mkdir(parents=True)
    (tmp_path / "especializadas" / "talento" / "plan.md").write_text(
        "# Plan", encoding="utf-8"
    )
    (tmp_path / "especializadas" / "README.md").write_text("# No indexar", encoding="utf-8")

    found = {relative: category for _path, relative, category in iter_knowledge_files(tmp_path)}

    assert found == {
        "especializadas/talento/plan.md": "talento",
        "general/guias/empleado.md": "general",
    }


def test_discovery_no_registra_el_contenedor_especializadas(tmp_path: Path) -> None:
    (tmp_path / "general").mkdir()
    (tmp_path / "especializadas" / "compensaciones").mkdir(parents=True)
    assert discover_filesystem_categories(tmp_path) == ["compensaciones", "general"]
