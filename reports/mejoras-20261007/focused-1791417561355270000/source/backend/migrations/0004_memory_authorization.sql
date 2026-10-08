-- Creado por Aldo Garcia.
-- Procedencia de memoria: NULL es legado sin alcance verificable.
ALTER TABLE conversation_messages ADD COLUMN authorization_scope VARCHAR(64) NULL;
ALTER TABLE conversation_summaries ADD COLUMN authorization_scope VARCHAR(64) NULL;
