<!-- Creado por Aldo Garcia. -->
# Incorporación y sincronización de conocimiento

Un documento nuevo forma parte de la base de conocimiento cuando termina su
extracción, fragmentación, generación de embeddings y publicación en el índice
y registro SQL. No requiere entrenar Gemma ni reiniciar el chat. Las consultas
posteriores recuperan sus fragmentos conforme a los permisos del usuario.

## Elegir el alcance

| Origen | Alcance y publicación |
| --- | --- |
| Carga administrativa | Corporativo en la categoría autorizada; indexación en la misma operación |
| Archivo nuevo en una raíz reconocida | Corporativo; alta incremental automática o manual |
| Adjunto de una conversación | Privado del usuario y conversación; nunca se promociona automáticamente |

Formatos: PDF, TXT, MD, DOCX, XLSX y CSV. Un PDF escaneado sin texto requiere
OCR previo: Matrix no ejecuta OCR. Los README, carpetas ocultas, enlaces/junctions
y formatos incompatibles se excluyen.

Las raíces y alias se definen en [knowledge-layout.yaml](../config/knowledge-layout.yaml).
Además de `data/knowledge`, se reconoce `RAG_KNOWLEDGE_ROOT` y las carpetas
configuradas directamente bajo `data`:

| Carpeta | Categoría |
| --- | --- |
| Prestaciones | `prestaciones` |
| Nomina / Nómina | `nomina` legacy; no agrupar nómina confidencial aquí |
| Compensaciones | `compensaciones` |
| Seguridad | `salud_ambiental` |
| Gastos medicos / Gastos médicos | `prestaciones`; no expedientes clínicos personales |
| ACR | Omitida hasta aprobar una categoría |

El mapeo no distingue mayúsculas ni acentos. Las categorías y sus accesos siguen
[categories.yaml](../config/authorization/categories.yaml) y la política de roles.
Crear una carpeta no concede acceso. `data/synthetic_test_data` y `var/uploads`
no se recorren como corpus corporativo.

## Flujo automático

Con el backend activo y `SCHEDULER_ENABLED=true`:

1. Al arrancar y cada `RAG_SYNC_INTERVAL_SECONDS` (60 s), se inventarían las
   raíces y se comparan las rutas con los registros corporativos existentes.
2. Los archivos nuevos deben tener al menos `RAG_SYNC_STABILITY_SECONDS`
   (10 s) de antigüedad en sus metadatos. Se comprueba además que su firma
   de archivo no cambie durante lectura, extracción y embeddings.
3. Cada pasada procesa como máximo ocho documentos. Si hay chat o inferencia
   activos, se aplaza hasta cinco pasadas; después se intenta uno, respetando
   el cupo de inferencia. Los 60 s son una frecuencia de revisión, no un SLA.
4. La ingesta publica una generación de fragmentos y su registro SQL. Solo los
   documentos listos y autorizados pueden contribuir a una respuesta.
5. Un fallo de un archivo no cancela las otras altas. Las pasadas rotan los
   candidatos para que los primeros archivos fallidos no bloqueen los posteriores.

La edad del archivo no demuestra que una copia haya terminado: una pausa larga
puede superar ese umbral. Para publicar de forma fiable, copiar primero a un
nombre oculto o con extensión `.tmp` en el mismo directorio y renombrarlo a su
nombre definitivo al terminar. La carga administrativa ya usa escritura temporal
y reemplazo atómico. Los archivos cambiantes se aplazan.

Una pasada sin altas ni limpiezas pendientes no abre Ollama/Qdrant ni crea un
trabajo de ingesta vacío. Sigue necesitando consultar SQL y recorrer directorios.

## Sincronización manual con el backend activo

La API usa el cliente Qdrant que ya pertenece al backend. Requiere sesión y
el permiso correspondiente; el POST también exige CSRF:

| Método y ruta | Permiso y resultado |
| --- | --- |
| `POST /api/v1/admin/knowledge/sync` | Administración de conocimiento; altas dentro de las categorías efectivas del usuario |
| `POST /api/v1/admin/knowledge/reconcile` | Reconciliación de altas/cambios/bajas solo en las categorías efectivas; cuerpo `{"force": false}` |
| `GET /api/v1/admin/knowledge/sync/status` | Lectura de diagnóstico; estado de la última pasada automática, sin rutas documentales |
| `GET /api/v1/admin/knowledge/summary` | Administración de conocimiento; inventario por categoría autorizada |

El estado del programador no sustituye la respuesta de una sincronización manual
ni la comprobación de cada documento. Si existe otro trabajo, se informa que
el bloqueo está ocupado; volver a intentar al terminar. No lanzar un segundo
cliente Qdrant embebido desde otra consola mientras el backend esté activo.

## Sincronización manual con el backend detenido

Desde la raíz, usando el entorno existente y sin cambiar credenciales:

```powershell
.\DETENER_MATRIX_RH.bat
$env:PYTHONPATH = Join-Path $PWD 'backend'
& .\.venv\Scripts\python.exe -m scripts.bootstrap ingest --new-only
.\INICIAR_MATRIX_RH.bat -NoBrowser
```

Inventariar archivos sin indexarlos:

```powershell
$env:PYTHONPATH = Join-Path $PWD 'backend'
& .\.venv\Scripts\python.exe -m scripts.bootstrap knowledge-status
```

`ingest --new-only` y `--force` son incompatibles. Las nuevas altas no modifican
documentos registrados, no retiran archivos desaparecidos y no reindexan adjuntos.

## Documentos existentes y fallos

La reconciliación completa existente se ejecuta cada
`RAG_REINDEX_INTERVAL_HOURS` (24 h). Compara SHA y huella del pipeline, procesa
modificaciones y retira del índice documentos eliminados de raíces disponibles.
No interpretar una raíz inaccesible como una baja. Las reindexaciones forzadas
se reservan para una operación deliberada y respaldada.

Un archivo vacío puede quedar registrado como `empty`; copiar después otro
contenido en la misma ruta es una modificación y corresponde al ciclo completo,
no a nuevas altas. Un error sin registro persistido puede reintentarse en la
siguiente pasada rápida. Revisar los contadores antes de repetir cargas.

Las escrituras de sincronización y carga administrativa comparten un bloqueo
SQL con propietario único, caducidad y comprobación antes de publicar. Esto no
convierte Qdrant y MySQL en una transacción distribuida: se mantienen las
compensaciones del servicio y la comprobación de documentos listos al recuperar.

Si un archivo cambia durante embeddings, se detecta antes de publicar vectores.
Si cambia durante la escritura vectorial, se retira únicamente la generación
del intento abortado. Si esa compensación también falla, se registra un trabajo
`generation_cleanup` para reintentar desde el programador o CLI global, hasta
ocho por pasada. Conserva identificadores, no texto ni rutas, y comprueba que
la generación no esté activa antes de retirarla. Si también falla SQL, el error
queda en logs y requiere diagnóstico: no existe garantía distribuida sin ambos
almacenes disponibles.

La reconciliación desde la API no procesa adjuntos privados ni ejecuta limpieza
global de vectores. Estas tareas permanecen en el programador y CLI operativos.
Un inventario limitado a categorías nunca puede dar de baja documentos ajenos.

## Comprobar que el conocimiento es utilizable

1. Confirmar documento `indexed`, fragmentos generados y ausencia de errores.
2. Consultar como usuario autorizado un dato explícito, pidiendo documento y página.
3. Comparar con el original, incluyendo condiciones y excepciones.
4. Repetir con un usuario sin acceso: no debe revelar texto, citas ni existencia
   de documentos restringidos.
5. Repetir la sincronización: no debe crear otra versión ni duplicar fragmentos.

Una búsqueda sin resultados también puede deberse a permisos, texto no extraíble
o similitud insuficiente. No bajar umbrales para ocultarlo. El
[informe de implementación](MEJORAS_2026-10-07.md) separa las pruebas sintéticas
de la validación pendiente con los servicios y documentos operativos.

El resumen de cada trabajo incluye `new_documents`, `deferred_documents`,
`pending_documents` y `failure_count`. Los pendientes pueden requerir varias
pasadas; un contador de archivos detectados no equivale a documentos indexados.
