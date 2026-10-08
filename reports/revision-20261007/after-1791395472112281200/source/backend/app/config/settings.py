# Creado por Aldo Garcia.
"""Configuracion tipada y validada de Matrix RH.

Todos los parametros operativos (RAG, Ollama, auth, uploads) viven aqui y no en
constantes dispersas, porque la seccion 9 exige que sean auditables y que se
puedan mostrar en un endpoint administrativo de diagnostico.

Las invariantes de seguridad se validan al construir el objeto: si la
configuracion es insegura el proceso no arranca, en lugar de degradarse en
silencio.
"""

from __future__ import annotations

import re
import secrets
from enum import StrEnum
from functools import lru_cache
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import unquote, urlparse

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.common.errors import ConfigurationError

#: Raiz del repositorio: <repo>/backend/app/config/settings.py -> <repo>
PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class AuthProvider(StrEnum):
    ENTRA = "entra"
    OIDC = "oidc"
    LOCAL_TEST = "local_test"


class QdrantMode(StrEnum):
    """Modo de operacion del vector store.

    ``embedded`` usa el almacenamiento local persistente del cliente oficial de
    Qdrant (mismo API, mismos filtros de metadata, persistencia en disco).
    ``server`` apunta a una instancia Qdrant dedicada. No existe fallback
    automatico entre modos: el modo es explicito y ``/ready`` falla si el modo
    configurado no esta operativo.
    """

    EMBEDDED = "embedded"
    SERVER = "server"


def _resolve(path_value: str | Path) -> Path:
    """Resuelve rutas relativas contra la raiz del repositorio."""
    path = Path(path_value)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _is_loopback_host(host: str) -> bool:
    """No resuelve DNS: solo localhost o una direccion loopback literal."""
    if host.casefold().rstrip(".") == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


class Settings(BaseSettings):
    """Configuracion completa de la aplicacion."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    # ---------------------------------------------------------------- app ---
    app_env: AppEnv = AppEnv.DEVELOPMENT
    app_name: str = "Matrix RH"
    app_base_url: str = "http://127.0.0.1:8000"
    app_host: str = "127.0.0.1"
    app_port: Annotated[int, Field(ge=1, le=65535)] = 8000
    app_log_level: str = "INFO"
    app_secret_key: SecretStr = SecretStr("")
    #: Solo los proxies concretos de TI pueden suministrar la IP del cliente.
    forwarded_allow_ips: str = "127.0.0.1,::1"

    # ------------------------------------------------------------- ollama ---
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_fast_model: str = "gemma4:latest"
    # Dos perfiles de tarea, un unico modelo generativo instalado.
    ollama_deep_model: str = "gemma4:latest"
    ollama_embedding_model: str = "embeddinggemma:latest"
    ollama_embedding_dimension: int = 768
    #: El presupuesto global incluye recuperacion y verificacion documental.
    ollama_timeout_seconds: float = 600.0
    ollama_keep_alive: str = "15m"
    #: Ambos perfiles comparten ventana para no recargar Gemma con otra reserva
    #: de contexto. La salida profunda es mayor; los resumenes largos se dividen
    #: en lotes. Este punto de partida no certifica capacidad de la GPU destino.
    ollama_fast_num_ctx: Annotated[int, Field(ge=2048, le=131072)] = 8192
    ollama_deep_num_ctx: Annotated[int, Field(ge=4096, le=262144)] = 8192
    ollama_fast_max_tokens: Annotated[int, Field(ge=64, le=8192)] = 1536
    ollama_deep_max_tokens: Annotated[int, Field(ge=128, le=16384)] = 3072
    ollama_embedding_cache_size: Annotated[int, Field(ge=0, le=4096)] = 256
    # Legado de configuracion: se acepta, pero el resumen por lotes es secuencial.
    ollama_summary_parallel_batches: Annotated[int, Field(ge=1, le=8)] = 2

    # Proveedores y parametros: unica configuracion operativa en .env.
    llm_provider: Literal["ollama", "openai_compatible", "vertex"] = "ollama"
    llm_deep_provider: Literal["ollama", "openai_compatible", "vertex"] = "ollama"
    llm_embedding_provider: Literal["ollama", "openai_compatible"] = "ollama"
    llm_local_only: bool = True
    llm_local_hosts: str = "127.0.0.1,localhost,::1"
    llm_api_base_url: str = "http://127.0.0.1:8080/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_vertex_base_url: str = ""
    llm_temperature: Annotated[float, Field(ge=0, le=2)] = 0.1
    llm_general_temperature: Annotated[float, Field(ge=0, le=2)] = 0.25
    llm_summary_temperature: Annotated[float, Field(ge=0, le=2)] = 0.08
    llm_retry_temperature: Annotated[float, Field(ge=0, le=2)] = 0.03
    llm_top_p: Annotated[float, Field(gt=0, le=1)] = 0.9
    # auto solicita contenido final sin thinking en Ollama; otros adapters
    # conservan su valor nativo. default permite elegir el comportamiento del
    # runtime expresamente; enabled/disabled siguen siendo opciones explicitas.
    llm_fast_thinking: Literal["auto", "default", "enabled", "disabled"] = "auto"
    llm_deep_thinking: Literal["auto", "default", "enabled", "disabled"] = "auto"
    llm_structured_thinking: Literal["auto", "default", "enabled", "disabled"] = "auto"
    llm_completion_retries: Annotated[int, Field(ge=0, le=1)] = 1
    llm_fast_top_k: Annotated[int, Field(ge=1, le=1000)] | None = None
    llm_deep_top_k: Annotated[int, Field(ge=1, le=1000)] | None = None
    # Legado sin efecto: el transporte no repite un POST de resultado ambiguo.
    llm_max_transient_retries: Annotated[int, Field(ge=0, le=2)] = 0
    # Cada generacion tiene un limite propio; el presupuesto de la consulta
    # incluye recuperacion, validacion y un posible fallback local.
    llm_fast_timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 180
    llm_deep_timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 180
    llm_request_deadline_seconds: Annotated[float, Field(gt=0, le=600)] = 600
    llm_planner_max_tokens: Annotated[int, Field(ge=64, le=4096)] = 600
    llm_planner_temperature: Annotated[float, Field(ge=0, le=2)] = 0.0
    llm_memory_max_tokens: Annotated[int, Field(ge=64, le=1024)] = 200
    llm_embedding_revision: str = "unverified"
    llm_fast_digest: str = ""
    llm_deep_digest: str = ""
    llm_embedding_digest: str = ""
    llm_query_template: str = "task: search result | query: {text}"
    llm_document_template: str = "title: {title} | text: {text}"
    llm_system_prefix: str = ""

    # La respuesta general debe distinguirse de las reglas internas verificadas.
    answer_allow_general_knowledge: bool = True
    answer_evidence_mode: Literal["cited", "extractive"] = "cited"

    # Control local global (proceso unico; despliegue multiproceso requiere cola externa).
    chat_max_inflight: Annotated[int, Field(ge=1, le=250)] = 4
    inference_max_inflight: Annotated[int, Field(ge=1, le=64)] = 2
    chat_queue_max_size: Annotated[int, Field(ge=0, le=1000)] = 50
    chat_queue_timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = 180
    chat_busy_threshold_percent: Annotated[int, Field(ge=1, le=100)] = 90
    upload_max_inflight: Annotated[int, Field(ge=1, le=16)] = 1
    upload_max_total_bytes: Annotated[int, Field(ge=1024)] = 134_217_728
    upload_max_documents_per_user: Annotated[int, Field(ge=1)] = 50
    upload_body_max_bytes: Annotated[int, Field(ge=1024)] = 30_000_000
    extraction_timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 30
    extraction_memory_mb: Annotated[int, Field(ge=128, le=4096)] = 512
    extraction_max_chars: Annotated[int, Field(ge=1000)] = 2_000_000

    # ---------------------------------------------------------------- rag ---
    rag_vector_store: Literal["qdrant"] = "qdrant"
    rag_chunk_size_tokens: Annotated[int, Field(ge=64, le=8000)] = 900
    rag_chunk_overlap_tokens: Annotated[int, Field(ge=0, le=4000)] = 120
    rag_embedding_dimension: Annotated[int, Field(ge=1)] = 768
    rag_top_k: Annotated[int, Field(ge=1, le=50)] = 6
    rag_fetch_k: Annotated[int, Field(ge=1, le=500)] = 24
    rag_min_similarity: Annotated[float, Field(ge=0.0, le=1.0)] = 0.35
    rag_mmr_lambda: Annotated[float, Field(ge=0.0, le=1.0)] = 0.65
    rag_search_strategy: str = "cosine_hnsw_metadata_filter_mmr"
    rag_reindex_interval_hours: Annotated[int, Field(ge=1, le=168)] = 24
    rag_knowledge_root: Path = Path("./data/knowledge")
    rag_collection_corporate: str = "matrix_rh_corporate"
    rag_collection_private: str = "matrix_rh_private"
    #: Un resumen de adjuntos recupera por ACL/metadata, no por similitud. El
    #: primer limite acota el scroll y el segundo el material entregado al LLM.
    rag_summary_scan_max_chunks: Annotated[int, Field(ge=1, le=2000)] = 256
    rag_summary_max_chunks: Annotated[int, Field(ge=1, le=128)] = 24

    # --------------------------------------------------------------- auth ---
    auth_provider: AuthProvider = AuthProvider.LOCAL_TEST
    local_test_auth_enabled: bool = True
    local_test_seed_users_enabled: bool = True
    local_test_seed_profile: str = "matrix_demo"
    matrix_seed_password: SecretStr = SecretStr("")
    session_cookie_name: str = "matrixrh_session"
    session_ttl_minutes: Annotated[int, Field(ge=1, le=10080)] = 480
    session_idle_minutes: Annotated[int, Field(ge=1, le=10080)] = 30
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    # -------------------------------------------------------------- entra ---
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: SecretStr = SecretStr("")
    entra_redirect_uri: str = ""
    entra_post_logout_redirect_uri: str = ""
    entra_allowed_groups: str = ""
    entra_role_mapping_file: Path = Path("./config/authorization/entra-role-mapping.yaml")

    # OIDC interno: Keycloak/AD FS. Entra conserva sus variables y adapter.
    oidc_issuer: str = ""
    oidc_authorization_endpoint: str = ""
    oidc_token_endpoint: str = ""
    oidc_jwks_uri: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_redirect_uri: str = ""
    oidc_allowed_groups: str = ""

    # ----------------------------------------------------------- database ---
    database_url: SecretStr = SecretStr("mysql+pymysql://root:@127.0.0.1:3306/matrix_rh_131?charset=utf8mb4")
    database_pool_size: Annotated[int, Field(ge=1, le=50)] = 5
    database_echo: bool = False
    #: Ruta opcional del datadir local. Preflight consulta @@datadir si falta.
    database_datadir_path: Path | None = None

    # Evidencia operativa; no activa cifrado ni sustituye BitLocker medible.
    storage_encryption_attested: bool = False
    database_encryption_attested: bool = False
    backup_encryption_attested: bool = False
    encryption_attestation_reference: Path | None = None

    # --------------------------------------------------------- structured ---
    structured_sources_config: Path = Path("./config/data_sources/sources.yaml")
    structured_query_timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 15
    structured_max_rows: Annotated[int, Field(ge=1, le=10000)] = 200

    # ------------------------------------------------------------- qdrant ---
    qdrant_mode: QdrantMode = QdrantMode.EMBEDDED
    qdrant_path: Path = Path("./var/qdrant")
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: SecretStr = SecretStr("")

    # ------------------------------------------------------ authorization ---
    authorization_policy_file: Path = Path("./config/authorization/categories.yaml")

    # ------------------------------------------------------------ uploads ---
    upload_max_bytes: Annotated[int, Field(ge=1024)] = 26_214_400
    upload_max_files_per_request: Annotated[int, Field(ge=1, le=50)] = 5
    upload_storage_root: Path = Path("./var/uploads")
    upload_allowed_extensions: str = ".docx,.md,.pdf,.txt,.xlsx,.csv"

    # -------------------------------------------------------- rate limits ---
    rate_limit_login_per_minute: Annotated[int, Field(ge=1, le=1000)] = 5
    rate_limit_chat_per_minute: Annotated[int, Field(ge=1, le=10000)] = 30

    # ---------------------------------------------------------- scheduler ---
    scheduler_enabled: bool = True
    retention_enabled: bool = False
    retention_conversation_days: Annotated[int, Field(ge=1)] | None = None
    retention_operation_days: Annotated[int, Field(ge=1)] | None = None
    retention_audit_days: Annotated[int, Field(ge=1)] | None = None
    retention_batch_size: Annotated[int, Field(ge=1, le=1000)] = 100

    # ------------------------------------------------------------ windows ---
    matrix_install_mode: str = "windows_local"
    matrix_use_wamp_mysql: bool = True
    matrix_external_connectors_required: bool = False
    #: Permite usar una base que YA existia antes de que Matrix RH la tocara.
    #: Por defecto False: una base preexistente puede pertenecer a otra
    #: aplicacion aunque en este momento este vacia, y apropiarse de ella en
    #: silencio es un riesgo que no se asume. El instalador prefiere crear una
    #: base nueva con un nombre libre antes que activar esta opcion.
    matrix_adopt_existing_database: bool = False

    # ------------------------------------------------------------------------
    # Validaciones de campo
    # ------------------------------------------------------------------------
    @field_validator("app_host")
    @classmethod
    def _validate_app_host(cls, value: str) -> str:
        if not value or value != value.strip() or any(char in value for char in "/\\[]?#@"):
            raise ValueError("APP_HOST debe ser un hostname o direccion literal sin URL ni corchetes.")
        return value

    @field_validator("forwarded_allow_ips")
    @classmethod
    def _validate_proxy_allowlist(cls, value: str) -> str:
        entries = [entry.strip() for entry in value.split(",") if entry.strip()]
        for entry in entries:
            try:
                network = ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError("FORWARDED_ALLOW_IPS requiere direcciones IP o CIDR concretos.") from exc
            if network.prefixlen == 0:
                raise ValueError("FORWARDED_ALLOW_IPS no puede confiar en todas las direcciones.")
        return ",".join(dict.fromkeys(entries))

    @field_validator("llm_fast_digest", "llm_deep_digest", "llm_embedding_digest")
    @classmethod
    def _validate_model_digest(cls, value: str) -> str:
        if not value:
            return ""
        value = value.removeprefix("sha256:").lower()
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("Los digests de modelo deben ser SHA-256 de 64 digitos hexadecimales.")
        return value

    @field_validator(
        "database_datadir_path", "encryption_attestation_reference",
        "retention_conversation_days", "retention_operation_days", "retention_audit_days",
        mode="before",
    )
    @classmethod
    def _empty_optional_value(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("app_log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"APP_LOG_LEVEL invalido: {value}")
        return upper

    @field_validator("ollama_base_url", "qdrant_url", "app_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    # ------------------------------------------------------------------------
    # Invariantes de modelo (fallan el arranque)
    # ------------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_rag_invariants(self) -> Settings:
        """Seccion 9: relaciones entre parametros del RAG."""
        if self.rag_chunk_size_tokens <= self.rag_chunk_overlap_tokens:
            raise ValueError(
                "RAG_CHUNK_SIZE_TOKENS debe ser estrictamente mayor que RAG_CHUNK_OVERLAP_TOKENS"
            )
        if self.rag_fetch_k < self.rag_top_k:
            raise ValueError("RAG_FETCH_K debe ser mayor o igual que RAG_TOP_K")
        if self.rag_embedding_dimension != self.ollama_embedding_dimension:
            raise ValueError(
                "RAG_EMBEDDING_DIMENSION y OLLAMA_EMBEDDING_DIMENSION deben coincidir"
            )
        if self.rag_summary_scan_max_chunks < self.rag_summary_max_chunks:
            raise ValueError(
                "RAG_SUMMARY_SCAN_MAX_CHUNKS debe ser mayor o igual que RAG_SUMMARY_MAX_CHUNKS"
            )
        return self

    @model_validator(mode="after")
    def _validate_environment_safety(self) -> Settings:
        """Requisitos 32 y 58: el modo local jamas puede vivir en produccion."""
        is_prod_like = self.app_env in (AppEnv.PRODUCTION, AppEnv.STAGING)

        if is_prod_like:
            if self.local_test_auth_enabled:
                raise ValueError(
                    f"LOCAL_TEST_AUTH_ENABLED=true es incompatible con APP_ENV={self.app_env}. "
                    "El proveedor local de pruebas debe estar deshabilitado."
                )
            if self.auth_provider is AuthProvider.LOCAL_TEST:
                raise ValueError(
                    f"AUTH_PROVIDER=local_test es incompatible con APP_ENV={self.app_env}. "
                    "Use AUTH_PROVIDER=entra u oidc (SSO interno)."
                )
            if self.local_test_seed_users_enabled:
                raise ValueError(f"LOCAL_TEST_SEED_USERS_ENABLED=true es incompatible con APP_ENV={self.app_env}.")
            if not self.session_cookie_secure:
                raise ValueError("SESSION_COOKIE_SECURE debe ser true en staging/production.")

        if is_prod_like and not self.app_secret_key.get_secret_value():
            raise ValueError("APP_SECRET_KEY es obligatoria fuera de development/test.")

        if self.auth_provider is AuthProvider.LOCAL_TEST and not self.local_test_auth_enabled:
            raise ValueError("AUTH_PROVIDER=local_test requiere LOCAL_TEST_AUTH_ENABLED=true.")

        if self.llm_provider != self.llm_deep_provider and self.ollama_fast_model == self.ollama_deep_model:
            raise ValueError("Modelos fast/deep de proveedores distintos requieren identificadores distintos.")

        if self.auth_provider is AuthProvider.OIDC:
            from urllib.parse import urlparse

            for field in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri", "redirect_uri"):
                value = getattr(self, f"oidc_{field}")
                parsed = urlparse(value)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.fragment:
                    raise ValueError(f"OIDC_{field.upper()} requiere una URL HTTPS configurada por TI.")
            if not self.oidc_client_id or not self.oidc_client_secret.get_secret_value():
                raise ValueError("OIDC_CLIENT_ID y OIDC_CLIENT_SECRET son obligatorios.")

        if self.auth_provider is AuthProvider.ENTRA:
            faltantes = [
                name
                for name, value in (
                    ("ENTRA_TENANT_ID", self.entra_tenant_id),
                    ("ENTRA_CLIENT_ID", self.entra_client_id),
                    ("ENTRA_REDIRECT_URI", self.entra_redirect_uri),
                )
                if not value or value == "replace_me"
            ]
            if self.is_production and self.entra_client_secret.get_secret_value() in ("", "replace_me"):
                faltantes.append("ENTRA_CLIENT_SECRET (cliente Web confidencial)")
            if faltantes:
                raise ValueError("AUTH_PROVIDER=entra requiere configurar: " + ", ".join(faltantes))
        return self

    @model_validator(mode="after")
    def _validate_network_exposure(self) -> Settings:
        """Una identidad local de pruebas nunca sirve a una interfaz de red."""
        exposed = not _is_loopback_host(self.app_host)
        base_host = urlparse(self.app_base_url).hostname
        if base_host is not None and not _is_loopback_host(base_host):
            exposed = True  # incluye el caso de un proxy delante del loopback.
        test_identity = (
            self.auth_provider is AuthProvider.LOCAL_TEST
            or self.local_test_auth_enabled
            or self.local_test_seed_users_enabled
        )
        if exposed and test_identity:
            raise ValueError(
                "APP_HOST/APP_BASE_URL expone Matrix RH en red con identidad o seed de pruebas. "
                "Use loopback o AUTH_PROVIDER=oidc/entra con LOCAL_TEST_* en false."
            )
        if exposed or self.app_env in (AppEnv.STAGING, AppEnv.PRODUCTION):
            database = urlparse(self.database_url.get_secret_value())
            username = unquote(database.username or "").strip().casefold()
            password = unquote(database.password or "")
            if not username or username in {"root", "postgres", "sa", "sys", "system"} or not password.strip():
                raise ValueError(
                    "DATABASE_URL requiere un usuario dedicado con contrasena "
                    "cuando el servicio se expone en red o en staging/production."
                )
        return self

    @model_validator(mode="after")
    def _validate_revision_and_retention(self) -> Settings:
        if self.app_env in (AppEnv.STAGING, AppEnv.PRODUCTION):
            if not self.llm_local_only or "vertex" in (self.llm_provider, self.llm_deep_provider):
                raise ValueError("staging/production requiere inferencia totalmente local.")
            if not self.llm_fast_digest or not self.llm_deep_digest:
                raise ValueError("LLM_FAST_DIGEST y LLM_DEEP_DIGEST son obligatorios en staging/production.")
            if not (
                self.storage_encryption_attested
                and self.database_encryption_attested
                and self.backup_encryption_attested
                and self.encryption_attestation_reference is not None
            ):
                raise ValueError(
                    "staging/production requiere STORAGE_ENCRYPTION_ATTESTED, "
                    "DATABASE_ENCRYPTION_ATTESTED, BACKUP_ENCRYPTION_ATTESTED y "
                    "ENCRYPTION_ATTESTATION_REFERENCE de TI; preflight verifica la evidencia."
                )
        if (
            self.llm_provider == self.llm_deep_provider
            and self.ollama_fast_model == self.ollama_deep_model
            and self.llm_fast_digest and self.llm_deep_digest
            and self.llm_fast_digest != self.llm_deep_digest
        ):
            raise ValueError("Un mismo modelo/proveedor fast y deep requiere el mismo digest.")
        if self.retention_enabled and all(
            value is None for value in (
                self.retention_conversation_days, self.retention_operation_days, self.retention_audit_days,
            )
        ):
            raise ValueError("RETENTION_ENABLED requiere al menos un plazo acordado por RH/Normatividad.")
        return self

    @model_validator(mode="after")
    def _autogenerate_dev_secret(self) -> Settings:
        """En development/test se genera una clave efimera para no obligar a
        escribir un secreto en el .env de un entorno de pruebas."""
        if not self.app_secret_key.get_secret_value():
            object.__setattr__(self, "app_secret_key", SecretStr(secrets.token_hex(32)))
        return self

    # ------------------------------------------------------------------------
    # Propiedades derivadas
    # ------------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def is_local_auth_allowed(self) -> bool:
        """El proveedor local solo existe en development/test."""
        return self.app_env in (AppEnv.DEVELOPMENT, AppEnv.TEST) and self.local_test_auth_enabled

    @property
    def knowledge_root_path(self) -> Path:
        return _resolve(self.rag_knowledge_root)

    @property
    def upload_storage_path(self) -> Path:
        return _resolve(self.upload_storage_root)

    @property
    def qdrant_storage_path(self) -> Path:
        return _resolve(self.qdrant_path)

    @property
    def authorization_policy_path(self) -> Path:
        return _resolve(self.authorization_policy_file)

    @property
    def structured_sources_path(self) -> Path:
        return _resolve(self.structured_sources_config)

    @property
    def entra_role_mapping_path(self) -> Path:
        return _resolve(self.entra_role_mapping_file)

    @property
    def allowed_upload_extensions(self) -> frozenset[str]:
        return frozenset(
            ext.strip().lower()
            for ext in self.upload_allowed_extensions.split(",")
            if ext.strip()
        )

    @property
    def entra_allowed_group_list(self) -> tuple[str, ...]:
        return tuple(g.strip() for g in self.entra_allowed_groups.split(",") if g.strip())

    def rag_parameters_snapshot(self) -> dict[str, object]:
        """Vista auditable de los parametros del RAG (seccion 9 y 24).

        La usa el endpoint administrativo ``/api/v1/admin/diagnostics`` y la
        documentacion generada, de modo que documentacion y codigo no puedan
        divergir.
        """
        return {
            "RAG_VECTOR_STORE": self.rag_vector_store,
            "RAG_CHUNK_SIZE_TOKENS": self.rag_chunk_size_tokens,
            "RAG_CHUNK_OVERLAP_TOKENS": self.rag_chunk_overlap_tokens,
            "RAG_EMBEDDING_DIMENSION": self.rag_embedding_dimension,
            "RAG_TOP_K": self.rag_top_k,
            "RAG_FETCH_K": self.rag_fetch_k,
            "RAG_MIN_SIMILARITY": self.rag_min_similarity,
            "RAG_MMR_LAMBDA": self.rag_mmr_lambda,
            "RAG_SEARCH_STRATEGY": self.rag_search_strategy,
            "RAG_REINDEX_INTERVAL_HOURS": self.rag_reindex_interval_hours,
            "OLLAMA_FAST_MODEL": self.ollama_fast_model,
            "OLLAMA_DEEP_MODEL": self.ollama_deep_model,
            "OLLAMA_EMBEDDING_MODEL": self.ollama_embedding_model,
            "OLLAMA_FAST_NUM_CTX": self.ollama_fast_num_ctx,
            "OLLAMA_DEEP_NUM_CTX": self.ollama_deep_num_ctx,
            "OLLAMA_FAST_MAX_TOKENS": self.ollama_fast_max_tokens,
            "OLLAMA_DEEP_MAX_TOKENS": self.ollama_deep_max_tokens,
            "OLLAMA_SUMMARY_PARALLEL_BATCHES": self.ollama_summary_parallel_batches,
            "OLLAMA_KEEP_ALIVE": self.ollama_keep_alive,
            "LLM_FAST_THINKING": self.llm_fast_thinking,
            "LLM_DEEP_THINKING": self.llm_deep_thinking,
            "LLM_STRUCTURED_THINKING": self.llm_structured_thinking,
            "LLM_COMPLETION_RETRIES": self.llm_completion_retries,
            "LLM_FAST_TOP_K": self.llm_fast_top_k,
            "LLM_DEEP_TOP_K": self.llm_deep_top_k,
            "LLM_FAST_TIMEOUT_SECONDS": self.llm_fast_timeout_seconds,
            "LLM_DEEP_TIMEOUT_SECONDS": self.llm_deep_timeout_seconds,
            "LLM_REQUEST_DEADLINE_SECONDS": self.llm_request_deadline_seconds,
            "ANSWER_ALLOW_GENERAL_KNOWLEDGE": self.answer_allow_general_knowledge,
            "ANSWER_EVIDENCE_MODE": self.answer_evidence_mode,
            "OLLAMA_EMBEDDING_CACHE_SIZE": self.ollama_embedding_cache_size,
            "RAG_SUMMARY_SCAN_MAX_CHUNKS": self.rag_summary_scan_max_chunks,
            "RAG_SUMMARY_MAX_CHUNKS": self.rag_summary_max_chunks,
            "source_of_truth": "backend/app/config/settings.py::Settings",
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Instancia unica de configuracion.

    Se envuelve el ``ValidationError`` de pydantic en ``ConfigurationError`` para
    que el mensaje que llega al operador sea accionable y no un volcado interno.
    """
    try:
        return Settings()
    except ValidationError as exc:
        # Pydantic incluye por defecto todo el input del model_validator. El
        # diagnostico nunca debe imprimir DSN, claves de sesion ni seed secrets.
        detail = "; ".join(
            error["msg"] for error in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        raise ConfigurationError("La configuracion de Matrix RH es invalida.", detail=detail) from None
    except Exception as exc:  # noqa: BLE001 - se re-lanza como error tipado
        raise ConfigurationError(
            "La configuracion de Matrix RH es invalida.", detail=type(exc).__name__
        ) from None


def reload_settings() -> Settings:
    """Reinicia la cache de configuracion (usado por pruebas y por preflight)."""
    get_settings.cache_clear()
    return get_settings()
