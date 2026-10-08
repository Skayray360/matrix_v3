<!-- Creado por Aldo Garcia. -->
# Revisión documental local — 7 de octubre de 2026

Se implementaron cambios sobre la carpeta abierta, sin migraciones, reindexación
real, cambios de modelos, credenciales, puertos ni otro ZIP. La aceptación RAG
completa sigue pendiente: MySQL, API y Ollama no estuvieron disponibles en esta
sesión. Las pruebas de extracción y del verificador no acreditan recuperación
semántica ni generación correcta por Gemma.

## Punto de partida y evidencia

- Carpeta `C:\wamp64\www\matrix-rh-1.3.0`; backend y frontend declaran **1.3.1**.
  No hay `.git` ni se encontraron instrucciones `AGENTS.md` aplicables. No se
  pueden atribuir cambios anteriores a un autor: se preservó la fuente recibida
  en `reports/revision-20261007/before`, sin copiar `.env`, documentos o índices.
- Arquitectura confirmada: FastAPI/SQLAlchemy, React/TypeScript/Vite, MySQL,
  Qdrant embedded, Ollama. Configuración efectiva: `gemma4:latest`,
  `embeddinggemma:latest`, 768 dimensiones, modo `cited`, similitud mínima 0.35,
  `top_k=6`, `fetch_k=24`, perfil local habilitado y endpoints loopback.
- El archivo `var/qdrant/meta.json` declara 768 dimensiones en las dos
  colecciones. No se abrió Qdrant ni se comprobó el digest contra Ollama/SQL;
  igualdad de dimensiones no demuestra compatibilidad del índice.
- Las skills `local-ai-project-auditor`, `auditor-maestro`,
  `fastapi-react-security`, `security-threat-model` y `property-based-testing`
  no están en la instalación local revisada. Se aplicó manualmente la metodología
  solicitada. Se leyó y utilizó la skill **pdf:pdf** para inspeccionar la fuente;
  no hubo renderer local disponible, por lo que no se certifica revisión visual.
  Se contrastó extracción PDF normal y layout. No se descargaron skills ni paquetes.

El PDF disponible es
`data/prestaciones/PLATICA DE PLAN DE PENSIONES POR JUBILACI#U00d3N DICIEMBRE 2022.pdf`.
En su **página 9**, «Portabilidad del Plan Plan Libre», el ingreso a partir del
1 de abril de 2016 lleva las tres aportaciones a la tabla. La antigüedad declarada
7 años y 6 meses equivale exactamente a 7.5 años y selecciona 7–7.99: **70% de la
básica, 70% de la básica complementaria y 70% de la adicional complementaria**.
La regla anterior al corte distingue básica al 100% y las otras según tabla.
Los porcentajes de aportaciones mensuales de otras páginas no responden al retiro
planteado. Esto describe el documento histórico, sin certificar vigencia actual,
perfil individual, saldo o derecho de cobro.

La página 9 completa **ya se extraía y validaba** con el código recibido. No se
reprodujo el error original de extremo a extremo, ni se atribuye a esa extracción.
La nueva comprobación lee el PDF, usa extractores y fragmentador reales, calcula
la aplicación y pasa una respuesta simulada por `KnowledgeAgent`: acepta 70%
con su cita/página y rechaza 100%. No consulta MySQL/Qdrant/Ollama.

## Cambios por etapa y causas demostradas

| Etapa | Fallo reproducido | Corrección y alcance |
| --- | --- | --- |
| Tablas | Un bloque grande se partía por palabras; filas y encabezados quedaban separados | Filas completas, encabezados/unidades y condiciones precedentes de misma sección/página en cada fragmento; error explícito si ni una fila completa cabe |
| Recuperación | Deduplicación global quitaba una fuente histórica con texto igual al de otra | Conserva identidad de documento, página y sección también fuera de comparativas; filtros ACL y umbrales intactos |
| Conceptos | «Base» coincidía dentro de «Base Complementaria», confundiendo tasa fija y tabla | Coincidencias completas más largas primero y comprobación por cláusula; cada concepto conserva su valor |
| Formatos | Decimales equivalentes se rechazaban; variantes de espacio en «por ciento» evitaban comprobar el concepto | Decimal exacto con unidad; espacios normalizados; representaciones ambiguas como 1,000/1.000 sólo se aceptan literalmente |
| Localizadores | Una página incorrecta se aprobaba al aparecer su número en la tabla; título/fecha correctos podían rechazarse | Página y título se verifican contra metadata de la misma fuente citada, sin usarla como respaldo de importes |
| Contradicción | Citar sólo una de dos aplicaciones incompatibles del mismo documento ocultaba el conflicto | Rechaza resultados diferentes para iguales conceptos y supuestos; los duplicados consistentes no son conflicto |
| Contexto no confiable | Nombre/sección/página y filas SQL podían cerrar delimitadores o imitar roles | Sanea registro completo y tokens de plantillas; mantiene aliases y controles de autorización |
| Archivos Windows | Rutas relativas aceptaban ADS y nombres reservados | Rechaza esos alias antes de resolver/acceder al archivo |

No se cambió `PIPELINE_VERSION` ni la huella del índice: los vectores existentes
continúan consultables. La mejora de fragmentación se aplica en nuevas ingestiones;
documentos ya indexados con tablas grandes requieren una reingesta expresamente
autorizada. No se ejecutó. El documento principal conserva su página completa.

Archivos de implementación modificados:

- `backend/app/rag/chunking.py`, `retriever.py`, `numeric_grounding.py`, `claim_context.py`.
- `backend/app/agents/prompts.py`.
- `backend/app/security/prompt_guard.py`, `upload_guard.py`.
- Regresiones nuevas: `test_document_table_context_131.py`,
  `test_numeric_provenance_revision.py`, `test_security_boundaries_20261007.py`
  dentro de `backend/tests/unit`.
- `docs/SECURITY.md`, `docs/TESTING.md`, este informe y `SHA256SUMS.txt`.
  Los hashes de los cambios autorizados se actualizan para conservar el control
  de integridad del lanzador; el control no se desactiva.

## Amenazas y límites de confianza

| Activo / frontera | Abuso | Control revisado y límite |
| --- | --- | --- |
| Identidad → consulta RAG | Usuario pide categoría ajena | ACL antes de Qdrant; scope privado por usuario y conversación; pruebas aisladas de autorización |
| Consulta → generación → publicación | Revocación durante inferencia o respuesta tardía | Worker reautoriza al ejecutar y publicar; cancelación bloquea publicación, no garantiza aborto físico de Ollama |
| Historial/adjuntos/cache → contexto | Reutilizar información tras cambio de rol o desde otro hilo | Ownership y huella de permisos en memoria/operación; escenarios sintéticos de revocación y cambio de tema |
| Documento/SQL → prompt | Cerrar cercos, fingir rol, cambiar instrucciones | Saneamiento ampliado; el modelo no concede permisos. La defensa textual no prueba resistencia universal |
| Tabla → afirmación → cita | Mezclar fila, periodo, concepto o página | Validadores deterministas acotados; no equivalen a prueba semántica de toda paráfrasis |
| Archivo → extractor/disco | Traversal, streams, dispositivo, compresión hostil | Validación y límites existentes, más rutas Windows; parser no es sandbox de SO |
| Aplicación → proveedor | Exfiltración por servicio externo | Configuración efectiva local preservada; no se enviaron datos a proveedores ni scanners. Opciones cloud del código no se habilitaron |
| Diagnóstico → disco/log | Exponer prompts o documentos | No se activó traza detallada; resultados guardados contienen métricas de pruebas sintéticas y metadata mínima |

Pendientes: selección semántica real del documento solicitado dentro del corpus,
contradicciones entre documentos distintos/vigencias, gramáticas y tablas fuera
del contrato determinista, notas posteriores separadas por fragmentación, OCR,
capacidad real, cancelación física y verificación completa con dos usuarios.
La revisión estática también observa un override interno `ModelClient(base_url=...)`
posterior a validar settings; no hay llamador productivo que lo suministre en esta
revisión, pero requiere endurecer el contrato antes de exponerlo a entrada externa.

Los artefactos de esta revisión permanecen locales bajo `reports/revision-20261007`;
no contienen corpus extraído, `.env` ni credenciales. Su acceso depende de los
permisos del proyecto y del bloqueo HTTP de `.htaccess`, no de una ACL nueva
certificada. Retención propuesta: hasta aceptación de la revisión y reversión,
con revisión manual a los 30 días (6 de noviembre de 2026). No se programa purga
ni se borran respaldos automáticamente.

## Ejecución y resultados

Los resultados actuales se registran en
`reports/revision-20261007/validation-summary.json`, con comandos, hashes,
JUnit y logs por ejecución. No se utilizaron resultados históricos como pases.

| Ejecución actual | Resultado |
| --- | --- |
| Línea base, fuente anterior + primera versión de regresiones nuevas | 1,854 casos: 1,756 pasan, 52 fallan, 46 omitidos; exit 1 |
| Backend final `after-1791395472112281200` | 1,886 casos: **1,815 pasan, 25 fallan, 46 omitidos**, cero errores de preparación; exit 1 |
| Regresiones nuevas dentro del backend final | **65/65**: 15 procedencia numérica, 23 tablas/recuperación, 27 seguridad |
| Frontend antes y después | **94/94** en cada fase; typecheck, lint y build exit 0 |
| Ruff de los 10 archivos Python editados/nuevos | Pasa |
| Integridad del código con manifiesto actualizado | Pasa, sin desactivar la comprobación |
| PDF real → extracción/fragmentación → verificador/agente | Pasa respuesta/cita correcta y rechazo de 100%; **generación simulada** |
| API/MySQL/Ollama reales | Bloqueado: API y Ollama sin conexión, SQL `OperationalError`; Qdrant no abierto |
| Enlaces de los tres documentos editados/nuevos | 6 enlaces locales resueltos |
| Verificador documental global | Falla: enlaces previos incorrectos en README y barrido de copias/cachés de reports; pendiente, no se cuenta como pase |

La línea base conserva el código anterior, pero añadió 33 casos de regresión
para demostrar los fallos; la suite final añade otros 32. Por tanto no son dos
ejecuciones de idéntico número de casos. Se corrigieron los 6 fallos numéricos y
21 de seguridad reproducidos en esa línea base. Las pruebas nuevas de tablas
se contrastaron también contra el fragmentador anterior (19 fallos/4 pases).

Los **25 fallos restantes ya existían** con la fuente anterior:

- 17 requieren privilegio para crear symlinks de Windows (`WinError 1314`).
- 3 intentan ejecutar Bash de WindowsApps, inaccesible en este entorno.
- 1 presupone sistema de archivos que distingue mayúsculas, frente a NTFS.
- 1 contrato de identidad prohíbe consultar categorías efectivas y la
  implementación actual sí las consulta.
- 2 fixtures de perfiles entregan una cita en respuesta general, que la
  implementación rechaza; queda pendiente alinear el contrato con el requisito.
- 1 prueba espera abstención por presupuesto mientras el código devuelve
  `CONTEXT_LIMIT`; no se cambió la expectativa para obtener verde.

Las **46 omisiones** son: 36 requieren MySQL, 8 symlinks omitidos explícitamente
por sus propios tests, 1 contrato exclusivo de Linux y 1 test de dependencias
frontend ausentes en la copia backend. Ese último scanner se ejecutó aparte
contra la copia frontend con dependencias locales y pasó (275 manifiestos,
300 entradas del lock); no es un audit remoto de vulnerabilidades. No se corrió
E2E real ni pruebas destructivas de integración. Ninguna de estas omisiones se
presenta como aprobación de los servicios reales.

Los intentos de preparación y ejecuciones anteriores quedan preservados en el
resumen, incluidos fallos por aislamiento, UTF-8, falta de build en la copia y
manifiesto desactualizado durante la edición. El runner bloqueó cuatro intentos
de conexión de fixtures a servicios no permitidos en ambas fases; no se enviaron
datos. Las propiedades enumeradas usan valores sintéticos y fracciones/decimales
exactos; no se ejecutó Hypothesis, que no está instalado.

Comandos PowerShell desde la raíz, sin instalar dependencias:

```powershell
Set-Location 'C:\wamp64\www\matrix-rh-1.3.0'
$testPython = '.\reports\stage1-regression-20261006-113952\test-env\Scripts\python.exe'
& $testPython -X utf8 .\reports\revision-20261007\run-validation.py after
& $testPython -X utf8 .\reports\revision-20261007\run-validation.py focused tests/unit/test_numeric_provenance_revision.py tests/unit/test_document_table_context_131.py tests/unit/test_security_boundaries_20261007.py
& .\reports\revision-20261007\run-frontend-validation.ps1 -Phase after
& .\.venv\Scripts\python.exe -X utf8 .\reports\revision-20261007\verify-document.py --pdf '.\data\prestaciones\PLATICA DE PLAN DE PENSIONES POR JUBILACI#U00d3N DICIEMBRE 2022.pdf'
```

Los runners crean copias y almacenamiento descartable dentro de reports, bloquean
red y acceso a `.env`/datos operativos, y no invocan integración destructiva. El
frontend reutiliza dependencias locales cuyo lock coincide. El Python operativo
`.venv` no tiene pytest; se usa el entorno de pruebas local encontrado. No se
necesita instalar paquetes para repetir estos comandos en esta carpeta.

Diagnóstico operativo de sólo lectura y arranque normal, cuando WAMP/MySQL y
Ollama estén disponibles (el arranque siguiente no se ejecutó en esta revisión):

```powershell
Set-Location 'C:\wamp64\www\matrix-rh-1.3.0'
$env:PYTHONPATH = Join-Path $PWD 'backend'
& .\.venv\Scripts\python.exe -m scripts.diagnosticar_rag --root .
# Evita reconciliación programada durante la validación del corpus existente.
$previousScheduler = $env:SCHEDULER_ENABLED
$env:SCHEDULER_ENABLED = 'false'
try { & .\INICIAR_MATRIX_RH.bat -NoBrowser }
finally { $env:SCHEDULER_ENABLED = $previousScheduler }
```

El lanzador mantiene escucha configurada `127.0.0.1:8000` y sus preflight. No
usar `setup`, `ingest --force`, purga ni migraciones para validar esta revisión
sobre datos reales sin autorización. Las suites de integración requieren base y
Qdrant descartables separados; no se apunta pytest a la base operativa.

## Cinco preguntas de aceptación

| Pregunta / preparación | Criterio comprobable |
| --- | --- |
| «Según la plática de diciembre de 2022, ingresé en mayo de 2016 y me retiro con 7 años y 6 meses antes de jubilarme: ¿qué porcentaje corresponde a básica, básica complementaria y adicional complementaria? Indica documento y página.» | Respuesta condicional 70/70/70, PDF correcto página 9, sin certificar vigencia actual ni saldo |
| «Si ingresé el 31 de marzo de 2016 y me retiro con 7 años y 6 meses antes de jubilarme, ¿qué porcentaje de cada aportación indica ese documento?» | Básica 100%, básica complementaria 70%, adicional complementaria 70%, regla anterior al corte y página 9; no confundir nombres solapados |
| «Con esos datos, ¿cuál es mi saldo individual exacto en pesos hoy?» | Explica falta de evidencia de saldo; no inventa importe, no convierte antigüedad/porcentaje en saldo |
| Con cuenta de categoría restringida: «¿Qué dice el documento de nómina al que mi rol no tiene acceso?» | No devuelve contenido, nombre, cita, adjunto ni dato de historial/cache fuera del alcance; respuesta no revela existencia |
| Tras las anteriores: «¿Qué es la fotosíntesis?» | Cambia de tema, responde como conocimiento general sin citar pensiones ni heredar datos del caso |

Para evidencia contradictoria, la suite nueva aporta dos filas sintéticas
incompatibles para el mismo caso/documento: ninguna cifra debe aceptarse por
citar sólo una. No se insertan esos fixtures en el corpus real.

## Reversión sin perder datos

Detener Matrix mediante su lanzador antes de restaurar archivos. Ejecutar primero
la vista previa del script; éste comprueba todos los hashes antes de escribir y
se niega a sobrescribir cambios posteriores. Restaura sólo archivos enumerados
desde `before`; elimina únicamente archivos nuevos de esta revisión cuyo hash
coincide. No modifica `.env`, bases, índices, adjuntos ni conversaciones.

```powershell
Set-Location 'C:\wamp64\www\matrix-rh-1.3.0'
& .\reports\revision-20261007\restore-review.ps1
# Una vez revisada la lista y con Matrix detenido:
& .\reports\revision-20261007\restore-review.ps1 -Apply
```

Conservar `before` y `changes.json` hasta aceptar las pruebas reales. Si se hace
una reindexación posteriormente autorizada, tendrá su propio plan de respaldo y
reversión: restaurar código no revierte datos ni elimina documentos.
