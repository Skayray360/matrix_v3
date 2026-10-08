# Creado por Aldo Garcia.
"""Plan de consulta estructurada: esquema, politica y compilacion a SQL."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.common.errors import ForbiddenError, StructuredQueryRejectedError
from app.structured_data.compiler import compile_plan, verify_sql_is_readonly
from app.structured_data.schemas import StructuredQueryPlan
from app.structured_data.sources import EntityConfig, SourceCatalog, SourceConfig
from app.structured_data.validator import QueryPolicyValidator

pytestmark = pytest.mark.unit


def build_catalog() -> SourceCatalog:
    source = SourceConfig(
        name="rh_demo",
        engine="mysql",
        description="fuente sintetica",
        enabled=True,
        secret_ref="MATRIX_DEMO_DB_DSN",
        allowed_roles=["matrix_admin_test"],
        row_scope="role_view",
        approved_security_views=["demo_plantilla"],
        entities=[
            EntityConfig(
                name="plantilla",
                table="demo_plantilla",
                allowed_columns=[
                    "empleado_id",
                    "departamento",
                    "centro_trabajo",
                    "puesto",
                    "antiguedad_anios",
                    "estatus",
                ],
                required_filters={"estatus": "activo"},
                max_rows=200,
            )
        ],
    )
    return SourceCatalog(sources={source.name: source})


@pytest.fixture()
def validator() -> QueryPolicyValidator:
    return QueryPolicyValidator(catalog=build_catalog())


def plan(**overrides) -> StructuredQueryPlan:  # noqa: ANN003
    payload = {
        "source": "rh_demo",
        "entity": "plantilla",
        "fields": ["departamento"],
        "filters": [],
        "aggregations": [{"function": "count", "field": "*", "alias": "total"}],
        "group_by": ["departamento"],
        "sort": [],
        "limit": 50,
    }
    payload.update(overrides)
    return StructuredQueryPlan.model_validate(payload)


class TestEsquemaDelPlan:
    def test_rechaza_campos_no_declarados(self):
        """``extra='forbid'``: un plan que inventa ``raw_sql`` no se ignora, se rechaza."""
        with pytest.raises(ValidationError):
            StructuredQueryPlan.model_validate(
                {"source": "rh_demo", "entity": "plantilla", "raw_sql": "SELECT 1"}
            )

    @pytest.mark.parametrize(
        "identificador",
        ["1tabla", "tabla; DROP TABLE users", "tabla--", "tab la", "", "a" * 80, "u*"],
    )
    def test_rechaza_identificadores_inseguros(self, identificador: str):
        with pytest.raises(ValidationError):
            StructuredQueryPlan.model_validate(
                {"source": identificador, "entity": "plantilla", "fields": ["a"]}
            )

    def test_rechaza_valores_no_escalares(self):
        with pytest.raises(ValidationError):
            StructuredQueryPlan.model_validate(
                {
                    "source": "rh_demo",
                    "entity": "plantilla",
                    "fields": ["departamento"],
                    "filters": [{"field": "departamento", "operator": "eq", "value": {"a": 1}}],
                }
            )

    def test_limite_maximo_acotado(self):
        with pytest.raises(ValidationError):
            plan(limit=999999)

    def test_columnas_referenciadas_incluyen_todo(self):
        p = plan(
            fields=["departamento"],
            filters=[{"field": "centro_trabajo", "operator": "eq", "value": "MTY"}],
            sort=[{"field": "departamento", "direction": "asc"}],
        )
        assert p.referenced_columns() == {"departamento", "centro_trabajo"}


class TestValidadorDePolitica:
    def test_plan_valido_se_acepta(self, validator: QueryPolicyValidator, admin_context):
        validated = validator.validate(plan(), ctx=admin_context)
        assert validated.entity.table == "demo_plantilla"
        assert validated.effective_limit == 50

    def test_columna_no_autorizada_se_rechaza(self, validator: QueryPolicyValidator, admin_context):
        with pytest.raises(StructuredQueryRejectedError, match="no autorizados"):
            validator.validate(plan(fields=["salario_mensual"], group_by=["salario_mensual"]), ctx=admin_context)

    def test_entidad_desconocida_se_rechaza(self, validator: QueryPolicyValidator, admin_context):
        with pytest.raises(StructuredQueryRejectedError):
            validator.validate(plan(entity="usuarios"), ctx=admin_context)

    def test_fuente_desconocida_se_rechaza(self, validator: QueryPolicyValidator, admin_context):
        with pytest.raises(StructuredQueryRejectedError):
            validator.validate(plan(source="otra_fuente"), ctx=admin_context)

    def test_usuario_sin_fuente_concedida_es_denegado(self, admin_context, restricted_context):
        from app.authorization.policy import PolicyEngine

        validator = QueryPolicyValidator(catalog=build_catalog(), policy_engine=PolicyEngine())
        with pytest.raises(ForbiddenError):
            validator.validate(plan(), ctx=restricted_context)

    def test_se_inyecta_el_filtro_organizacional_obligatorio(
        self, validator: QueryPolicyValidator, admin_context
    ):
        validated = validator.validate(plan(), ctx=admin_context)
        campos = [f.field for f in validated.injected_filters]
        assert "estatus" in campos

    def test_el_limite_se_acota_al_maximo_de_la_entidad(
        self, validator: QueryPolicyValidator, admin_context
    ):
        validated = validator.validate(plan(limit=5000), ctx=admin_context)
        assert validated.effective_limit == 200


class TestCompilador:
    def test_genera_select_parametrizado(self, validator: QueryPolicyValidator, admin_context):
        validated = validator.validate(
            plan(filters=[{"field": "centro_trabajo", "operator": "eq", "value": "MTY"}]),
            ctx=admin_context,
        )
        compiled = compile_plan(validated)
        assert compiled.sql.startswith("SELECT")
        assert "demo_plantilla" in compiled.sql
        # El valor viaja como parametro, jamas interpolado en el SQL.
        assert "MTY" not in compiled.sql
        assert "MTY" in compiled.parameters.values()

    def test_el_valor_malicioso_no_altera_la_sentencia(
        self, validator: QueryPolicyValidator, admin_context
    ):
        payload = "MTY'; DROP TABLE users; --"
        validated = validator.validate(
            plan(filters=[{"field": "centro_trabajo", "operator": "eq", "value": payload}]),
            ctx=admin_context,
        )
        compiled = compile_plan(validated)
        assert "DROP" not in compiled.sql.upper()
        assert payload in compiled.parameters.values()

    def test_in_vacio_produce_condicion_falsa(self, validator: QueryPolicyValidator, admin_context):
        validated = validator.validate(
            plan(filters=[{"field": "departamento", "operator": "in", "value": []}]),
            ctx=admin_context,
        )
        compiled = compile_plan(validated)
        assert "1 = 0" in compiled.sql

    def test_aplica_el_limite_pidiendo_una_fila_de_mas(
        self, validator: QueryPolicyValidator, admin_context
    ):
        """El SQL pide `limit + 1` para poder detectar truncamiento."""
        compiled = compile_plan(validator.validate(plan(limit=25), ctx=admin_context))
        assert compiled.limit == 25
        assert compiled.sql_limit == 26
        assert "LIMIT 26" in compiled.sql


class TestVerificacionPorAst:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT a FROM t; DROP TABLE t",
            "DELETE FROM t",
            "UPDATE t SET a = 1",
            "INSERT INTO t VALUES (1)",
            "DROP TABLE t",
            "CREATE TABLE x (a INT)",
            "ALTER TABLE t ADD COLUMN b INT",
            "SELECT a FROM t -- comentario",
            "SELECT a FROM otra_tabla",
            "SELECT a FROM t UNION SELECT b FROM u",
            "SELECT (SELECT b FROM u) FROM t",
        ],
    )
    def test_rechaza_sql_no_permitido(self, sql: str):
        with pytest.raises(StructuredQueryRejectedError):
            verify_sql_is_readonly(sql, dialect="mysql", allowed_table="t")

    def test_acepta_un_select_simple_sobre_la_tabla_autorizada(self):
        verify_sql_is_readonly("SELECT `a` FROM `t` WHERE `b` = :p0 LIMIT 10", dialect="mysql", allowed_table="t")
