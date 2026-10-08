<!-- Creado por Aldo Garcia. -->

# Operación cotidiana

## Arranque y parada

Desde `C:\wamp64\www\matrix-rh-1.3.0`, `INICIAR_MATRIX_RH.bat` verifica imports,
preflight y propiedad del proceso/puerto antes de abrir la interfaz. Espera
`/health`/`/ready` con `ReadyTimeoutSeconds` (180 s de forma predeterminada), sin
añadir el plazo completo de una generación al presupuesto de arranque.

`DETENER_MATRIX_RH.bat` solicita cierre y concede hasta 30 s de gracia; después
termina los procesos Matrix identificados de esa raíz y sus descendientes. La
identidad incluye ruta, comando, PID y fecha de creación para evitar un PID
reutilizado. Confirma el cierre y libera la reserva que corresponde a esa instancia.
No termina WAMP, Ollama ni procesos ajenos: son servicios compartidos.

La reserva v2 identifica equipo, raíz y proceso. Si el propietario local murió
o su PID fue reutilizado, el arranque puede recuperarla sin esperar el TTL.
No toma una reserva viva, ajena o cuya identidad no se pueda comprobar. Una
reserva heredada que sólo contiene UUID puede necesitar expirar una vez; ver
[Diagnóstico](TROUBLESHOOTING.md#reserva-del-servicio-de-ia).

Instalar, iniciar y detener se excluyen entre sí mientras están ejecutándose.
Si otra operación de esta carpeta ya está activa, se informa el conflicto;
no lanzar otro proceso para forzar el paso. Un arranque sin espera de reserva no
significa carga instantánea de Python, MySQL, Qdrant o del modelo.

## Reconciliación

```powershell
$env:PYTHONPATH = Join-Path $PWD 'backend'
& .\.venv\Scripts\python.exe -m scripts.bootstrap status --read-only
& .\.venv\Scripts\python.exe -m scripts.bootstrap ingest
```

`status --read-only` consulta SQL y el inventario de modelos sin adquirir ni
crear el índice embedded; su resultado Qdrant omitido no acredita salud.
Consultar `/api/v1/ready` del servicio para esa comprobación.

Con Qdrant embedded, detener backend antes de ejecutar herramientas que abran
el índice en otro proceso. El scheduler del backend reconcilia cada 24 h por
defecto y usa lock SQL. Revisar errores por archivo y estado de privados; un
job con errores no acredita indexación completa. `--force` reconstruye, no
concede permisos ni omite fingerprint.

## Incidentes

`DIAGNOSTICO_MATRIX_RH.bat` separa imports, configuración, servicios, puertos,
procesos, metadata RAG/modelos y logs. `-ProbarModelos` añade inferencia sintética
con el backend detenido. Corregir el primer fallo obligatorio. Para inferencia,
correlacionar `request_id`/`operation_id`, código de transporte/status/deadline y
modelo; no publicar `.env`, prompts, respuestas o DSN privados.

La cola SQL conserva aceptación 202, estado y resultado. No reenviar con una
clave distinta mientras una operación está pendiente. Cancelar bloquea su
publicación; el runtime puede seguir consumiendo recursos hasta terminar.
Una sesión/rol revocados obligan a consultar de nuevo con autorización válida.
Al cerrar el dispatcher se expiran sus operaciones en ejecución, sin repetir
automáticamente inferencias ambiguas. Los trabajos en cola se conservan sujetos
a sus plazos. Detener Matrix no acredita cancelación inmediata de GPU en Ollama.

## Respaldo, restauración y rollback

Detener Matrix y respaldar `.env`, MySQL, corpus, uploads y Qdrant como conjunto.
Guardar backups fuera del ZIP fuente y probar restauración aislada. Mantener
política explícita para conversaciones, auditoría, logs y backups; la limpieza
de bajas/sesiones no define por sí misma un plazo de retención vigente. Qdrant sin manifest SQL no restaura ACL/propietarios.

1. Suspender el uso, esperar operaciones y detener el proceso API; no copiar
   Qdrant embedded abierto. Confirmar qué rutas externas utiliza `.env`.
2. Respaldar SQL mediante el procedimiento aprobado del motor y copiar configuración,
   corpus y `var/uploads`/Qdrant con el backend detenido. Conservar código y manifiesto
   de esa versión. Proteger el respaldo porque incluye datos y secretos.
3. Restaurar primero en una instalación aislada y con servicios detenidos. Usar
   `.env`, SQL, archivos e índice del mismo conjunto; revisar rutas y permisos.
4. Verificar integridad, migraciones, `/ready`, login, documentos, adjuntos y
   controles negativos por rol. Si el índice no es compatible, reingerir desde
   archivos originales, sin borrar manifest ni falsificar su huella.
5. Sólo después de aceptar la copia aislada, aprobar el retorno a servicio.

El proyecto no incluye una restauración empresarial automática ni un rollback
genérico de DDL. Volver sólo al código anterior no revierte SQL ni cambios de datos.
Mantener la instalación anterior detenida durante una actualización que comparta
su base. Ver [instalación/actualización](INSTALACION.md) y
[diagnóstico](TROUBLESHOOTING.md).

## Purga y retención

Eliminar desde la UI confirma primero la baja y cancela resultados pendientes.
Si Qdrant o disco falla, aparece `conversation_cleanup_pending`: el contenido
permanece oculto y la UI permite reintentar. No liberar cuota ni borrar metadata
manualmente; las rutas pendientes permiten terminar la limpieza.

Para limpiar bajas de versiones anteriores, detener el backend embedded,
respaldar el estado y ejecutar desde raíz:

```powershell
$env:PYTHONPATH = Join-Path $PWD 'backend'
& .\.venv\Scripts\python.exe -m scripts.purge --deleted-only --batch-size 100
# Revisar la vista previa antes de aplicar el lote.
& .\.venv\Scripts\python.exe -m scripts.purge --deleted-only --batch-size 100 --apply
```

Repetir lotes hasta `conversation_candidates=0`; `pending_cleanup` exige resolver
el error antes de dar la purga por completa. El comando sin `--apply` sólo
consulta. No elimina contenido vigente con `--deleted-only`. Migración 0008 deja
bajas antiguas con `purged_at=NULL` para procesarlas. No se cambia la BD durante
una vista previa de purga.

El scheduler mantiene sesiones/OIDC vencidos y bajas pendientes por lotes.
Retención de conversaciones, respuestas de operaciones y auditoría requiere
`RETENTION_ENABLED=true` y los plazos explícitos `RETENTION_CONVERSATION_DAYS`,
`RETENTION_OPERATION_DAYS`, `RETENTION_AUDIT_DAYS`. `RETENTION_BATCH_SIZE=100`
limita cada pasada a 1–1000. RH/Normatividad decide esos plazos; campos vacíos
no autorizan borrar esa clase de contenido. Mantener la política de backups,
logs y snapshots separadamente. Ver [SECURITY.md](SECURITY.md).
