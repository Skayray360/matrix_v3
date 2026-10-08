<!-- Creado por Aldo Garcia. -->
# Validación reproducible

| Nivel | Ubicación | Alcance |
| --- | --- | --- |
| Backend | `backend/tests/unit`, `security`, `integration`, `test_chat_queue.py` | Contratos, ACL, errores, borrado, sesiones y SQL real |
| RAG | `backend/tests/rag_eval/golden_set.yaml` | 43 casos; `scripts.rag_eval --full` añade generación real |
| Frontend | `frontend/tests/*.test.*` | Estado, API, ErrorBoundary, avisos y purga pendiente |
| Visual | `frontend/tests/visual` | Browser con API simulada, layouts y recuperaciones |
| E2E | `frontend/tests/e2e` | Browser contra backend/SQL reales del perfil de prueba |
| Windows | `windows/Validate-MatrixRH.ps1` | Gate ordenado en copia descartable |

Desde raíz, en copia de pruebas PowerShell:

```powershell
$env:UV_PROJECT_ENVIRONMENT = Join-Path $PWD '.venv'
uv sync --project backend --frozen --extra dev --python 3.12 --no-python-downloads
$env:PYTHONPATH = Join-Path $PWD 'backend'
$env:APP_ENV = 'test'
$env:SCHEDULER_ENABLED = 'false'
& .\.venv\Scripts\python.exe -m ruff check backend/app backend/scripts backend/seeds backend/tests
& .\.venv\Scripts\python.exe -m mypy backend/app
& .\.venv\Scripts\python.exe -m scripts.verify_documentation
& .\.venv\Scripts\python.exe -m pytest backend/tests --tb=short -ra
```

Integración requiere base dedicada `matrix_rh_test`, `DATABASE_URL` privado y
`MATRIX_TEST_ALLOW_DESTRUCTIVE=true`; nunca usar la base operativa. El password
sintético de tests se establece en `tests/conftest.py` exclusivamente con
`APP_ENV=test`, no es el default de la aplicación. Runtime `--no-dev` no instala
herramientas de pruebas/auditoría.

Antes de descargar, revisar lock y avisos; desde `frontend`:

```bash
corepack npm ci --ignore-scripts --no-audit --no-fund
corepack npm run lint
corepack npm run format:check
corepack npm run typecheck
corepack npm test
corepack npm run build
corepack npm run e2e:install
corepack npm run test:visual
```

El CI usa npm del runtime Node seleccionado; Windows/Docker usan Corepack para
verificar el hash de `packageManager`. Ambos instalan el mismo lock sin hooks.
El workflow se activa en push main, PR y manual: Ruff/Mypy, frontend, parser
PowerShell, SQL completo en MySQL/MariaDB y 9 E2E sin generación. Las actions
se fijan por SHA. No se ejecutó GitHub Actions remoto durante esta entrega.

`npm run e2e` necesita el backend real activo sirviendo el build. La suite
completa incluye escenarios que requieren modelos/corpus reales; no confundir
9 recorridos sin Ollama con aceptación de chat/RAG. Visual usa API simulada.
El gate Windows integral también exige esa pila descartable y marca suites
omitidas como incompletas. `-QuickRag`/`-SkipE2E` reducen alcance.

Registrar modelo/digest, corpus, entorno y omisiones. Un skip no es un pase;
Mocks, JUnit y BitLocker simulado no acreditan WAMP, GPU, SSO ni cifrado.
Resultados actuales: [validación](../reports/VALIDACION_1.3.1.md).

`scripts.verify_documentation` comprueba destinos locales de enlaces del paquete;
no consulta Internet ni valida anclas o veracidad del contenido. El gate de entrega
también debe comprobar build, archivos obligatorios, secretos y manifiesto SHA.


## Corpus sintético separado

Los fixtures documentales viven en `data/synthetic_test_data/knowledge/`. El scanner
los excluye de la instalación normal. Las pruebas unitarias los leen directamente.
Para `rag_eval` real, usar una copia desechable del proyecto y copiar esos documentos
a una raíz de pruebas distinta, con base `matrix_rh_test`, configuración y Qdrant
propios. Ajustar `RAG_KNOWLEDGE_ROOT`, ingerir allí y ejecutar el golden set; nunca
mezclarlo con los documentos de operación. No se ejecutó esa evaluación con Gemma
real en el entorno de preparación.

## Aceptación manual en el equipo destino

| Prueba | Acción | Resultado a comprobar |
| --- | --- | --- |
| Identidad | «¿Quién eres?» | `Soy Matrix RH.`; esta ruta no prueba Ollama |
| Generación general | «Explica percepción y deducción con un ejemplo ficticio.» | Respuesta no vacía y procedencia general, sin política interna inventada |
| RAG | Preguntar una cifra/condición explícita de un documento autorizado y pedir fuente | Pasaje/cita correctos; si se abstiene, diagnosticar la causa, no dar la calidad por aprobada |
| Memoria | «Resume lo anterior en tres puntos», luego recargar | Continuidad e historial propio, sin mezclar otras conversaciones |
| Permisos | `MatrixR1` pregunta por una categoría no concedida | No revela contenido ni fuentes restringidas |

Añadir una pregunta sin respuesta en el corpus y casos con tablas, negaciones,
fechas, unidades y documentos homónimos. Revisar los pasajes originales con un
responsable RH. La exactitud de una cifra no demuestra corrección de sus condiciones.

## Recorridos de integración y browser

| Recorrido | Aceptación |
| --- | --- |
| Login/logout | Cookie/CSRF, expiración y cierre real de sesión |
| Cola | Submit 202, polling, `completed`, idempotencia y recuperación de conexión |
| Cancelación | La operación cancelada no publica una respuesta tardía |
| Conversaciones | Otro usuario no enumera, modifica ni usa historial ajeno |
| Adjuntos | Esperar `indexed`, resumir con citas y mantener aislamiento por conversación |
| Administración | Sólo publica dentro de categorías efectivamente autorizadas |
| Revocación | Cambiar rol durante inferencia impide publicar/reutilizar alcance retirado |
| SQL controlado | Fuente/entidad/filas/columnas permitidas y negativas en el motor real |
| UI | Acceso, móvil, tema, fuentes y errores sin desbordamiento ni secretos |

El helper de chat debe correlacionar la solicitud actual y el nuevo mensaje:
un mensaje anterior no es una respuesta exitosa. Listar tests no es ejecutarlos.
No introducir credenciales reales ni desactivar autorización para aprobar un caso.

## Mapa de requisitos a pruebas

| Contrato | Implementación | Referencia de aceptación |
| --- | --- | --- |
| ACL documental | `authorization/`, `rag/vector_store.py` | `test_authorization.py`, `test_admin_category_scope.py` |
| Scope SQL | `structured_data/` | `test_structured_role_scope.py`, `test_structured_query.py` |
| Memoria y permisos vigentes | `memory/service.py` | `test_memory_and_agent.py` |
| Cola e idempotencia | `agents/chat_service.py`, `chat_queue.py` | `backend/tests/test_chat_queue.py` |
| Índice y rollback | `rag/index_manifest.py`, ingesta | `test_rag_integrity_final.py`, `test_rag_delete_transactions.py` |
| Verificación numérica RAG-01 | `rag/numeric_grounding.py` | `test_grounding_numeric_rag_hotfix.py` |
| Abstención diferenciada | `agents/knowledge_agent.py` | `test_rag_validation_diagnostics_hotfix.py` |
| Citas breves y reintento documental | `rag/citation_aliases.py`, `agents/knowledge_agent.py` | `test_rag_citation_transport.py`; alias desconocidos y fuentes omitidas deben seguir rechazados |
| Esquema SQL real | `database/migrator.py`, DDL | `integration/test_schema_and_seed.py` |
| Modelo/runtime | `llm/`, configuración | Contratos de perfiles y smoke real en destino |
| Calidad/capacidad | Modelo, corpus, configuración y hardware | Evaluación RAG completa y benchmark acotado |

Conservar versión, digests, corpus, configuración no secreta, entorno, resultados
y omisiones de cada ejecución. Los números de un informe anterior no aprueban
esta revisión aunque se conserve la misma versión interna 1.3.1.

## Aceptación de actualización y reinicio

En una copia de pruebas Windows, con datos sintéticos y servicios propios:

1. Actualizar sobre la misma raíz siguiendo [Instalación](INSTALACION.md). Verificar
   que se conservan la conexión SQL, cuentas, historial, documentos y YAML aprobados.
2. Ejecutar el instalador dos veces: no duplica usuarios/migraciones ni recrea la
   base. Deben quedar cuatro BAT en la raíz; la limpieza conserva Markdown
   personalizados y respalda lanzadores retirados.
3. Iniciar, consultar disponibilidad, detener y volver a iniciar. Comprobar que
   no quedan procesos Matrix identificados del ciclo anterior ni una espera por
   TTL de una reserva v2 cuyo propietario local ya terminó.
4. En la copia de pruebas, comprobar recuperación de propietario muerto/PID
   reutilizado y rechazo de propietario vivo, ajeno o desconocido. Una UUID
   heredada sin identidad puede exigir una sola expiración; no borrar locks.
5. Comprobar que WAMP, Ollama y procesos de otras carpetas siguen funcionando;
   que parada/arranque simultáneos se rechazan; y que una operación cancelada o
   expirada no publica un resultado tardío ni se regenera automáticamente.

Estos pasos son criterios de aceptación, no resultados ya ejecutados en el
equipo del usuario. El informe actual distingue pruebas sintéticas de Windows,
MySQL y modelos reales.
