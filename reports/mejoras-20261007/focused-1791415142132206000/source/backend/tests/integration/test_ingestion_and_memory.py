# Creado por Aldo Garcia.
"""Ingesta corporativa/privada y memoria conversacional contra MySQL real.

Usa Qdrant en memoria y un cliente de embeddings simulado, de modo que la prueba
ejercita el pipeline completo sin depender de Ollama ni del almacen en disco.
"""

from __future__ import annotations

import hashlib

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import select

from app.common.errors import ForbiddenError, NotFoundError
from app.config import get_settings
from app.database.models import Document, User
from app.ingestion.service import INGESTION_VERSION, IngestionService
from app.memory.service import MemoryService, authorization_fingerprint
from app.rag.schemas import SCOPE_CONVERSATION, SCOPE_CORPORATE
from app.rag.vector_store import VectorStore

pytestmark = pytest.mark.integration

DIMENSION = 768


class FakeEmbeddingClient:
    """Embeddings deterministas: la prueba no depende de Ollama."""

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * DIMENSION
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for offset in range(0, 32, 4):
                vector[int.from_bytes(digest[offset : offset + 4], "big") % DIMENSION] += 1.0
        norm = sum(v * v for v in vector) ** 0.5
        return [v / norm for v in vector] if norm else [1.0] + [0.0] * (DIMENSION - 1)

    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:  # noqa: ARG002
        return [self._vector(t) for t in texts]

    def embed_one(self, text: str, *, model: str | None = None) -> list[float]:  # noqa: ARG002
        return self._vector(text)


# Cada seccion supera HEADING_BREAK_MIN_TOKENS (80) a proposito: por debajo de
# ese umbral el chunker las fusiona, que es el comportamiento deseado para no
# generar chunks de solo titulo.
_RELLENO_A = "contenido relevante de la primera seccion del documento sintetico " * 12
_RELLENO_B = "contenido relevante de la segunda seccion del documento sintetico " * 12

DOCUMENTO = f"""# Politica de prueba

## Seccion primera
{_RELLENO_A}

## Seccion segunda
{_RELLENO_B}
"""


@pytest.fixture()
def service() -> IngestionService:
    store = VectorStore(client=QdrantClient(location=":memory:"))
    return IngestionService(store=store, llm=FakeEmbeddingClient())


@pytest.fixture()
def usuario(db_session):  # noqa: ANN001, ANN201
    return db_session.execute(select(User).where(User.username == "Matrix")).scalar_one()


@pytest.fixture()
def contexto(db_session, usuario):  # noqa: ANN001, ANN201
    from app.authorization.policy import get_policy_engine
    from app.common.ids import new_id

    return get_policy_engine().build_context(
        db_session, user=usuario, session_id=new_id(), request_id=new_id()
    )


class TestIngestaCorporativa:
    def test_indexa_un_documento_y_actualiza_el_manifest(self, db_session, service, tmp_path):
        archivo = tmp_path / "politica.md"
        archivo.write_text(DOCUMENTO, encoding="utf-8")

        outcome = service.ingest_corporate_file(
            db_session,
            absolute_path=archivo,
            relative_path="prestaciones/politica-integracion.md",
            category="prestaciones",
        )
        assert outcome.status == "indexed"
        assert outcome.chunk_count >= 2, "los encabezados deben abrir chunks nuevos"

        document = db_session.get(Document, outcome.document_id)
        assert document.scope == SCOPE_CORPORATE
        assert document.category == "prestaciones"
        assert document.ingestion_version == INGESTION_VERSION
        assert len(document.sha256) == 64

    def test_reingerir_sin_cambios_no_reindexa(self, db_session, service, tmp_path):
        archivo = tmp_path / "estable.md"
        archivo.write_text(DOCUMENTO, encoding="utf-8")
        ruta = "prestaciones/estable-integracion.md"

        primera = service.ingest_corporate_file(
            db_session, absolute_path=archivo, relative_path=ruta, category="prestaciones"
        )
        segunda = service.ingest_corporate_file(
            db_session, absolute_path=archivo, relative_path=ruta, category="prestaciones"
        )
        assert primera.skipped is False
        assert segunda.skipped is True
        assert segunda.status == "unchanged"
        assert segunda.document_id == primera.document_id

    def test_un_cambio_de_contenido_reindexa_sin_duplicar(self, db_session, service, tmp_path):
        archivo = tmp_path / "cambia.md"
        archivo.write_text(DOCUMENTO, encoding="utf-8")
        ruta = "prestaciones/cambia-integracion.md"

        primera = service.ingest_corporate_file(
            db_session, absolute_path=archivo, relative_path=ruta, category="prestaciones"
        )
        archivo.write_text(DOCUMENTO + "\n## Seccion tercera\nTexto nuevo suficiente.\n", encoding="utf-8")
        segunda = service.ingest_corporate_file(
            db_session, absolute_path=archivo, relative_path=ruta, category="prestaciones"
        )

        assert segunda.document_id == primera.document_id, "no debe crear un documento nuevo"
        assert segunda.skipped is False
        assert segunda.chunk_count >= primera.chunk_count

    def test_un_documento_sin_texto_queda_vacio_y_no_inventa(self, db_session, service, tmp_path):
        archivo = tmp_path / "vacio.md"
        archivo.write_text("   \n\n   \n", encoding="utf-8")

        outcome = service.ingest_corporate_file(
            db_session,
            absolute_path=archivo,
            relative_path="prestaciones/vacio-integracion.md",
            category="prestaciones",
        )
        assert outcome.status == "empty"
        assert outcome.chunk_count == 0

    def test_una_ruta_con_traversal_se_rechaza(self, db_session, service, tmp_path):
        from app.common.errors import ValidationFailedError

        archivo = tmp_path / "x.md"
        archivo.write_text(DOCUMENTO, encoding="utf-8")
        with pytest.raises(ValidationFailedError):
            service.ingest_corporate_file(
                db_session,
                absolute_path=archivo,
                relative_path="../../fuera.md",
                category="prestaciones",
            )

    def test_borrar_un_documento_lo_retira_del_indice(self, db_session, service, tmp_path):
        archivo = tmp_path / "borrable.md"
        archivo.write_text(DOCUMENTO, encoding="utf-8")
        outcome = service.ingest_corporate_file(
            db_session,
            absolute_path=archivo,
            relative_path="prestaciones/borrable-integracion.md",
            category="prestaciones",
        )
        document = db_session.get(Document, outcome.document_id)

        service.delete_document(db_session, document)
        assert document.deleted_at is not None
        assert document.status == "deleted"
        assert document.chunk_count == 0


class TestAdjuntoPrivado:
    def test_se_indexa_en_el_namespace_del_usuario(self, db_session, service, contexto):
        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto, title="adjuntos")

        outcome = service.ingest_conversation_attachment(
            db_session,
            data=DOCUMENTO.encode("utf-8"),
            display_name="privado.md",
            internal_filename="privado-test.md",
            mime_type="text/markdown",
            owner_user_id=contexto.user_id,
            conversation_id=conversation.id,
        )
        assert outcome.status == "indexed"

        document = db_session.get(Document, outcome.document_id)
        assert document.scope == SCOPE_CONVERSATION
        assert document.category is None
        assert document.owner_user_id == contexto.user_id
        assert document.conversation_id == conversation.id

    def test_borrar_la_conversacion_retira_sus_documentos(self, db_session, service, contexto):
        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto, title="a borrar")
        service.ingest_conversation_attachment(
            db_session,
            data=DOCUMENTO.encode("utf-8"),
            display_name="temporal.md",
            internal_filename="temporal-test.md",
            mime_type="text/markdown",
            owner_user_id=contexto.user_id,
            conversation_id=conversation.id,
        )

        removidos = service.delete_conversation_documents(db_session, conversation.id)
        assert removidos == 1


class TestMemoriaConversacional:
    def test_ciclo_completo_de_una_conversacion(self, db_session, contexto):
        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto)
        assert conversation.title == "Nueva conversacion"

        memory.rename_if_untitled(db_session, conversation, "Cuantos dias de vacaciones tengo?")
        assert conversation.title.startswith("Cuantos dias")

        memory.append_message(db_session, conversation, role="user", content="pregunta")
        memory.append_message(
            db_session,
            conversation,
            role="assistant",
            content="respuesta",
            model="gemma4:latest",
            source_ids=("prestaciones/x.md#1",),
            authorized_categories=("prestaciones",),
        )
        mensajes = memory.list_messages(db_session, conversation.id)
        assert [m.role for m in mensajes] == ["user", "assistant"]
        assert mensajes[1].source_ids == ["prestaciones/x.md#1"]

    def test_el_orden_del_hilo_no_depende_del_reloj(self, db_session, contexto):
        """Regresion: dos mensajes escritos en el mismo microsegundo se
        devolvian invertidos porque el orden se basaba en ``created_at``."""
        from app.common.ids import utcnow_naive

        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto, title="orden")
        # El orquestador registra la procedencia antes de persistir cada turno.
        # Sin huella los assistants historicos se descartan deliberadamente.
        categories = frozenset({"prestaciones"})
        db_session.info["authorization_scope"] = authorization_fingerprint(db_session, contexto, categories)
        instante = utcnow_naive()

        for indice in range(6):
            mensaje = memory.append_message(
                db_session,
                conversation,
                role="user" if indice % 2 == 0 else "assistant",
                content=f"turno-{indice}",
            )
            # Se fuerza la MISMA marca de tiempo en todos los mensajes.
            mensaje.created_at = instante
        db_session.flush()

        contenidos = [m.content for m in memory.list_messages(db_session, conversation.id)]
        assert contenidos == [f"turno-{i}" for i in range(6)]

        recientes = memory.build_context(
            db_session,
            contexto,
            conversation,
            authorized_categories=categories,
            recent_turns=3,
        )
        assert [t.content for t in recientes.turns] == ["turno-3", "turno-4", "turno-5"]

    def test_el_contexto_descarta_turnos_fuera_del_alcance_actual(self, db_session, contexto):
        """Si se retira una categoria, los turnos que se apoyaron en ella no vuelven."""
        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto, title="permisos")

        # Primer turno con permisos amplios. La retirada de nomina invalida su
        # huella completa, incluso si otro texto anterior solo citara prestaciones.
        previous_scope = authorization_fingerprint(db_session, contexto, frozenset({"nomina", "prestaciones"}))
        db_session.info["authorization_scope"] = previous_scope
        memory.append_message(
            db_session,
            conversation,
            role="assistant",
            content="respuesta basada en nomina",
            authorized_categories=("nomina",),
        )
        # Segundo turno producido despues de retirar nomina, con alcance vigente.
        current_scope = authorization_fingerprint(db_session, contexto, frozenset({"prestaciones"}))
        assert previous_scope != current_scope
        db_session.info["authorization_scope"] = current_scope
        memory.append_message(
            db_session,
            conversation,
            role="assistant",
            content="respuesta basada en prestaciones",
            authorized_categories=("prestaciones",),
        )

        contexto_actual = memory.build_context(
            db_session, contexto, conversation, authorized_categories=frozenset({"prestaciones"})
        )
        contenidos = [t.content for t in contexto_actual.turns]
        assert any("prestaciones" in c for c in contenidos)
        assert not any("nomina" in c for c in contenidos)
        assert contexto_actual.dropped_turns == 1

    def test_no_se_ensambla_contexto_ajeno(self, db_session, contexto):
        from tests.conftest import make_context

        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto, title="propia")
        ajeno = make_context(username="Otro")

        with pytest.raises(ForbiddenError):
            memory.build_context(
                db_session, ajeno, conversation, authorized_categories=frozenset({"prestaciones"})
            )

    def test_una_conversacion_borrada_no_se_recupera(self, db_session, contexto):
        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto, title="efimera")
        memory.delete_conversation(db_session, contexto, conversation.id)

        with pytest.raises(NotFoundError):
            memory.get_owned_conversation(db_session, contexto, conversation.id)

    def test_el_resumen_se_almacena_y_se_recupera(self, db_session, contexto):
        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto, title="resumida")
        categories = frozenset({"prestaciones"})
        db_session.info["authorization_scope"] = authorization_fingerprint(db_session, contexto, categories)
        memory.store_summary(
            db_session, conversation.id, summary="El usuario pregunto por vacaciones.", message_count=4
        )
        contexto_actual = memory.build_context(
            db_session, contexto, conversation, authorized_categories=categories
        )
        assert "vacaciones" in contexto_actual.summary

    def test_las_respuestas_y_resumen_sin_procedencia_no_regresan_al_contexto(self, db_session, contexto):
        """Datos anteriores a las huellas no autorizan memoria derivada de fuentes."""
        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto, title="legado")
        db_session.info.pop("authorization_scope", None)
        memory.append_message(db_session, conversation, role="user", content="pregunta propia")
        assistant = memory.append_message(
            db_session, conversation, role="assistant", content="respuesta historica no verificable",
            authorized_categories=("prestaciones",),
        )
        summary = memory.store_summary(
            db_session, conversation.id, summary="Resumen historico no verificable.", message_count=2
        )
        assert assistant.authorization_scope is None
        assert summary.authorization_scope is None
        current = memory.build_context(
            db_session, contexto, conversation, authorized_categories=frozenset({"prestaciones"})
        )
        assert [turn.content for turn in current.turns] == ["pregunta propia"]
        assert current.summary == ""
        assert current.dropped_turns == 1

    def test_un_alcance_revocado_invalida_resumen_y_respuestas_aun_sin_citas_revocadas(self, db_session, contexto):
        """El contenido pudo heredar nomina en memoria aunque solo cite prestaciones."""
        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto, title="revocacion")
        previous_scope = authorization_fingerprint(db_session, contexto, frozenset({"nomina", "prestaciones"}))
        db_session.info["authorization_scope"] = previous_scope
        memory.append_message(
            db_session, conversation, role="assistant", content="respuesta derivada del alcance anterior",
            authorized_categories=("prestaciones",),
        )
        memory.store_summary(
            db_session, conversation.id, summary="Resumen del alcance anterior.", message_count=1
        )
        current = memory.build_context(
            db_session, contexto, conversation, authorized_categories=frozenset({"prestaciones"})
        )
        assert current.turns == ()
        assert current.summary == ""
        assert current.dropped_turns == 1

    def test_necesita_resumen_solo_al_crecer(self, db_session, contexto):
        memory = MemoryService()
        conversation = memory.create_conversation(db_session, contexto, title="corta")
        assert memory.needs_summary(db_session, conversation.id) is False


class TestConfiguracionDeColecciones:
    def test_las_colecciones_estan_separadas(self):
        settings = get_settings()
        assert settings.rag_collection_corporate != settings.rag_collection_private
