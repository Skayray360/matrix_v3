# Creado por Aldo Garcia.
"""Regresiones de aislamiento entre dominios administrativos de RH."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from app.api.routes import admin
from app.common.errors import ForbiddenError
from tests.conftest import make_context


def test_ruta_oficial_separa_general_y_especializadas() -> None:
    assert admin._official_relative_path("general", "politica.pdf") == "general/politica.pdf"
    assert (
        admin._official_relative_path("reclutamiento", "proceso.pdf")
        == "especializadas/reclutamiento/proceso.pdf"
    )


def test_administrador_funcional_no_publica_en_otro_dominio(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = SimpleNamespace(
        effective_categories=lambda _ctx: frozenset({"general", "reclutamiento"})
    )
    monkeypatch.setattr(admin, "get_policy_engine", lambda: engine)

    with pytest.raises(ForbiddenError):
        admin._require_effective_category(
            SimpleNamespace(),  # type: ignore[arg-type]
            "nomina_confidencial",
        )


def test_administrador_funcional_publica_solo_en_categoria_efectiva(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(
        effective_categories=lambda _ctx: frozenset({"general", "reclutamiento"})
    )
    monkeypatch.setattr(admin, "get_policy_engine", lambda: engine)

    admin._require_effective_category(
        SimpleNamespace(),  # type: ignore[arg-type]
        "reclutamiento",
    )


def test_ruta_de_carga_bloquea_antes_de_leer_archivo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(effective_categories=lambda _ctx: frozenset({"general"}))
    monkeypatch.setattr(admin, "get_policy_engine", lambda: engine)

    class NeverRead(BytesIO):
        def read(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            raise AssertionError("un archivo de otra categoria no debe leerse")

    file = UploadFile(file=NeverRead(b"contenido"), filename="nomina.txt")
    with pytest.raises(ForbiddenError):
        admin.upload_corporate_document(
            category="nomina_confidencial",
            file=file,
            db=SimpleNamespace(),  # type: ignore[arg-type]
            ctx=make_context(
                permissions=frozenset({"knowledge.admin"}),
                categories=frozenset({"general"}),
            ),
        )


def test_resumen_administrativo_filtra_categorias_efectivas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(
        effective_categories=lambda _ctx: frozenset({"general", "reclutamiento"})
    )
    monkeypatch.setattr(admin, "get_policy_engine", lambda: engine)

    class Session:
        statement = None

        def execute(self, statement):  # noqa: ANN001, ANN201
            self.statement = statement
            return SimpleNamespace(all=lambda: [])

    db = Session()
    result = admin.knowledge_summary(
        db=db,  # type: ignore[arg-type]
        ctx=make_context(
            permissions=frozenset({"knowledge.admin"}),
            categories=frozenset({"general", "reclutamiento"}),
        ),
    )

    assert result == {
        "categories": [],
        "known_categories": ["general", "reclutamiento"],
    }
    assert db.statement is not None
    compiled = db.statement.compile()
    assert set(compiled.params["category_1"]) == {"general", "reclutamiento"}
