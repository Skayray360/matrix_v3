-- Creado por Aldo Garcia.
-- No modifica el historial ni elimina contenido durante la migracion.
-- Las bajas antiguas quedan pendientes de la purga explicita o del scheduler.
ALTER TABLE conversations ADD COLUMN purged_at DATETIME NULL;
CREATE INDEX ix_conversations_purge ON conversations (deleted_at, purged_at);
