# Creado por Aldo Garcia.
"""Estado de instalacion: fixtures locales y SQL sintetico, sin servicios."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text

from scripts import installation_state as state

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


def env_path(root):
    path = root / "backend/config/.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def settings(root, **changes):
    values = {
        "qdrant_mode": "embedded", "qdrant_storage_path": root / "knowledge-base/state/qdrant",
        "upload_storage_path": root / "knowledge-base/state/uploads", "knowledge_root_path": root / "knowledge-base/documents",
        "rag_collection_corporate": "matrix_rh_corporate", "rag_collection_private": "matrix_rh_private",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def document(**changes):
    values = {
        "scope": "corporate", "relative_path": "prestaciones/documento.md", "storage_path": None,
        "owner_user_id": None, "conversation_id": None, "deleted": False,
        "chunk_count": 8, "has_active_generation": True, "cleanup_pending": True,
    }
    values.update(changes)
    return values


def metadata(root, *, corporate=True, private=False, storage=True):
    root.mkdir(parents=True)
    names = [name for enabled, name in (
        (corporate, "matrix_rh_corporate"), (private, "matrix_rh_private"),
    ) if enabled]
    (root / "meta.json").write_text(json.dumps({"collections": dict.fromkeys(names, {}), "aliases": {}}))
    if storage:
        for name in names:
            target = root / "collection" / name / "storage.sqlite"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"synthetic metadata-only storage fixture")


def corpus(root):
    path = root / "knowledge-base/documents/prestaciones/documento.md"
    path.parent.mkdir(parents=True)
    path.write_text("CONTENIDO_PRIVADO_NO_LEER", encoding="utf-8")
    return path


def codes(report):
    return {issue["code"] for issue in report["issues"]}


def test_reintento_con_env_existente_y_sql_publicado_bloquea_meta_vacio(tmp_path, monkeypatch, capsys):
    config = settings(tmp_path)
    env_path(tmp_path).write_text("SECRET=NO_PUBLICAR_CREDENCIAL", encoding="utf-8")
    metadata(config.qdrant_storage_path, corporate=False)
    monkeypatch.setattr(state, "load_target_settings", lambda root: config)
    monkeypatch.setattr(state, "database_snapshot", lambda *args: ("matrix", [document()]))
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert state.main(["--root", str(tmp_path)]) == 0
    normal = json.loads(capsys.readouterr().out)
    assert state.main(["--root", str(tmp_path), "--guard-install"]) == 1
    guard_output = capsys.readouterr().out
    guard = json.loads(guard_output)
    assert normal == guard
    assert codes(guard) == {"qdrant_collection_missing", "corporate_files_missing"}
    assert guard["sql_documents"][0]["cleanup_pending_active_generation"] == 1
    assert not guard["guard_allowed"]
    assert "detener.bat" in guard["next_action"]
    assert "installation_backup.py" in guard["next_action"]
    assert "sin adoptar bases ni estado de otras carpetas" in guard["next_action"]
    assert "NO_PUBLICAR" not in guard_output
    assert "documento.md" not in guard_output
    assert str(tmp_path) not in guard_output
    assert before == sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))


@pytest.mark.parametrize("database_state", ["absent", "empty", "matrix"])
def test_instalacion_sin_referencias_no_necesita_colecciones_ni_crea_rutas(tmp_path, database_state):
    report = state.evaluate_state(settings(tmp_path), tmp_path, database_state, [])
    assert report["issues"] == []
    assert report["qdrant"]["status"] == "missing"
    assert not list(tmp_path.iterdir())


def test_continuacion_con_metadata_y_corpus_solo_declara_presencia(tmp_path, monkeypatch):
    config = settings(tmp_path)
    private_document = corpus(tmp_path)
    metadata(config.qdrant_storage_path)
    original_open = Path.open

    def protected_open(path, *args, **kwargs):
        assert path != private_document, "El inventario no debe leer el corpus."
        assert path.name != "storage.sqlite", "No se debe abrir Qdrant ni su SQLite."
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", protected_open)
    report = state.evaluate_state(config, tmp_path, "matrix", [document()])
    assert report["issues"] == []
    assert report["corporate_files"]["present"] == 1
    assert report["vectors_verified"] is False
    assert report["qdrant_opened"] is False
    assert report["document_contents_read"] is False
    assert not report["qdrant"]["collections"]["conversation"]["metadata_present"]


@pytest.mark.parametrize("legacy", [False, True])
def test_inventario_corporativo_conserva_alias_canonicos_y_anteriores(tmp_path, legacy):
    config = settings(tmp_path, qdrant_mode="server")
    target = tmp_path / "knowledge-base/beneficios/documento.md"
    target.parent.mkdir(parents=True)
    target.write_text("DOCUMENTO_PRIVADO", encoding="utf-8")
    (tmp_path / "backend/config").mkdir(parents=True)
    (tmp_path / "backend/config/knowledge-layout.yaml").write_text(
        "version: 1\ndata_aliases:\n  beneficios: prestaciones\n", encoding="utf-8"
    )
    relative = "beneficios/documento.md" if legacy else "data-alias/beneficios/documento.md"
    report = state.evaluate_state(config, tmp_path, "matrix", [document(
        relative_path=relative, storage_path=r"C:\old\data\beneficios\documento.md" if legacy else None,
    )])
    assert report["issues"] == []
    assert report["corporate_files"]["present"] == 1
    assert report["qdrant"]["status"] == "server_not_verified"


@pytest.mark.parametrize("storage", [None, r"C:\old\data\knowledge\prestaciones\documento.md"])
def test_alias_no_suplanta_documento_ausente_de_knowledge(tmp_path, storage):
    config = settings(tmp_path, qdrant_mode="server")
    target = tmp_path / "knowledge-base/prestaciones/documento.md"
    target.parent.mkdir(parents=True)
    target.write_text("OTRO_DOCUMENTO", encoding="utf-8")
    (tmp_path / "backend/config").mkdir(parents=True)
    (tmp_path / "backend/config/knowledge-layout.yaml").write_text(
        "version: 1\ndata_aliases:\n  prestaciones: prestaciones\n", encoding="utf-8"
    )
    report = state.evaluate_state(config, tmp_path, "matrix", [document(storage_path=storage)])
    assert codes(report) == {"corporate_files_missing"}
    assert report["corporate_files"]["present"] == 0


def test_restauracion_en_rutas_externas_remapea_adjunto_sin_leer_bytes(tmp_path, monkeypatch):
    root = tmp_path / "new-project"
    root.mkdir()
    config = settings(root, upload_storage_path=tmp_path / "external-uploads", qdrant_storage_path=tmp_path / "external-index")
    metadata(config.qdrant_storage_path, corporate=False, private=True)
    upload = config.upload_storage_path / "owner-fixture/conversation-fixture/internal.pdf"
    upload.parent.mkdir(parents=True)
    upload.write_text("CONTENIDO_PRIVADO_ADJUNTO", encoding="utf-8")
    row = document(
        scope="conversation", owner_user_id="owner-fixture", conversation_id="conversation-fixture",
        storage_path=r"C:\old-project\var\uploads\owner-fixture\conversation-fixture\internal.pdf",
    )
    original_open = Path.open

    def protected_open(path, *args, **kwargs):
        assert path != upload
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", protected_open)
    report = state.evaluate_state(config, root, "matrix", [row])
    assert report["issues"] == []
    assert report["attachments"] == {"referenced": 1, "present": 1, "missing": 0, "invalid": 0}
    assert "owner-fixture" not in json.dumps(report)
    assert "internal.pdf" not in json.dumps(report)


@pytest.mark.parametrize(("owner", "code"), [("valid", "attachment_files_missing"), ("..", "attachment_paths_invalid")])
def test_adjunto_ausente_o_namespace_invalido_bloquea_sin_ids(tmp_path, owner, code):
    row = document(scope="conversation", owner_user_id=owner, conversation_id="private-conversation", storage_path="file.pdf")
    report = state.evaluate_state(settings(tmp_path, qdrant_mode="server"), tmp_path, "matrix", [row])
    assert codes(report) == {code}
    assert "private-conversation" not in json.dumps(report)


def test_meta_sin_archivo_de_coleccion_no_equivale_a_estado_trasladado(tmp_path):
    config = settings(tmp_path)
    metadata(config.qdrant_storage_path, storage=False)
    corpus(tmp_path)
    report = state.evaluate_state(config, tmp_path, "matrix", [document()])
    assert codes(report) == {"qdrant_storage_missing"}


@pytest.mark.parametrize("payload", ["PRIVADO invalid json", '{"collections": []}', "[1, 2]", "x" * (1024 * 1024 + 1)])
def test_meta_invalido_acotado_no_filtra_entrada(tmp_path, payload):
    meta = tmp_path / "meta.json"
    meta.write_text(payload)
    report = state.qdrant_metadata(tmp_path, {"corporate": "fixture"})
    assert report["status"] == "unreadable"
    assert "PRIVADO" not in json.dumps(report)


def test_metadatos_y_adjuntos_no_siguen_enlaces(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "meta.json").write_text('{"collections": {}, "aliases": {}}')
    local = tmp_path / "local"
    local.mkdir()
    try:
        (local / "meta.json").symlink_to(external / "meta.json")
        (local / "owner").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("El sistema no permite crear enlaces en el fixture.")
    assert state.qdrant_metadata(local, {})["status"] == "unreadable"
    (external / "conversation").mkdir()
    (external / "conversation/file.pdf").write_text("PRIVADO")
    row = document(scope="conversation", owner_user_id="owner", conversation_id="conversation", storage_path="file.pdf")
    assert state.attachment_inventory([row], local)["missing"] == 1


def test_carpeta_previa_no_se_asume_disponible_y_no_abre_env(tmp_path, monkeypatch):
    absent = state.previous_inventory(tmp_path / "missing")
    assert not absent["directory_present"] and not absent["configuration_present"]
    previous = tmp_path / "previous"
    previous.mkdir()
    env_path(previous).write_text("SECRET_NO_LEER", encoding="utf-8")
    original_open = Path.open

    def protected_open(path, *args, **kwargs):
        assert path.name != ".env"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", protected_open)
    report = state.previous_inventory(previous)
    assert report["configuration_present"]
    assert report["standard_locations_only"]
    assert "SECRET" not in json.dumps(report)


@pytest.mark.parametrize("current_schema", [False, True])
def test_sql_lectura_compatible_con_esquema_anterior_a_migrar(current_schema):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.connect() as conn:
            extra = ", active_generation TEXT, index_cleanup_pending INTEGER" if current_schema else ""
            conn.execute(text(
                "CREATE TABLE documents (scope TEXT, relative_path TEXT, storage_path TEXT, "
                "owner_user_id TEXT, conversation_id TEXT, deleted_at TEXT, chunk_count INTEGER" + extra + ")"
            ))
            values = ", 'private-generation', 1" if current_schema else ""
            conn.execute(text("INSERT INTO documents VALUES ('corporate', 'fixture.md', NULL, NULL, NULL, NULL, 12" + values + ")"))
            if current_schema:
                conn.execute(text(
                    "INSERT INTO documents VALUES ('corporate', 'empty.md', NULL, NULL, NULL, NULL, 0, '', 1)"
                ))
            conn.commit()
            columns = {"scope", "relative_path", "storage_path", "owner_user_id", "conversation_id", "deleted_at", "chunk_count"}
            if current_schema:
                columns |= {"active_generation", "index_cleanup_pending"}
            event.listen(conn, "before_cursor_execute", state.select_only_guard)
            rows = state.read_document_metadata(conn, table="documents", columns=columns)
            assert len(rows) == (2 if current_schema else 1) and rows[0]["chunk_count"] == 12
            assert bool(rows[0]["has_active_generation"]) is current_schema
            assert bool(rows[0]["cleanup_pending"]) is current_schema
            if current_schema:
                assert not rows[1]["has_active_generation"]
            assert "private-generation" not in json.dumps(rows)
            with pytest.raises(state.StateCheckError):
                conn.execute(text("UPDATE documents SET chunk_count = 0"))
    finally:
        engine.dispose()


@pytest.mark.parametrize("statement", ["UPDATE documents SET x=1", "DELETE FROM documents", "CREATE TABLE bad(x INT)", "INSERT INTO documents VALUES(1)", "SHOW TABLES"])
def test_guardia_sql_rechaza_instrucciones_fuera_de_select(statement):
    with pytest.raises(state.StateCheckError):
        state.select_only_guard(None, None, statement, None, None, False)


@pytest.mark.parametrize("database_kind", ["matrix", "foreign", "absent", "empty"])
def test_snapshot_verifica_propiedad_solo_con_select_y_cierra_motor(tmp_path, monkeypatch, database_kind):
    import sqlalchemy
    from pydantic import SecretStr

    executed = []
    closed = []
    watched = []

    class Result:
        def __init__(self, values):
            self.values = values

        def first(self):
            return self.values[0] if self.values else None

        def all(self):
            return self.values

        def scalars(self):
            return [row[0] for row in self.values]

        def mappings(self):
            return self.values

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append("connection")

        def execute(self, query, parameters=None):
            statement = str(query)
            state.select_only_guard(None, None, statement, parameters, None, False)
            executed.append(statement)
            if "INFORMATION_SCHEMA.SCHEMATA" in statement:
                return Result([] if database_kind == "absent" else [("fixture",)])
            if "INFORMATION_SCHEMA.TABLES" in statement:
                return Result([] if database_kind == "empty" else [("schema_migrations",), ("documents",)])
            if "schema_migrations`" in statement:
                return Result([("0001", "fixture-checksum" if database_kind == "matrix" else "foreign-checksum")])
            if "INFORMATION_SCHEMA.COLUMNS" in statement:
                return Result([(column,) for column in (
                    "scope", "relative_path", "storage_path", "owner_user_id", "conversation_id",
                    "deleted_at", "chunk_count", "active_generation", "index_cleanup_pending",
                )])
            return Result([document()])

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            closed.append("engine")

    def make_engine(url, **kwargs):
        assert not url.database
        assert kwargs["hide_parameters"] and not kwargs["echo"]
        return Engine()

    monkeypatch.setattr(sqlalchemy, "create_engine", make_engine)
    monkeypatch.setattr(sqlalchemy.event, "listen", lambda *args: watched.append(args[1]))
    monkeypatch.setattr(state, "_migration_checksums", lambda root: {"0001": "fixture-checksum"})
    config = settings(tmp_path, database_url=SecretStr("mysql+pymysql://fixture:replace_me@127.0.0.1:1/matrix_fixture"))
    if database_kind == "foreign":
        with pytest.raises(state.StateCheckError, match="database_not_matrix"):
            state.database_snapshot(config, tmp_path)
        assert not any("FROM `matrix_fixture`.`documents`" in query for query in executed)
    else:
        kind, rows = state.database_snapshot(config, tmp_path)
        assert kind == database_kind
        assert len(rows) == (1 if database_kind == "matrix" else 0)
    assert watched == ["before_cursor_execute"]
    assert closed == ["connection", "engine"]
    assert all(query.startswith("SELECT ") for query in executed)
    assert not any("password" in query or "error_message" in query or "sha256" in query for query in executed)


def test_error_driver_o_configuracion_no_filtra_excepcion(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state, "load_target_settings", lambda root: settings(root))

    def fail(*args):
        raise RuntimeError("mysql://PRIVATE_PASSWORD@host/private_db DOCUMENTO_PRIVADO")

    monkeypatch.setattr(state, "database_snapshot", fail)
    assert state.main(["--root", str(tmp_path), "--guard-install"]) == 1
    output = capsys.readouterr().out
    assert codes(json.loads(output)) == {"database_unavailable"}
    assert "PRIVATE_PASSWORD" not in output and "DOCUMENTO_PRIVADO" not in output


def test_root_diferente_a_app_cargada_se_rechaza_sin_reimportar(tmp_path):
    target = tmp_path / "backend/app/config"
    target.mkdir(parents=True)
    (target / "settings.py").write_text("raise AssertionError('no ejecutar')")
    env_path(tmp_path).write_text("fixture")
    app_before = sys.modules["app"]
    with pytest.raises(state.StateCheckError, match="wrong_configuration_origin"):
        state.load_target_settings(tmp_path)
    assert sys.modules["app"] is app_before


def test_script_en_zip_nuevo_importa_settings_de_root_objetivo(tmp_path):
    root = tmp_path / "old target with spaces"
    config = root / "backend/app/config"
    config.mkdir(parents=True)
    (config.parent / "__init__.py").write_text("")
    (config / "__init__.py").write_text("from .settings import get_settings\n")
    (config / "settings.py").write_text("def get_settings():\n    return 'target-setting'\n")
    env_path(root).write_text("SECRET_NO_LEER")
    script = ROOT / "backend/scripts/installation_state.py"
    source = (
        "import json,runpy,sys; from pathlib import Path; "
        "state=runpy.run_path(sys.argv[1]); root=Path(sys.argv[2]); "
        "value=state['load_target_settings'](root); "
        "print(json.dumps({'target_settings':value=='target-setting', "
        "'origin_correct':Path(sys.modules['app.config.settings'].__file__).is_relative_to(root)}))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", source, str(script), str(root)],
        capture_output=True, text=True, check=False, timeout=20, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"target_settings": True, "origin_correct": True}
    assert not list(root.rglob("__pycache__"))


def test_installer_guardia_precede_todas_las_escrituras_y_no_depende_skip_ingest():
    source = (ROOT / "backend/scripts/windows/MatrixRH.ps1").read_text(encoding="utf-8")
    install = source.split("function Install-Stack {", 1)[1].split("function Stop-Stack {", 1)[0]
    guard = install.index("'scripts.installation_state'")
    assert install.index("Start-MySql") < guard
    for operation in ("'migrate'", "'scripts.local_identity'", "'ingest'"):
        assert guard < install.index(operation)
    guarded_line = install[:guard].rsplit("\n", 1)[1] + install[guard:].split("\n", 1)[0]
    assert "Invoke-Python" in guarded_line and "'--guard-install'" in guarded_line
    assert "$SkipIngest" not in install
    python = source.split("function Invoke-Python(", 1)[1].split("\nfunction ", 1)[0]
    assert "Invoke-Checked" in python
