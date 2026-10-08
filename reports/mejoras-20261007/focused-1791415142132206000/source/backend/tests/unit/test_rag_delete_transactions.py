# Creado por Aldo Garcia.
"""El rollback SQL conserva evidencia y un commit de baja la oculta antes de limpiar Qdrant."""

from contextlib import contextmanager

import pytest

from app.rag.index_manifest import indexing_fingerprint
from app.rag.schemas import SCOPE_CONVERSATION
from tests.unit import test_private_reindex_final as private_fixtures

pytestmark = pytest.mark.unit
# Reutilizar el entorno SQLite/Qdrant aislado, sin conectar servicios reales.
environment = private_fixtures.environment


def indexed_attachment(env):
    outcome = env.service.ingest_conversation_attachment(
        env.db, data=b"# Politica sintetica\n\nLa persona debe solicitar autorizacion previa.",
        display_name="politica.md", internal_filename="fixture.md", mime_type="text/markdown",
        owner_user_id="user-a", conversation_id="chat-a",
    )
    env.db.commit()
    from app.database.models import Document

    return env.db.get(Document, outcome.document_id)


def private_evidence(env):
    return env.store.list_private_chunks(
        user_id="user-a", conversation_id="chat-a", limit=10, index_fingerprint=indexing_fingerprint(env.llm)
    )[0]


@pytest.mark.parametrize("conversation_delete", [False, True])
def test_rollback_de_baja_no_destruye_vectores(environment, conversation_delete):
    env = environment
    document = indexed_attachment(env)
    assert private_evidence(env)
    if conversation_delete:
        assert env.service.delete_conversation_documents(env.db, "chat-a") == 1
    else:
        env.service.delete_document(env.db, document)
    env.db.rollback()
    assert document.status == "indexed"
    assert private_evidence(env)


def test_commit_oculta_evidencia_y_limpieza_posterior_retira_bytes(environment, monkeypatch):
    env = environment
    document = indexed_attachment(env)
    collection = env.store.collection_for(SCOPE_CONVERSATION)
    count = env.store.count(collection)
    assert count > 0
    env.service.delete_document(env.db, document)
    env.db.commit()
    assert not private_evidence(env)
    assert env.store.count(collection) == count

    @contextmanager
    def committed_scope():
        with env.factory() as db:
            yield db
            db.commit()

    monkeypatch.setattr("app.database.engine.session_scope", committed_scope)
    assert env.store.cleanup_generations() == 1
    assert env.store.count(collection) == 0
