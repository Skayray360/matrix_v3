<!-- Creado por Aldo Garcia. -->

# Controles de seguridad y límites

| Superficie | Control implementado | Código |
| --- | --- | --- |
| Identidad | Argon2id local de pruebas, OIDC y sin fallback entre proveedores | `app/auth/` |
| Sesión | Hash de token, cookie HttpOnly, CSRF y expiración | `auth/sessions.py`, `api/deps.py` |
| Permisos | Contexto firmado, roles/categorías, DENY por defecto | `authorization/` |
| RAG | ACL, fingerprint y generación activa dentro de recuperación | `rag/vector_store.py`, `index_manifest.py` |
| SQL | Plan JSON cerrado, entidades/filas/columnas, parámetros y AST | `structured_data/` |
| Historial | Ownership y alcance vigente | `memory/service.py`, rutas de conversaciones |
| Inferencia tardía | Reautorización, cancelación, expiración e idempotencia | `agents/chat_service.py`, `chat_queue.py` |
| Upload | Extensión/firma/tamaño, rutas de ZIP, cuotas y parser acotado | `security/upload_guard.py`, `ingestion/` |
| Recursos | Cupos de chat/inferencia/carga y rate limits | `security/admission.py`, `rate_limit.py` |
| HTTP | Cabeceras de seguridad y error sin eco del payload | `api/middleware.py`, `main.py` |
| Observabilidad | Redacción y códigos de fallo correlacionados | `common/redaction.py`, `logging.py` |
| Dependencias | Locks, revisión del árbol, audit y scanner de secretos | `scripts/verify_supply_chain.py`, `secrets_scan.py` |

Producción exige HTTPS, cookies seguras, IdP real y desactivar cuentas sintéticas.
El perfil 100 % local usa OIDC interno y runtime local; Entra/Vertex implican
cloud sólo si se seleccionan explícitamente. No exponer SQL, Qdrant ni Ollama a
usuarios externos como sustituto del proxy de aplicación.

## Límites

La extracción en subproceso acota tiempo/RSS/salida; no es un sandbox de sistema
operativo. Las políticas/credenciales read-only de los conectores deben existir
en el motor real. Los filtros de la aplicación no reemplazan mínimo privilegio
ni vistas corporativas. Citas y grounding no demuestran que el documento sea
verdadero, vigente o completo.

Los límites de recursos, rate limiter y dispatcher asumen un proceso API. No
habilitar múltiples workers para eludir cupos. La limpieza física de datos se
coordina con commits y mantenimiento; borrado lógico no equivale a borrado
irreversible inmediato en backups y almacenes.

Los controles necesitan pruebas positivas y negativas en topología real:
revocación durante inferencia, identidad distinta, documento restringido,
SQL fuera de scope, upload malformado, sesión expirada y caída de servicios.
Las pruebas de esta entrega y las pendientes están en
[VALIDACION_1.3.1.md](../reports/VALIDACION_1.3.1.md).

Ver [TESTING.md](TESTING.md).

## Controles operativos

- Identidad/seed local sólo en loopback literal, incluido `APP_BASE_URL`. Escucha
  de red y perfiles staging/production rechazan SQL root/superusuarios o sin
  contraseña. El seed nuevo exige secreto explícito y conserva hashes previos.
- Sesiones: 480 minutos absolutos y 30 por inactividad de solicitudes,
  configurables. Se purgan expiradas, revocadas e inactivas por lotes. CSRF usa
  `secrets.compare_digest`; el proxy sólo es confiable en IP/CIDR específicos.
- Registro de categorías vigente por consulta y recarga consistente. Un YAML
  inválido falla cerrado. No se conserva un wildcard sensible revocado.
- SQL: los filtros obligatorios del usuario se resuelven desde contexto firmado
  y se combinan por AND con pregunta, catálogo y concesiones de rol; no admiten
  ampliación desde el prompt. Vistas de seguridad requieren aprobación nominal.
- Borrado de conversación: commit SQL sin contenido y cancelación primero,
  después Qdrant/bytes; cualquier fallo queda reintentable y consume cuota hasta
  retirar bytes. Se conserva una fila vacía y claves de idempotencia sin payload.

## Cifrado en reposo

Staging/producción exigen `STORAGE_ENCRYPTION_ATTESTED`,
`DATABASE_ENCRYPTION_ATTESTED`, `BACKUP_ENCRYPTION_ATTESTED` y un archivo local
`ENCRYPTION_ATTESTATION_REFERENCE`. Estos campos son declaraciones de TI y no
activan cifrado. En Windows también se consulta BitLocker de rutas de proyecto,
corpus, uploads, Qdrant embedded y datadir MySQL local; falta de permisos, volumen
no verificable o protección incompleta bloquean instalación/arranque.
`DATABASE_DATADIR_PATH` permite declarar el datadir local; vacío consulta
`@@datadir`. MySQL remoto, backups y host Linux/Docker dependen de la evidencia
externa; no se presentan como cifrado medido por la aplicación. Development/test
emiten aviso. TI debe validar discos, servidor SQL y restore real.

Retención es configurable y está desactivada hasta aprobar plazos de RH. Borrar
archivos no garantiza eliminación de bloques físicos, binlogs, snapshots o
backups. Esos almacenes requieren política y cifrado propios.

## Fronteras y amenazas que deben probarse

| Amenaza | Control principal | Aceptación necesaria |
| --- | --- | --- |
| Suplantación y robo de sesión | Sesión servidor, hash, OIDC y CSRF | Login/logout/expiración y callback del IdP real |
| Ampliación de permisos | Contexto firmado desde SQL y denegación por defecto | Roles acumulativos, acceso denegado y revocación |
| Fuga documental o por memoria | ACL, dueño/conversación y huella vigente | Dos usuarios, Nómina segregada y cambio de rol |
| Instrucciones maliciosas en documentos | Evidencia como datos y herramientas limitadas | Preguntas/documentos adversariales; no basta el prompt |
| SQL fuera de alcance | Plan JSON, parámetros, AST, filtros y cuenta read-only | Columnas/filas prohibidas y motor real |
| Archivo hostil | Firma, tamaño, rutas, cuotas y extracción acotada | Traversal, ZIP comprimido y límites de recursos |
| Saturación y resultado tardío | Cola, cupos, deadline y reautorización | Carga controlada, cancelación y recuperación |
| Estado incoherente | Generaciones SQL/Qdrant, SHA y limpieza pendiente | Fallos intermedios, respaldo y restore |
| Fuga en logs o dependencias | Redacción, locks y escáneres | Revisar eventos y avisos actuales, no sólo reportes históricos |

La redacción de logs reduce exposición, no garantiza detectar todo secreto.
El aislamiento del parser no es un sandbox de SO; ni el guard de prompts ni una
cita válida eliminan todas las inyecciones o errores. IdP, TLS, runtime, sistema
operativo, vistas SQL y backups siguen bajo responsabilidad del entorno de TI.
