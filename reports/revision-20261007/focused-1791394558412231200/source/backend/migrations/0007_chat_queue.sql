-- Creado por Aldo Garcia.
-- Payload temporal: se elimina en todo estado terminal. Las filas anteriores
-- permanecen validas con NULL y nunca se reejecutan al reiniciar.
ALTER TABLE chat_operations ADD COLUMN message TEXT NULL;
ALTER TABLE chat_operations ADD COLUMN session_id VARCHAR(36) NULL;
ALTER TABLE chat_operations ADD COLUMN request_id VARCHAR(128) NULL;
ALTER TABLE chat_operations ADD COLUMN started_at DATETIME NULL;
CREATE INDEX ix_chat_operations_status_created ON chat_operations (status, created_at);
