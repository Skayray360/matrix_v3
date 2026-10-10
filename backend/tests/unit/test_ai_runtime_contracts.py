# Creado por Aldo Garcia.
"""Regresiones de identidad, resumen privado, jerarquia y fallback local."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from qdrant_client import QdrantClient

from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.orchestrator import Orchestrator
from app.agents.prompts import GENERAL_SYSTEM_POLICY, IDENTITY_ANSWER, SYSTEM_POLICY
from app.common.answers import UNVERIFIED_ANSWER_NOTICE, safe_nonfactual_text
from app.common.errors import OllamaUnavailableError
from app.common.ids import new_id
from app.config import get_settings
from app.llm.model_policy import Intent, ModelChoice, ModelPolicy
from app.llm.ollama_client import ChatResult
from app.memory.service import ConversationContext
from app.rag.retriever import RetrievalResult, Retriever
from app.rag.schemas import SCOPE_CONVERSATION, Chunk, ChunkMetadata, Evidence
from app.rag.vector_store import VectorStore
from tests.conftest import make_context

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def markdown_transport(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_structured_output", False)


def evidence(index: int, *, chars: int = 120) -> Evidence:
    return Evidence(
        source_id=f"__private__/archivo.md#{index}",
        text=(f"Seccion {index} con contenido verificable del documento. " * 80)[:chars],
        score=1.0,
        category="__private__",
        filename="archivo.md",
        section=f"Seccion {index}",
        page_or_sheet="",
        document_id="doc-1",
        chunk_id=f"chunk-{index}",
        scope=SCOPE_CONVERSATION,
    )


class ScriptedLlm:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def chat(self, *, model: str, messages: list[dict[str, str]], **kwargs) -> ChatResult:  # noqa: ANN003
        self.calls.append({"model": model, "messages": messages, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ChatResult(content=response, model=model, latency_ms=3)


class HierarchicalLlm:
    """Genera una salida valida a partir de los source_id visibles en cada fase."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def chat(self, *, model: str, messages: list[dict[str, str]], **_kwargs) -> ChatResult:
        prompt = messages[-1]["content"]
        self.calls.append(model)
        if "RESUMENES_PARCIALES_AUTORIZADOS" in prompt:
            source_ids = tuple(dict.fromkeys(re.findall(r"\[\[([^\]]+)\]\]", prompt)))
            content = "Resumen final " + " ".join(f"[[{sid}]]" for sid in source_ids)
        else:
            source_ids = tuple(dict.fromkeys(re.findall(r"\[source_id: ([^\]]+)\]", prompt)))
            content = "Resumen parcial " + " ".join(f"[[{sid}]]" for sid in source_ids)
        return ChatResult(content=content, model=model, latency_ms=2)


class TestContratoDeIntencion:
    def test_identidad_es_inmutable(self):
        policy = ModelPolicy()
        for question in ("¿Quién eres?", "¿Cómo te llamas?", "Identifícate"):
            assert policy.classify_intent(question) is Intent.IDENTITY
        assert IDENTITY_ANSWER == "Soy Matrix."
        assert IDENTITY_ANSWER in SYSTEM_POLICY
        assert IDENTITY_ANSWER in GENERAL_SYSTEM_POLICY

    def test_resumen_y_general_no_se_confunden(self):
        policy = ModelPolicy()
        for question in (
            "Hazme un resumen",
            "Resume el archivo que cargué",
            "Resúmelo",
            "Sintetízalo",
        ):
            assert policy.classify_intent(question) is Intent.DOCUMENT_SUMMARY
        assert policy.classify_intent("Explica que es el aprendizaje automatico") is Intent.GENERAL
        assert policy.classify_intent("Explica que es la fotosintesis") is Intent.GENERAL
        assert policy.classify_intent("Que dice la politica de vacaciones?") is Intent.DOCUMENTAL

    @pytest.mark.parametrize(
        "question",
        [
            "Cuantos dias de vacaciones tengo?",
            "Cuando pagan el aguinaldo?",
            "Como funciona la nomina en nuestra empresa?",
            "Cual es mi salario y compensacion?",
            "Que beneficios y prestaciones existen?",
            "Cual es el horario de la jornada?",
            "Como tramito una incapacidad o permiso laboral?",
            "Como es el reclutamiento y la capacitacion?",
            "Que aplican las relaciones laborales?",
        ],
    )
    def test_temas_rh_son_documentales_fail_closed(self, question: str):
        assert ModelPolicy().classify_intent(question) is Intent.DOCUMENTAL

    def test_resumen_extenso_activa_modelo_profundo(self):
        decision = ModelPolicy().route(
            "Resume el archivo",
            intent=Intent.DOCUMENT_SUMMARY,
            evidence_count=12,
            evidence_chars=30_000,
        )
        assert decision.choice is ModelChoice.DEEP
        assert "large_document_summary" in decision.signals


class TestIdentidadSinDependencias:
    def test_identidad_autoriza_memoria_sin_rag_ni_modelos(self, monkeypatch):
        monkeypatch.setattr("app.agents.orchestrator.authorization_fingerprint", lambda *_: "scope")
        class Never:
            def __getattr__(self, name):  # noqa: ANN001, ANN204
                raise AssertionError(f"no debe tocar {name}")

        @dataclass
        class Memory:
            sequence: int = 0

            def get_owned_conversation(self, _db, current, conversation_id):
                assert conversation_id == conversation.id
                assert current.user_id == conversation.user_id
                return conversation

            def build_context(self, *_args, **_kwargs):
                return ConversationContext(conversation_id="c-1")

            def rename_if_untitled(self, *_args, **_kwargs) -> None:
                return None

            def append_message(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
                self.sequence += 1
                return SimpleNamespace(id=f"m-{self.sequence}")

        class Audit:
            def __init__(self) -> None:
                self.records = []

            def record(self, _db, record):  # noqa: ANN001, ANN201
                self.records.append(record)
                return "a-1"

        ctx = make_context()
        conversation = SimpleNamespace(id="c-1", user_id=ctx.user_id, title="Nueva conversacion")
        orchestrator = Orchestrator(
            llm=Never(),
            policy_engine=SimpleNamespace(effective_categories=lambda _: frozenset()),
            retriever=Never(),
            structured_tool=Never(),
            memory=Memory(),
            audit=Audit(),
        )
        outcome = orchestrator.handle_chat(
            MagicMock(), ctx=ctx, conversation=conversation, message="¿Quién eres?"
        )
        assert outcome.answer == "Soy Matrix."
        assert outcome.intent == "identity"
        assert outcome.model == ""

    def test_consulta_general_consulta_rag_autorizado_y_no_inventa_fuentes(self, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr("app.agents.orchestrator.authorization_fingerprint", lambda *_args: "scope")

        class Never:
            def __getattr__(self, name):  # noqa: ANN001, ANN204
                raise AssertionError(f"no debe tocar {name}")

        class Policy:
            def effective_categories(self, _ctx):  # noqa: ANN001, ANN201
                return frozenset()

        class Memory:
            def __init__(self) -> None:
                self.sequence = 0

            def rename_if_untitled(self, *_args, **_kwargs) -> None:
                return None

            def append_message(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
                self.sequence += 1
                return SimpleNamespace(id=f"m-{self.sequence}")

            def build_context(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
                return ConversationContext(conversation_id="c-1")

            def needs_summary(self, *_args, **_kwargs) -> bool:
                return False

        class Audit:
            def record(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
                return "a-1"

        ctx = make_context(categories=frozenset(), wildcard=False)
        conversation = SimpleNamespace(id="c-1", user_id=ctx.user_id, title="Nueva conversacion")
        llm = ScriptedLlm(responses=["La fotosintesis convierte energia luminosa."])
        from app.rag.retriever import RetrievalResult
        retriever = MagicMock()
        retriever.retrieve.return_value = RetrievalResult()
        outcome = Orchestrator(
            llm=llm,
            policy_engine=Policy(),
            retriever=retriever,
            structured_tool=Never(),
            memory=Memory(),
            audit=Audit(),
        ).handle_chat(
            MagicMock(),
            ctx=ctx,
            conversation=conversation,
            message="Explica que es la fotosintesis",
        )
        assert retriever.retrieve.call_args.kwargs["authorized_categories"] == frozenset()
        assert outcome.intent == "general"
        assert outcome.public_sources() == []
        assert "La fotosintesis" in outcome.answer
        assert outcome.answer_basis == "general"
        assert len(llm.calls) == 1


class TestResumenConEvidencia:
    def test_documental_sin_citas_se_descarta_tras_un_retry(self):
        llm = ScriptedLlm(
            responses=["La politica concede treinta dias.", "Insisto: son treinta dias."]
        )
        result = KnowledgeAgent(llm=llm, policy=ModelPolicy()).synthesize(
            question="Cuantos dias de vacaciones tengo?",
            evidences=(evidence(0),), model_name="gemma4:latest", intent=Intent.DOCUMENTAL,
        )
        assert result.answer.startswith(UNVERIFIED_ANSWER_NOTICE) and safe_nonfactual_text(result.answer)
        assert not result.cited_source_ids and "treinta" not in result.answer
        assert len(llm.calls) == 2

    def test_no_acepta_falsa_insuficiencia_y_entrega_resumen_citable(self):
        llm = ScriptedLlm(
            responses=[
                "No cuento con informacion documental suficiente.",
                "No cuento con informacion documental suficiente.",
            ]
        )
        agent = KnowledgeAgent(llm=llm, policy=ModelPolicy())
        result = agent.synthesize(
            question="Resume el archivo",
            evidences=(evidence(0), evidence(1)),
            model_name="gemma4:latest",
            deep_model_name="gemma4:latest",
            intent=Intent.DOCUMENT_SUMMARY,
        )
        assert "Resumen extractivo" in result.answer
        assert "[[__private__/archivo.md#0]]" in result.answer
        assert "no cuento con informacion" not in result.answer.lower()
        assert result.cited_source_ids
        assert [call["model"] for call in llm.calls] == [
            "gemma4:latest",
            "gemma4:latest",
        ]

    def test_archivo_sin_texto_pide_precision_sin_inventar_contenido(self):
        llm = ScriptedLlm(responses=["¿Qué documento desea consultar?"])
        result = KnowledgeAgent(llm=llm, policy=ModelPolicy()).synthesize(
            question="Resume el archivo vacio",
            evidences=(),
            model_name="gemma4:latest",
            intent=Intent.DOCUMENT_SUMMARY,
        )
        assert result.grounding.declares_insufficiency is True
        assert len(llm.calls) == 1

    def test_usa_presupuesto_del_modelo_rapido(self):
        llm = ScriptedLlm(responses=[f"{evidence(0).text} [[__private__/archivo.md#0]]."])
        KnowledgeAgent(llm=llm, policy=ModelPolicy()).synthesize(
            question="Resume el archivo",
            evidences=(evidence(0),),
            model_name="gemma4:latest",
            intent=Intent.DOCUMENT_SUMMARY,
        )
        call = llm.calls[0]
        assert call["num_ctx"] == get_settings().ollama_fast_num_ctx
        assert call["max_tokens"] == get_settings().ollama_fast_max_tokens

    def test_modelo_local_ausente_no_activa_otro_generador(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "ollama_deep_model", "synthetic-alternate")
        llm = ScriptedLlm(
            responses=[
                OllamaUnavailableError(detail="HTTP 404 en /api/chat"),
                f"{evidence(0).text} [[__private__/archivo.md#0]].",
            ]
        )
        with pytest.raises(OllamaUnavailableError):
            KnowledgeAgent(llm=llm, policy=ModelPolicy()).synthesize(
                question="Resume el archivo", evidences=(evidence(0),),
                model_name="gemma4:latest", intent=Intent.DOCUMENT_SUMMARY,
            )
        assert [call["model"] for call in llm.calls] == ["gemma4:latest"]

    def test_archivo_largo_usa_map_reduce_y_preserva_citas(self):
        # Cuarenta unidades completas requieren mapas en el perfil rapido, pero
        # su consolidacion cabe en el profundo: verifica ambas fases sin usar
        # frases parciales como sustituto de evidencia real.
        evidences = tuple(evidence(index, chars=400) for index in range(40))
        llm = HierarchicalLlm()
        result = KnowledgeAgent(llm=llm, policy=ModelPolicy()).synthesize(
            question="Resume todo el archivo largo",
            evidences=evidences,
            model_name="gemma4:latest",
            deep_model_name="gemma4:latest",
            intent=Intent.DOCUMENT_SUMMARY,
        )
        assert result.hierarchical is True
        assert result.map_batches > 1
        assert result.model == "gemma4:latest"
        assert set(result.cited_source_ids) == {item.source_id for item in evidences}
        assert "gemma4:latest" in llm.calls
        assert llm.calls[-1] == "gemma4:latest"


def private_chunk(*, user: str, conversation: str, index: int) -> Chunk:
    metadata = ChunkMetadata(
        chunk_id=new_id(),
        document_id=f"doc-{user}-{conversation}",
        document_sha256="a" * 64,
        filename="adjunto.md",
        relative_path="adjunto.md",
        category="__private__",
        subpath="",
        mime_type="text/markdown",
        page_or_sheet="",
        section=f"Seccion {index}",
        chunk_index=index,
        embedding_model="embeddinggemma:latest",
        embedding_dimension=768,
        ingestion_version="3",
        scope=SCOPE_CONVERSATION,
        owner_user_id=user,
        conversation_id=conversation,
    )
    return Chunk(text=f"Contenido privado {user} {conversation} {index}", metadata=metadata)


@pytest.mark.usefixtures("manifest_db")
class TestRecuperacionDeResumenPrivado:
    def test_scroll_respeta_usuario_y_conversacion_sin_embedding(self):
        store = VectorStore(client=QdrantClient(location=":memory:"))
        chunks = [
            private_chunk(user="u-a", conversation="c-a", index=0),
            private_chunk(user="u-a", conversation="c-a", index=1),
            private_chunk(user="u-b", conversation="c-a", index=2),
            private_chunk(user="u-a", conversation="c-b", index=3),
        ]
        store.upsert_chunks(chunks, [[1.0] + [0.0] * 767 for _ in chunks])

        class NoEmbedding:
            def embed_one(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
                raise AssertionError("el resumen por adjunto no debe generar embedding")

        result = Retriever(store=store, llm=NoEmbedding()).retrieve_attachment_summary(
            ctx=make_context(user_id="u-a"), conversation_id="c-a"
        )
        assert [item.text for item in result.evidences] == [
            "Contenido privado u-a c-a 0",
            "Contenido privado u-a c-a 1",
        ]
        assert result.used_private_scope is True
        assert result.truncated is False

    def test_limite_se_reporta_y_no_se_oculta(self):
        store = VectorStore(client=QdrantClient(location=":memory:"))
        chunks = [private_chunk(user="u", conversation="c", index=i) for i in range(3)]
        store.upsert_chunks(chunks, [[1.0] + [0.0] * 767 for _ in chunks])
        listed, truncated = store.list_private_chunks(
            user_id="u", conversation_id="c", limit=2
        )
        assert len(listed) == 2
        assert truncated is True

    def test_conversacion_sin_chunks_devuelve_evidencia_vacia(self):
        store = VectorStore(client=QdrantClient(location=":memory:"))
        result = Retriever(store=store, llm=SimpleNamespace()).retrieve_attachment_summary(
            ctx=make_context(user_id="u"), conversation_id="sin-adjuntos"
        )
        assert result == RetrievalResult()
