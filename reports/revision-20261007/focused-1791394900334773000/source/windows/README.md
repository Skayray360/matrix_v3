<!-- Creado por Aldo Garcia. -->
# Herramientas Windows

La guía principal es [Instalación y actualización](../docs/INSTALACION.md).
Esta referencia describe lanzadores y opciones, sin duplicar ese procedimiento.

| Lanzador de la raíz | Acción |
| --- | --- |
| `INSTALAR_MATRIX_RH.bat` | Instalar o actualizar dependencias, configuración, migraciones y corpus en la misma carpeta |
| `INICIAR_MATRIX_RH.bat` | Verificar y arrancar un proceso backend de esta carpeta |
| `DETENER_MATRIX_RH.bat` | Cerrar los procesos identificados de la instancia; no detiene WAMP/Ollama compartidos |
| `DIAGNOSTICO_MATRIX_RH.bat` | Leer dependencias, puertos, procesos, metadata RAG/modelos y logs |

Son los únicos cuatro BAT de la raíz. Para ejecutar chat, JSON y embeddings
sintéticos con el backend detenido, usar **`DIAGNOSTICO_MATRIX_RH.bat -ProbarModelos`**.
Esta opción consume recursos y puede cargar modelos. El diagnóstico ordinario
no ejecuta inferencia ni abre Qdrant embebido.

PowerShell 5.1+ se invoca desde BAT con bypass sólo para ese proceso. No se cambia
la política global. La raíz usa Python 3.12 x64; `.venv` se crea para esa ubicación.

## Dependencias reproducibles y opciones

Si falta uv se prepara 0.12.19 en `var/tools/uv`; se acepta el rango
`>=0.11.33,<0.13.0` para un uv ya instalado. El wheel Windows x64 y su SHA-256 están
fijados en `uv-bootstrap-requirements.txt`. El backend usa
`uv sync --frozen --no-dev`, sin resolver nuevamente las versiones del lock.
La instalación requiere el índice de paquetes aprobado o las cachés adecuadas.

| Opción de instalación | Efecto |
| --- | --- |
| Sin parámetros | Usar frontend compilado y preparar dependencias Python fijadas |
| `-RebuildFrontend` | Requiere Node/Corepack, comprueba lock/árbol y recompila |
| `-SkipFrontend` | Alias compatible para usar `dist`; no combinar con `-RebuildFrontend` |
| `-SkipIngest` | Omitir indexación inicial de documentos |
| `-Offline` | No descargar uv; usar cachés Windows x64 previamente preparadas |
| `-WithAutostart` | Registrar arranque al iniciar sesión, sólo tras instalación correcta |

El ZIP no incorpora instaladores externos, pesos ni todas las cachés. Node sólo
se necesita para reconstrucción; sus requisitos están en [frontend](../frontend/README.md).

## Guardas operativas

- `.env` nuevo recibe secretos aleatorios. No se imprimen claves ni se restablecen
  contraseñas de cuentas existentes al repetir seed/instalación.
- Se conserva MySQL de WAMP; no se cambian usuarios/contraseñas del servidor.
  Bases ajenas se rechazan. Adoptar una base vacía de TI requiere decisión explícita.
- Ollama se reutiliza; si está cerrado puede intentarse `ollama serve` local con
  `OLLAMA_NO_CLOUD=1`. No se hace `ollama pull` ni se sustituyen pesos.
- El diagnóstico ordinario no arranca modelos, migra ni abre Qdrant embebido.
  `-ProbarModelos` sí ejerce inferencia y consume recursos; ejecutarlo deliberadamente.
- Un puerto ocupado no autoriza terminar procesos ajenos. La parada comprueba
  pertenencia al proyecto; un PID reutilizado no basta para identificarlo.
- Un entorno virtual trasladado no se reutiliza: se conserva con sufijo previo y
  se prepara el nuevo. No distribuir esos respaldos dentro del ZIP.
- El guard de integridad detecta mezcla de código; no editar hashes para ocultarla.
  Un checksum acredita coherencia de bytes, no identidad del publicador.

La actualización se hace en **`C:\wamp64\www\matrix-rh-1.3.0`**: detener,
respaldar, sustituir código y ejecutar `INSTALAR_MATRIX_RH.bat`. No trasladar
estado a otra carpeta. El instalador conserva la conexión SQL y respalda cambios
operativos de `.env`; las migraciones se aplican sobre esa base. También retira
archivos heredados mediante una lista exacta, con respaldo y protección de
Markdown personalizados. Revisar [el procedimiento completo](../docs/INSTALACION.md).

## Reinicio después de la parada

La parada tiene hasta 30 s de gracia antes de terminar procesos Matrix que
pertenezcan a esta raíz, incluyendo descendientes identificados. Una reserva
v2 de un propietario local muerto se recupera sin esperar su expiración. Las
reservas ajenas, vivas o desconocidas no se borran a ciegas; una UUID heredada
puede exigir esperar una sola expiración. El arranque conserva un límite de
readiness de 180 s por defecto (`-ReadyTimeoutSeconds`) para fallos de servicios,
sin el antiguo plazo adicional asociado a la duración de una consulta de IA.

Instalar/iniciar/detener utilizan exclusión mutua y rechazan solapamientos.
No se promete arranque instantáneo de modelos fríos ni cancelación GPU de Ollama
cuando se detiene el proceso Matrix. Ver [operación](../docs/RUNBOOK.md).

## Validación de desarrollo

`Validate-MatrixRH.ps1` puede crear/borrar datos de prueba. Requiere `APP_ENV=test`,
base dedicada y `MATRIX_TEST_ALLOW_DESTRUCTIVE=true`; no ejecutarlo contra uso real.
Las pruebas PowerShell omitidas por falta de Windows/pwsh no cuentan como aprobadas.
Contratos sintéticos tampoco acreditan WAMP, BitLocker, GPU, pesos o SSO reales.

[Pruebas](../docs/TESTING.md) · [Operación](../docs/RUNBOOK.md) ·
[Diagnóstico](../docs/TROUBLESHOOTING.md).
