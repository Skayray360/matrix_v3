# Creado por Aldo Garcia.
"""El diagnostico no adquiere el indice embebido ni deja clientes abiertos."""
import json
from types import SimpleNamespace

from app.config import QdrantMode
from scripts import bootstrap


def test_status_read_only_no_crea_ni_abre_qdrant(monkeypatch, tmp_path, capsys):
    from app import config
    from app.database import engine
    from app.llm import provider
    from app.rag import vector_store

    closed = []

    class Client:
        def ping(self):
            return True

        def close(self):
            closed.append(True)

    def forbidden():
        raise AssertionError("El diagnostico no debe abrir Qdrant")

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(
        app_env="test", auth_provider="local_test", qdrant_mode=QdrantMode.EMBEDDED,
        qdrant_storage_path=tmp_path / "indice-ausente", ollama_base_url="http://127.0.0.1:11434",
        ollama_fast_model="fast", ollama_deep_model="deep", ollama_embedding_model="embed",
    ))
    monkeypatch.setattr(engine, "check_database", lambda: (True, "MariaDB sintetico"))
    monkeypatch.setattr(provider, "ModelClient", Client)
    monkeypatch.setattr(vector_store, "get_vector_store", forbidden)
    assert bootstrap.cmd_status(SimpleNamespace(read_only=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["qdrant"]["ok"] is None
    assert "omitido" in payload["qdrant"]["detail"]
    assert closed == [True]
    assert not (tmp_path / "indice-ausente").exists()
