# Creado por Aldo Garcia.
"""Ramas restantes de los modulos criticos de seguridad y datos.

Objetivo declarado en `docs/README.md`: cobertura >= 95 % en autorizacion,
autenticacion, seguridad y acceso a datos estructurados. Cada caso cubre una
rama de error o de rechazo que, sin prueba, quedaria sin ejercitar precisamente
en el codigo donde mas importa.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from app.auth.provider import IntegrationStatus
from app.common.errors import ConfigurationError, StructuredQueryRejectedError, UnsupportedFileError
from app.security.upload_guard import validate_upload
from app.structured_data.adapters import ReadOnlySourceAdapter
from app.structured_data.compiler import CompiledQuery
from app.structured_data.sources import EntityConfig, SourceConfig

pytestmark = [pytest.mark.unit, pytest.mark.security]


def source(**overrides) -> SourceConfig:  # noqa: ANN003
    base = {
        "name": "demo",
        "engine": "mysql",
        "enabled": True,
        "secret_ref": "MATRIX_BRANCH_DSN",
        "row_scope": "role_view",
        "approved_security_views": ["t"],
        "entities": [EntityConfig(name="e", table="t", allowed_columns=["a"])],
    }
    base.update(overrides)
    return SourceConfig(**base)


class TestAdapterRamasDeError:
    def test_sin_dsn_no_se_puede_crear_el_engine(self):
        adapter = ReadOnlySourceAdapter(source())
        with pytest.raises(ConfigurationError, match="DSN"):
            adapter._get_engine()

    def test_un_dsn_invalido_falla_al_crear_el_engine(self, monkeypatch):
        monkeypatch.setenv("MATRIX_BRANCH_DSN", "driver-inexistente://u:p@h/db")
        adapter = ReadOnlySourceAdapter(source())
        with pytest.raises(ConfigurationError, match="engine"):
            adapter._get_engine()

    def test_rechaza_cualquier_sql_que_no_empiece_por_select(self, monkeypatch):
        """Ultima barrera antes del motor, aunque el compilador ya lo impida."""
        monkeypatch.setenv("MATRIX_BRANCH_DSN", "sqlite://")
        adapter = ReadOnlySourceAdapter(source(engine="sqlite"))
        peligroso = CompiledQuery(
            sql="DELETE FROM t", parameters={}, dialect="sqlite", limit=10, sql_limit=11
        )
        with pytest.raises(StructuredQueryRejectedError, match="SELECT"):
            adapter.execute(peligroso, entity_name="e")

    def test_un_fallo_de_ejecucion_se_traduce_sin_filtrar_sql(self, monkeypatch):
        monkeypatch.setenv("MATRIX_BRANCH_DSN", "sqlite://")
        adapter = ReadOnlySourceAdapter(source(engine="sqlite"))
        # La tabla no existe en la base en memoria: el motor falla.
        consulta = CompiledQuery(
            sql="SELECT a FROM tabla_inexistente LIMIT 11",
            parameters={},
            dialect="sqlite",
            limit=10,
            sql_limit=11,
        )
        with pytest.raises(StructuredQueryRejectedError) as excinfo:
            adapter.execute(consulta, entity_name="e")
        assert "tabla_inexistente" not in excinfo.value.message

    def test_los_argumentos_de_conexion_dependen_del_motor(self):
        assert "connect_timeout" in ReadOnlySourceAdapter(source()).\
            _connect_args()
        assert "connect_timeout" in ReadOnlySourceAdapter(source(engine="postgresql"))._connect_args()
        assert "timeout" in ReadOnlySourceAdapter(source(engine="sqlserver"))._connect_args()
        assert ReadOnlySourceAdapter(source(engine="oracle"))._connect_args() == {}

    def test_dispose_es_seguro_aunque_no_haya_engine(self):
        adapter = ReadOnlySourceAdapter(source())
        adapter.dispose()  # sin engine creado
        adapter.dispose()  # idempotente

    def test_health_check_real_contra_sqlite(self, monkeypatch):
        monkeypatch.setenv("MATRIX_BRANCH_DSN", "sqlite://")
        ok, detail = ReadOnlySourceAdapter(source(engine="sqlite")).health_check()
        assert ok is True
        assert detail == str(IntegrationStatus.CONNECTED_AND_VALIDATED)

    def test_ejecucion_real_devuelve_columnas_y_filas(self, monkeypatch):
        monkeypatch.setenv("MATRIX_BRANCH_DSN", "sqlite://")
        adapter = ReadOnlySourceAdapter(source(engine="sqlite"))
        consulta = CompiledQuery(
            sql="SELECT 1 AS a, 'x' AS b LIMIT 11",
            parameters={},
            dialect="sqlite",
            limit=10,
            sql_limit=11,
        )
        outcome = adapter.execute(consulta, entity_name="e")
        assert outcome.columns == ("a", "b")
        assert outcome.row_count == 1
        assert outcome.truncated is False
        assert outcome.to_public_dict()["rows"] == [[1, "x"]]


class TestUploadGuardRamasRestantes:
    def test_un_ooxml_con_demasiadas_entradas_se_rechaza(self, monkeypatch):
        from app.security import upload_guard

        monkeypatch.setattr(upload_guard, "MAX_ARCHIVE_ENTRIES", 3)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for i in range(10):
                archive.writestr(f"parte{i}.xml", b"<xml/>")
        with pytest.raises(UnsupportedFileError, match="entradas"):
            validate_upload(buffer.getvalue(), filename="muchas.docx")

    def test_un_ratio_de_compresion_sospechoso_se_rechaza(self, monkeypatch):
        from app.security import upload_guard

        monkeypatch.setattr(upload_guard, "MAX_COMPRESSION_RATIO", 2)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", b"0" * 200_000)
        with pytest.raises(UnsupportedFileError, match="compresion"):
            validate_upload(buffer.getvalue(), filename="ratio.docx")

    def test_un_xlsx_valido_se_acepta(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml", b"<xml/>")
        resultado = validate_upload(buffer.getvalue(), filename="libro.xlsx")
        assert resultado.extension == ".xlsx"
        assert resultado.mime_type.endswith("spreadsheetml.sheet")

    def test_el_nombre_original_se_conserva_para_auditoria(self):
        resultado = validate_upload(b"# doc", filename="../../Politica Final.md")
        assert resultado.original_filename == "../../Politica Final.md"
        assert resultado.safe_display_name == "Politica Final.md"


class TestEntraRamasRestantes:
    def test_el_estado_es_error_si_el_tenant_no_responde(self, monkeypatch):
        from app.auth.entra_provider import EntraIdentityProvider
        from app.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "entra_tenant_id", "tenant-real", raising=False)
        monkeypatch.setattr(settings, "entra_client_id", "client-real", raising=False)
        monkeypatch.setattr(settings, "entra_redirect_uri", "https://x/cb", raising=False)

        def sin_red(_request):  # noqa: ANN001, ANN202
            raise httpx.ConnectError("sin conexion")

        provider = EntraIdentityProvider(http_client=httpx.Client(transport=httpx.MockTransport(sin_red)))
        assert provider.status() is IntegrationStatus.ERROR

    def test_el_estado_es_error_si_el_discovery_devuelve_404(self, monkeypatch):
        from app.auth.entra_provider import EntraIdentityProvider
        from app.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "entra_tenant_id", "tenant-real", raising=False)
        monkeypatch.setattr(settings, "entra_client_id", "client-real", raising=False)
        monkeypatch.setattr(settings, "entra_redirect_uri", "https://x/cb", raising=False)

        provider = EntraIdentityProvider(
            http_client=httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))
        )
        assert provider.status() is IntegrationStatus.ERROR

    def test_el_discovery_con_issuer_incorrecto_se_rechaza(self, monkeypatch):
        from app.auth.entra_provider import EntraIdentityProvider
        from app.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "entra_tenant_id", "tenant-real", raising=False)
        monkeypatch.setattr(settings, "entra_client_id", "client-real", raising=False)
        monkeypatch.setattr(settings, "entra_redirect_uri", "https://x/cb", raising=False)

        provider = EntraIdentityProvider(
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"issuer": "x"}))
            )
        )
        assert provider.status() is IntegrationStatus.ERROR


class TestProveedorSeleccionado:
    def test_un_proveedor_desconocido_falla(self, monkeypatch):
        from app.auth.provider import get_identity_provider
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "auth_provider", "inventado", raising=False)
        with pytest.raises(ConfigurationError, match="AUTH_PROVIDER"):
            get_identity_provider()

    def test_el_proveedor_local_no_se_instancia_fuera_de_desarrollo(self, monkeypatch):
        from app.auth.local_provider import LocalTestIdentityProvider
        from app.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(type(settings), "is_local_auth_allowed", property(lambda _s: False))
        with pytest.raises(ConfigurationError, match="no puede instanciarse"):
            LocalTestIdentityProvider()

    def test_el_estado_del_proveedor_local_refleja_el_entorno(self, monkeypatch):
        from app.auth.local_provider import LocalTestIdentityProvider
        from app.config import get_settings

        provider = LocalTestIdentityProvider()
        settings = get_settings()
        monkeypatch.setattr(type(settings), "is_local_auth_allowed", property(lambda _s: False))
        assert provider.status() is IntegrationStatus.DISABLED


class TestRedaccionRamasRestantes:
    def test_redacta_dentro_de_listas_y_tuplas(self):
        from app.common.redaction import REDACTED, redact_value

        # secrets-scan: allow (cadena sintetica; la prueba verifica que se redacte)
        datos = ["mysql+pymysql://root:Clave123@host/db", ("password", "otra")]
        redactado = redact_value(datos)
        assert "Clave123" not in str(redactado)
        assert REDACTED in str(redactado)

    def test_los_valores_no_textuales_se_conservan(self):
        from app.common.redaction import redact_value

        assert redact_value(42) == 42
        assert redact_value(None) is None
        assert redact_value(True) is True

    def test_el_truncado_no_altera_textos_cortos(self):
        from app.common.redaction import truncate_for_log

        assert truncate_for_log("texto corto", limit=100) == "texto corto"
