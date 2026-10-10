-- Creado por Aldo Garcia.
-- Memoria de la consulta efectiva formada solo desde preguntas del usuario.
-- No reescribe turnos anteriores ni genera antecedentes para filas historicas.
ALTER TABLE conversation_messages ADD COLUMN context_query MEDIUMTEXT NULL;
