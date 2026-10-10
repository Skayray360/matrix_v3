# Creado por Aldo Garcia.
"""Casos que cierran huecos de cobertura en modulos criticos.

Cada prueba de este modulo existe porque el informe de cobertura mostro una rama
sin ejercitar en un componente de seguridad o de datos. No son pruebas de
relleno: todas comprueban un comportamiento que importa.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.common.errors import (
    ErrorCode,
    ForbiddenError,
    MatrixError,
    StructuredQueryRejectedError,
    UnauthorizedError,
)
from app.structured_data.compiler import compile_plan
from app.structured_data.schemas import StructuredQueryPlan
from app.structured_data.sources import EntityConfig, SourceCatalog, SourceConfig
from app.structured_data.validator import QueryPolicyValidator
from tests.conftest import make_context

pytestmark = [pytest.mark.unit, pytest.mark.security]


# ---------------------------------------------------------------------------
# Validador de planes: ramas de rechazo poco frecuentes
# ---------------------------------------------------------------------------
def build_validator(**entity_overrides) -> QueryPolicyValidator:  # noqa: ANN003
    entity_kwargs = {
        "name": "plantilla",
        "table": "demo_plantilla",
        "allowed_columns": ["departamento", "estatus", "empleado_id"],
        "max_rows": 50,
    }
    entity_kwargs.update(entity_overrides)
    source = SourceConfig(
        name="rh_demo",
        engine="mysql",
        enabled=True,
        secret_ref="X",
        row_scope="role_view",
        approved_security_views=["demo_plantilla"],
        entities=[EntityConfig(**entity_kwargs)],
    )
    return QueryPolicyValidator(catalog=SourceCatalog(sources={source.name: source}))


def plan(**overrides) -> StructuredQueryPlan:  # noqa: ANN003
    payload = {
        "source": "rh_demo",
        "entity": "plantilla",
        "fields": ["departamento"],
        "aggregations": [{"function": "count", "field": "*", "alias": "total"}],
        "group_by": ["departamento"],
        "limit": 10,
    }
    payload.update(overrides)
    return StructuredQueryPlan.model_validate(payload)


class TestValidadorRamasDeRechazo:
    def test_un_plan_sin_proyeccion_ni_agregacion_se_rechaza(self, admin_context):
        with pytest.raises(StructuredQueryRejectedError, match="proyectar"):
            build_validator().validate(plan(fields=[], aggregations=[], group_by=[]), ctx=admin_context)

    def test_agregacion_con_campos_pero_sin_group_by_se_rechaza(self, admin_context):
        with pytest.raises(StructuredQueryRejectedError, match="group_by"):
            build_validator().validate(plan(group_by=[]), ctx=admin_context)

    def test_group_by_fuera_de_la_proyeccion_se_rechaza(self, admin_context):
        with pytest.raises(StructuredQueryRejectedError, match="contenido"):
            build_validator().validate(
                plan(fields=["departamento"], group_by=["estatus"]), ctx=admin_context
            )

    def test_una_entidad_sin_columnas_habilitadas_se_rechaza(self, admin_context):
        validator = build_validator(allowed_columns=[])
        with pytest.raises(StructuredQueryRejectedError, match="columnas habilitadas"):
            validator.validate(plan(), ctx=admin_context)

    def test_un_filtro_obligatorio_sobre_columna_no_permitida_se_rechaza(self, admin_context):
        validator = build_validator(
            allowed_columns=["departamento"], required_filters={"columna_oculta": "x"}
        )
        with pytest.raises(StructuredQueryRejectedError, match="filtro sobre una columna"):
            validator.validate(
                plan(fields=["departamento"], group_by=["departamento"]), ctx=admin_context
            )

    def test_una_fuente_deshabilitada_se_rechaza(self, admin_context):
        source = SourceConfig(
            name="rh_demo",
            engine="mysql",
            enabled=False,
            entities=[EntityConfig(name="plantilla", table="t", allowed_columns=["a"])],
        )
        validator = QueryPolicyValidator(catalog=SourceCatalog(sources={source.name: source}))
        with pytest.raises(StructuredQueryRejectedError, match="habilitada"):
            validator.validate(plan(fields=["a"], aggregations=[], group_by=[]), ctx=admin_context)

    def test_sin_motor_de_politicas_se_usa_allowed_roles(self):
        source = SourceConfig(
            name="rh_demo",
            engine="mysql",
            enabled=True,
            allowed_roles=["otro_rol"],
            entities=[EntityConfig(name="plantilla", table="t", allowed_columns=["a"])],
        )
        validator = QueryPolicyValidator(catalog=SourceCatalog(sources={source.name: source}))
        ctx = make_context(roles=frozenset({"rol_sin_acceso"}))
        with pytest.raises(ForbiddenError):
            validator.validate(plan(fields=["a"], aggregations=[], group_by=[]), ctx=ctx)

    def test_el_filtro_obligatorio_se_anade_ademas_del_declarado(self, admin_context):
        validator = build_validator(required_filters={"estatus": "activo"})
        validated = validator.validate(
            plan(
                fields=["departamento"],
                group_by=["departamento"],
                filters=[{"field": "estatus", "operator": "eq", "value": "baja"}],
            ),
            ctx=admin_context,
        )
        campos = [f.field for f in validated.all_filters()]
        # El del plan y el obligatorio: el obligatorio no sustituye al del plan.
        assert campos.count("estatus") == 2

        compiled = compile_plan(validated)
        assert list(compiled.parameters.values()).count("activo") == 1


# ---------------------------------------------------------------------------
# Errores tipados
# ---------------------------------------------------------------------------
class TestErroresTipados:
    def test_la_representacion_publica_no_incluye_el_detalle(self):
        error = ForbiddenError(detail="el usuario intento leer nomina")
        publico = error.to_public_dict(request_id="req-1")
        assert publico["code"] == str(ErrorCode.FORBIDDEN)
        assert publico["request_id"] == "req-1"
        assert "nomina" not in str(publico)

    def test_el_mensaje_de_denegacion_es_generico(self):
        """Revelar el recurso denegado permitiria inferir que existe."""
        assert "acceso" in ForbiddenError().message.lower()
        assert "nomina" not in ForbiddenError().message.lower()

    def test_cada_error_declara_su_codigo_y_estado(self):
        for clase in (UnauthorizedError, ForbiddenError, StructuredQueryRejectedError):
            error = clase()
            assert isinstance(error, MatrixError)
            assert error.code
            assert 200 <= error.status_code < 600

    def test_el_catalogo_de_codigos_es_cerrado(self):
        esperados = {
            "unauthorized",
            "forbidden",
            "unsupported_file",
            "file_too_large",
            "extraction_failed",
            "ingestion_failed",
            "ollama_unavailable",
            "embedding_dimension_mismatch",
            "qdrant_unavailable",
            "database_unavailable",
            "structured_query_rejected",
            "insufficient_evidence",
            "out_of_scope",
            "policy_missing",
        }
        assert esperados <= {str(c) for c in ErrorCode}


# ---------------------------------------------------------------------------
# Guard de cargas: ramas restantes
# ---------------------------------------------------------------------------
class TestUploadGuardRamas:
    def test_un_ooxml_que_no_es_zip_se_rechaza(self):
        from app.common.errors import UnsupportedFileError
        from app.security.upload_guard import validate_upload

        with pytest.raises(UnsupportedFileError, match="no corresponde"):
            validate_upload(b"esto no es un docx", filename="falso.docx")

    def test_un_docx_valido_pequeno_se_acepta(self):
        import io

        from docx import Document

        from app.security.upload_guard import validate_upload

        buffer = io.BytesIO()
        document = Document()
        document.add_paragraph("Contenido sintetico de un archivo Word valido.")
        document.save(buffer)
        resultado = validate_upload(buffer.getvalue(), filename="valido.docx")
        assert resultado.extension == ".docx"

    def test_resolve_within_acepta_subcarpetas(self, tmp_path: Path):
        from app.security.upload_guard import resolve_within

        destino = resolve_within(tmp_path, "prestaciones/2026/doc.md")
        assert destino.is_relative_to(tmp_path.resolve())
        assert destino.name == "doc.md"

    def test_un_nombre_solo_de_puntos_se_normaliza(self):
        from app.security.upload_guard import sanitize_display_name

        assert sanitize_display_name("...") == "documento"


# ---------------------------------------------------------------------------
# Contexto de usuario
# ---------------------------------------------------------------------------
class TestContextoRamas:
    def test_has_permission_y_has_role(self):
        ctx = make_context()
        assert ctx.has_role("matrix_admin_test") is True
        assert ctx.has_role("rol_inexistente") is False
        assert ctx.has_permission("knowledge.admin") is True
        assert ctx.is_knowledge_admin is True

    def test_un_usuario_restringido_no_es_administrador(self, restricted_context):
        assert restricted_context.is_knowledge_admin is False
        assert restricted_context.has_permission("users.admin") is False


# ---------------------------------------------------------------------------
# Configuracion: propiedades derivadas
# ---------------------------------------------------------------------------
class TestPropiedadesDerivadas:
    def test_extensiones_permitidas(self):
        from app.config import get_settings

        extensiones = get_settings().allowed_upload_extensions
        assert ".docx" in extensiones
        assert ".exe" not in extensiones

    def test_grupos_de_entra_se_parsean(self):
        from app.config.settings import Settings

        settings = Settings(
            app_env="test",
            app_secret_key="0" * 64,
            entra_allowed_groups=" grupo-a , grupo-b ,, ",
            _env_file=None,
        )
        assert settings.entra_allowed_group_list == ("grupo-a", "grupo-b")

    def test_las_rutas_relativas_se_resuelven_contra_la_raiz(self):
        from app.config import PROJECT_ROOT, get_settings

        settings = get_settings()
        assert settings.knowledge_root_path.is_absolute()
        assert settings.upload_storage_path.is_absolute()
        assert str(settings.authorization_policy_path).startswith(str(PROJECT_ROOT))

    def test_la_url_de_ollama_se_normaliza(self):
        from app.config.settings import Settings

        settings = Settings(
            app_env="test",
            app_secret_key="0" * 64,
            ollama_base_url="http://127.0.0.1:11434/",
            _env_file=None,
        )
        assert settings.ollama_base_url == "http://127.0.0.1:11434"

    def test_un_nivel_de_log_invalido_se_rechaza(self):
        from pydantic import ValidationError

        from app.config.settings import Settings

        with pytest.raises(ValidationError, match="APP_LOG_LEVEL"):
            Settings(app_env="test", app_secret_key="0" * 64, app_log_level="VERBOSO", _env_file=None)
