<!-- Creado por Aldo Garcia. -->
# Cambios de Matrix RH

## Corrección de limpieza en Windows — 6 de octubre de 2026

- Se corrige el falso cambio de archivo al comparar `lstat` y `fstat`: los bits
  de ejecución y `ctime` pueden diferir entre ambas APIs. Se conservan identidad,
  tipo, tamaño, modificación, hashes y validación antes/después de cada lectura.
- Los fallos de limpieza ahora indican código, fase, ruta relativa y error del
  sistema; no exponen contenido ni rutas externas. La retirada requiere respaldo.
- Regresiones para diferencias de atributos, sustitución de archivos y errores
  de respaldo; contrato de limpieza agregado al job Windows de CI.
- Se conserva carpeta, versión interna, cuatro BAT, configuración y corpus.

## Revisión del 6 de octubre de 2026 — misma instalación

- Carpeta `matrix-rh-1.3.0` conservada; versión interna 1.3.1 sin renumeración.
- Cuatro BAT: instalar, diagnosticar, iniciar y detener. La instalación repetida
  actualiza la misma carpeta y conserva la conexión SQL y el estado existente.
- Diagnóstico de modelos y metadata RAG integrado; `-ProbarModelos` solicita
  deliberadamente las sondas sintéticas con el backend detenido.
- Reserva de IA vinculada al proceso local para recuperar un propietario muerto
  comprobado sin esperar el TTL. Un propietario vivo o desconocido sigue protegido.
- Parada de procesos identificados de la instalación y sus descendientes; WAMP
  y Ollama compartidos permanecen disponibles.
- Citas breves por llamada, convertidas a sus fuentes originales antes de validar,
  y reintento documental específico si la primera respuesta mezcló procedencias.
  Se conservan validación numérica, permisos y abstención ante falta de respaldo.
- Documentación duplicada retirada por rutas y hashes conocidos; PDF del ZIP
  recibido conservados. Guías adaptadas a actualización sobre la carpeta actual.

Las pruebas de esta revisión y sus límites se registran en
[VALIDACION_1.3.1.md](reports/VALIDACION_1.3.1.md). Los resultados de una entrega
anterior no validan estos cambios. La calidad con Gemma/GPU en destino sigue
requiriendo aceptación sobre las preguntas y los PDF originales.

## 1.3.1 — entrega completa y documentación ordenada

- README con estructura visual fiel a React/TypeScript, FastAPI, IA local y datos.
- Guías canónicas por responsabilidad; documentación redundante e informes
  históricos retirados del ZIP tras consolidar información útil.
- RAG-01 integrado: equivalencias numéricas conservadoras con unidades/fuentes y
  mensaje distinto para respuesta generada que no supera validación.
- Diagnóstico RAG de metadata en `backend/scripts`, sin inferencia ni apertura del
  almacén Qdrant embebido; permisos, modelos y contratos se conservan.
- Guía única de modelos/rendimiento con límites, procedencia y mediciones pendientes,
  sin prometer rendimiento máximo ni capacidad multiusuario no medida.
- Históricamente se ofreció instalación en carpeta nueva. La revisión actual
  reemplaza ese procedimiento por actualización sobre la carpeta existente.
- Comprobación de enlaces locales en los controles de documentación/entrega.
- Frontend compilado incluido; no cambia el diseño ni exige Node para operar.

Resultados actuales y omisiones: [VALIDACION_1.3.1.md](reports/VALIDACION_1.3.1.md).
La preparación no equivale a instalación Windows/GPU/SSO real ni a aceptación de
todos los PDF con Gemma en destino. Los originales anteriores permanecen disponibles.

## Base 1.3.0 conservada

Gemma único con perfiles FAST/DEEP, EmbeddingGemma independiente, cola persistente,
memoria con permisos, ingesta protegida, alias documentales, frontend same-origin,
migraciones incrementales y corpus sintético separado. El detalle y la evidencia
histórica pertenecen al ZIP 1.3.0, no se presentan como pruebas de esta revisión.
