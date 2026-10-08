-- Creado por Aldo Garcia.
-- Las columnas nulas identifican el indice legado, que exige reindexacion.
ALTER TABLE documents ADD COLUMN active_generation VARCHAR(36) NULL;
ALTER TABLE documents ADD COLUMN index_fingerprint VARCHAR(64) NULL;
ALTER TABLE documents ADD COLUMN index_cleanup_pending BOOLEAN NOT NULL DEFAULT FALSE;
