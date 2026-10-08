-- Creado por Aldo Garcia.
-- Migracion 0003.
--
-- Motivo: el orden de los turnos de una conversacion dependia de created_at.
-- La resolucion del reloj no garantiza que dos inserciones consecutivas tengan
-- marcas distintas, asi que dos mensajes escritos en el mismo microsegundo
-- podian devolverse invertidos: el usuario veia la respuesta antes que su
-- propia pregunta, y el contexto enviado al modelo quedaba desordenado.
--
-- Solucion: una secuencia monotona por fila. AUTO_INCREMENT sobre una columna
-- no primaria requiere que sea la primera columna de un indice, de ahi la clave
-- unica.

ALTER TABLE conversation_messages
    ADD COLUMN seq BIGINT NOT NULL AUTO_INCREMENT,
    ADD UNIQUE KEY uq_conversation_messages_seq (seq);

-- Indice de lectura del hilo: por conversacion y en orden de llegada.
CREATE INDEX ix_messages_conversation_seq ON conversation_messages (conversation_id, seq);
