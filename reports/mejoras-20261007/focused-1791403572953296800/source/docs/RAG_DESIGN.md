<!-- Creado por Aldo Garcia. -->

# RAG, publicación y gobierno documental

## Dónde publicar y quién puede consultar

| Ruta | Uso |
| --- | --- |
| `data/knowledge/general/<tema>/` | Documentación general clasificada |
| `data/knowledge/especializadas/<dominio>/` | Documentos de una categoría especializada |
| `data/knowledge/<categoria>/` | Clasificación heredada compatible, incluida `prestaciones` |
| `data/<alias>/` | Sólo alias declarados en `config/knowledge-layout.yaml` |
| `var/uploads/` | Adjuntos privados por propietario/conversación; no corpus corporativo |
| `data/synthetic_test_data/` | Fixtures ficticios, excluidos de la ingesta normal |

Los alias Prestaciones y Gastos médicos se clasifican como `prestaciones`; Nómina
como `nomina`, Compensaciones como `compensaciones` y Seguridad como `salud_ambiental`.
Se normalizan mayúsculas y acentos. `ACR` está sin categoría y se omite hasta que
su responsable apruebe una clasificación. El YAML es la referencia efectiva.

Una categoría/carpeta no concede permisos. Separar Nómina General y Confidencial;
un wildcard de negocio no abre la categoría restringida. Publicar sólo documentos
con dueño funcional, vigencia, sensibilidad y aprobación identificados.

1. Revisar la categoría en `config/authorization/categories.yaml` y los roles que
   podrán consultarla. Probar el mapeo con `scripts.load_entra_mapping --dry-run`.
2. Materializar concesiones explícitamente después de su aprobación. La publicación
   administrativa exige `knowledge.admin` **y** acceso a la categoría.
3. Copiar un ejemplar de cada documento a la ruta aprobada; no duplicarlo por alias.
4. Con backend detenido si Qdrant es embedded, inventariar e ingerir desde la raíz:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
& .\.venv\Scripts\python.exe -m scripts.bootstrap knowledge-status
& .\.venv\Scripts\python.exe -m scripts.bootstrap ingest
& .\.venv\Scripts\python.exe -m scripts.bootstrap status --read-only
```

5. Revisar errores, documentos, fragmentos y SHA; reiniciar y probar un usuario
   autorizado y otro denegado. El inventario cuenta archivos, no acredita extracción.

Se admiten PDF textual, DOCX, TXT, MD, XLSX y CSV. PDF escaneado requiere OCR previo
externo aprobado. Excel usa valores cacheados, sin ejecutar fórmulas ni macros.
README, archivos ocultos, enlaces/junctions y fuentes sin clasificación se excluyen.
Mover un documento puede cambiar su categoría: requiere revisión de permisos.

## Pipeline

| Etapa | Implementación |
| --- | --- |
| Descubrimiento | `app/ingestion/reconciler.py`: categorías y cambios SHA-256 |
| Extracción | `loaders.py`, `isolated_extraction.py`, `extract_worker.py` |
| Fragmentación | `app/rag/chunking.py`: bloques estructurales y procedencia |
| Embedding | `embedding_prompts.py` y `InferenceClient` |
| Visibilidad | `index_manifest.py`: pipeline, fingerprint y generaciones activas SQL |
| Almacén | `vector_store.py`: Qdrant embedded o server |
| Recuperación | `retriever.py`: filtro → fetch → dedup → MMR/diversidad → top_k |
| Respuesta | `knowledge_agent.py` y `grounding.py` |

## Valores predeterminados

| Variable | Valor |
| --- | ---: |
| `RAG_CHUNK_SIZE_TOKENS` | 900 estimados |
| `RAG_CHUNK_OVERLAP_TOKENS` | 120 estimados |
| `RAG_EMBEDDING_DIMENSION` | 768 |
| `RAG_FETCH_K` | 24 |
| `RAG_TOP_K` | 6 |
| `RAG_MIN_SIMILARITY` | 0.35 |
| `RAG_MMR_LAMBDA` | 0.65 |
| `RAG_REINDEX_INTERVAL_HOURS` | 24 |
| `PIPELINE_VERSION` | 7 |

La estimación usa palabras × 1.3; no es el tokenizador real del modelo. El límite
incluye solape y redondeo. Una página/hoja distinta abre otro fragmento y no
hereda solape. Se conserva procedencia para citas; los bloques que superan el
presupuesto se dividen. Revisar tablas y procedimientos largos sobre corpus real.

## Autorización antes de recuperar

El corpus usa `matrix_rh_corporate` y filtros de categorías efectivas. Los adjuntos
usan `matrix_rh_private` y el par propietario/conversación. Se añaden la huella
compatible y las generaciones activas del manifest SQL dentro de la consulta a
Qdrant. El resumen privado hace scroll por metadata y ACL; no relaja el alcance
ni aplica un umbral semántico para excluir partes del adjunto.

El índice se invalida al cambiar pipeline, modelo, dimensión, revisión/digest o
plantillas. Las entradas de embedding no se truncan silenciosamente: un error
de tamaño o dimensión aborta esa indexación. Reingerir tanto corpus como
privados desde los archivos originales y comprobar sus SHA. No editar payloads
para simular compatibilidad.

## Escritura y borrado

La nueva generación se escribe antes de activarla con commit SQL. Un fallo
previo al commit puede dejar puntos invisibles en Qdrant; el manifest anterior
permanece como referencia. `index_cleanup_pending` solicita limpiar generaciones
anteriores. El borrado lógico desactiva el manifest; la limpieza física ocurre
posteriormente. No hay transacción ACID compartida entre MySQL y Qdrant.

Restaurar conjuntamente SQL, corpus, uploads y Qdrant. El índice por sí solo no
contiene todos los permisos, propietarios o datos necesarios para reconstruir
la aplicación.

## Evaluación y límites

`backend/tests/rag_eval/golden_set.yaml` y `scripts.rag_eval` comprueban recuperación,
ACL y, con `--full`, generación/grounding. Registrar corpus, modelos/digests,
configuración y resultados. Ajustar parámetros sólo tras comparar evidencia,
no para ocultar ausencia de permisos o documentos.

La recuperación es densa con coseno, deduplicación, MMR y diversidad. No hay BM25
ni reranker. Los formatos actuales son DOCX, MD, PDF textual, TXT, XLSX y CSV.
No se implementan OCR, DOC legacy ni PPTX. `grounded=true` no certifica exactitud
semántica, vigencia o cobertura completa del corpus.

Ver [Modelos y rendimiento](MODELOS_Y_RENDIMIENTO.md) antes de cambiar embeddings,
contexto o parámetros para intentar mejorar una respuesta.

## Adjuntos, bajas y reconciliación

Esperar `indexed` antes de resumir un adjunto. La recuperación mantiene dueño y
conversación; no publica el archivo como corporativo. Al trasladar una instalación
se comprueba SHA dentro del almacenamiento privado restaurado, sin buscar archivos
de otra conversación.

El scheduler reconcilia cada 24 horas por defecto y comparte lock SQL con la CLI.
`ingest --force` reconstruye aunque el SHA no cambie: no concede acceso ni omite la
huella. Una raíz inaccesible conserva sus documentos con aviso; una baja detectada
en una fuente disponible y recorrida completamente retira su visibilidad.

La eliminación SQL retira mensajes/resúmenes/respuestas y revoca el manifest de
adjuntos antes de confirmar. Después se purgan vectores y bytes del namespace
exacto; un fallo deja `purged_at=NULL` y metadata mínima para reintentar sin
seguir enlaces fuera del root. Los archivos huérfanos tras rollback también
consumen cuota y se incluyen en la purga. Ver [RUNBOOK.md](RUNBOOK.md).

El golden set contiene **43 casos**, conservando los 36 originales y añadiendo
preguntas RH ambiguas; 23 tienen anotaciones grounded. Las regresiones aisladas
no sustituyen `scripts.rag_eval --full` contra embeddings, modelos y corpus reales.
No se añadió BM25 ni un reranker: requiere benchmark de recuperación, citas,
ACL, latencia y coste con los pesos aprobados antes de elegir arquitectura.



## Compatibilidad con 1.3.0 y control numérico

La revisión del extractor es 4 y `PIPELINE_VERSION` es 7, heredadas de 1.3.0. Se aplican las
validaciones de tamaño/tipo/compresión antes de extraer documentos de carpetas y
se limitan filas físicas y columnas XLSX en el lector. La huella distingue
revisiones de extracción anteriores. Si se reutiliza un índice incompatible,
reconciliar/reingerir antes de esperar evidencia. El ZIP revisado conserva los
documentos recibidos, pero no incluye índices de uso ni estado SQL. La actualización
conserva el estado ya instalado; los ejemplos ficticios quedan fuera de la ingesta.

Las citas corporativas conservan la ruta relativa; los adjuntos privados incorporan
el ID de documento para distinguir nombres iguales. Se conserva la autorización
por categoría/propietario/conversación, incluyendo la revalidación al publicar.

1.3.1 integra RAG-01: el verificador `cited` contrasta cantidades con su unidad,
cardinales españoles simples y porcentajes de tablas compatibles. Reconoce casos
como «un préstamo»/«1 préstamo» y «quinquenio»/«5 años» cuando la fuente lo permite;
no admite cantidades inventadas, unidades distintas ni referencias desconocidas.
Mantiene la abstención si una respuesta no supera los controles. Este cambio no
modifica la huella de ingesta respecto de 1.3.0 ni exige reindexar por sí mismo.
Una restauración desde versiones anteriores o con modelos distintos sí exige
revisar compatibilidad antes de recuperar evidencia.

Se distinguen ausencia de evidencia autorizada y rechazo de una síntesis generada.
El evento `agent.answer_validation_failed` registra la causa y conteos, sin volcar
documentos o respuestas. `grounded=true` sigue sin ser verificación semántica total:
probar citas, tablas, negaciones, condiciones y casos sin respuesta con Gemma real.

## Citas y recuperación de respuestas rechazadas

Esta revisión entrega al modelo etiquetas breves como `[[E1]]` para documentos
y `[[S1]]` para resultados estructurados. Se calculan para la evidencia que cabe
en **esa llamada**, después de ajustar el contexto. El servidor las convierte en
identificadores canónicos antes de verificar la respuesta; el navegador conserva
las fuentes originales. No se adivinan nombres de archivos parecidos ni se
aceptan fuentes fuera del mapa autorizado.

El reintento vuelve a construir su mapa para la evidencia realmente enviada.
Los alias desconocidos, las citas inventadas, las cifras sin respaldo y las
fuentes descartadas del contexto siguen sujetos al verificador. Las etiquetas
facilitan copiar una cita larga; no conceden permisos ni prueban la interpretación.

Si la primera respuesta mezcla información documentada con orientación general
citada, el reintento exige sólo la sección documental y afirmaciones respaldadas.
No basta cambiar un encabezado para aceptar una suposición. Se conservan controles
de procedencia, números, unidades, ACL y publicación; si vuelven a fallar se
mantiene la abstención. Los datos que el usuario incluye en una pregunta tampoco
se convierten automáticamente en evidencia del PDF.

Estos cambios corrigen el transporte de referencias y el manejo de una respuesta
rechazada. No sustituyen la extracción, recuperación ni aceptación semántica con
Gemma real. Repetir las preguntas del usuario sobre pensiones y fondo de ahorro
comparando citas, porcentajes, condiciones y texto original.
