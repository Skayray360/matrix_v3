# Creado por Aldo Garcia.
"""Modelo ORM de la base interna de Matrix RH.

Estos modelos reflejan exactamente el DDL de ``backend/migrations``. La prueba
``tests/integration/test_schema_matches_models.py`` compara ambos para impedir
que la migracion y el ORM se desincronicen con el tiempo.

Ninguna tabla guarda secretos: las credenciales locales guardan unicamente un
hash Argon2id y las fuentes externas guardan el *nombre* de la variable de
entorno que contiene el DSN.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.common.answers import AnswerBasis

# Todas las columnas DATETIME almacenan UTC sin tzinfo (ver utcnow_naive).
from app.common.ids import new_id
from app.common.ids import utcnow_naive as utcnow


class Base(DeclarativeBase):
    """Base declarativa comun."""


def _uuid_pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_id)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #: ``local_test`` o ``entra``. Determina el adapter que autentico al usuario.
    auth_source: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Marca las cuentas sinteticas de prueba para poder deshabilitarlas en bloque.
    is_synthetic_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, onupdate=utcnow, nullable=False
    )


class LocalCredential(Base):
    """Credencial local de desarrollo/test. Solo hash Argon2id."""

    __tablename__ = "local_credentials"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    password_hash_argon2id: Mapped[str] = mapped_column(String(255), nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, onupdate=utcnow, nullable=False
    )


class IdentityLink(Base):
    """Vincula el subject de un proveedor de identidad con el usuario logico."""

    __tablename__ = "identity_links"

    id: Mapped[str] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[str] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    is_test_role: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[str] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(512), default="", nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[str] = mapped_column(String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[str] = mapped_column(String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class EntraGroupRoleMapping(Base):
    """Mapeo grupo/app-role externo -> rol interno de Matrix RH."""

    __tablename__ = "entra_group_role_mappings"

    id: Mapped[str] = _uuid_pk()
    provider: Mapped[str] = mapped_column(String(32), default="entra", nullable=False)
    external_key: Mapped[str] = mapped_column(String(255), nullable=False)
    external_kind: Mapped[str] = mapped_column(String(32), default="group", nullable=False)
    role_id: Mapped[str] = mapped_column(String(36), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class CategoryPermission(Base):
    """Permiso de lectura sobre una categoria documental.

    ``is_wildcard`` concede las categorias de negocio presentes y futuras. Se
    implementa en la capa de politicas y NO eliminando el filtro de Qdrant.
    """

    __tablename__ = "category_permissions"

    id: Mapped[str] = _uuid_pk()
    role_id: Mapped[str] = mapped_column(String(36), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    access: Mapped[str] = mapped_column(String(16), default="read", nullable=False)
    is_wildcard: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class StructuredSourcePermission(Base):
    __tablename__ = "structured_source_permissions"

    id: Mapped[str] = _uuid_pk()
    role_id: Mapped[str] = mapped_column(String(36), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    allowed_entities: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    row_filter: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class AuthorizationPolicy(Base):
    """Politica ABAC ligera adicional al RBAC."""

    __tablename__ = "authorization_policies"

    id: Mapped[str] = _uuid_pk()
    subject_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(255), nullable=False)
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    conditions: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class SessionRecord(Base):
    """Sesion de servidor. El navegador nunca ve tokens de Entra ID."""

    __tablename__ = "sessions"

    id: Mapped[str] = _uuid_pk()
    #: SHA-256 del token de cookie: si se filtra la BD, la cookie no es reusable.
    session_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    auth_source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    client_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OidcLoginState(Base):
    """State/nonce/PKCE del flujo Authorization Code de Entra ID."""

    __tablename__ = "oidc_login_states"

    id: Mapped[str] = _uuid_pk()
    state: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(256), nullable=False)
    redirect_after: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_purge", "deleted_at", "purged_at"),)

    id: Mapped[str] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, onupdate=utcnow, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    #: Se confirma solo tras retirar vectores, bytes y metadatos privados.
    #: NULL en una baja permite reintentar tambien purgas de instalaciones viejas.
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = _uuid_pk()
    #: Secuencia monotona de insercion. El orden del hilo NO puede depender de
    #: created_at: la resolucion del reloj no garantiza marcas distintas entre
    #: dos inserciones consecutivas.
    seq: Mapped[int] = mapped_column(BigInteger, autoincrement=True, unique=True, nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answer_basis: Mapped[AnswerBasis | None] = mapped_column(String(16), nullable=True)
    authorization_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_ids: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    #: Categorias autorizadas cuando se produjo el mensaje. Permite reconstruir
    #: el contexto sin reintroducir informacion que el rol ya no puede ver.
    authorized_categories: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"

    id: Mapped[str] = _uuid_pk()
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    authorization_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    covered_until_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class ChatOperation(Base):
    """Resultado idempotente por usuario/clave; no se reenvia trabajo ambiguo."""

    __tablename__ = "chat_operations"
    __table_args__ = (Index("ix_chat_operations_status_created", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Solo mientras esta pendiente. Nunca almacenar cookie, token ni contexto RBAC.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class Document(Base):
    """Documento corporativo (``scope='corporate'``) o adjunto privado
    (``scope='conversation'``)."""

    __tablename__ = "documents"

    id: Mapped[str] = _uuid_pk()
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ingestion_version: Mapped[str] = mapped_column(String(32), default="1", nullable=False)
    active_generation: Mapped[str | None] = mapped_column(String(36), nullable=True)
    index_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    index_cleanup_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, onupdate=utcnow, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id: Mapped[str] = _uuid_pk()
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class DocumentAccessPolicy(Base):
    __tablename__ = "document_access_policies"

    id: Mapped[str] = _uuid_pk()
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    allowed_roles: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    allowed_groups: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="internal", nullable=False)
    source_owner: Mapped[str] = mapped_column(String(128), default="rh", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[str] = _uuid_pk()
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 'trigger' es palabra reservada en MySQL; la columna se llama trigger_source.
    trigger_source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    stats: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class JobLock(Base):
    __tablename__ = "job_locks"

    lock_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    locked_by: Mapped[str] = mapped_column(String(128), nullable=False)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class StructuredDataSource(Base):
    """Fuente estructurada. ``secret_ref`` es el NOMBRE de la variable de entorno."""

    __tablename__ = "structured_data_sources"

    id: Mapped[str] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PREPARED_NOT_CONNECTED", nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class StructuredSourcePolicy(Base):
    __tablename__ = "structured_source_policies"

    id: Mapped[str] = _uuid_pk()
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    entity: Mapped[str] = mapped_column(String(128), nullable=False)
    allowed_columns: Mapped[Any] = mapped_column(JSON, nullable=False)
    required_filters: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    max_rows: Mapped[int] = mapped_column(Integer, default=200, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class AuditEvent(Base):
    """Evento de auditoria. Nunca contiene prompts completos ni secretos."""

    __tablename__ = "audit_events"

    id: Mapped[str] = _uuid_pk()
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    user_opaque_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role_set_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_tools: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    source_ids: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    authorization_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource: Mapped[str | None] = mapped_column(String(255), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(48), default="ok", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)


class SchemaMigration(Base):
    """Registro de migraciones aplicadas (lo gestiona ``app.database.migrator``)."""

    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow, nullable=False)
