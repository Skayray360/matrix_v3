-- Creado por Aldo Garcia.
-- Metadatos de procedencia y fallos seguros. Conserva mensajes y usuarios.
ALTER TABLE conversation_messages ADD COLUMN answer_basis VARCHAR(16) NULL;
ALTER TABLE chat_operations ADD COLUMN error_code VARCHAR(32) NULL;
