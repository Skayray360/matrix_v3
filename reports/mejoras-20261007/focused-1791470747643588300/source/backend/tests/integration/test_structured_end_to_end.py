# Creado por Aldo Garcia.
"""Consulta estructurada de punta a punta contra una fuente SQLite real.

SQLite es uno de los motores soportados y no requiere servidor, lo que permite
ejercitar el camino completo -- plan -> validador -> compilador -> AST -> adapter
-> ejecucion -> evidencia -- con datos reales y sinteticos.

Es la prueba que demuestra que la cuenta de solo lectura y los limites de filas
funcionan de verdad, no sólo que el compilador genera la cadena correcta.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.common.errors import ForbiddenError, StructuredQueryRejectedError
from app.structured_data.compiler import compile_plan, verify_sql_is_readonly
from app.structured_data.schemas import StructuredQueryPlan
from app.structured_data.sources import EntityConfig, SourceCatalog, SourceConfig
from app.structured_data.tool import StructuredDataTool
from app.structured_data.validator import QueryPolicyValidator

pytestmark = pytest.mark.integration

FILAS = [
    ("E001", "Operaciones", "Torreon", "Tecnico", 5, "activo"),
    ("E002", "Operaciones", "Torreon", "Supervisor", 9, "activo"),
    ("E003", "Calidad", "Monterrey", "Analista", 2, "activo"),
    ("E004", "Calidad", "Monterrey", "Analista", 7, "baja"),
    ("E005", "Mantenimiento", "Torreon", "Tecnico", 12, "activo"),
]


@pytest.fixture()
def base_sqlite(tmp_path: Path) -> Path:
    ruta = tmp_path / "demo.sqlite3"
    with sqlite3.connect(ruta) as conexion:
        conexion.execute(
            """
            CREATE TABLE demo_plantilla (
                empleado_id      TEXT,
                departamento     TEXT,
                centro_trabajo   TEXT,
                puesto           TEXT,
                antiguedad_anios INTEGER,
                estatus          TEXT,
                salario_mensual  REAL
            )
            """
        )
        conexion.executemany(
            "INSERT INTO demo_plantilla VALUES (?, ?, ?, ?, ?, ?, 0)", FILAS
        )
    return ruta


@pytest.fixture()
def catalogo(base_sqlite: Path, monkeypatch) -> SourceCatalog:  # noqa: ANN001
    monkeypatch.setenv("MATRIX_SQLITE_TEST_DSN", f"sqlite:///{base_sqlite.as_posix()}")
    source = SourceConfig(
        name="rh_demo",
        engine="sqlite",
        description="fuente sintetica de prueba",
        enabled=True,
        secret_ref="MATRIX_SQLITE_TEST_DSN",
        max_rows=100,
        allowed_roles=["matrix_admin_test"],
        row_scope="role_view",
        approved_security_views=["demo_plantilla"],
        entities=[
            EntityConfig(
                name="plantilla",
                table="demo_plantilla",
                description="plantilla sintetica",
                # salario_mensual existe en la tabla pero NO esta permitido.
                allowed_columns=[
                    "empleado_id",
                    "departamento",
                    "centro_trabajo",
                    "puesto",
                    "antiguedad_anios",
                    "estatus",
                ],
                required_filters={"estatus": "activo"},
                max_rows=3,
            )
        ],
    )
    return SourceCatalog(sources={source.name: source})


@pytest.fixture()
def tool(catalogo: SourceCatalog) -> StructuredDataTool:
    return StructuredDataTool(catalog=catalogo)


def plan(**overrides) -> StructuredQueryPlan:  # noqa: ANN003
    payload = {
        "source": "rh_demo",
        "entity": "plantilla",
        "fields": ["departamento"],
        "aggregations": [{"function": "count", "field": "*", "alias": "total"}],
        "group_by": ["departamento"],
        "sort": [{"field": "departamento", "direction": "asc"}],
        "limit": 50,
    }
    payload.update(overrides)
    return StructuredQueryPlan.model_validate(payload)


class TestEjecucionReal:
    def test_agregacion_por_departamento(self, tool: StructuredDataTool, admin_context):
        evidence = tool.run(plan(), ctx=admin_context)
        assert evidence.source == "rh_demo"
        assert evidence.entity == "plantilla"
        assert "departamento" in evidence.columns
        assert "total" in evidence.columns

        conteos = {fila[0]: fila[1] for fila in evidence.rows}
        # El filtro obligatorio estatus='activo' excluye a E004 (Calidad, baja).
        assert conteos["Operaciones"] == 2
        assert conteos["Calidad"] == 1

    def test_el_filtro_organizacional_obligatorio_se_aplica_siempre(
        self, tool: StructuredDataTool, admin_context
    ):
        evidence = tool.run(
            plan(
                fields=["empleado_id"],
                aggregations=[],
                group_by=[],
                sort=[{"field": "empleado_id", "direction": "asc"}],
            ),
            ctx=admin_context,
        )
        ids = [fila[0] for fila in evidence.rows]
        assert "E004" not in ids, "el registro dado de baja no debe aparecer"

    def test_el_limite_de_la_entidad_acota_el_resultado(self, tool: StructuredDataTool, admin_context):
        evidence = tool.run(
            plan(fields=["empleado_id"], aggregations=[], group_by=[], limit=50), ctx=admin_context
        )
        # max_rows de la entidad es 3 aunque el plan pida 50.
        assert evidence.row_count == 3
        assert evidence.truncated is True

    def test_filtro_por_valor_parametrizado(self, tool: StructuredDataTool, admin_context):
        evidence = tool.run(
            plan(
                fields=["empleado_id"],
                aggregations=[],
                group_by=[],
                filters=[{"field": "centro_trabajo", "operator": "eq", "value": "Monterrey"}],
            ),
            ctx=admin_context,
        )
        assert evidence.row_count == 1
        assert evidence.rows[0][0] == "E003"

    def test_filtro_in(self, tool: StructuredDataTool, admin_context):
        evidence = tool.run(
            plan(
                fields=["empleado_id"],
                aggregations=[],
                group_by=[],
                filters=[
                    {"field": "departamento", "operator": "in", "value": ["Calidad", "Mantenimiento"]}
                ],
            ),
            ctx=admin_context,
        )
        assert {fila[0] for fila in evidence.rows} == {"E003", "E005"}

    def test_filtro_de_comparacion(self, tool: StructuredDataTool, admin_context):
        evidence = tool.run(
            plan(
                fields=["empleado_id"],
                aggregations=[],
                group_by=[],
                filters=[{"field": "antiguedad_anios", "operator": "gte", "value": 9}],
            ),
            ctx=admin_context,
        )
        assert {fila[0] for fila in evidence.rows} == {"E002", "E005"}

    def test_la_evidencia_es_trazable(self, tool: StructuredDataTool, admin_context):
        evidence = tool.run(plan(), ctx=admin_context)
        assert evidence.source_id == "db:rh_demo/plantilla"
        assert evidence.as_markdown_table().startswith("| departamento")


class TestControlesDeAcceso:
    def test_una_columna_no_declarada_se_rechaza(self, tool: StructuredDataTool, admin_context):
        """`salario_mensual` existe en la tabla pero no esta en la allowlist."""
        with pytest.raises(StructuredQueryRejectedError, match="no autorizados"):
            tool.run(
                plan(fields=["salario_mensual"], aggregations=[], group_by=[]), ctx=admin_context
            )

    def test_un_usuario_sin_la_fuente_concedida_es_denegado(
        self, catalogo: SourceCatalog, restricted_context
    ):
        from app.authorization.policy import PolicyEngine

        tool = StructuredDataTool(catalog=catalogo, policy_engine=PolicyEngine())
        with pytest.raises(ForbiddenError):
            tool.run(plan(), ctx=restricted_context)

    def test_el_catalogo_visible_no_incluye_columnas_prohibidas(
        self, tool: StructuredDataTool, admin_context
    ):
        catalogo_visible = tool.available_entities(admin_context)
        columnas = catalogo_visible[0]["entities"][0]["columns"]
        assert "salario_mensual" not in columnas


class TestDialectos:
    """El compilador debe producir el limite correcto por motor.

    El limite que aparece en el SQL es ``limit + 1``: la fila extra es la que
    permite marcar el resultado como truncado.
    """

    def _validated(self, engine: str, dialect_limit: str):  # noqa: ANN202
        source = SourceConfig(
            name="fuente",
            engine=engine,
            enabled=True,
            secret_ref="X",
            row_scope="role_view",
            approved_security_views=["tabla"],
            entities=[
                EntityConfig(name="e", table="tabla", allowed_columns=["a", "b"], max_rows=10)
            ],
        )
        validator = QueryPolicyValidator(catalog=SourceCatalog(sources={source.name: source}))
        from tests.conftest import make_context

        ctx = make_context(sources=frozenset({"fuente"}))
        validated = validator.validate(
            StructuredQueryPlan.model_validate(
                {"source": "fuente", "entity": "e", "fields": ["a"], "limit": 10}
            ),
            ctx=ctx,
        )
        compiled = compile_plan(validated)
        assert dialect_limit in compiled.sql
        return compiled

    def test_mysql_usa_limit(self):
        self._validated("mysql", "LIMIT 11")

    def test_postgresql_usa_limit(self):
        self._validated("postgresql", "LIMIT 11")

    def test_sqlserver_usa_top(self):
        compiled = self._validated("sqlserver", "TOP 11")
        assert "[tabla]" in compiled.sql

    def test_oracle_usa_fetch_first(self):
        compiled = self._validated("oracle", "FETCH FIRST 11 ROWS ONLY")
        assert '"tabla"' in compiled.sql


class TestOperadoresNulos:
    def test_is_null_e_is_not_null_no_generan_parametros(self, tool: StructuredDataTool, admin_context):
        from app.structured_data.validator import QueryPolicyValidator

        validator = QueryPolicyValidator(catalog=tool._catalog)
        validated = validator.validate(
            plan(
                fields=["empleado_id"],
                aggregations=[],
                group_by=[],
                filters=[{"field": "puesto", "operator": "is_not_null"}],
            ),
            ctx=admin_context,
        )
        compiled = compile_plan(validated)
        assert "IS NOT NULL" in compiled.sql
        # Solo el filtro obligatorio aporta parametro.
        assert len(compiled.parameters) == 1


class TestVerificacionAstAdicional:
    def test_acepta_group_by_y_order_by(self):
        verify_sql_is_readonly(
            "SELECT `a`, COUNT(*) AS `t` FROM `tabla` GROUP BY `a` ORDER BY `a` ASC LIMIT 5",
            dialect="mysql",
            allowed_table="tabla",
        )

    def test_rechaza_sql_no_analizable(self):
        with pytest.raises(StructuredQueryRejectedError):
            verify_sql_is_readonly("SELECT FROM WHERE", dialect="mysql", allowed_table="tabla")
