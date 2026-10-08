# Creado por Aldo Garcia.
"""Reconciliacion incremental del knowledge root.

Verifica el comportamiento exigido por la seccion 10.B: altas, modificaciones por
SHA-256, bajas, categorias nuevas con deny-by-default, idempotencia y lock.

Usa un knowledge root temporal, Qdrant en memoria y embeddings simulados: no
depende de Ollama ni del almacen en disco.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import select

from app.database.models import Document, IngestionJob, JobLock
from app.ingestion.reconciler import (
    LockNotAcquired,
    category_from_relative_path,
    iter_knowledge_files,
    reconcile_knowledge,
    reconcile_lock,
    reconcile_with_lock,
)
from app.ingestion.service import IngestionService
from app.rag.schemas import SCOPE_CORPORATE
from app.rag.vector_store import VectorStore

pytestmark = pytest.mark.integration

DIMENSION = 768
CONTENIDO = "contenido corporativo sintetico de prueba con longitud suficiente " * 10


class FakeEmbeddingClient:
    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * DIMENSION
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "big") % DIMENSION] += 1.0
        norm = sum(v * v for v in vector) ** 0.5
        return [v / norm for v in vector] if norm else [1.0] + [0.0] * (DIMENSION - 1)

    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:  # noqa: ARG002
        return [self._vector(t) for t in texts]

    def embed_one(self, text: str, *, model: str | None = None) -> list[float]:  # noqa: ARG002
        return self._vector(text)


@pytest.fixture()
def knowledge_root(tmp_path: Path, monkeypatch) -> Path:  # noqa: ANN001
    """Knowledge root aislado, apuntado por la configuracion."""
    root = tmp_path / "knowledge"
    (tmp_path / "data" / "knowledge").mkdir(parents=True)
    monkeypatch.setattr("app.ingestion.knowledge_layout.PROJECT_ROOT", tmp_path)
    (root / "prestaciones").mkdir(parents=True)
    (root / "prestaciones" / "politica.md").write_text(
        f"# Politica\n\n## Seccion\n{CONTENIDO}\n", encoding="utf-8"
    )
    (root / "prestaciones" / "README.md").write_text("# No indexar\n", encoding="utf-8")

    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(type(settings), "knowledge_root_path", property(lambda _self: root))
    return root


@pytest.fixture()
def service() -> IngestionService:
    return IngestionService(
        store=VectorStore(client=QdrantClient(location=":memory:")), llm=FakeEmbeddingClient()
    )


@pytest.fixture(autouse=True)
def _limpiar_documentos(db_session):  # noqa: ANN001, ANN202
    """Deja la tabla de documentos corporativos limpia antes y despues.

    La reconciliacion compara contra TODO lo registrado, asi que un residuo de
    otra prueba se contaria como "archivo eliminado".
    """
    from sqlalchemy import delete

    db_session.execute(delete(Document).where(Document.scope == SCOPE_CORPORATE))
    db_session.commit()
    yield
    db_session.execute(delete(Document).where(Document.scope == SCOPE_CORPORATE))
    db_session.execute(delete(JobLock))
    db_session.commit()


class TestRecorridoDelKnowledgeRoot:
    def test_clasifica_el_arbol_oficial_y_el_formato_anterior(self):
        assert category_from_relative_path("general/prestaciones/politica.md") == "general"
        assert (
            category_from_relative_path("especializadas/nomina_confidencial/politica.md")
            == "nomina_confidencial"
        )
        assert category_from_relative_path("prestaciones/politica.md") == "prestaciones"

    def test_no_inventa_categoria_para_un_archivo_sin_dominio(self):
        assert category_from_relative_path("especializadas/suelto.md") is None
        assert category_from_relative_path("especializadas/general/politica.md") is None

    def test_ignora_los_readme(self, knowledge_root: Path):
        nombres = [relative for _abs, relative, _cat in iter_knowledge_files(knowledge_root)]
        assert "prestaciones/politica.md" in nombres
        assert not any("README" in n for n in nombres)

    def test_ignora_archivos_sueltos_en_la_raiz(self, knowledge_root: Path):
        (knowledge_root / "suelto.md").write_text("# suelto\n", encoding="utf-8")
        nombres = [relative for _abs, relative, _cat in iter_knowledge_files(knowledge_root)]
        assert "suelto.md" not in nombres

    def test_ignora_extensiones_no_soportadas(self, knowledge_root: Path):
        (knowledge_root / "prestaciones" / "notas.xyz").write_text("x", encoding="utf-8")
        nombres = [relative for _abs, relative, _cat in iter_knowledge_files(knowledge_root)]
        assert not any(n.endswith(".xyz") for n in nombres)

    def test_ignora_carpetas_ocultas(self, knowledge_root: Path):
        oculta = knowledge_root / ".borrador"
        oculta.mkdir()
        (oculta / "x.md").write_text("# x\n", encoding="utf-8")
        categorias = {cat for _abs, _rel, cat in iter_knowledge_files(knowledge_root)}
        assert ".borrador" not in categorias

    def test_la_categoria_es_la_carpeta_de_primer_nivel(self, knowledge_root: Path):
        anidado = knowledge_root / "prestaciones" / "2026"
        anidado.mkdir()
        (anidado / "anexo.md").write_text(f"# Anexo\n{CONTENIDO}\n", encoding="utf-8")
        categorias = {cat for _abs, _rel, cat in iter_knowledge_files(knowledge_root)}
        assert categorias == {"prestaciones"}

    def test_general_es_una_categoria_publica_explicita(self, knowledge_root: Path):
        general = knowledge_root / "general" / "prestaciones"
        general.mkdir(parents=True)
        (general / "guia.md").write_text(f"# Guia\n{CONTENIDO}\n", encoding="utf-8")

        encontrados = {
            relative: category for _abs, relative, category in iter_knowledge_files(knowledge_root)
        }
        assert encontrados["general/prestaciones/guia.md"] == "general"

    def test_especializadas_usa_el_dominio_como_categoria(self, knowledge_root: Path):
        nomina = knowledge_root / "especializadas" / "nomina_confidencial"
        nomina.mkdir(parents=True)
        (nomina / "cierre.md").write_text(f"# Cierre\n{CONTENIDO}\n", encoding="utf-8")

        encontrados = {
            relative: category for _abs, relative, category in iter_knowledge_files(knowledge_root)
        }
        assert encontrados["especializadas/nomina_confidencial/cierre.md"] == "nomina_confidencial"

    def test_un_root_inexistente_no_rompe(self, tmp_path: Path):
        assert list(iter_knowledge_files(tmp_path / "no-existe")) == []


class TestReconciliacion:
    def test_detecta_altas(self, db_session, service, knowledge_root: Path):  # noqa: ARG002
        stats = reconcile_knowledge(db_session, service=service, trigger="test")
        assert stats.scanned_files == 1
        assert stats.new_documents == 1
        assert stats.failures == []

    def test_es_idempotente(self, db_session, service, knowledge_root: Path):  # noqa: ARG002
        reconcile_knowledge(db_session, service=service, trigger="test")
        segunda = reconcile_knowledge(db_session, service=service, trigger="test")
        assert segunda.new_documents == 0
        assert segunda.unchanged_documents == 1
        assert segunda.updated_documents == 0

    def test_detecta_modificaciones_por_sha(self, db_session, service, knowledge_root: Path):
        reconcile_knowledge(db_session, service=service, trigger="test")

        archivo = knowledge_root / "prestaciones" / "politica.md"
        archivo.write_text(f"# Politica\n\n## Seccion\n{CONTENIDO}\nlinea nueva\n", encoding="utf-8")

        stats = reconcile_knowledge(db_session, service=service, trigger="test")
        assert stats.updated_documents == 1
        assert stats.new_documents == 0

    def test_detecta_bajas_y_retira_los_vectores(self, db_session, service, knowledge_root: Path):
        reconcile_knowledge(db_session, service=service, trigger="test")
        (knowledge_root / "prestaciones" / "politica.md").unlink()

        stats = reconcile_knowledge(db_session, service=service, trigger="test")
        assert stats.deleted_documents == 1

        document = db_session.execute(
            select(Document).where(Document.scope == SCOPE_CORPORATE)
        ).scalar_one()
        assert document.deleted_at is not None
        assert document.chunk_count == 0

    def test_detecta_categorias_nuevas(self, db_session, service, knowledge_root: Path):
        reconcile_knowledge(db_session, service=service, trigger="test")

        nueva = knowledge_root / "seguridad_patrimonial"
        nueva.mkdir()
        (nueva / "manual.md").write_text(f"# Manual\n\n## Seccion\n{CONTENIDO}\n", encoding="utf-8")

        stats = reconcile_knowledge(db_session, service=service, trigger="test")
        assert "seguridad_patrimonial" in stats.new_categories

    def test_un_archivo_ilegible_no_aborta_el_trabajo(self, db_session, service, knowledge_root: Path):
        # Un PDF invalido falla al extraerse; el resto debe indexarse igualmente.
        (knowledge_root / "prestaciones" / "roto.pdf").write_bytes(b"%PDF-1.4 basura")

        stats = reconcile_knowledge(db_session, service=service, trigger="test")
        assert stats.scanned_files == 2
        assert len(stats.failures) == 1
        assert stats.new_documents >= 1

    def test_registra_el_trabajo(self, db_session, service, knowledge_root: Path):  # noqa: ARG002
        reconcile_knowledge(db_session, service=service, trigger="prueba_unitaria")
        job = db_session.execute(
            select(IngestionJob).order_by(IngestionJob.started_at.desc()).limit(1)
        ).scalar_one()
        assert job.trigger_source == "prueba_unitaria"
        assert job.status in ("completed", "failed")
        assert job.finished_at is not None
        assert job.stats["scanned_files"] >= 1


class TestLock:
    def test_el_lock_impide_una_segunda_ejecucion(self, db_session):
        def segundo_proceso() -> None:
            with reconcile_lock(db_session, owner="proceso-2"):
                pass  # pragma: no cover - no debe alcanzarse

        # El primer lock debe seguir tomado mientras se intenta el segundo.
        with reconcile_lock(db_session, owner="proceso-1"), pytest.raises(LockNotAcquired):
            segundo_proceso()

    def test_el_lock_se_libera_al_terminar(self, db_session):
        with reconcile_lock(db_session, owner="proceso-1"):
            pass
        assert db_session.get(JobLock, "knowledge_reconcile") is None

    def test_el_lock_se_libera_aunque_falle_el_trabajo(self, db_session):
        with pytest.raises(RuntimeError), reconcile_lock(db_session, owner="proceso-1"):
            raise RuntimeError("fallo simulado")
        assert db_session.get(JobLock, "knowledge_reconcile") is None

    def test_reconcile_with_lock_se_salta_si_ya_hay_una_en_curso(self, db_session, knowledge_root):  # noqa: ARG002
        with reconcile_lock(db_session, owner="otro-proceso"):
            assert reconcile_with_lock(db_session, trigger="test") is None
