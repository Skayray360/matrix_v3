-- Creado por Aldo Garcia.
-- Migracion 0001: esquema inicial de la base interna de Matrix RH.
--
-- Reglas aplicadas:
--   * Claves primarias opacas CHAR(36) (UUID4): no revelan orden ni volumen.
--   * Ninguna tabla almacena contrasenas en claro; solo hash Argon2id.
--   * Ninguna tabla almacena DSN ni secretos de conexion: se guarda la
--     referencia logica a la variable de entorno que los contiene.
--   * Todas las sentencias son IF NOT EXISTS para que la migracion sea
--     idempotente y se pueda reejecutar desde el instalador.

CREATE TABLE IF NOT EXISTS users (
    id                CHAR(36)     NOT NULL,
    username          VARCHAR(128) NOT NULL,
    display_name      VARCHAR(256) NOT NULL,
    email             VARCHAR(256) NULL,
    auth_source       VARCHAR(32)  NOT NULL,
    is_active         TINYINT(1)   NOT NULL DEFAULT 1,
    is_synthetic_test TINYINT(1)   NOT NULL DEFAULT 0,
    created_at        DATETIME(6)  NOT NULL,
    updated_at        DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_username (username),
    KEY ix_users_auth_source (auth_source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Credenciales locales: EXCLUSIVAMENTE para development/test.
-- password_hash_argon2id contiene el hash codificado PHC de Argon2id.
CREATE TABLE IF NOT EXISTS local_credentials (
    user_id                CHAR(36)     NOT NULL,
    password_hash_argon2id VARCHAR(255) NOT NULL,
    failed_attempts        INT          NOT NULL DEFAULT 0,
    locked_until           DATETIME(6)  NULL,
    updated_at             DATETIME(6)  NOT NULL,
    PRIMARY KEY (user_id),
    CONSTRAINT fk_local_credentials_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Vincula un subject de un proveedor (local_test o entra) con el usuario logico.
-- Permite activar Entra ID sin reescribir roles ni politicas.
CREATE TABLE IF NOT EXISTS identity_links (
    id            CHAR(36)     NOT NULL,
    user_id       CHAR(36)     NOT NULL,
    provider      VARCHAR(32)  NOT NULL,
    subject_id    VARCHAR(255) NOT NULL,
    tenant_id     VARCHAR(255) NULL,
    created_at    DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_identity_links_provider_subject (provider, subject_id),
    KEY ix_identity_links_user (user_id),
    CONSTRAINT fk_identity_links_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS roles (
    id           CHAR(36)     NOT NULL,
    name         VARCHAR(128) NOT NULL,
    description  VARCHAR(512) NOT NULL DEFAULT '',
    is_test_role TINYINT(1)   NOT NULL DEFAULT 0,
    created_at   DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_roles_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS permissions (
    id          CHAR(36)     NOT NULL,
    name        VARCHAR(128) NOT NULL,
    description VARCHAR(512) NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    UNIQUE KEY uq_permissions_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id       CHAR(36) NOT NULL,
    permission_id CHAR(36) NOT NULL,
    PRIMARY KEY (role_id, permission_id),
    CONSTRAINT fk_role_permissions_role FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE,
    CONSTRAINT fk_role_permissions_permission FOREIGN KEY (permission_id) REFERENCES permissions (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_roles (
    user_id CHAR(36) NOT NULL,
    role_id CHAR(36) NOT NULL,
    granted_at DATETIME(6) NOT NULL,
    PRIMARY KEY (user_id, role_id),
    CONSTRAINT fk_user_roles_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_user_roles_role FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Mapeo generico proveedor -> rol interno (grupos o app roles de Entra ID).
CREATE TABLE IF NOT EXISTS entra_group_role_mappings (
    id            CHAR(36)     NOT NULL,
    provider      VARCHAR(32)  NOT NULL DEFAULT 'entra',
    external_key  VARCHAR(255) NOT NULL,
    external_kind VARCHAR(32)  NOT NULL DEFAULT 'group',
    role_id       CHAR(36)     NOT NULL,
    created_at    DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_group_role (provider, external_kind, external_key, role_id),
    CONSTRAINT fk_group_role_role FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Politica de acceso a categorias documentales. Deny-by-default: la ausencia de
-- fila NO concede acceso. is_wildcard cubre categorias futuras de negocio.
CREATE TABLE IF NOT EXISTS category_permissions (
    id          CHAR(36)     NOT NULL,
    role_id     CHAR(36)     NOT NULL,
    category    VARCHAR(128) NOT NULL,
    access      VARCHAR(16)  NOT NULL DEFAULT 'read',
    is_wildcard TINYINT(1)   NOT NULL DEFAULT 0,
    created_at  DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_category_permissions (role_id, category, access),
    KEY ix_category_permissions_category (category),
    CONSTRAINT fk_category_permissions_role FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS structured_source_permissions (
    id               CHAR(36)     NOT NULL,
    role_id          CHAR(36)     NOT NULL,
    source_name      VARCHAR(128) NOT NULL,
    allowed_entities JSON         NULL,
    row_filter       VARCHAR(512) NULL,
    created_at       DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_structured_source_permissions (role_id, source_name),
    CONSTRAINT fk_structured_source_permissions_role FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Politicas genericas adicionales (ABAC ligero) evaluadas por el policy engine.
CREATE TABLE IF NOT EXISTS authorization_policies (
    id            CHAR(36)     NOT NULL,
    subject_kind  VARCHAR(32)  NOT NULL,
    subject_key   VARCHAR(128) NOT NULL,
    resource_kind VARCHAR(64)  NOT NULL,
    resource_key  VARCHAR(255) NOT NULL,
    effect        VARCHAR(8)   NOT NULL,
    conditions    JSON         NULL,
    created_at    DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    KEY ix_authorization_policies_lookup (resource_kind, resource_key, subject_kind, subject_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Sesiones de servidor. El navegador solo recibe un identificador opaco.
CREATE TABLE IF NOT EXISTS sessions (
    id             CHAR(36)     NOT NULL,
    session_token_hash CHAR(64) NOT NULL,
    user_id        CHAR(36)     NOT NULL,
    csrf_token     VARCHAR(128) NOT NULL,
    auth_source    VARCHAR(32)  NOT NULL,
    created_at     DATETIME(6)  NOT NULL,
    last_seen_at   DATETIME(6)  NOT NULL,
    expires_at     DATETIME(6)  NOT NULL,
    revoked_at     DATETIME(6)  NULL,
    client_fingerprint CHAR(64) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_sessions_token (session_token_hash),
    KEY ix_sessions_user (user_id),
    KEY ix_sessions_expires (expires_at),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Estado transitorio del flujo OIDC (state + nonce + PKCE verifier).
CREATE TABLE IF NOT EXISTS oidc_login_states (
    id             CHAR(36)     NOT NULL,
    state          VARCHAR(128) NOT NULL,
    nonce          VARCHAR(128) NOT NULL,
    code_verifier  VARCHAR(256) NOT NULL,
    redirect_after VARCHAR(512) NULL,
    created_at     DATETIME(6)  NOT NULL,
    expires_at     DATETIME(6)  NOT NULL,
    consumed_at    DATETIME(6)  NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_oidc_state (state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS conversations (
    id         CHAR(36)     NOT NULL,
    user_id    CHAR(36)     NOT NULL,
    title      VARCHAR(255) NOT NULL,
    created_at DATETIME(6)  NOT NULL,
    updated_at DATETIME(6)  NOT NULL,
    deleted_at DATETIME(6)  NULL,
    PRIMARY KEY (id),
    KEY ix_conversations_owner (user_id, deleted_at, updated_at),
    CONSTRAINT fk_conversations_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS conversation_messages (
    id              CHAR(36)    NOT NULL,
    conversation_id CHAR(36)    NOT NULL,
    user_id         CHAR(36)    NOT NULL,
    role            VARCHAR(16) NOT NULL,
    content         MEDIUMTEXT  NOT NULL,
    model           VARCHAR(64) NULL,
    intent          VARCHAR(64) NULL,
    source_ids      JSON        NULL,
    authorized_categories JSON  NULL,
    created_at      DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY ix_messages_conversation (conversation_id, created_at),
    CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id                CHAR(36)    NOT NULL,
    conversation_id   CHAR(36)    NOT NULL,
    summary           TEXT        NOT NULL,
    covered_until_message_id CHAR(36) NULL,
    message_count     INT         NOT NULL DEFAULT 0,
    created_at        DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY ix_summaries_conversation (conversation_id, created_at),
    CONSTRAINT fk_summaries_conversation FOREIGN KEY (conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS documents (
    id              CHAR(36)     NOT NULL,
    scope           VARCHAR(16)  NOT NULL,
    category        VARCHAR(128) NULL,
    owner_user_id   CHAR(36)     NULL,
    conversation_id CHAR(36)     NULL,
    filename        VARCHAR(255) NOT NULL,
    relative_path   VARCHAR(1024) NULL,
    storage_path    VARCHAR(1024) NULL,
    mime_type       VARCHAR(128) NOT NULL,
    size_bytes      BIGINT       NOT NULL DEFAULT 0,
    sha256          CHAR(64)     NOT NULL,
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
    chunk_count     INT          NOT NULL DEFAULT 0,
    error_message   VARCHAR(512) NULL,
    ingestion_version VARCHAR(32) NOT NULL DEFAULT '1',
    created_at      DATETIME(6)  NOT NULL,
    updated_at      DATETIME(6)  NOT NULL,
    deleted_at      DATETIME(6)  NULL,
    PRIMARY KEY (id),
    KEY ix_documents_sha (sha256),
    KEY ix_documents_category (category, deleted_at),
    KEY ix_documents_conversation (conversation_id),
    KEY ix_documents_scope_path (scope, relative_path(255))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS document_versions (
    id          CHAR(36)    NOT NULL,
    document_id CHAR(36)    NOT NULL,
    sha256      CHAR(64)    NOT NULL,
    chunk_count INT         NOT NULL DEFAULT 0,
    ingested_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY ix_document_versions_document (document_id, ingested_at),
    CONSTRAINT fk_document_versions_document FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS document_access_policies (
    id           CHAR(36)    NOT NULL,
    document_id  CHAR(36)    NOT NULL,
    allowed_roles JSON       NULL,
    allowed_groups JSON      NULL,
    sensitivity  VARCHAR(32) NOT NULL DEFAULT 'internal',
    source_owner VARCHAR(128) NOT NULL DEFAULT 'rh',
    created_at   DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_document_access_policies_document (document_id),
    CONSTRAINT fk_document_access_policies_document FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id          CHAR(36)    NOT NULL,
    job_type    VARCHAR(32) NOT NULL,
    status      VARCHAR(16) NOT NULL,
    -- 'trigger' es palabra reservada en MySQL: se usa trigger_source.
    trigger_source VARCHAR(32) NOT NULL DEFAULT 'manual',
    started_at  DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    stats       JSON        NULL,
    error_message VARCHAR(1024) NULL,
    PRIMARY KEY (id),
    KEY ix_ingestion_jobs_status (status, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Lock cooperativo para impedir dos reconciliaciones simultaneas.
CREATE TABLE IF NOT EXISTS job_locks (
    lock_name  VARCHAR(64)  NOT NULL,
    locked_by  VARCHAR(128) NOT NULL,
    locked_at  DATETIME(6)  NOT NULL,
    expires_at DATETIME(6)  NOT NULL,
    PRIMARY KEY (lock_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Fuentes estructuradas. secret_ref es el NOMBRE de la variable de entorno que
-- contiene el DSN, nunca el DSN.
CREATE TABLE IF NOT EXISTS structured_data_sources (
    id          CHAR(36)     NOT NULL,
    name        VARCHAR(128) NOT NULL,
    engine      VARCHAR(32)  NOT NULL,
    status      VARCHAR(32)  NOT NULL DEFAULT 'PREPARED_NOT_CONNECTED',
    secret_ref  VARCHAR(128) NOT NULL,
    description VARCHAR(512) NOT NULL DEFAULT '',
    enabled     TINYINT(1)   NOT NULL DEFAULT 0,
    last_checked_at DATETIME(6) NULL,
    created_at  DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_structured_data_sources_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS structured_source_policies (
    id           CHAR(36)     NOT NULL,
    source_name  VARCHAR(128) NOT NULL,
    entity       VARCHAR(128) NOT NULL,
    allowed_columns JSON      NOT NULL,
    required_filters JSON     NULL,
    max_rows     INT          NOT NULL DEFAULT 200,
    created_at   DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_structured_source_policies (source_name, entity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS audit_events (
    id              CHAR(36)     NOT NULL,
    request_id      VARCHAR(64)  NOT NULL,
    event_type      VARCHAR(64)  NOT NULL,
    user_opaque_id  CHAR(36)     NULL,
    role_set_hash   VARCHAR(64)  NULL,
    conversation_id CHAR(36)     NULL,
    intent          VARCHAR(64)  NULL,
    selected_model  VARCHAR(64)  NULL,
    selected_tools  JSON         NULL,
    source_ids      JSON         NULL,
    authorization_decision VARCHAR(16) NULL,
    resource        VARCHAR(255) NULL,
    latency_ms      INT          NULL,
    status          VARCHAR(16)  NOT NULL DEFAULT 'ok',
    error_code      VARCHAR(64)  NULL,
    created_at      DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    KEY ix_audit_request (request_id),
    KEY ix_audit_user_time (user_opaque_id, created_at),
    KEY ix_audit_type_time (event_type, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
