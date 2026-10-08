-- Creado por Aldo Garcia.
-- Migracion 0002.
--
-- Motivo: `audit_events.status` se dimensiono a VARCHAR(16) y los estados reales
-- del orquestador son mas largos (`insufficient_evidence` = 21 caracteres). Un
-- evento de auditoria que no se puede escribir es un fallo de control, no un
-- detalle cosmetico: se amplia la columna y tambien `ingestion_jobs.status` y
-- `authorization_decision`, que sufren el mismo riesgo al crecer el vocabulario.

ALTER TABLE audit_events MODIFY COLUMN status VARCHAR(48) NOT NULL DEFAULT 'ok';
ALTER TABLE audit_events MODIFY COLUMN authorization_decision VARCHAR(32) NULL;
ALTER TABLE ingestion_jobs MODIFY COLUMN status VARCHAR(32) NOT NULL;
