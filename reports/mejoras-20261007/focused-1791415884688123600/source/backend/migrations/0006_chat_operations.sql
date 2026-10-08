-- Creado por Aldo Garcia.
CREATE TABLE IF NOT EXISTS chat_operations (
 id VARCHAR(64) NOT NULL PRIMARY KEY,
 user_id VARCHAR(36) NOT NULL,
 conversation_id VARCHAR(36) NOT NULL,
 request_hash VARCHAR(64) NOT NULL,
 authorization_scope VARCHAR(64) NOT NULL,
 status VARCHAR(16) NOT NULL,
 response JSON NULL,
 created_at DATETIME NOT NULL,
 INDEX ix_chat_operations_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
