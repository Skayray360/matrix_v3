# Creado por Aldo Garcia.
"""Servicios de ingesta, scheduler, fuentes estructuradas y catalogo.

Todo con dobles o Qdrant en memoria: no requiere infraestructura externa.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from app.auth.provider import IntegrationStatus
from app.common.errors import ConfigurationError
from app.config import get_settings
from app.rag.vector_store import VectorStore
from app.structured_data.adapters import ENGINE_DRIVERS, ReadOnlySourceAdapter
from app.structured_data.sources import EntityConfig, SourceCatalog, SourceConfig, load_sources
from app.structured_data.tool import StructuredDataTool, StructuredEvidence

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Catalogo de fuentes
# ---------------------------------------------------------------------------
class TestCatalogoDeFuentes:
    def test_carga_el_catalogo_real_del_proyecto(self):
        catalog = load_sources()
        assert "rh_demo" in catalog.names()
        # Ninguna fuente esta habilitada por defecto: PREPARED_NOT_CONNECTED.
        assert catalog.enabled_names() == ()

    def test_el_reporte_de_estado_no_expone_credenciales(self):
        for row in load_sources().status_report():
            serialized = str(row).lower()
            assert "password" not in serialized
            assert "://" not in serialized
            # secret_ref es el NOMBRE de la variable, no su valor.
            assert row["secret_ref"].isupper() or row["secret_ref"] == ""

    def test_un_archivo_inexistente_devuelve_catalogo_vacio(self, tmp_path: Path):
        assert load_sources(tmp_path / "no-existe.yaml").names() == ()

    def test_un_archivo_invalido_falla_de_forma_diagnostica(self, tmp_path: Path):
        malo = tmp_path / "sources.yaml"
        malo.write_text("sources:\n  - name: 1invalido\n    engine: mysql\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="invalido"):
            load_sources(malo)

    def test_rechaza_un_motor_no_soportado(self):
        with pytest.raises(ValueError, match="Motor no soportado"):
            SourceConfig(name="x", engine="cassandra")

    def test_el_dsn_se_lee_del_entorno(self, monkeypatch):
        source = SourceConfig(name="x", engine="mysql", secret_ref="MATRIX_TEST_DSN")
        assert source.dsn() is None
        monkeypatch.setenv("MATRIX_TEST_DSN", "mysql+pymysql://u:p@h/db")
        assert source.dsn() == "mysql+pymysql://u:p@h/db"

    def test_estados_de_integracion(self, monkeypatch):
        deshabilitada = SourceConfig(name="x", engine="mysql", enabled=False)
        assert deshabilitada.status() is IntegrationStatus.DISABLED

        preparada = SourceConfig(name="x", engine="mysql", enabled=True, secret_ref="MATRIX_TEST_DSN2")
        assert preparada.status() is IntegrationStatus.PREPARED_NOT_CONNECTED

        monkeypatch.setenv("MATRIX_TEST_DSN2", "mysql+pymysql://u:p@h/db")
        # Una variable definida no demuestra que la fuente exista/responda.
        assert preparada.status() is IntegrationStatus.PREPARED_NOT_CONNECTED


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
class TestAdapterReadOnly:
    def _source(self, **overrides) -> SourceConfig:  # noqa: ANN003
        base = {
            "name": "demo",
            "engine": "mysql",
            "enabled": True,
            "secret_ref": "MATRIX_ADAPTER_DSN",
            "row_scope": "role_view",
            "approved_security_views": ["demo_plantilla"],
            "entities": [
                EntityConfig(name="plantilla", table="demo_plantilla", allowed_columns=["a"])
            ],
        }
        base.update(overrides)
        return SourceConfig(**base)

    def test_detecta_falta_de_dsn(self):
        problemas = ReadOnlySourceAdapter(self._source()).validate_configuration()
        assert any("MATRIX_ADAPTER_DSN" in p for p in problemas)

    def test_detecta_dsn_invalido(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ADAPTER_DSN", "esto no es un dsn")
        problemas = ReadOnlySourceAdapter(self._source()).validate_configuration()
        assert any("DSN invalido" in p for p in problemas)

    def test_detecta_fuente_sin_entidades(self):
        problemas = ReadOnlySourceAdapter(self._source(entities=[])).validate_configuration()
        assert any("entidades" in p for p in problemas)

    def test_configuracion_valida_no_reporta_problemas(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ADAPTER_DSN", "mysql+pymysql://u:p@127.0.0.1:3306/db")
        assert ReadOnlySourceAdapter(self._source()).validate_configuration() == []

    def test_health_check_de_una_fuente_deshabilitada(self):
        ok, detail = ReadOnlySourceAdapter(self._source(enabled=False)).health_check()
        assert ok is False
        assert detail == str(IntegrationStatus.DISABLED)

    def test_health_check_sin_dsn(self):
        ok, detail = ReadOnlySourceAdapter(self._source()).health_check()
        assert ok is False
        assert detail == str(IntegrationStatus.PREPARED_NOT_CONNECTED)

    def test_health_check_con_dsn_inalcanzable(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ADAPTER_DSN", "mysql+pymysql://u:p@127.0.0.1:59999/db")
        ok, detail = ReadOnlySourceAdapter(self._source()).health_check()
        assert ok is False
        assert detail.startswith(str(IntegrationStatus.ERROR))

    def test_hay_driver_declarado_para_cada_motor_soportado(self):
        for engine in ("mysql", "mariadb", "postgresql", "sqlserver", "oracle"):
            assert engine in ENGINE_DRIVERS


# ---------------------------------------------------------------------------
# Tool de datos estructurados
# ---------------------------------------------------------------------------
class TestStructuredDataTool:
    def _catalog(self) -> SourceCatalog:
        source = SourceConfig(
            name="rh_demo",
            engine="mysql",
            enabled=True,
            secret_ref="MATRIX_DEMO_DB_DSN",
            description="demo",
            row_scope="role_view",
            approved_security_views=["demo_plantilla"],
            entities=[
                EntityConfig(
                    name="plantilla",
                    table="demo_plantilla",
                    description="plantilla",
                    allowed_columns=["departamento", "estatus"],
                )
            ],
        )
        return SourceCatalog(sources={source.name: source})

    def test_el_catalogo_visible_respeta_los_permisos(self, admin_context, restricted_context):
        tool = StructuredDataTool(catalog=self._catalog())
        assert [s["source"] for s in tool.available_entities(admin_context)] == ["rh_demo"]
        # El usuario restringido no tiene la fuente concedida: ni siquiera la ve.
        assert tool.available_entities(restricted_context) == []

    def test_el_reporte_de_salud_lista_todas_las_fuentes(self):
        report = StructuredDataTool(catalog=self._catalog()).health_report()
        assert [r["source"] for r in report] == ["rh_demo"]
        assert report[0]["status"] in {str(s) for s in IntegrationStatus}

    def test_una_fuente_desconocida_se_rechaza(self):
        from app.common.errors import StructuredQueryRejectedError

        tool = StructuredDataTool(catalog=SourceCatalog(sources={}))
        with pytest.raises(StructuredQueryRejectedError):
            tool._adapter("inexistente")


class TestEvidenciaEstructurada:
    def _evidence(self, rows) -> StructuredEvidence:  # noqa: ANN001
        return StructuredEvidence(
            source_id="db:rh_demo/plantilla",
            source="rh_demo",
            entity="plantilla",
            columns=("departamento", "personas"),
            rows=tuple(rows),
            row_count=len(rows),
            truncated=False,
        )

    def test_se_serializa_como_tabla_markdown(self):
        markdown = self._evidence([("Operaciones", 120), ("Calidad", 35)]).as_markdown_table()
        assert "| departamento | personas |" in markdown
        assert "Operaciones" in markdown

    def test_acota_las_filas_en_el_prompt(self):
        rows = [(f"area{i}", i) for i in range(60)]
        markdown = self._evidence(rows).as_markdown_table(max_rows=10)
        assert "se muestran 10 de 60 filas" in markdown

    def test_sin_columnas_lo_dice(self):
        vacio = StructuredEvidence(
            source_id="db:x/y", source="x", entity="y", columns=(), rows=(), row_count=0, truncated=False
        )
        assert vacio.as_markdown_table() == "(sin resultados)"

    def test_la_vista_publica_incluye_la_trazabilidad(self):
        public = self._evidence([("Operaciones", 120)]).to_public_dict()
        assert public["source_id"] == "db:rh_demo/plantilla"
        assert public["row_count"] == 1


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
class TestScheduler:
    def test_estado_inicial(self):
        from app.jobs.scheduler import ReconcileScheduler

        scheduler = ReconcileScheduler(interval_hours=24)
        assert scheduler.state.running is False
        assert scheduler.state.last_status == "never_run"
        assert scheduler.interval.total_seconds() == 24 * 3600

    def test_un_fallo_no_mata_el_hilo(self, monkeypatch):
        from app.jobs import scheduler as scheduler_module
        from app.jobs.scheduler import ReconcileScheduler

        def explota(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("fallo simulado")

        monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: None, raising=False)
        monkeypatch.setattr("app.database.engine.session_scope", explota)

        scheduler = ReconcileScheduler(interval_hours=24)
        scheduler.run_once()  # no debe propagar
        assert scheduler.state.last_status.startswith("error:")
        assert scheduler.state.last_run_at is not None

    def test_arranque_y_parada_son_idempotentes(self, monkeypatch):
        from app.jobs.scheduler import ReconcileScheduler

        scheduler = ReconcileScheduler(interval_hours=24)
        # La sincronizacion inicial ahora es inmediata; este contrato comprueba
        # el ciclo de vida del hilo, sin abrir una base real ni ingerir archivos.
        monkeypatch.setattr(scheduler, "run_sync_once", lambda: None)
        scheduler.start()
        scheduler.start()  # segunda llamada: no duplica el hilo
        assert scheduler.state.running is True
        scheduler.stop()
        assert scheduler.state.running is False


# ---------------------------------------------------------------------------
# Coleccion de Qdrant
# ---------------------------------------------------------------------------
class TestColeccionesDelVectorStore:
    def test_selecciona_la_coleccion_por_scope(self):
        store = VectorStore(client=QdrantClient(location=":memory:"))
        settings = get_settings()
        assert store.collection_for("corporate") == settings.rag_collection_corporate
        assert store.collection_for("conversation") == settings.rag_collection_private

    def test_ensure_collection_es_idempotente(self):
        store = VectorStore(client=QdrantClient(location=":memory:"))
        name = get_settings().rag_collection_corporate
        store.ensure_collection(name)
        store.ensure_collection(name)
        assert store.count(name) == 0
