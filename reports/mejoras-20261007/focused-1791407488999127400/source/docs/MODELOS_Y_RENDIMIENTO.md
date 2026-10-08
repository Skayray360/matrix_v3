<!-- Creado por Aldo Garcia. -->

# Modelos y rendimiento — Matrix RH 1.3.1

Esta guía describe la configuración entregada, no una medición de la computadora
destino. No está acreditado un aprovechamiento del «100 %», una cantidad de
usuarios simultáneos ni una velocidad de respuesta. El objetivo es responder
correctamente con fuentes autorizadas, dentro de tiempos y recursos medidos.
Una GPU al 100 % no demuestra mejor calidad; `100% GPU` en `ollama ps` indica
residencia del modelo, no utilización sostenida.

## 1. Modelos, localidad y configuración efectiva

La distribución configura Ollama en `http://127.0.0.1:11434` para generación y
embeddings, con `LLM_LOCAL_ONLY=true`. `ModelClient` separa el proveedor de la
lógica de RH. Los adapters alternativos se conservan por compatibilidad, pero
no están activados ni constituyen un fallback cloud. Los pesos y Ollama no
están dentro del ZIP; tampoco se descarga un modelo adicional al consultar.

| Función | Configuración entregada | Uso y límite |
| --- | --- | --- |
| Respuesta habitual, FAST | `OLLAMA_FAST_MODEL=gemma4:latest` | Contexto `OLLAMA_FAST_NUM_CTX=8192`; salida `OLLAMA_FAST_MAX_TOKENS=1536` |
| Comparación/resumen complejo, DEEP | `OLLAMA_DEEP_MODEL=gemma4:latest` | Contexto `OLLAMA_DEEP_NUM_CTX=8192`; salida `OLLAMA_DEEP_MAX_TOKENS=3072` |
| Plan JSON de consultas autorizadas | Mismo modelo; `LLM_PLANNER_MAX_TOKENS=600` | No genera SQL ejecutable libremente |
| Memoria resumida de conversación | Mismo modelo; `LLM_MEMORY_MAX_TOKENS=200` | Contexto de diálogo, nunca fuente factual |
| Embeddings | `OLLAMA_EMBEDDING_MODEL=embeddinggemma:latest` | `OLLAMA_EMBEDDING_DIMENSION=768` y `RAG_EMBEDDING_DIMENSION=768`; dimensión real validada |

FAST y DEEP son perfiles del mismo modelo, no dos modelos residentes distintos.
En respuestas documentales, estructuradas y mixtas, DEEP limita la salida a
`min(techo_deep, techo_fast + max(0, contexto_deep - contexto_fast))`.
Con ventanas iguales de 8192, ambos reservan hasta 1536 tokens de salida para
conservar el mismo espacio de evidencia. Los resúmenes mantienen su techo de
3072. La ruta registra
su intención y señales. No se repite el mismo modelo como supuesto fallback
alternativo después de un timeout. Con otro modelo local configurado, cualquier
fallback sigue sujeto a presupuestos, compatibilidad y validación.

| Control de Matrix | Valor entregado | Interpretación |
| --- | --- | --- |
| `LLM_FAST_TIMEOUT_SECONDS`, `LLM_DEEP_TIMEOUT_SECONDS` | 180 / 180 s | Presupuesto de cada etapa de generación, incluida su recuperación de finalización |
| `LLM_REQUEST_DEADLINE_SECONDS` | 600 s | Presupuesto compartido de la consulta: recuperación, planificación, memoria y generación |
| `OLLAMA_TIMEOUT_SECONDS` | 600 s | Transporte, acotado adicionalmente por los presupuestos anteriores |
| `LLM_COMPLETION_RETRIES` | 1 | Puede regenerar una finalización incompleta/vacía elegible; no repite un timeout ambiguo |
| `OLLAMA_KEEP_ALIVE` | `15m` | Retención solicitada después de usar el modelo; comprobar residencia real |
| `LLM_TEMPERATURE` / `LLM_GENERAL_TEMPERATURE` | 0.1 / 0.25 | Respuesta documental / explicación general |
| `LLM_SUMMARY_TEMPERATURE` / `LLM_RETRY_TEMPERATURE` | 0.08 / 0.03 | Resumen / corrección tras validación |
| `LLM_TOP_P` | 0.9 | Muestreo; no es un porcentaje de capacidad |
| `LLM_FAST_TOP_K`, `LLM_DEEP_TOP_K` | Sin valor por defecto | Opcionales; no confundir con `RAG_TOP_K` |
| `OLLAMA_EMBEDDING_CACHE_SIZE` | 256 entradas | Caché de embeddings por modelo, revisión y contenido |

El código rechaza resultados tardíos, pero cancelar en Matrix no garantiza
detener inmediatamente una generación ya ejecutándose en Ollama. No interpretar
los timeouts HTTP como un interruptor absoluto del proceso de GPU.

La evidencia histórica aportada por el usuario es `gemma4:latest`, ID corto
`dc35e8d9c606`, 6.6 GB; y `embeddinggemma:latest`, `85462619ee72`, 621 MB. Está
registrada en [el manifiesto de modelos](../config/model-manifest.json), no
verificada en el runtime durante la preparación de este ZIP. El alias, el peso
en disco y un ID corto no acreditan variante, contexto máximo, cuantización,
licencia, capacidades ni VRAM requerida. No extrapolar hardware de otra instalación.

## 2. Contexto, RAG y capacidades deliberadamente acotadas

El empaquetado de contexto reserva salida y protocolo; estima **3 caracteres por
token** y descuenta **1400 tokens de margen**. Sin prefijo adicional, los techos
estimados de mensajes serializados son 15 768 caracteres FAST y DEEP documental;
DEEP para resumen conserva 11 160,
incluyendo instrucciones, pregunta, memoria, metadatos y evidencia. No son un
conteo del tokenizer. La memoria se retira antes de desplazar evidencia; se
incluyen fragmentos completos y se avisa si hay limitación. Los resúmenes largos
se dividen en lotes y pueden recurrir a extractos; no leen todo el corpus sin límite.

`RAG_FETCH_K=24` recupera candidatos y `RAG_TOP_K=6` limita los seleccionados,
con `RAG_MIN_SIMILARITY=0.35` y `RAG_MMR_LAMBDA=0.65`. Los fragmentos usan
presupuesto estimado 900/120 tokens de tamaño/solape. Que se recuperen seis no
garantiza que los seis quepan después de reservar salida. El escaneo de resumen
está acotado por `RAG_SUMMARY_SCAN_MAX_CHUNKS=256` y los lotes por
`RAG_SUMMARY_MAX_CHUNKS=24`.

| Capacidad | Estado en esta entrega | Motivo / siguiente comprobación |
| --- | --- | --- |
| Generación, RAG y JSON estructurado | Implementados | Ejecutar smoke y evaluar corpus real autorizado por separado |
| Thinking | `LLM_FAST_THINKING`, `LLM_DEEP_THINKING`, `LLM_STRUCTURED_THINKING` en `auto` | En Ollama envía `think=false`; prioriza respuesta final dentro del presupuesto. `enabled`, `disabled` y `default` son opciones explícitas, no prueba de soporte |
| Ventanas mayores | Configurables | Confirmar metadatos, VRAM, evidencia omitida, calidad y p95 antes de aumentarlas |
| Visión/audio y tool calling nativo | No integrados en este flujo | No atribuirlos al alias ni activarlos sin necesidad, interfaz, permisos y pruebas; la planificación JSON actual no equivale a tool calling nativo |
| Streaming de tokens | No usado (`stream=false`) | La UI espera una respuesta validada; no se publica texto aún no verificado |
| Búsqueda híbrida / reranker | No incorporados | Proponerlos solo si la evaluación separada de recuperación demuestra su utilidad |
| Fine-tuning | No realizado | RAG y controles externos siguen siendo la base; no incorporar datos privados a pesos para eludir ACL |

`ANSWER_ALLOW_GENERAL_KNOWLEDGE=true` permite explicaciones generales
identificadas, no inventar políticas privadas. `ANSWER_EVIDENCE_MODE=cited`
contrasta referencias autorizadas y cifras, pero no certifica veracidad semántica
completa. No desactivar permisos, citas o grounding para aumentar la tasa de respuestas.

## 3. Cola, concurrencia y parámetros heredados

El despliegue requiere **un proceso API** propietario del dispatcher.
`CHAT_MAX_INFLIGHT=4` y `INFERENCE_MAX_INFLIGHT=2` dan un límite efectivo de
**2 trabajos de chat activos**: `min(chat, inference)`. Los cupos de inferencia
también se comparten con otras llamadas. Por usuario y conversación se admite
un trabajo activo. `CHAT_QUEUE_MAX_SIZE=50` permite solicitudes en espera,
no acredita 50 generaciones simultáneas; `CHAT_QUEUE_TIMEOUT_SECONDS=180`
limita la espera. El aviso usa `CHAT_BUSY_THRESHOLD_PERCENT=90` de los cupos
de Matrix, no un sensor de utilización GPU.

| Variable conservada por compatibilidad | Valor declarado | Comportamiento real |
| --- | --- | --- |
| `OLLAMA_SUMMARY_PARALLEL_BATCHES` | 2 | Sin efecto: el map del resumen ejecuta sus lotes secuencialmente |
| `LLM_MAX_TRANSIENT_RETRIES` | 0 | Sin efecto: el transporte no repite automáticamente un POST ambiguo, aunque se configure otro valor |

El diagnóstico mantiene las claves históricas, las identifica en
`inference.legacy_configuration_fields` y separa `inference.effective`:
un lote de resumen a la vez y cero reintentos transitorios. No aumentar estos
valores esperando más rendimiento.

El paralelismo interno de Ollama es independiente de los cupos de Matrix.
`OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_MAX_QUEUE`,
`OLLAMA_FLASH_ATTENTION` y `OLLAMA_KV_CACHE_TYPE` pertenecen al proceso servidor
Ollama: ponerlos en el `.env` de Matrix no configura automáticamente ese proceso.
Más contexto y paralelismo pueden elevar memoria y provocar ejecución parcial
en CPU. No se cambiaron durante esta entrega. Consulte la
[FAQ oficial de Ollama](https://docs.ollama.com/faq) antes de un ajuste deliberado.

## 4. Diagnóstico seguro de la instalación

Desde la raíz de **la instalación a revisar**, después de crear su entorno virtual:

```powershell
$env:PYTHONPATH = Join-Path $PWD 'backend'
& .\.venv\Scripts\python.exe -m scripts.local_model_diagnostics
```

No genera texto, embeddings ni carga modelos; no abre SQL/Qdrant ni documentos.
Consulta configuración seleccionada, metadatos locales `/api/version`, `/api/tags`,
`/api/ps`, `/api/show` y, si existe, una lectura de `nvidia-smi`. Omite credenciales,
prompts y plantillas. `--no-network` omite HTTP; `--no-gpu` omite la lectura GPU.
`--timeout 3` es el límite total por consulta HTTP, no de todo el informe.

Puede guardarlo con `--output var\reports\modelos-131.json`; use un nombre nuevo
porque este diagnóstico puede sobrescribir el archivo indicado. El hardware
reportado pertenece a la máquina que ejecuta el comando: podría no ser el del
servidor de inferencia. Una lectura GPU es una instantánea, no un benchmark.

Consultas manuales equivalentes de metadatos, sin inferencia:

```powershell
curl.exe --silent --show-error --noproxy "*" --max-time 20 "http://127.0.0.1:11434/api/version"
curl.exe --silent --show-error --noproxy "*" --max-time 20 "http://127.0.0.1:11434/api/ps"
$cuerpo = @{model='gemma4:latest'} | ConvertTo-Json
$modelo = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:11434/api/show' -ContentType 'application/json' -Body $cuerpo -TimeoutSec 20
$modelo | Select-Object details, capabilities, thinking, model_info | ConvertTo-Json -Depth 8
ollama ps
```

Repita `/api/show` con `embeddinggemma:latest` o la etiqueta exacta configurada.
Revise también `$modelo.license` localmente. `/api/show` describe el artefacto;
`/api/ps` describe modelos cargados y su contexto activo. Un modelo instalado
puede no aparecer en `ps` si está descargado de memoria. La documentación de
[show](https://docs.ollama.com/api-reference/show-model-details) y
[ps](https://docs.ollama.com/api/ps) explica la diferencia; los ejemplos oficiales
no prueban las capacidades de sus pesos concretos.

Para inventario técnico ampliado existe `scripts.model_inventory --output RUTA`;
conserve ese informe localmente y revíselo antes de compartirlo. El diagnóstico
anterior es el preferido para soporte por su lista restringida de metadatos.

## 5. Identidad de pesos y smoke sintético manual

`:latest` no fija una revisión. Tras verificar procedencia/licencia del artefacto,
respalde privadamente `.env` y ejecute, de forma deliberada:

```powershell
& .\.venv\Scripts\python.exe -m scripts.bootstrap pin-models
```

**Este comando sí modifica `.env`**: completa digests vacíos desde el runtime
local; verifica los existentes y se detiene ante discrepancias, sin reemplazarlos.
No invente un SHA-256 completo desde el ID corto. Staging/producción requieren
pins de generación y el resto de sus controles de despliegue. Un cambio de pesos
requiere evaluación, revisión del pin por TI y rollback, no borrar el pin para
silenciar una alerta. No envíe su `.env` como diagnóstico.

El smoke siguiente **sí ejecuta inferencia y embeddings**, consume recursos y
puede cargar modelos. Hágalo únicamente en desarrollo/pruebas sin usuarios
activos, con autorización del responsable del entorno:

```powershell
.\DIAGNOSTICO_MATRIX_RH.bat -ProbarModelos
```

Alternativa explícita (elija nombre de salida nuevo):

```powershell
$env:PYTHONPATH = Join-Path $PWD 'backend'
New-Item -ItemType Directory -Path 'var\reports' -Force | Out-Null
& .\.venv\Scripts\python.exe -m scripts.model_smoke_test --profile all --run-inference --output var\reports\smoke-modelos-131.json
```

El módulo admite `fast`, `deep`, `all`; sin `--run-inference` no abre conexiones.
Comprueba inventario, revisión de embeddings, dos vectores sintéticos no nulos,
respuesta numérica y plan JSON validado. Para `all` son cuatro pruebas de chat
principales más embeddings; puede haber recuperación acotada de finalización.
No usa corpus corporativo, SQL ni Qdrant. No sobrescribe una salida existente.
La instalación también usa estas sondas antes de migraciones e ingesta.

`scripts.preflight --llm-only` es una comprobación **activa**: prueba embeddings,
no solo presencia de modelos. Un `/ready` positivo o un smoke correcto no
acreditan respuestas sobre sus PDF, multimodalidad, carga sostenida ni ausencia
de errores en otro tipo de consulta.

## 6. Medición y optimización pendientes

No se ejecutó aquí un benchmark sobre el Ollama ni la GPU del usuario. Antes de
optimizar, registre CPU/RAM/GPU/VRAM, SO, driver, runtime, digests, cuantización,
parámetros, corpus/golden set y procesos competidores. Separe carga fría de
caliente. El tamaño del archivo del modelo no es su consumo total: también
influyen cachés de contexto, activaciones y reserva para solicitudes simultáneas.

Protocolo opcional, manual y acotado en entorno desechable con datos sintéticos:

1. Defina primero los objetivos de latencia y calidad y quién puede abortar.
   Arranque con concurrencia 1; pruebe 2 solo después de comprobar memoria y errores.
2. Use 3 solicitudes de calentamiento, excluidas de la medición. Compare chat
   corto, RAG de 2000–4000 tokens reales de entrada y resúmenes, sin rebasar el
   contexto configurado. Mantenga salidas de prueba en 150–600 tokens.
3. Apunte a 30 respuestas por escenario y tres repeticiones. Acote cada ejecución
   a 10 minutos y aborte ante falta de memoria, primera fuga ACL, más de 5 % de
   errores o p95 que exceda el objetivo acordado. Si faltan muestras, márquelo
   exploratorio: no certifique capacidad ni p99 estable.
4. Correlacione las medidas de la tabla siguiente. Cambie una sola variable y
   repita con las mismas preguntas y criterios. Conserve configuración/índice
   anterior para rollback; no mezcle una mejora de velocidad con pérdida de fidelidad.

| Indicador | Evidencia disponible / limitación |
| --- | --- |
| Tokens/s de decodificación | Evento `llm.chat`: `generation_tokens_per_second = eval_count * 1e9 / eval_duration_ns`; no incluye cola/carga/prefill |
| Carga y prefill | `load_duration_ns`, `prompt_eval_duration_ns`, `prompt_eval_count`; campo ausente no significa cero |
| Latencia y errores | Latencia HTTP/orquestador, tipos de fallo y estados de cola; calcular p50/p95/p99 por escenario con tamaño de muestra |
| GPU/CPU/RAM/VRAM | Muestreo local durante la prueba y picos; `ollama ps` para residencia CPU/GPU; no confundir instantánea con promedio |
| Recuperación y fidelidad | Fuentes esperadas recuperadas, citas pertinentes, cifras/condiciones correctas, abstenciones y cero fugas ACL |
| TTFT | No instrumentado en este flujo no streaming; no sustituirlo por tiempo de prefill ni latencia total |

La [API de chat de Ollama](https://docs.ollama.com/api/chat) define los conteos y
duraciones; Matrix no registra prompts ni thinking en esas métricas. Existe
`scripts.load_test` para una prueba E2E manual con sesiones sintéticas distintas,
`--queued`, límites de tiempo y SLO explícitos. No ejecutarlo en producción ni
interpretar `capacity_certified=false` como un pase. Su duración limita envíos;
una solicitud ya admitida puede extender el tiempo hasta su deadline. Las
sesiones de prueba son secretos locales y deben revocarse al terminar.

Un objetivo de «50 usuarios» debe separar usuarios conectados, preguntas en cola
y generaciones simultáneas. No subir cupos ni contexto para cumplir una cifra
sin estas mediciones. La revisión estática de 1.3.1 no identifica un límite
accidental que justifique aumentar automáticamente esos presupuestos.

## 7. Cambiar embeddings o entrenar no sustituye el diagnóstico

### Medición local reproducible añadida el 7 de octubre

Desde la raíz, con el entorno instalado:

```powershell
$env:PYTHONPATH = Join-Path $PWD 'backend'
& .\.venv\Scripts\python.exe -m scripts.benchmark_response --output reports/inventario-local.json
```

Por defecto solo inventaría hardware/configuración y consulta el estado de Ollama.
No abre documentos, SQL ni Qdrant. `--no-network` evita también las consultas de
estado a Ollama. Para medir generación local con una pregunta sintética fija:

```powershell
& .\.venv\Scripts\python.exe -m scripts.benchmark_response --generate --samples 10 --concurrency 1 --output reports/benchmark-c1.json
& .\.venv\Scripts\python.exe -m scripts.benchmark_response --generate --samples 10 --concurrency 2 --output reports/benchmark-c2.json
```

El segundo comando respeta `INFERENCE_MAX_INFLIGHT`; no cambia su valor. Comparar
p50/p95, carga, tokens/s, reintentos y validaciones correctas. La cola registrada
es la del cliente de esta prueba, no la cola de API ni la interna de Ollama.
La primera muestra puede incluir carga; no se descarga ni precalienta el modelo.
Esta prueba no evalúa recuperación documental ni exactitud del RAG.

En esta revisión se observaron 20 núcleos físicos, 28 lógicos, unos 127.7 GiB de
RAM y una NVIDIA RTX 2000 Ada Generation Laptop GPU con 8188 MiB de VRAM. Es un
inventario puntual; Ollama no respondió y no hubo medición real de generación.
No se cambiaron concurrencia, modelos, Flash Attention ni cuantización KV.

`ANSWER_STRUCTURED_OUTPUT=true` habilita un contrato JSON interno para respuestas
documentales citadas sin SQL ni resumen. Cada afirmación lleva sus propias citas;
el backend presenta texto y vuelve a validar procedencia, cifras y contexto.
El schema también reserva espacio de entrada. Está **desactivado por defecto**:
sus pruebas son sintéticas y requiere comparar aceptación, tokens y latencia con
Gemma instalado antes de activarlo. Un JSON válido no acredita veracidad.

### Compatibilidad del índice

El baseline conserva estas plantillas: consulta
`task: search result | query: {text}` y documento
`title: {title} | text: {text}`. Cambiar modelo/revisión, dimensión, plantillas
o chunking afecta la huella del índice; reingiera desde archivos originales,
sin truncar/rellenar vectores y usando colecciones compatibles con la dimensión.
La versión de pipeline no representa un modelo nuevo.

Antes de sustituir embeddings, evalúe el mismo corpus y golden set: recall@k,
ranking, citas, abstenciones, fugas ACL, tiempo de ingesta, latencia y memoria.
Defina umbrales de aceptación antes del ensayo; cero fugas ACL es condición
obligatoria. No compare similitud bruta entre modelos ni baje el umbral para
hacer visible un archivo no autorizado. Conserve índice/configuración previos
para rollback y use `scripts.rag_eval` conforme a la guía de pruebas.

Para documentos no recuperados, revise ingesta, clasificación, permisos y ranking;
para citas fallidas, evidencia y validación; para formato, prompt/schema; para
latencia, medidas del runtime. Fine-tuning solo sería otro proyecto con dataset
autorizado/versionado, separación train/eval, privacidad, baseline y rollback.
Esta entrega no entrena pesos ni añade herramientas de entrenamiento al runtime.

Fuentes de la configuración: `backend/app/config/settings.py`,
`backend/app/llm/model_policy.py`, `provider.py`, `ollama_client.py`,
`backend/app/agents/knowledge_agent.py` y `chat_queue.py`. Referencias oficiales
consultadas el 6 de octubre de 2026 UTC; vuelva a validarlas al actualizar Ollama.

[Índice documental](README.md) · [Pruebas](TESTING.md) · [Diseño RAG](RAG_DESIGN.md)
