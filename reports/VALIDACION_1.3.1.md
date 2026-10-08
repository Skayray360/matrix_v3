<!-- Creado por Aldo Garcia. -->
# Validación de la corrección sobre el proyecto existente

**Fecha:** 6 de octubre de 2026. **Base:** ZIP recibido `matrix-rh-1.3.0(1).zip`.
**Carpeta conservada:** `matrix-rh-1.3.0`. **Versión interna conservada:** 1.3.1,
que ya estaba en los metadatos del archivo recibido. Esta revisión sustituye el
informe anterior; no crea otro proyecto ni exige cambiar de base SQL.

## Corrección posterior: limpieza bloqueada en Windows

**Incidencia recibida:** instalación detenida tras sincronizar dependencias,
en `scripts.clean_legacy_layout`. El diagnóstico adjunto confirma puerto 8000
libre, ningún proceso Matrix, MySQL/Ollama disponibles, seis PDF con 105 chunks
compatibles y reserva del dispatcher expirada. Estos datos no prueban generación
RAG correcta, pero no muestran bloqueo de lease ni ausencia de documentos.

**Defecto confirmado en el código:** se comparaba la tupla completa de `lstat`
con `fstat`. Windows puede añadir permisos de ejecución a `.bat` según el nombre;
además, `ctime` no siempre representa lo mismo entre ambas consultas. Tres casos
sintéticos con bytes/identidad intactos fallaron antes de la corrección y pasan
después. El mensaje original no incluía la ruta ni el motivo concreto; por eso,
la atribución de esta incidencia particular es **PROBABLE**, no una medición
directa del sistema de archivos del usuario.

Fuentes de contraste: implementación de
[CPython para permisos por extensión](https://chromium.googlesource.com/external/github.com/python/cpython/+/refs/tags/v3.9.7/Modules/posixmodule.c)
y [reporte de diferencias de ctime en Windows](https://github.com/python/cpython/issues/157671).
Son referencias del comportamiento; el reporte de ctime utiliza Python 3.14 y
no equivale a una ejecución de Python 3.12.10 en el equipo del usuario.

**IMPLEMENTED:** comparación entre APIs por dispositivo, identidad, tipo,
tamaño y fecha de modificación; comparación completa antes/después dentro de
cada API. Permanecen el rechazo de enlaces/junctions, hashes exactos, respaldo
previo y revalidación antes de retirar. Errores ahora incluyen `code`, `detail`,
`phase`, `path` relativo, `errno` y `winerror`, sin volcar excepciones privadas.
Se agregó un contrato de limpieza con Python 3.12 al job Windows de CI.

**Validación de esta corrección:** 85 pruebas aprobadas, una omitida, cero fallos
en limpieza, consolidación del instalador y entrega; 37 corresponden a limpieza.
Ruff y Mypy del módulo corregido aprobados. YAML y Python del contrato de CI
validados y su escenario ejecutado en Linux. **NOT_EVALUATED:** ejecución del
job Windows, instalación nativa y generación con Ollama; no se ejecutó CI remoto.
Las pruebas amplias de la revisión anterior se conservan abajo como evidencia
histórica, no se presentan como una ejecución nueva de esta corrección.

**Aplicación:** reemplazar el código y `SHA256SUMS.txt` por el contenido completo
del ZIP actualizado, sobre la misma raíz, conservando `.env`, `.venv`, `data/`,
`var/`, configuración local y SQL. Ejecutar Instalar de nuevo; no recrear usuarios
ni base de datos para este error. La limpieza repetida es idempotente y conserva
respaldos previos. Si falla por otro motivo, usar el nuevo JSON para identificarlo.

## Alcance y criterios conservados

Remediación acotada con Local AI Project Auditor 1.2: RAG/citas, operación Windows,
diagnóstico, limpieza controlada y documentación. Revisores reales separados de
RAG, runtime, evidencia documental y documentación; coordinación y QA integrados.

Se conservan React/TypeScript, FastAPI/Python 3.12, MySQL, Qdrant, Ollama, Gemma4,
EmbeddingGemma, interfaz, API, permisos, historial y corpus. No se cambian modelos,
contexto, embeddings, umbrales de recuperación ni reglas de prestaciones. El
conocimiento general permanece disponible según configuración, separado de las
fuentes documentales. Las citas y cantidades siguen validándose.

Preparación en Linux, en copia aislada sin `.env` operativo. Lectura autorizada de
PDF/logs e inspección del índice adjunto sin modificarlo. Pruebas de código con
SQLite temporal, datos sintéticos y dobles de servicios; sin conexión al Windows,
MySQL ni Ollama del usuario. No se ejecutan cargas, migraciones reales ni borrados
en la instalación del usuario. Score y cobertura de auditoría integral:
**NOT_EVALUATED**. No se certifica producción ni aprovechamiento del modelo al 100 %.

## Hallazgos, cambios y límites

| Hallazgo | Evidencia | Corrección | Estado |
| --- | --- | --- | --- |
| Respuesta documental dentro de «Orientación general» | Log del 6 de octubre: rechazo de procedencia mezclada | Regeneración específica con respuesta documental citada; configuración general global conservada | IMPLEMENTED |
| Cita fuera de la evidencia recuperada | Log del 6 de octubre: identificador no autorizado | Etiquetas breves por llamada, resueltas exclusivamente contra fuentes autorizadas que sí entraron al contexto; retorno de identificadores originales | IMPLEMENTED |
| Reserva huérfana y espera de hasta 690 s | Código anterior extendía readiness por el deadline del modelo | Propietario con huella local, PID y creación; recuperación transaccional del proceso local comprobado muerto, sin esperar su expiración | IMPLEMENTED |
| Parada incompleta o PID de launcher diferente del backend real | Revisión de ownership, árbol y control de parada | Registro del Python real, parada cooperativa y posterior terminación de supervivientes propios con identidad revalidada | IMPLEMENTED |
| Respuesta tardía del dueño anterior | Riesgo al cambiar propietario | Solicitudes en ejecución expiran sin repetición automática; publicación exige dueño vigente, operación activa y permisos actuales | IMPLEMENTED |
| Siete BAT y restos de documentos anteriores | Inventario del ZIP recibido | Cuatro BAT; limpieza exacta con respaldo previo y SHA para documentación/assets | IMPLEMENTED |
| Carpeta y metadatos diferentes, sin manifiesto | Carpeta 1.3.0 con código 1.3.1; faltaba SHA256SUMS | Se conserva esa carpeta y se regenera el manifiesto; no exige renombrar el proyecto | IMPLEMENTED |
| Generación real de las dos respuestas | No hay acceso al runtime del usuario | Pruebas de contrato y procedimiento de aceptación en destino | PARTIAL |

Las etiquetas `E1`/`S1` sólo son transporte interno: no añaden fuentes ni convierten
una cifra inventada en válida. Se reconstruyen después de empaquetar cada contexto.
Los alias desconocidos, evidencia descartada, citas en orientación general y
números del supuesto del usuario sin respaldo siguen rechazándose. No se devuelve
un fragmento relacionado como si demostrara por sí solo la respuesta solicitada.

## Evidencia de los PDF consultados

| Caso de control | Contenido comprobado | Fuente y página |
| --- | --- | --- |
| Fondo de ahorro | Un préstamo por ejercicio; máximo total acumulado de aportaciones del empleado y empresa a la fecha de solicitud; sin intereses | `PLATICA DE PRESTACIONES JUNIO 2020.pdf`, p. 15 |
| Ingreso en mayo de 2016; retiro con 7 años y 6 meses antes de jubilación | Ingreso posterior al 1 de abril de 2016; fila 7–7.99 años: 70 % de cada aportación básica, básica complementaria y adicional complementaria | `PLATICA DE PLAN DE PENSIONES POR JUBILACI#U00d3N DICIEMBRE 2022.pdf`, p. 9 |

El parser conserva completos ambos pasajes y la tabla. El índice adjunto contiene
los fragmentos y sus hashes corresponden a los PDF. Esto descarta su ausencia en
esa copia del índice; no demuestra el ranking ni la selección exacta de aquellas
solicitudes en el runtime. Respuestas fieles controladas pasan el contrato de citas
y cifras. **No se ejecutó generación real con Gemma para estas preguntas.**
Los documentos son las versiones históricas indicadas, no una verificación externa
de la vigencia actual de las prestaciones.

## Validación de la revisión anterior a la corrección de limpieza

Se fijó `PYTHONPATH` a la copia actual y se verificaron las rutas de los módulos,
para no confundirla con una instalación editable anterior del entorno de QA.

| Comprobación | Resultado |
| --- | --- |
| Backend: unitarias, seguridad y cola | **1608 aprobadas, 80 omitidas, 0 fallos, 0 errores**; 1688 recopiladas |
| Frontend: Vitest | **86 aprobadas**, 11 archivos, 0 fallos |
| Ruff: app, scripts, seeds y tests | Aprobado |
| Mypy: app | Aprobado; 87 archivos |
| Bandit: app | 0 hallazgos |
| Secretos en contenido distribuible | 0 secretos reales detectados |
| Suministro local del frontend | Controles ejecutados aprobados: lock, gestor, árbol, scripts e indicadores; 277 manifiestos instalados, 300 entradas del lock |
| Documentación | 69 Markdown distribuidos; cero enlaces locales rotos |
| Cabeceras de código/guías propias | 330 archivos comprobados, sin faltantes; documentos de negocio no se atribuyen al desarrollador |
| Integridad del ZIP | 379 archivos, una raíz `matrix-rh-1.3.0/`, CRC correcto y 378 hashes válidos más el propio manifiesto |
| Preservación | Los seis PDF y los 22 archivos de corpus/fixtures no README son idénticos al ZIP recibido |
| Interfaz y esquema | Fuentes y build vigente del frontend conservados; nueve migraciones SQL sin cambios |
| Extracción y manipulación | Extracción limpia aprobada; alteración sintética del código detectada, restauración aprobada |
| Actualización sobre carpeta existente | 68 restos conocidos respaldados exactamente y retirados: 64 Markdown, tres BAT, un asset antiguo |
| Estado durante actualización simulada | `.env`, archivo representativo de índice, upload y documento nuevo sintéticos preservados; limpieza repetida idempotente e integridad final aprobada |

Las 80 omisiones: 43 por falta de PowerShell, 36 por falta de MySQL/MariaDB y una
por acceso restringido a `/proc` del proceso hijo. No se cuentan como aprobadas.
Los resultados focales están incluidos en el total; no se suman de nuevo.
La prueba de actualización no restaura una base SQL real ni prueba un índice
Qdrant operativo. No se ejecutó CI remoto, auditoría online nueva de CVE, Windows
real, prueba GPU, carga multiusuario ni E2E contra servicios reales.

## Parada y reinicio: comportamiento y límites

- `DETENER_MATRIX_RH.bat` busca los procesos propios y sus descendientes: solicita
  cierre, espera hasta 30 s de gracia y termina los supervivientes revalidados.
  `-Force` omite la gracia; no omite comprobaciones de propiedad ni el mutex.
- Se cierran conexiones HTTP propias y se impide publicar respuestas tardías.
  No se termina WAMP/Ollama compartido; la desconexión HTTP no garantiza cancelar
  físicamente un cálculo que el servidor de modelos ya esté ejecutando.
- Un nuevo proceso recupera una reserva v2 local cuando verifica que el dueño
  murió o que su PID fue reutilizado. No añade los antiguos 660/690 s de espera.
- Reservas UUID antiguas o de identidad no verificable no se roban. Si siguen
  vigentes, el primer inicio informa el bloqueo y los segundos restantes; esa
  transición antigua puede requerir una expiración inicial.
- Instalar, iniciar y detener usan exclusión mutua. Si hay una operación en curso,
  otra se rechaza inmediatamente. No forzar la parada de una instalación que esté
  migrando datos. Si sólo hay un arranque bloqueado, cerrar esa operación con
  Ctrl+C y ejecutar Detener después.
- Readiness espera dependencias reales con el presupuesto normal; quitar una
  reserva huérfana no hace instantánea la carga de Python, MySQL o los modelos.

## Entrega y aceptación pendiente

Únicos BAT: **INSTALAR_MATRIX_RH**, **DIAGNOSTICO_MATRIX_RH**, **INICIAR_MATRIX_RH**
y **DETENER_MATRIX_RH**. El diagnóstico ordinario incorpora metadata de modelos,
RAG y reserva del dispatcher sin exponer su identidad. `-ProbarModelos` es una
prueba sintética activa explícita con backend detenido.

El ZIP conserva los PDF/documentos recibidos. No distribuye `.env`, `.venv`,
node_modules, índices, logs operativos, conversaciones, base SQL ni pesos. Para
actualizar, detener, respaldar y sustituir el código en la misma carpeta,
conservando configuración, `data/`, `var/`, SQL y cambios locales aprobados.
Ejecutar Instalar y luego repetir las dos preguntas en una conversación nueva.
Ver [instalación](../docs/INSTALACION.md) y [diagnóstico](../docs/TROUBLESHOOTING.md).

La limpieza sólo retira MD/assets de hashes conocidos; conserva personalizados.
Los tres lanzadores retirados se archivan aun si fueron modificados, con bytes
exactos y extensión `.retired`. Un fallo de respaldo cancela la retirada. Para
revertir, restaurar un conjunto compatible de código y respaldos; no mezclar
módulos de ambas revisiones ni eliminar el manifiesto para ignorar discrepancias.
