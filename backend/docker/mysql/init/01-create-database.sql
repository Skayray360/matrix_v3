-- Creado por Aldo Garcia.
-- Inicializacion de la base interna de Matrix RH.
--
-- Este script sólo lo ejecuta el contenedor de MySQL en su primer arranque. El
-- ESQUEMA (tablas, índices) NO se crea aquí: lo aplican las migraciones
-- versionadas de backend/migrations, para que exista un único mecanismo y un
-- único registro de versiones.
--
-- Aquí sólo se garantiza que la base exista con el juego de caracteres correcto
-- y que el usuario de aplicación tenga permisos mínimos.

CREATE DATABASE IF NOT EXISTS matrix_rh_131
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

-- El usuario de aplicación lo crea la imagen a partir de MYSQL_USER/MYSQL_PASSWORD.
-- Se le conceden únicamente los permisos de datos y los necesarios para aplicar
-- migraciones idempotentes. No se concede DROP DATABASE, FILE ni SUPER.
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
    ON matrix_rh_131.* TO 'matrixrh'@'%';

FLUSH PRIVILEGES;
