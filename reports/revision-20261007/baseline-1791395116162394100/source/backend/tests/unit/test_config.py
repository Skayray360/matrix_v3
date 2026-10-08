# Creado por Aldo Garcia.
"""Validacion de configuracion: invariantes del RAG y bloqueos de entorno."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import Settings

pytestmark = pytest.mark.unit


def build(**overrides):  # noqa: ANN201
    """Construye Settings ignorando el .env del equipo."""
    base = {
        "app_env": "development",
        "app_secret_key": "0" * 64,
        "local_test_auth_enabled": True,
        "auth_provider": "local_test",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


class TestInvariantesRag:
    def test_chunk_size_debe_superar_al_overlap(self):
        with pytest.raises(ValidationError, match="CHUNK_SIZE_TOKENS"):
            build(rag_chunk_size_tokens=200, rag_chunk_overlap_tokens=200)

    def test_fetch_k_no_puede_ser_menor_que_top_k(self):
        with pytest.raises(ValidationError, match="FETCH_K"):
            build(rag_top_k=10, rag_fetch_k=5)

    def test_mmr_lambda_fuera_de_rango_se_rechaza(self):
        with pytest.raises(ValidationError):
            build(rag_mmr_lambda=1.5)

    def test_dimensiones_deben_coincidir(self):
        with pytest.raises(ValidationError, match="DIMENSION"):
            build(rag_embedding_dimension=768, ollama_embedding_dimension=1024)

    def test_valores_por_defecto_son_los_exigidos(self):
        settings = build()
        assert settings.rag_chunk_size_tokens == 900
        assert settings.rag_chunk_overlap_tokens == 120
        assert settings.rag_top_k == 6
        assert settings.rag_fetch_k == 24
        assert settings.rag_min_similarity == 0.35
        assert settings.rag_mmr_lambda == 0.65
        assert settings.rag_reindex_interval_hours == 24
        assert settings.rag_embedding_dimension == 768


class TestModelosObligatorios:
    def test_modelos_por_defecto_son_los_declarados(self):
        settings = build()
        assert settings.ollama_fast_model == "gemma4:latest"
        assert settings.ollama_deep_model == "gemma4:latest"
        assert settings.ollama_embedding_model == "embeddinggemma:latest"

    def test_url_de_ollama_es_local_por_defecto(self):
        assert build().ollama_base_url == "http://127.0.0.1:11434"


class TestSeguridadDeEntorno:
    """Requisito 32: el modo local no puede habilitarse en produccion."""

    def test_produccion_con_auth_local_falla(self):
        with pytest.raises(ValidationError, match="production"):
            build(
                app_env="production",
                auth_provider="local_test",
                local_test_auth_enabled=True,
                session_cookie_secure=True,
            )

    def test_produccion_con_flag_local_habilitado_falla(self):
        with pytest.raises(ValidationError, match="LOCAL_TEST_AUTH_ENABLED"):
            build(
                app_env="production",
                auth_provider="entra",
                local_test_auth_enabled=True,
                entra_tenant_id="t",
                entra_client_id="c",
                entra_redirect_uri="https://x/cb",
                session_cookie_secure=True,
            )

    def test_produccion_con_seed_habilitado_falla(self):
        with pytest.raises(ValidationError, match="SEED"):
            build(
                app_env="production",
                auth_provider="entra",
                local_test_auth_enabled=False,
                local_test_seed_users_enabled=True,
                entra_tenant_id="t",
                entra_client_id="c",
                entra_redirect_uri="https://x/cb",
                session_cookie_secure=True,
            )

    def test_produccion_exige_cookie_segura(self):
        with pytest.raises(ValidationError, match="SECURE"):
            build(
                app_env="production",
                auth_provider="entra",
                local_test_auth_enabled=False,
                local_test_seed_users_enabled=False,
                entra_tenant_id="t",
                entra_client_id="c",
                entra_redirect_uri="https://x/cb",
                session_cookie_secure=False,
            )

    def test_produccion_valida_arranca_con_entra(self):
        settings = build(
            app_env="production",
            auth_provider="entra",
            entra_client_secret="synthetic-confidential-client-secret",
            local_test_auth_enabled=False,
            local_test_seed_users_enabled=False,
            entra_tenant_id="tenant",
            entra_client_id="client",
            entra_redirect_uri="https://matrix/cb",
            session_cookie_secure=True,
            # secrets-scan: allow (fixture sintetico aislado; sin credenciales de un servicio real)
            database_url="mysql+pymysql://matrixrh:synthetic-db-password@127.0.0.1/matrix_rh",
            llm_fast_digest="a" * 64,
            llm_deep_digest="a" * 64,
            storage_encryption_attested=True,
            database_encryption_attested=True,
            backup_encryption_attested=True,
            encryption_attestation_reference="synthetic-encryption-attestation.md",
        )
        assert settings.is_production is True
        assert settings.is_local_auth_allowed is False

    def test_entra_sin_configuracion_falla(self):
        with pytest.raises(ValidationError, match="ENTRA_"):
            build(auth_provider="entra", local_test_auth_enabled=False)

    def test_modo_local_permitido_en_development_y_test(self):
        assert build(app_env="development").is_local_auth_allowed is True
        assert build(app_env="test").is_local_auth_allowed is True


class TestSnapshotAuditable:
    def test_snapshot_expone_parametros_y_no_secretos(self):
        snapshot = build().rag_parameters_snapshot()
        assert snapshot["RAG_TOP_K"] == 6
        assert snapshot["OLLAMA_EMBEDDING_MODEL"] == "embeddinggemma:latest"
        assert snapshot["OLLAMA_FAST_NUM_CTX"] == 8192
        assert snapshot["OLLAMA_DEEP_NUM_CTX"] == 8192
        assert snapshot["OLLAMA_KEEP_ALIVE"] == "15m"
        assert snapshot["OLLAMA_EMBEDDING_CACHE_SIZE"] == 256
        assert snapshot["RAG_SUMMARY_SCAN_MAX_CHUNKS"] == 256
        serialized = str(snapshot).lower()
        for forbidden in ("password", "secret", "mysql+pymysql"):
            assert forbidden not in serialized
