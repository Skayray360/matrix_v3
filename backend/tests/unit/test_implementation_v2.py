# Creado por Aldo Garcia.
"""Regresiones de la auditoria v2: datos sinteticos y transportes controlados."""

from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO

import httpx
import pytest
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.common.answers import GENERAL_HEADING
from app.common.errors import ConfigurationError, FileTooLargeError, OllamaUnavailableError
from app.common.ids import new_id, utcnow_naive
from app.config.settings import Settings
from app.database.models import Base, Conversation, ConversationMessage, ConversationSummary, Document
from app.llm.provider import ModelClient
from app.memory.service import MemoryService
from app.rag.grounding import verify_grounding
from app.rag.index_manifest import indexing_fingerprint
from app.rag.schemas import Evidence
from app.rag.vector_store import VectorStore
from app.security.admission import admission, snapshot
from app.security.upload_guard import read_bounded
from tests.conftest import make_context
from tests.unit.test_rag_pipeline_isolated import make_chunk

pytestmark = pytest.mark.unit


def configure(monkeypatch, path):
    def current():
        return Settings(_env_file=path)

    monkeypatch.setattr("app.llm.provider.get_settings", current)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", current)
    return current


@pytest.fixture
def sql(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", pool_size=1, max_overflow=0)
    Base.metadata.create_all(engine)
    # SQLite no implementa AUTO_INCREMENT en una columna no-PK como MySQL.
    from itertools import count

    from sqlalchemy import event

    sequence = count(1)

    def assign_seq(mapper, connection, target):
        if target.seq is None:
            target.seq = next(sequence)

    event.listen(ConversationMessage, "before_insert", assign_seq)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        yield db, factory, engine
    event.remove(ConversationMessage, "before_insert", assign_seq)
    engine.dispose()


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["answer", "count"],
    "additionalProperties": False,
}


def test_model_and_runtime_change_only_env(monkeypatch, tmp_path, manifest_db):
    from app.rag.retriever import Retriever

    path = tmp_path / "models.env"
    path.write_text("OLLAMA_FAST_MODEL=gemma-test\n")
    current = configure(monkeypatch, path)
    requests = []
    vector = [1.0] + [0.0] * 767
    store = VectorStore(client=QdrantClient(location=":memory:"))
    store.upsert_chunks([make_chunk("La politica concede 12 dias de vacaciones.", category="prestaciones")], [vector])
    ctx = make_context(categories=frozenset({"prestaciones"}), wildcard=False)

    def handle(request):
        import json

        payload = json.loads(request.content) if request.content else {}
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "embeddinggemma:latest", "digest": "unverified"}]})
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [vector for _ in payload["input"]]})
        requests.append((request.url.path, payload))
        if request.url.path == "/api/chat":
            assert payload["format"] == SCHEMA
            return httpx.Response(200, json={"message": {"content": '{"answer":"ok","count":12}'}})
        if request.url.path == "/v1/chat/completions":
            assert payload["response_format"]["json_schema"]["schema"] == SCHEMA
            return httpx.Response(
                200, json={"choices": [{"message": {"content": '{"answer":"ok","count":12}'}, "finish_reason": "stop"}]}
            )
        raise AssertionError(request.url)

    def execute_same_business_call():
        client = ModelClient(client=httpx.Client(transport=httpx.MockTransport(handle)))
        retrieval = Retriever(store=store, llm=client).retrieve(
            ctx=ctx, question="vacaciones", authorized_categories=frozenset({"prestaciones"}), include_private=False
        )
        assert retrieval.evidences
        result = client.chat(
            model=current().ollama_fast_model,
            messages=[{"role": "user", "content": retrieval.evidences[0].text}],
            response_schema=SCHEMA,
        )
        return result.content, tuple(e.source_id for e in retrieval.evidences)

    original = execute_same_business_call()
    # Solo se edita este archivo; RAG, argumentos de negocio y schema son los mismos.
    path.write_text(
        "LLM_PROVIDER=openai_compatible\nOLLAMA_FAST_MODEL=llama-test\n" "LLM_API_BASE_URL=http://localhost:8080/v1\n"
    )
    changed = execute_same_business_call()
    assert original == changed
    assert [payload["model"] for _, payload in requests] == ["gemma-test", "llama-test"]
    assert [url for url, _ in requests] == ["/api/chat", "/v1/chat/completions"]


def test_cloud_rejected_before_network(monkeypatch, tmp_path):
    path = tmp_path / "models.env"
    path.write_text("LLM_DEEP_PROVIDER=vertex\nOLLAMA_DEEP_MODEL=synthetic-cloud-model\n")
    configure(monkeypatch, path)
    with pytest.raises(ConfigurationError, match="LLM_LOCAL_ONLY"):
        ModelClient(client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("red no autorizada"))))


@pytest.mark.parametrize("content", ['{"answer":"ok","count":"12"}', '{"answer":"ok"}', "no es json"])
def test_schema_violation_never_enters_business(monkeypatch, tmp_path, content):
    path = tmp_path / "models.env"
    path.write_text("OLLAMA_FAST_MODEL=model-test\n")
    configure(monkeypatch, path)
    client = ModelClient(
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"message": {"content": content}}))
        )
    )
    with pytest.raises(OllamaUnavailableError, match="schema"):
        client.chat(model="model-test", messages=[], response_schema=SCHEMA)


def test_explicit_cloud_opt_in_is_rejected_before_any_client_or_credentials(monkeypatch, tmp_path):
    path = tmp_path / "models.env"
    path.write_text(
        "LLM_LOCAL_ONLY=false\nLLM_DEEP_PROVIDER=vertex\nOLLAMA_DEEP_MODEL=gemini-test\n"
        "LLM_VERTEX_BASE_URL=https://vertex.example.test/models\n"
    )
    configure(monkeypatch, path)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("No crear cliente cloud"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: pytest.fail("No crear cliente cloud"))
    with pytest.raises(ConfigurationError, match="todos los entornos"):
        ModelClient()


def test_embedding_runtime_shape_validation(monkeypatch, tmp_path):
    path = tmp_path / "models.env"
    path.write_text(
        "LLM_EMBEDDING_PROVIDER=openai_compatible\nOLLAMA_EMBEDDING_DIMENSION=2\nRAG_EMBEDDING_DIMENSION=2\n"
    )
    configure(monkeypatch, path)
    client = ModelClient(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]}
                )
            )
        )
    )
    assert client.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]


def test_busy_server_one_attempt(monkeypatch, tmp_path):
    from app.agents.knowledge_agent import KnowledgeAgent
    from app.llm.model_policy import Intent, ModelPolicy

    path = tmp_path / "models.env"
    path.write_text("")
    configure(monkeypatch, path)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503)

    agent = KnowledgeAgent(
        llm=ModelClient(client=httpx.Client(transport=httpx.MockTransport(handle))), policy=ModelPolicy()
    )
    with pytest.raises(OllamaUnavailableError):
        agent._chat_with_fallback(
            model_name="gemma4:latest", fallback_model_name="qwen3.6:latest", messages=[], intent=Intent.GENERAL
        )
    assert len(calls) == 1


def test_admission_limit_releases_on_failure():
    with pytest.raises(RuntimeError), admission("test-v2", 1):
        with pytest.raises(OllamaUnavailableError), admission("test-v2", 1):
            pass
        raise RuntimeError("synthetic failure")
    assert "test-v2" not in snapshot()


def test_upload_bounded_without_content_length():
    with pytest.raises(FileTooLargeError):
        read_bounded(BytesIO(b"a" * 4097), max_bytes=4096)
    assert read_bounded(BytesIO(b"a" * 4096), max_bytes=4096) == b"a" * 4096


def test_revoked_summary_and_legacy_messages_not_visible(sql):
    db, _, _ = sql
    ctx = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
    memory = MemoryService()
    conv = memory.create_conversation(db, ctx)
    db.add(
        ConversationMessage(
            id=new_id(),
            seq=1,
            conversation_id=conv.id,
            user_id=ctx.user_id,
            role="assistant",
            content="dato revocado",
            authorized_categories=["nomina"],
            source_ids=["nomina/x#0"],
        )
    )
    db.add(ConversationSummary(id=new_id(), conversation_id=conv.id, summary="dato revocado", message_count=1))
    db.flush()
    context = memory.build_context(db, ctx, conv, authorized_categories=frozenset({"prestaciones"}))
    assert context.summary == ""
    assert context.turns == ()
    assert context.dropped_turns == 1


def test_history_window_and_cursor_not_first_200(sql):
    db, _, _ = sql
    memory = MemoryService()
    ctx = make_context()
    conv = memory.create_conversation(db, ctx)
    for seq in range(1, 214):
        db.add(
            ConversationMessage(
                id=new_id(),
                seq=seq,
                conversation_id=conv.id,
                user_id=ctx.user_id,
                role="user",
                content=f"mensaje-{seq}",
            )
        )
    db.flush()
    recent = memory.list_messages(db, conv.id)
    assert recent[0].seq == 14 and recent[-1].seq == 213
    older = memory.list_messages(db, conv.id, before_seq=recent[0].seq)
    assert [m.seq for m in older] == list(range(1, 14))
    memory.store_summary(db, conv.id, summary="resumen", message_count=213)
    assert memory.needs_summary(db, conv.id) is False


def test_valid_citation_cannot_support_false_number():
    evidence = Evidence(
        source_id="x/a#0",
        text="Corresponden 12 dias de vacaciones.",
        score=1,
        category="x",
        filename="a",
        section="",
        page_or_sheet="",
        document_id="d",
        chunk_id="c",
    )
    bad = verify_grounding("Corresponden 900 dias de vacaciones [[x/a#0]].", (evidence,))
    assert bad.grounded is False and bad.citations_valid is True
    valid = verify_grounding("Corresponden 12 dias de vacaciones [[x/a#0]].", (evidence,))
    assert valid.extractive_verified is True
    assert valid.factual_verified is False
    assert not verify_grounding("No hay evidencia. Corresponden 900 dias.", (evidence,)).grounded


def test_structured_only_source_accepted():
    from app.structured_data.tool import StructuredEvidence

    e = StructuredEvidence(
        source_id="sql/rh#1",
        source="rh",
        entity="count",
        columns=("empleados",),
        rows=((12,),),
        row_count=1,
        truncated=False,
    )
    report = verify_grounding("empleados: 12 [[sql/rh#1]].", (), structured=(e,))
    assert report.grounded and not report.invalid_source_ids


def test_generation_not_visible_before_manifest_commit(sql, monkeypatch):
    db, factory, _ = sql

    @contextmanager
    def scope():
        with factory() as session:
            yield session

    monkeypatch.setattr("app.rag.index_manifest.session_scope", scope)
    store = VectorStore(client=QdrantClient(location=":memory:"))
    old = make_chunk("version anterior", category="prestaciones", document_id="doc")
    old = replace(old, metadata=replace(old.metadata, generation="old", index_fingerprint=indexing_fingerprint()))
    store.upsert_chunks([old], [[1.0] + [0.0] * 767])
    db.add(
        Document(
            id="doc",
            scope="corporate",
            category="prestaciones",
            filename="politica.md",
            mime_type="text/markdown",
            sha256="a" * 64,
            status="indexed",
            active_generation="old",
            index_fingerprint=indexing_fingerprint(),
        )
    )
    db.commit()
    new = replace(old, text="version incompleta", metadata=replace(old.metadata, chunk_id=new_id(), generation="new"))
    store.upsert_chunks([new], [[1.0] + [0.0] * 767])
    # Fallo simulado antes del commit de activacion: los puntos nuevos existen,
    # pero la consulta debe conservar exclusivamente la version anterior.
    hits = store.search(
        collection=store.collection_for("corporate"),
        query_vector=[1.0] + [0.0] * 767,
        query_filter=store.build_corporate_filter({"prestaciones"}),
        limit=5,
    )
    assert [hit.payload["text"] for hit in hits] == ["version anterior"]
    store.close()


def test_fingerprint_changes_with_embedding_revision(monkeypatch):
    monkeypatch.setattr(
        "app.rag.index_manifest.get_settings", lambda: Settings(_env_file=None, llm_embedding_revision="v1")
    )
    first = indexing_fingerprint()
    monkeypatch.setattr(
        "app.rag.index_manifest.get_settings", lambda: Settings(_env_file=None, llm_embedding_revision="v2")
    )
    assert indexing_fingerprint() != first


def test_extraction_runs_isolated_and_returns_document():
    from app.ingestion.isolated_extraction import extract_document

    result = extract_document(b"Contenido sintetico de prueba", filename="prueba.txt")
    assert "Contenido sintetico" in result.plain_text()


def test_oidc_callback_bound_to_browser(monkeypatch):
    from starlette.requests import Request
    from starlette.responses import Response

    from app.api.routes.auth import auth_callback
    from app.common.errors import UnauthorizedError
    from app.config import AuthProvider, get_settings

    monkeypatch.setattr(get_settings(), "auth_provider", AuthProvider.ENTRA)
    request = Request({"type": "http", "headers": [(b"cookie", b"matrixrh_oidc_state=browser-A")]})
    monkeypatch.setattr("app.api.routes.auth.get_identity_provider", lambda: pytest.fail("callback sin correlacion"))
    with pytest.raises(UnauthorizedError, match="navegador"):
        auth_callback(request, Response(), code="synthetic", state="browser-B", db=None)


def test_inactive_entra_never_constructed(monkeypatch):
    from app.auth.provider import get_identity_provider
    from app.config import AuthProvider, get_settings

    monkeypatch.setattr(get_settings(), "auth_provider", AuthProvider.LOCAL_TEST)
    monkeypatch.setattr("app.auth.entra_provider.EntraIdentityProvider", lambda: pytest.fail("AD inactivo"))
    assert get_identity_provider().name == "local_test"


def test_oidc_metadata_and_keys_not_login_claim(monkeypatch):
    from app.auth.entra_provider import EntraIdentityProvider
    from app.auth.provider import IntegrationStatus
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "entra_tenant_id", "tenant-test")
    monkeypatch.setattr(settings, "entra_client_id", "client-test")
    monkeypatch.setattr(settings, "entra_redirect_uri", "https://matrix.example.test/api/v1/auth/callback")
    provider = EntraIdentityProvider()
    metadata = {
        "issuer": provider.issuer,
        "authorization_endpoint": provider.authorization_endpoint,
        "token_endpoint": provider.token_endpoint,
        "jwks_uri": provider.jwks_uri,
    }
    provider._http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"keys": [{"kty": "RSA", "kid": "synthetic"}]} if request.url.path.endswith("/keys") else metadata,
            )
        )
    )
    assert provider.status() is IntegrationStatus.ENDPOINTS_REACHABLE


def test_sql_connection_released_during_generation(sql):
    from unittest.mock import MagicMock

    from app.agents.orchestrator import Orchestrator
    from app.llm.ollama_client import ChatResult

    db, factory, engine = sql
    ctx = make_context(permissions=frozenset(), sources=frozenset())
    memory = MemoryService()
    conversation = memory.create_conversation(db, ctx)
    db.commit()
    observations = []

    class Model:
        def chat(self, **kwargs):
            observations.append(engine.pool.checkedout())
            with factory() as separate:
                separate.execute(select(Conversation.id)).all()
            return ChatResult(content="Hola.", model=kwargs["model"], latency_ms=1)

    policies = MagicMock()
    policies.effective_categories.return_value = frozenset()
    orchestrator = Orchestrator(
        llm=Model(),
        memory=memory,
        policy_engine=policies,
        audit=MagicMock(),
        structured_tool=MagicMock(),
        retriever=MagicMock(),
    )
    result = orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message="Hola")
    assert result.answer == f"{GENERAL_HEADING}\n\nHola."
    assert result.answer_basis == "general"
    assert observations == [0]


@pytest.mark.asyncio
async def test_multipart_rejected_before_parser_without_content_length(monkeypatch):
    from app.api.body_limit import UploadBodyLimitMiddleware
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "upload_body_max_bytes", 1024)
    delivered = []
    requests = iter(
        [
            {"type": "http.request", "body": b"x" * 700, "more_body": True},
            {"type": "http.request", "body": b"x" * 700, "more_body": False},
        ]
    )

    async def receive():
        return next(requests)

    async def send(message):
        delivered.append(message)

    async def parser(*args):
        pytest.fail("El parser no debe recibir un cuerpo excesivo")

    await UploadBodyLimitMiddleware(parser)(
        {"type": "http", "headers": [(b"content-type", b"multipart/form-data; boundary=test")], "method": "POST"},
        receive,
        send,
    )
    assert delivered[0]["status"] == 413


def test_idempotent_chat_and_revocation_before_publication(sql, monkeypatch):
    from unittest.mock import MagicMock

    from starlette.requests import Request

    from app.agents import chat_service
    from app.agents.orchestrator import Orchestrator
    from app.api.routes import chat as route
    from app.api.schemas import ChatRequest
    from app.common.errors import ForbiddenError
    from app.database.models import ChatOperation
    from app.llm.ollama_client import ChatResult

    db, factory, engine = sql
    # Este escenario necesita la segunda conexion breve para revalidar al publicar.
    check_engine = create_engine(engine.url)
    check_factory = sessionmaker(check_engine, expire_on_commit=False)
    monkeypatch.setattr(chat_service, "get_sessionmaker", lambda: check_factory)
    ctx = make_context(permissions=frozenset(), sources=frozenset())
    current = [ctx]
    monkeypatch.setattr(route, "get_user_context", lambda request, database: current[0])
    policy = MagicMock()
    policy.effective_categories.return_value = frozenset()
    monkeypatch.setattr(chat_service, "get_policy_engine", lambda: policy)
    calls = []
    revoke = [False]

    class Model:
        def chat(self, **kwargs):
            calls.append(kwargs)
            if revoke[0]:
                current[0] = replace(ctx, roles=frozenset({"changed-role"}))
            return ChatResult(content="Hola.", model=kwargs["model"], latency_ms=1)

    orchestrator = Orchestrator(
        llm=Model(), policy_engine=policy, retriever=MagicMock(), structured_tool=MagicMock(), audit=MagicMock()
    )
    monkeypatch.setattr(chat_service, "get_orchestrator", lambda: orchestrator)
    request = Request({"type": "http", "headers": []})
    payload = ChatRequest(message="Hola", client_request_id="request-1234567890")
    first = route.chat(payload, request, db, ctx)
    duplicate = route.chat(payload, request, db, ctx)
    assert duplicate.message_id == first.message_id
    assert len(calls) == 1
    db.commit()
    revoke[0] = True
    with pytest.raises(ForbiddenError, match="permisos cambiaron"):
        route.chat(
            ChatRequest(message="Hola", conversation_id=first.conversation_id, client_request_id="request-2234567890"),
            request,
            db,
            ctx,
        )
    statuses = db.execute(select(ChatOperation.status).order_by(ChatOperation.created_at)).scalars().all()
    assert statuses == ["completed", "failed"]
    assistants = db.execute(select(ConversationMessage).where(ConversationMessage.role == "assistant")).scalars().all()
    assert len(assistants) == 1
    check_engine.dispose()


def test_stalled_operation_expires_without_replay(sql):
    from datetime import timedelta

    from app.api.routes.chat import _expire_stalled_operation
    from app.database.models import ChatOperation

    db, _, _ = sql
    operation = ChatOperation(
        id="a" * 64,
        user_id=new_id(),
        conversation_id=new_id(),
        request_hash="b" * 64,
        authorization_scope="c" * 64,
        status="running",
        created_at=utcnow_naive() - timedelta(hours=1),
    )
    db.add(operation)
    db.commit()
    _expire_stalled_operation(db, operation)
    db.commit()
    assert operation.status == "expired"
    assert operation.response is None
