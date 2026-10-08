<!-- Creado por Aldo Garcia. -->
# Qdrant

El perfil Windows usa Qdrant embebido en `var/qdrant`; un solo proceso puede
abrirlo. El modo servidor es opcional y no convierte la aplicación en distribuida.

Esta carpeta queda como punto de organización para configuración específica
de TI; no contiene datos ni un servidor preinstalado. Las opciones actuales
se declaran en `.env.example` y `docker-compose.yml` de la raíz.

Respaldar índice, SQL, corpus y adjuntos como conjunto. Un índice sin manifest SQL
no recupera permisos; no borrarlo o copiarlo mientras está abierto.
[Topologías](../README.md) · [Respaldo y recuperación](../../docs/RUNBOOK.md).
