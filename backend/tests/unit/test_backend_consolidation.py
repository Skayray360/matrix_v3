# Creado por Aldo Garcia.
"""Contratos preservados al consolidar GitHub 1.1 y la revision 1.2."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine, event, text

from app.agents.orchestrator import Orchestrator
from app.common.answers import GENERAL_HEADING
from app.common.errors import ConfigurationError, EmbeddingDimensionMismatchError, OllamaUnavailableError
from app.config.settings import Settings
from app.database import migrator
from app.database.models import ConversationMessage
from app.llm.ollama_client import ChatResult
from app.llm.provider import ModelClient, vertex_schema
from app.memory.service import MemoryService, authorization_fingerprint
from app.structured_data.schemas import StructuredQueryPlan
from tests.conftest import make_context
from tests.unit import test_implementation_v2

pytestmark = pytest.mark.unit
sql = test_implementation_v2.sql


def test_identity_remains_visible_without_accessing_sources(sql):
    db, _, _ = sql
    memory = MemoryService()
    ctx = make_context(permissions=frozenset(), sources=frozenset())
    conversation = memory.create_conversation(db, ctx)

    class Never:
        def __getattr__(self, name):
            pytest.fail(f"La identidad fija no debe consultar {name}")

    outcome = Orchestrator(
        llm=Never(),
        policy_engine=Never(),
        retriever=Never(),
        structured_tool=Never(),
        memory=memory,
        audit=MagicMock(),
    ).handle_chat(db, ctx=ctx, conversation=conversation, message="¿Quién eres?")
    scope = authorization_fingerprint(db, ctx, frozenset())
    history = [m for m in memory.list_messages(db, conversation.id) if memory.message_visible(m, frozenset(), scope)]
    assert outcome.answer == "Soy Matrix."
    assert [m.content for m in history] == ["¿Quién eres?", "Soy Matrix."]


@pytest.mark.parametrize("content,source_ids", [("Dato restringido", None), ("Soy Matrix RH.", ["private-source"])])
def test_identity_label_does_not_bypass_history_authorization(content, source_ids):
    message = ConversationMessage(
        role="assistant",
        intent="identity",
        content=content,
        source_ids=source_ids,
        authorized_categories=None,
        authorization_scope=None,
    )
    assert not MemoryService.message_visible(message, frozenset(), "current-scope")


def test_general_answer_keeps_behavior_without_claiming_documentary_support(sql):
    db, _, _ = sql
    ctx = make_context(permissions=frozenset(), sources=frozenset())
    memory = MemoryService()
    conversation = memory.create_conversation(db, ctx)
    llm = MagicMock()
    llm.chat.return_value = ChatResult(content="Hola.", model="local-test", latency_ms=1)
    policies = MagicMock()
    policies.effective_categories.return_value = frozenset()
    retriever = MagicMock()
    outcome = Orchestrator(
        llm=llm,
        policy_engine=policies,
        retriever=retriever,
        structured_tool=MagicMock(),
        memory=memory,
        audit=MagicMock(),
    ).handle_chat(db, ctx=ctx, conversation=conversation, message="Hola")
    assert outcome.answer == f"{GENERAL_HEADING}\n\nHola."
    assert outcome.answer_basis == "general"
    assert outcome.grounded is False
    assert outcome.public_sources() == []
    retriever.retrieve.assert_not_called()


def configured_model(monkeypatch, body):
    settings = Settings(_env_file=None, ollama_embedding_dimension=1, rag_embedding_dimension=1)
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)
    return ModelClient(client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))))


@pytest.mark.parametrize(
    "body",
    [
        {"message": {"content": None}},
        {"message": {"content": {"answer": "fragmento"}}},
        {"message": []},
        {"message": {"content": "respuesta parcial"}, "done_reason": "length"},
        {"message": {"content": "respuesta parcial"}, "done": False},
    ],
)
def test_incomplete_or_malformed_ollama_reply_is_rejected(monkeypatch, body):
    client = configured_model(monkeypatch, body)
    with pytest.raises(OllamaUnavailableError):
        client.chat(model=client.settings.ollama_fast_model, messages=[])


@pytest.mark.parametrize("value", [True, "0.5", None])
def test_malformed_ollama_embedding_is_rejected(monkeypatch, value):
    client = configured_model(monkeypatch, {"embeddings": [[value]]})
    with pytest.raises(EmbeddingDimensionMismatchError):
        client.embed(["documento sintetico"])


@pytest.mark.parametrize("value", ["MX", 12, 1.5, True, None, ["MX", 12, False, None]])
def test_actual_query_schema_keeps_filter_types_for_vertex(value):
    schema = StructuredQueryPlan.model_json_schema()
    payload = {
        "source": "rh_demo",
        "entity": "employees",
        "filters": [{"field": "country", "operator": "eq", "value": value}],
    }
    Draft202012Validator(schema).validate(payload)
    parsed = StructuredQueryPlan.model_validate(payload)
    assert parsed.filters[0].value == value
    converted = vertex_schema(schema)
    typed_value = converted["properties"]["filters"]["items"]["properties"]["value"]
    assert typed_value["nullable"] is True
    assert {branch["type"] for branch in typed_value["anyOf"]} == {"STRING", "NUMBER", "BOOLEAN", "ARRAY"}


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_real_query_schema_and_response_survive_adapter_change(monkeypatch, tmp_path, provider):
    path = tmp_path / "models.env"
    path.write_text(
        f"LLM_PROVIDER={provider}\nLLM_LOCAL_ONLY=true\n"
        "OLLAMA_FAST_MODEL=synthetic-planner\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=path)
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)
    client = ModelClient()
    payload = {
        "source": "rh_demo",
        "entity": "employees",
        "filters": [{"field": "country", "operator": "eq", "value": "MX"}],
    }
    answer = json.dumps(payload)
    captured = []

    def post(url, request):
        captured.append(request)
        if provider == "ollama":
            return {"message": {"content": answer}, "done": True, "done_reason": "stop"}
        return {"choices": [{"message": {"content": answer}, "finish_reason": "stop"}]}

    monkeypatch.setattr(client, "_post", post)
    result = client.chat(
        model=settings.ollama_fast_model, messages=[], response_schema=StructuredQueryPlan.model_json_schema()
    )
    assert StructuredQueryPlan.model_validate_json(result.content).filters[0].value == "MX"
    assert captured
    client.close()


@pytest.fixture
def migration_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, checksum TEXT)"))
    monkeypatch.setattr(migrator, "list_tables", lambda _: {"schema_migrations", "users"})
    yield engine
    engine.dispose()


@pytest.mark.parametrize("count", [1, 3, 6])
def test_matrix_migration_history_recognizes_legacy_and_current_databases(migration_db, count):
    with migration_db.begin() as conn:
        for migration in migrator.discover_migrations()[:count]:
            conn.execute(
                text("INSERT INTO schema_migrations VALUES (:v, :c)"), {"v": migration.version, "c": migration.checksum}
            )
    statements = []
    event.listen(migration_db, "before_cursor_execute", lambda conn, cursor, sql, *args: statements.append(sql))
    assert migrator.inspect_ownership(migration_db) == (True, set())
    assert all(statement.startswith("SELECT ") for statement in statements)


@pytest.mark.parametrize(
    "version,checksum", [("0001", "alien-checksum"), ("20200101", "alien-checksum"), ("0003", None)]
)
def test_homonymous_migration_table_does_not_claim_a_foreign_database(migration_db, version, checksum):
    checksum = checksum or next(m.checksum for m in migrator.discover_migrations() if m.version == version)
    with migration_db.begin() as conn:
        conn.execute(text("INSERT INTO schema_migrations VALUES (:v, :c)"), {"v": version, "c": checksum})
    ours, tables = migrator.inspect_ownership(migration_db)
    assert ours is False
    assert "schema_migrations" in tables
    with pytest.raises(ConfigurationError, match="registro de migraciones incompatible"):
        migrator.assert_database_is_ours(migration_db)


def test_database_lookup_matches_underscores_literally(monkeypatch):
    settings = SimpleNamespace(
        database_url=SimpleNamespace(get_secret_value=lambda: "mysql+pymysql://localhost/matrix_rh")
    )
    monkeypatch.setattr(migrator, "get_settings", lambda: settings)
    statements = []
    connection = MagicMock()
    connection.__enter__.return_value = connection

    def execute(statement, params=None):
        statements.append((str(statement), params))
        if params:
            assert "SCHEMA_NAME = :n" in str(statement)
            assert params == {"n": "matrix_rh"}
        return SimpleNamespace(first=lambda: None)

    connection.execute.side_effect = execute
    engine = SimpleNamespace(connect=lambda: connection, dispose=lambda: None)
    monkeypatch.setattr(migrator, "create_engine", lambda *args, **kwargs: engine)
    assert migrator.ensure_database_exists() == ("matrix_rh", True)
    assert statements[1][0].startswith("CREATE DATABASE ")


@pytest.mark.parametrize(
    "provider,body",
    [
        ("openai_compatible", {"choices": ["invalid"]}),
        ("openai_compatible", {"choices": [{"message": ["invalid"], "finish_reason": "stop"}]}),
    ],
)
def test_malformed_adapter_reply_raises_controlled_error(monkeypatch, provider, body):
    settings = Settings(
        _env_file=None, llm_provider=provider, llm_deep_provider=provider, llm_local_only=True,
    )
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)
    client = ModelClient()
    monkeypatch.setattr(client, "_post", lambda *args: body)
    with pytest.raises(OllamaUnavailableError):
        client.chat(model=settings.ollama_fast_model, messages=[])
    client.close()


@pytest.mark.parametrize(
    "entries",
    [
        ["invalid"],
        [{"index": 0, "embedding": None}],
        [{"index": 0, "embedding": [True]}],
        [{"index": False, "embedding": [0.2]}],
    ],
)
def test_compatible_embedding_rejects_invalid_protocol_types(monkeypatch, entries):
    settings = Settings(
        _env_file=None,
        llm_embedding_provider="openai_compatible",
        ollama_embedding_dimension=1,
        rag_embedding_dimension=1,
    )
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)
    client = ModelClient()
    monkeypatch.setattr(client, "_post", lambda *args: {"data": entries})
    with pytest.raises(EmbeddingDimensionMismatchError):
        client.embed(["dato sintetico"])
    client.close()
