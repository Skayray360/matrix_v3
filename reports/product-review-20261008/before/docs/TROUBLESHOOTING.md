<!-- Creado por Aldo Garcia. -->

# Diagnóstico y recuperación

| Síntoma | Causa que comprobar | Acción |
| --- | --- | --- |
| `ModuleNotFoundError` / FAIL dependencias | Venv sin paquetes o intérprete equivocado | Ejecutar instalación desde la raíz; revisar sync y comprobación de imports |
| Instalador exige otra versión uv | Mezcla de scripts antiguos o banner incompatible | Usar esta entrega completa; rango admitido `>=0.11.33,<0.13.0` |
| Limpieza cancela tras sincronizar dependencias | Comparación de atributos corregida en esta entrega, o fallo real de ruta/respaldo | Sustituir el código y manifiesto completos por esta corrección; repetir Instalar. Si persiste, revisar `code`, `detail`, `phase`, `path`, `errno` y `winerror` |
| Backend sin escucha 8000 | Instalación/startup detenido o preflight fallido | Corregir primer FAIL y ejecutar `INICIAR_MATRIX_RH.bat` |
| MySQL puerto abierto pero conexión falla | DSN, usuario, base o permisos | Revisar `DATABASE_URL` privado y salida de bootstrap |
| Qdrant 6333 sin escucha | Modo embedded activo o servidor detenido | En embedded no se necesita ese puerto; en server verificar URL/servicio |
| Ollama disponible, modelo ausente | Nombre/pesos distintos del `.env` | Preparar modelo configurado y repetir preflight LLM |
| Timeout con Ollama 600 s | Límite de generación por perfil de 180 s, global de 600 s o valores heredados | Revisar límites efectivos, perfil/modelo y referencia de operación; ejecutar sonda sintética |
| «Respuesta incompleta» con Ollama abierto | Límite de salida, thinking sin contenido final o finalización no admitida | Con backend detenido, ejecutar `DIAGNOSTICO_MATRIX_RH.bat -ProbarModelos`; revisar `finish_reason`, `thinking_chars`, `output_token_limit` y `failure_kind` |
| La interfaz conserva el borrador sin respuesta | Operación fallida, expirada o resultado todavía no confirmado | Buscar referencia en el log; reenviar el mismo texto si sólo se perdió la conexión evita duplicarlo |
| «Evidencia insuficiente» tras ingesta | ACL/categoría, fingerprint, texto o ranking | Revisar manifest, rol, extracción y golden set; ingesta no concede acceso |
| «Encontré documentación relacionada, pero no pude validar…» | Se recuperó evidencia pero la respuesta no superó citas/contraste | Correlacionar `agent.answer_validation_failed`; no desactivar permisos ni inventar cifras para evitar la abstención |
| PDF de `data/Prestaciones` no aparece | Carpeta/mapeo, texto extraíble o ingesta pendiente | Ejecutar `knowledge-status`, comprobar categoría `prestaciones` y reconciliar con backend embedded detenido |
| Índice locked | Otro proceso usa Qdrant embedded | Detener el propietario antes de abrirlo desde CLI/validación |
| 503/429 o cola llena | Cupos/ritmo/lease/operación pendiente | Esperar estado; medir capacidad, no añadir workers para eludir lease |
| Conversación histórica no visible | Alcance antiguo sin fingerprint verificable | Conservar dato para revisión; no asignar permisos nuevos automáticamente |
| UI sin assets | Falta build o root equivocado | Confirmar `frontend/dist/index.html`, instalar/recompilar cuando corresponda |
| OIDC falla | Redirect/origen/issuer/TLS/client/grupos | Validar flujo con TI; no activar fallback local en producción |

## Dependencias incompletas en Windows

Si el error aparece en **«Retirando entradas antiguas con respaldo»**, la
sincronización de dependencias ya concluyó, pero la instalación todavía no
terminó. No necesita recrear la base, borrar `.venv` ni volver a cargar los PDF.
Esta corrección compara de forma compatible los metadatos de Windows y conserva
los controles de respaldo e integridad. Reemplazar el contenido de código del ZIP
en la misma carpeta, incluido **SHA256SUMS.txt**, y ejecutar Instalar de nuevo.

Si vuelve a fallar, el JSON indica el archivo relativo y la fase. `file_in_use`
requiere cerrar el proceso que tiene el archivo abierto; `permission_denied`
requiere revisar sus permisos/atributo de sólo lectura; `disk_full` indica falta
de espacio para respaldar. Una ruta con enlace/junction se rechaza. Compartir ese
JSON permite precisar el siguiente paso. No desactivar la limpieza ni el control
de integridad para ocultar un error real.

Desde PowerShell en la raíz de esta versión:

```powershell
uv --version
.\INSTALAR_MATRIX_RH.bat -SkipFrontend
.\DIAGNOSTICO_MATRIX_RH.bat
```

El instalador sincroniza el lock antes de importar configuración y verifica los
módulos runtime. El preflight puede informar paquetes ausentes sin traceback.
Si el sync falla, guardar su error concreto; instalar `pydantic` aislado no
completa el resto de dependencias del proyecto.

No copiar `.venv` entre rutas, editar migraciones aplicadas, manipular ACL en
payloads Qdrant ni publicar secretos en capturas. La actualización se realiza
sobre `C:\wamp64\www\matrix-rh-1.3.0`, con parada y respaldo previos, conservando
configuración/datos y usando el instalador idempotente: [INSTALACION.md](INSTALACION.md).

## Servicio disponible y generación real

`/health` 200 acredita que el proceso responde. `/ready` verifica dependencias,
no ejecuta una consulta del modelo. Un HTTP 200 del polling sólo acredita que
se pudo consultar la operación; su resultado debe llegar a `completed`.
Un `.err.log` vacío tampoco demuestra ausencia de errores: la aplicación
publica sus logs JSON principales en `backend-*.log`.

`LLM_REQUEST_DEADLINE_SECONDS=600` limita la consulta completa;
`LLM_FAST_TIMEOUT_SECONDS=180` y `LLM_DEEP_TIMEOUT_SECONDS=180` limitan
cada generación. El transporte de 600 s no amplía por sí solo estos límites.
Los valores personalizados de `.env` y del proceso prevalecen.

La detención solicita cierre cooperativo y concede hasta 30 s antes de terminar
los procesos identificados de esa carpeta y sus descendientes. Una caída local
comprobada del propietario de una reserva v2 permite recuperarla sin esperar
el TTL. Una reserva viva, ajena o antigua sin identidad comprobable sigue protegida.
El arranque usa `ReadyTimeoutSeconds` (180 s predeterminados), sin sumar el plazo
global de inferencia. Ese límite es una espera máxima por disponibilidad, no
una pausa obligatoria antes de cada inicio.

Consulte [MODELOS_Y_RENDIMIENTO.md](MODELOS_Y_RENDIMIENTO.md) para aislar generación y
embeddings con datos sintéticos. No cambie permisos, usuarios o contraseñas
para corregir una lentitud de inferencia.

## RAG responde sin información aunque existen PDF

Primero distinguir servicios, recuperación y validación: `/ready=true` no prueba
que la pregunta recupere el pasaje correcto ni que Gemma produzca una respuesta válida.
El diagnóstico general ya incluye metadata RAG/modelos. Para revisar explícitamente
el usuario que realizó la consulta, desde la raíz instalada:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
& .\.venv\Scripts\python.exe -m scripts.diagnosticar_rag --root . --username Matrix
```

El diagnóstico consulta metadata SQL e inventario de modelos; no genera embeddings,
no ejecuta inferencia, no abre Qdrant embedded y no modifica permisos o documentos.
Revisar su JSON antes de compartirlo. Está diseñado para omitir secretos y contenido
de documentos, prompts y respuestas.

| Evidencia observada | Siguiente revisión |
| --- | --- |
| Usuario sin categoría efectiva | Rol/mapeo aprobados; la ingesta no da permisos |
| Documentos sin chunks o sin generación compatible | Extracción, errores por archivo, pins y huella de índice |
| `rag.retrieved` con cero resultados | Pregunta, filtros, umbral/ranking y presencia real del pasaje |
| Evidencia recuperada + `agent.answer_validation_failed` | Fuentes citadas y causa de rechazo de la síntesis |

RAG-01 integrado corrige equivalencias numéricas documentadas, pero no garantiza
resolver cualquier fallo de recuperación o generación. Si hay evidencia y rechazo,
guardar la causa de `agent.answer_validation_failed`: no basta con saber que el
PDF existe. Repetir en conversación nueva preguntas de números, tablas,
condiciones y ausencia de evidencia con los modelos reales.

## Reserva del servicio de IA

«Este proceso no posee el servicio de IA» puede indicar otra API activa o una
reserva durable. Ejecutar `DIAGNOSTICO_MATRIX_RH.bat` y comprobar proceso, raíz
y propietario. No eliminar `job_locks`, no matar procesos ajenos ni abrir varios
workers para sortear la protección.

- **Reserva v2 local:** identifica equipo/raíz, PID, fecha de creación y nonce.
  Si el proceso murió o el PID fue reutilizado, el arranque la recupera sin
  esperar el vencimiento. Un proceso vivo mantiene su propiedad.
- **Reserva de otra raíz/equipo o no verificable:** no se recupera automáticamente.
  Detener al propietario correcto o resolver la causa antes de iniciar otra API.
- **Reserva heredada con UUID solamente:** no permite demostrar quién la posee.
  Puede ser necesaria su expiración una vez al aplicar esta revisión. Después,
  las reservas nuevas incorporan identidad de proceso. No atribuirles un PID
  inventado ni borrar toda la tabla.

Para repetir un inicio después de una parada correcta no debe existir una espera
fija por TTL. Sí puede haber carga de dependencias/modelos o un servicio caído.
Compartir sólo `request_id`, códigos y metadata revisada, sin secretos.
