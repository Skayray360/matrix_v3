<!-- Creado por Aldo Garcia. -->
# Matrix RH · 1.3.1

Asistente de Recursos Humanos para consultar, comparar y resumir documentos autorizados, con citas verificables y conversaciones privadas. La inferencia, los embeddings y el almacenamiento de información se ejecutan localmente.

## Guía rápida

| Necesito… | Ir a… |
| --- | --- |
| Instalar por primera vez | [Instalación Windows/WAMP](docs/INSTALACION.md) |
| Iniciar o diagnosticar | [Operación](#operación-en-powershell) |
| Incorporar documentos | [Sincronización de conocimiento](docs/SINCRONIZACION_CONOCIMIENTO.md) |
| Entender permisos y adjuntos | [Autorización](docs/AUTHENTICATION_AUTHORIZATION.md) |
| Medir respuestas y uso del modelo | [Modelos y rendimiento](docs/MODELOS_Y_RENDIMIENTO.md) |
| Repetir la validación actual | [Mejoras del 7 de octubre](docs/MEJORAS_2026-10-07.md) |
| Resolver un error | [Diagnóstico](docs/TROUBLESHOOTING.md) y [runbook](docs/RUNBOOK.md) |

## Arquitectura

| Componente | Responsabilidad |
| --- | --- |
| React, TypeScript y Vite | Chat, historial, fuentes y administración |
| FastAPI | API, permisos, cola y validación de respuestas |
| MySQL/MariaDB en WAMP | Identidades, conversaciones, documentos y auditoría |
| Qdrant embebido | Índices de fragmentos corporativos y privados |
| Ollama local | `gemma4:latest` para generación y `embeddinggemma:latest` para embeddings de 768 dimensiones |

Los perfiles rápido y profundo pueden usar el mismo modelo con presupuestos distintos. La memoria del diálogo no sustituye las fuentes. Una cita acredita la procedencia y los controles implementados; no demuestra por sí sola la veracidad semántica de toda la respuesta. Véanse [arquitectura](docs/ARCHITECTURE.md) y [diseño RAG](docs/RAG_DESIGN.md).

## Operación en PowerShell

Desde esta carpeta, con la instalación y los modelos locales ya preparados:

```powershell
Set-Location C:\wamp64\www\matrix-rh-1.3.0
.\DIAGNOSTICO_MATRIX_RH.bat
.\INICIAR_MATRIX_RH.bat -NoBrowser
```

Abrir la dirección que indique el arranque (por defecto `http://127.0.0.1:8000`). Para detener de forma cooperativa:

```powershell
.\DETENER_MATRIX_RH.bat
```

El arranque valida integridad y configuración. No iniciar varios procesos contra la misma carpeta de Qdrant embebido. Si falta el entorno, seguir la guía de instalación antes de ejecutar `INSTALAR_MATRIX_RH.bat`. Las credenciales están en la configuración privada; no se distribuyen en este README.

## Base de conocimiento

La carga administrativa incorpora documentos corporativos inmediatamente. También se pueden colocar archivos nuevos en las carpetas reconocidas: con `SCHEDULER_ENABLED=true`, el backend busca altas al arrancar y cada 60 segundos. Los nuevos documentos indexados quedan disponibles para las siguientes consultas de usuarios con permiso sobre su categoría, sin reiniciar ni entrenar el modelo.

1. Elegir una categoría aprobada; por ejemplo, `data/knowledge/especializadas/prestaciones/`.
2. Publicar un PDF, TXT, MD, DOCX, XLSX o CSV completo. Para copias largas, copiar con extensión `.tmp` y renombrar al terminar.
3. Esperar la sincronización o solicitarla mediante la API administrativa.
4. Comprobar el estado de indexación y hacer una pregunta con respuesta conocida, documento y página. Copiar un archivo no acredita que se haya extraído correctamente.

La sincronización rápida solo incorpora archivos nuevos. Las modificaciones y bajas siguen el proceso completo existente de reconciliación. Los adjuntos del chat conservan su alcance privado; no pasan al corpus corporativo. El [procedimiento completo](docs/SINCRONIZACION_CONOCIMIENTO.md) explica estados, reintentos, permisos, categorías y comandos manuales.

## Configuración

Usar [.env.example](.env.example) como referencia y conservar el `.env` local. Los valores efectivos se validan en [settings.py](backend/app/config/settings.py).

| Variable | Función |
| --- | --- |
| `RAG_KNOWLEDGE_ROOT` | Raíz adicional; también se reconocen `data/knowledge` y los alias configurados |
| `RAG_SYNC_INTERVAL_SECONDS` | Intervalo de altas nuevas; predeterminado 60 s |
| `RAG_SYNC_STABILITY_SECONDS` | Edad mínima del archivo antes de incorporarlo; predeterminado 10 s |
| `RAG_REINDEX_INTERVAL_HOURS` | Reconciliación completa existente; predeterminado 24 h |
| `SCHEDULER_ENABLED` | Activa tareas periódicas del backend |
| `ANSWER_EVIDENCE_MODE` | Contrato de evidencia: `cited` o `extractive` |
| `ANSWER_STRUCTURED_OUTPUT` | Transporte JSON interno opcional; desactivado hasta evaluarlo con el modelo instalado |
| `OLLAMA_*`, `LLM_*` | Modelos, ventanas, salida, tiempos y generación |
| `CHAT_MAX_INFLIGHT`, `INFERENCE_MAX_INFLIGHT` | Admisión de solicitudes e inferencias locales |

La categoría determina el ámbito del documento, pero no concede permisos. No aumentar concurrencia o ventanas sin medir memoria, latencia y calidad. Los cambios de embeddings deben seguir el contrato de compatibilidad del índice.

## Estructura del proyecto

```text
backend/         API, agentes, RAG, ingesta, scripts y pruebas
frontend/        Interfaz React y pruebas
config/          Accesos, categorías y mapeo de carpetas
data/knowledge/  Documentos corporativos por categoría
data/            Alias de conocimiento y fixtures sintéticos separados
var/             Datos operativos, índices, adjuntos y registros privados
windows/         Arranque, diagnóstico, instalación y parada
docs/            Guías técnicas y de operación
reports/         Evidencias de validación y respaldos de código
```

## Pruebas y seguridad

Las pruebas se ejecutan en almacenamiento sintético aislado. MySQL, Ollama, la GPU y el RAG real requieren validación adicional en el equipo destino. Los [resultados y comandos reproducibles](docs/MEJORAS_2026-10-07.md) distinguen pruebas aprobadas, fallos previos y comprobaciones pendientes.

Consultar [TESTING](docs/TESTING.md) y [SECURITY](docs/SECURITY.md). Conservar `.env`, corpus y `var` al actualizar; nunca usar la base operativa para suites destructivas. Los respaldos y manifiestos de esta revisión permiten revertir únicamente sus cambios de código sin borrar documentos ni conversaciones.
