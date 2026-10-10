-- Creado por Aldo Garcia.
-- Aditiva: no reescribe contenido, citas, permisos ni mensajes anteriores.
-- Aplicar mediante el runner habitual; comprobar primero en una copia aislada.
ALTER TABLE conversation_messages ADD COLUMN source_details JSON NULL;
