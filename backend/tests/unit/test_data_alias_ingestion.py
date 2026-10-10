# Creado por Aldo Garcia.
"""Corpus sintetico multi raiz: alias, idempotencia, bajas y ACL antes de RAG."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.common.errors import ConfigurationError, IngestionFailedError
from app.config.settings import Settings
from app.database.models import Base, Document, DocumentAccessPolicy, DocumentVersion
from app.ingestion.knowledge_layout import inventory_knowledge, load_knowledge_layout
from app.ingestion.loaders import extract_document
from app.ingestion.reconciler import reconcile_knowledge
from app.ingestion.service import IngestionService
from app.rag.retriever import Retriever
from app.rag.schemas import SCOPE_CORPORATE
from app.rag.vector_store import VectorStore
from tests.conftest import make_context
from tests.unit.test_rag_pipeline_isolated import FakeEmbeddingClient

pytestmark = pytest.mark.unit
DECLARED = {"general", "prestaciones", "nomina", "compensaciones", "salud_ambiental", "nomina_confidencial"}
LAYOUT = """version: 1
data_aliases:
  Prestaciones: prestaciones
  Nomina: nomina
  Compensaciones: compensaciones
  Seguridad: salud_ambiental
  Gastos medicos: prestaciones
  ACR: null
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "knowledge-base" / "documents").mkdir(parents=True)
    (tmp_path / "backend" / "config").mkdir(parents=True)
    (tmp_path / "backend" / "config" / "knowledge-layout.yaml").write_text(LAYOUT, encoding="utf-8")
    return tmp_path


def write(root: Path, relative: str, text: str = "# Documento sintetico\n\nInformacion ficticia para prueba.") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def inventory(project: Path, *, knowledge_root: Path | None = None):
    return inventory_knowledge(
        project_root=project, knowledge_root=knowledge_root or project / "knowledge-base" / "documents",
        declared_categories=DECLARED,
    )


def test_alias_windows_accent_and_case_preserves_declared_category(project: Path):
    write(project, "knowledge-base/PRESTACIONES/2026/documento.PDF")
    write(project, "knowledge-base/Nómina/cierre.md")
    write(project, "knowledge-base/Gastos Médicos/seguro.md")
    write(project, "knowledge-base/Seguridad/simulacro.md")
    scan = inventory(project)
    assert {item.relative_path: item.category for item in scan.files} == {
        "data-alias/prestaciones/2026/documento.PDF": "prestaciones",
        "data-alias/nomina/cierre.md": "nomina",
        "data-alias/gastos_medicos/seguro.md": "prestaciones",
        "data-alias/seguridad/simulacro.md": "salud_ambiental",
    }
    assert scan.as_dict()["pdf_files"] == 1
    assert scan.as_dict()["files_by_category"]["prestaciones"] == 2


def test_inventory_combines_original_custom_and_alias_without_duplicates(project: Path):
    write(project, "knowledge-base/documents/prestaciones/antiguo.md")
    write(project, "knowledge-base/Prestaciones/nuevo.md")
    custom = project / "personal"
    write(custom, "general/guia.md")
    scan = inventory(project, knowledge_root=custom)
    assert {item.relative_path for item in scan.files} == {
        "prestaciones/antiguo.md", "data-alias/prestaciones/nuevo.md", "configured-knowledge/general/guia.md",
    }
    assert len(inventory(project, knowledge_root=project / "knowledge-base").files) == 2
    assert len(inventory(project, knowledge_root=project / "knowledge-base" / "documents" / "prestaciones").files) == 2
    alias_as_root = inventory(project, knowledge_root=project / "knowledge-base" / "Prestaciones")
    assert len(alias_as_root.files) == 2
    assert len({source.root for source in alias_as_root.sources}) == len(alias_as_root.sources)


def test_unknown_acr_and_synthetic_data_never_enter_corporate_inventory(project: Path):
    write(project, "knowledge-base/ACR/desconocido.pdf")
    write(project, "knowledge-base/Carpeta privada/registro.pdf")
    write(project, "knowledge-base/synthetic_test_data/nomina/datos.md")
    write(project, "knowledge-base/documents/desconocida/datos.md")
    scan = inventory(project)
    assert scan.files == []
    assert {warning["code"] for warning in scan.warnings} == {
        "unmapped_alias", "unmapped_data_folders", "undeclared_category",
    }
    assert "desconocido.pdf" not in str(scan.as_dict())


def test_private_namespace_cannot_be_selected_as_corporate_root(project: Path):
    private = project / "knowledge-base" / "state" / "uploads"
    write(private, "prestaciones/personal.md")
    scan = inventory(project, knowledge_root=private)
    assert not scan.files
    assert any(warning["code"] == "private_root_rejected" for warning in scan.warnings)


def test_synthetic_fixture_namespace_cannot_be_selected_as_corporate_root(project: Path):
    synthetic = project / "backend" / "tests" / "fixtures"
    write(synthetic, "prestaciones/fixture.md")
    scan = inventory(project, knowledge_root=synthetic)
    assert not scan.files
    assert any(warning["code"] == "test_root_rejected" for warning in scan.warnings)


@pytest.mark.parametrize("namespace", ["state", "models", "unclassified", "backups"])
def test_runtime_and_unclassified_namespaces_cannot_be_selected_as_corporate_root(project: Path, namespace):
    excluded = project / "knowledge-base" / namespace
    write(excluded, "prestaciones/registro.md")
    scan = inventory(project, knowledge_root=excluded)
    assert not scan.files
    assert scan.unavailable_sources


@pytest.mark.parametrize("folder", ["backend", "backend/config", "frontend"])
def test_source_and_configuration_folders_cannot_enter_corporate_inventory(project: Path, folder):
    excluded = project / folder
    write(excluded, "prestaciones/configuracion-interna.md")
    scan = inventory(project, knowledge_root=excluded)
    assert not scan.files
    assert any(warning["code"] == "private_root_rejected" for warning in scan.warnings)


def test_external_configured_root_is_rejected_before_reading_documents(project: Path):
    external = project.parent / f"{project.name}-outside"
    write(external, "prestaciones/ajeno.md")
    scan = inventory(project, knowledge_root=external)
    assert not scan.files
    assert "configured-knowledge" in scan.unavailable_sources
    assert any(warning["code"] == "external_root_rejected" for warning in scan.warnings)


def test_project_wide_root_does_not_ingest_secrets_or_runtime_documents(project: Path):
    write(project, "knowledge-base/documents/prestaciones/permitido.md")
    for excluded in ("state", "models", "backups", "unclassified"):
        write(project, f"knowledge-base/{excluded}/prestaciones/privado.md")
    write(project, "backend/config/prestaciones/interno.md")
    write(project, "frontend/prestaciones/interno.md")
    write(project, ".env", "CLAVE_SINTETICA=fixture")
    scan = inventory(project, knowledge_root=project)
    assert [item.relative_path for item in scan.files] == ["prestaciones/permitido.md"]


def test_readme_unsupported_and_hidden_files_are_not_candidates(project: Path):
    write(project, "knowledge-base/Prestaciones/README.md")
    write(project, "knowledge-base/Prestaciones/notas.doc")
    write(project, "knowledge-base/Prestaciones/.privado.md")
    write(project, "knowledge-base/Prestaciones/.oculto/personal.md")
    scan = inventory(project)
    assert not scan.files
    assert scan.ignored_files == 3


def test_symlink_files_and_folders_cannot_escape_to_private_storage(project: Path):
    private = write(project, "knowledge-base/state/uploads/secret.md")
    alias = project / "knowledge-base" / "Prestaciones"
    alias.mkdir()
    (alias / "enlace.md").symlink_to(private)
    (alias / "enlace-folder").symlink_to(private.parent, target_is_directory=True)
    assert not inventory(project).files


def test_symlink_alias_root_is_rejected(project: Path):
    target = write(project, "knowledge-base/state/uploads/personal.md").parent
    (project / "knowledge-base" / "Prestaciones").symlink_to(target, target_is_directory=True)
    scan = inventory(project)
    assert not scan.files
    assert "data-alias/prestaciones" in scan.unavailable_sources


def test_equivalent_alias_folders_are_ambiguous_on_case_sensitive_host(project: Path):
    write(project, "knowledge-base/Prestaciones/a.md")
    write(project, "knowledge-base/PRESTACIONES/b.md")
    scan = inventory(project)
    assert not scan.files
    assert any(warning["code"] == "ambiguous_alias" for warning in scan.warnings)


@pytest.mark.parametrize("yaml_text", [
    "version: 2\n", "version: 1\ndata_aliases:\n  ../uploads: prestaciones\n",
    "version: 1\ndata_aliases:\n  uploads: prestaciones\n",
    *(f"version: 1\ndata_aliases:\n  {reserved}: prestaciones\n"
      for reserved in ("documents", "state", "models", "unclassified", "backups")),
    "version: 1\ndata_aliases:\n  Nómina: nomina\n  NOMINA: nomina\n",
    "version: 1\ndata_aliases:\n  Prestaciones: prestaciones\n  Prestaciones: nomina\n",
    "!!python/object/apply:builtins.str [inseguro]",
])
def test_invalid_alias_policy_fails_closed(project: Path, yaml_text: str):
    policy = project / "backend" / "config" / "knowledge-layout.yaml"
    policy.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_knowledge_layout(policy)


def test_alias_to_undeclared_category_fails_before_scanning(project: Path):
    policy = project / "backend" / "config" / "knowledge-layout.yaml"
    policy.write_text("version: 1\ndata_aliases:\n  ACR: dominio_inventado\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="no declaradas"):
        inventory(project)


@pytest.fixture
def corpus_environment(project: Path, monkeypatch):
    settings = Settings(_env_file=None, rag_knowledge_root=str(project / "knowledge-base" / "documents"))
    for module in (
        "app.ingestion.service", "app.ingestion.reconciler", "app.ingestion.knowledge_layout",
        "app.rag.index_manifest", "app.rag.retriever", "app.rag.vector_store",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    monkeypatch.setattr("app.ingestion.knowledge_layout.PROJECT_ROOT", project)
    monkeypatch.setattr("app.ingestion.service.extract_document", extract_document)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def scope():
        with factory() as db:
            yield db

    monkeypatch.setattr("app.rag.index_manifest.session_scope", scope)
    store = VectorStore(client=QdrantClient(location=":memory:"))
    llm = FakeEmbeddingClient()
    service = IngestionService(store=store, llm=llm)
    with factory() as db:
        yield SimpleNamespace(db=db, store=store, llm=llm, service=service, settings=settings, project=project)
    store.close()
    engine.dispose()


def test_alias_reconcile_is_idempotent_and_true_file_deletion_retracts_index(corpus_environment):
    env = corpus_environment
    path = write(env.project, "knowledge-base/Prestaciones/vacaciones.md", "# Vacaciones\n\nPolitica sintetica de vacaciones.")
    first = reconcile_knowledge(env.db, service=env.service)
    second = reconcile_knowledge(env.db, service=env.service)
    assert first.new_documents == second.unchanged_documents == 1
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == 1
    path.unlink()
    third = reconcile_knowledge(env.db, service=env.service)
    assert third.deleted_documents == 1
    assert env.db.scalar(select(Document.status)) == "deleted"


def test_missing_root_and_changed_custom_root_preserve_previous_documents(corpus_environment):
    env = corpus_environment
    path = write(env.project, "knowledge-base/Prestaciones/vacaciones.md")
    reconcile_knowledge(env.db, service=env.service)
    path.parent.rename(path.parent.with_name("Prestaciones desconectadas"))
    scan = reconcile_knowledge(env.db, service=env.service)
    assert scan.deleted_documents == 0 and scan.preserved_documents == 1
    original = write(env.project, "personal-a/prestaciones/politica.md")
    env.settings.rag_knowledge_root = str(original.parent.parent)
    reconcile_knowledge(env.db, service=env.service)
    env.settings.rag_knowledge_root = str(env.project / "personal-b")
    (env.project / "personal-b").mkdir()
    changed = reconcile_knowledge(env.db, service=env.service)
    assert changed.deleted_documents == 0


@pytest.mark.parametrize("relocated", [False, True])
def test_legacy_data_alias_adoption_keeps_id_and_version(corpus_environment, relocated: bool):
    env = corpus_environment
    path = write(env.project, "knowledge-base/Prestaciones/vacaciones.md")
    previous = env.service.ingest_corporate_file(
        env.db, absolute_path=path, relative_path="Prestaciones/vacaciones.md", category="prestaciones"
    )
    env.db.commit()
    document = env.db.get(Document, previous.document_id)
    generation = document.active_generation
    if relocated:
        document.storage_path = r"C:\wamp64\www\matrix-rh-1.2.6\data\Prestaciones\vacaciones.md"
        env.db.commit()
    result = reconcile_knowledge(env.db, service=env.service)
    assert result.unchanged_documents == 1 and result.deleted_documents == 0
    assert document.id == previous.document_id
    assert document.active_generation == generation
    assert document.relative_path == "data-alias/prestaciones/vacaciones.md"
    assert env.db.scalar(select(func.count()).select_from(Document)) == 1
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == 1


def test_category_reassignment_republishes_acl_and_does_not_duplicate_document(corpus_environment):
    env = corpus_environment
    path = write(env.project, "knowledge-base/Prestaciones/politica.md")
    first = env.service.ingest_corporate_file(
        env.db, absolute_path=path, relative_path="Prestaciones/politica.md", category="nomina"
    )
    env.db.commit()
    result = reconcile_knowledge(env.db, service=env.service)
    assert not result.failures
    assert env.db.scalar(select(func.count()).select_from(Document)) == 1
    assert env.db.get(Document, first.document_id).category == "prestaciones"
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == 2


def test_policy_change_republishes_metadata_even_when_source_bytes_do_not_change(corpus_environment, monkeypatch):
    from app.authorization.categories import CategoryPolicy

    env = corpus_environment
    write(env.project, "knowledge-base/Prestaciones/politica.md")
    reconcile_knowledge(env.db, service=env.service)
    document = env.db.scalar(select(Document))
    previous_generation = document.active_generation
    previous_digest = document.sha256
    revised = CategoryPolicy(
        name="prestaciones", allowed_groups=["restricted-test"], sensitivity="restricted", source_owner="test-owner",
    )
    monkeypatch.setattr("app.ingestion.service.get_registry", lambda: SimpleNamespace(get=lambda category: revised))
    result = reconcile_knowledge(env.db, service=env.service)
    assert not result.failures and result.updated_documents == 1
    assert document.active_generation != previous_generation
    assert document.sha256 == previous_digest
    policy = env.db.scalar(select(DocumentAccessPolicy))
    assert policy.allowed_groups == ["restricted-test"] and policy.sensitivity == "restricted"
    points, _ = env.store._client.scroll(collection_name=env.settings.rag_collection_corporate, limit=100)
    current = [point.payload for point in points if point.payload["generation"] == document.active_generation]
    assert current and all(payload["allowed_groups"] == ["restricted-test"] for payload in current)
    assert reconcile_knowledge(env.db, service=env.service).unchanged_documents == 1


def test_empty_update_retracts_old_evidence_once_and_is_idempotent(corpus_environment):
    env = corpus_environment
    path = write(env.project, "knowledge-base/Prestaciones/politica.md")
    reconcile_knowledge(env.db, service=env.service)
    document = env.db.scalar(select(Document))
    assert document.active_generation
    path.write_text("", encoding="utf-8")
    changed = reconcile_knowledge(env.db, service=env.service)
    assert changed.empty_documents == 1 and document.active_generation is None
    versions = env.db.scalar(select(func.count()).select_from(DocumentVersion))
    assert versions == 2
    unchanged = reconcile_knowledge(env.db, service=env.service)
    assert unchanged.unchanged_documents == 1
    assert env.db.scalar(select(func.count()).select_from(DocumentVersion)) == versions
    restricted = make_context(categories=frozenset({"prestaciones"}), wildcard=False, permissions=frozenset())
    result = Retriever(store=env.store, llm=env.llm).retrieve(
        question="informacion ficticia", ctx=restricted, authorized_categories=restricted.allowed_categories,
    )
    assert not result.evidences


def test_alias_documents_obey_category_acl_before_retrieval(corpus_environment):
    env = corpus_environment
    write(env.project, "knowledge-base/Prestaciones/beneficios.md", "# Prestaciones\n\nPrestaciones seguro beneficio autorizado.")
    write(env.project, "knowledge-base/Nómina/nomina.md", "# Nomina\n\nPrestaciones seguro salario nomina restringida.")
    reconcile_knowledge(env.db, service=env.service)
    restricted = make_context(categories=frozenset({"prestaciones"}), wildcard=False, permissions=frozenset())
    result = Retriever(store=env.store, llm=env.llm).retrieve(
        question="prestaciones seguro", ctx=restricted, authorized_categories=restricted.allowed_categories,
    )
    assert result.evidences and {item.category for item in result.evidences} == {"prestaciones"}
    assert all(item.scope == SCOPE_CORPORATE for item in result.evidences)


def test_direct_ingestion_rejects_symlink_even_when_inventory_is_bypassed(corpus_environment):
    env = corpus_environment
    target = write(env.project, "knowledge-base/state/uploads/privado.md")
    link = env.project / "knowledge-base" / "enlace.md"
    link.symlink_to(target)
    with pytest.raises(IngestionFailedError, match="enlaces"):
        env.service.ingest_corporate_file(
            env.db, absolute_path=link, relative_path="prestaciones/enlace.md", category="prestaciones"
        )


def test_scanned_pdf_without_text_is_reported_and_not_published(corpus_environment):
    env = corpus_environment
    path = env.project / "knowledge-base" / "Prestaciones" / "escaneado.pdf"
    path.parent.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    result = reconcile_knowledge(env.db, service=env.service)
    assert result.pdf_without_text == result.empty_documents == result.documents_with_warnings == 1
    assert result.chunks_in_scanned_files == 0
    assert env.db.scalar(select(Document.status)) == "empty"
    assert not result.failures


def test_incomplete_directory_walk_cannot_delete_previous_index(corpus_environment, monkeypatch):
    env = corpus_environment
    path = write(env.project, "knowledge-base/Prestaciones/politica.md")
    reconcile_knowledge(env.db, service=env.service)
    path.unlink()

    def denied_walk(_root, *, followlinks=False, onerror=None):
        assert not followlinks
        onerror(PermissionError("fallo sintetico sin rutas"))
        return iter(())

    monkeypatch.setattr("app.ingestion.knowledge_layout.os.walk", denied_walk)
    result = reconcile_knowledge(env.db, service=env.service)
    assert result.deleted_documents == 0 and result.preserved_documents == 1
    assert env.db.scalar(select(Document.status)) == "indexed"


def test_alias_policy_error_does_not_remove_previously_published_documents(corpus_environment):
    env = corpus_environment
    write(env.project, "knowledge-base/Prestaciones/politica.md")
    reconcile_knowledge(env.db, service=env.service)
    (env.project / "backend" / "config" / "knowledge-layout.yaml").write_text(
        "version: 1\ndata_aliases:\n  Prestaciones: desconocida\n", encoding="utf-8"
    )
    with pytest.raises(ConfigurationError):
        reconcile_knowledge(env.db, service=env.service)
    assert env.db.scalar(select(Document.status)) == "indexed"


def test_read_only_preflight_counts_alias_pdfs_without_creating_runtime_or_running_parser(project: Path, monkeypatch):
    from scripts.preflight import PreflightReport, check_paths

    write(project, "knowledge-base/Prestaciones/documento.pdf", "PDF sintetico; el inventario no debe abrirlo.")
    monkeypatch.setattr("app.ingestion.knowledge_layout.PROJECT_ROOT", project)
    settings = SimpleNamespace(knowledge_root_path=project / "knowledge-base" / "documents", upload_storage_path=project / "knowledge-base" / "state" / "uploads")
    report = PreflightReport()
    check_paths(report, settings, read_only=True)
    check = next(check for check in report.checks if check.name == "conocimiento_documental")
    assert check.status == "OK" and "1 archivos indexables; 1 PDF" in check.detail
    assert not (project / "knowledge-base" / "state").exists()
