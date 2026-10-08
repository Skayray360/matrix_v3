<!-- Creado por Aldo Garcia. -->

# backend/scripts/

Scripts operativos. Los BAT y los PowerShell de la raíz **delegan aquí**: la
lógica vive en un solo sitio.

| Script | Qué hace |
|---|---|
| `preflight.py` | Comprobación del entorno; `--json`, `--read-only`, `--dependencies-only`, `--llm-only` |
| `diagnosticar_rag.py` | Metadata de recuperación/permisos, sin inferencia ni apertura de Qdrant embedded |
| `verify_documentation.py` | Verifica destinos de enlaces locales antes de empaquetar; no valida anclas |
| `uv_runtime.py` | Compatibilidad del gestor antes de instalar paquetes; stdlib, `--banner`, `--target` |
| `bootstrap.py` | `migrate`, `seed`, `ingest`, `setup`, `serve`, `pin-models`, `status --read-only`, `knowledge-status`, `upgrade-config` |
| `release_transfer.py` | Traslado entre entregas completas detenidas; preserva estado y no mezcla código |
| `configuration_upgrade.py` | Respaldo de `.env` antes de migrar ajustes operativos; conserva secretos y pines |
| `clean_legacy_layout.py` | Limpieza acotada de lanzadores/documentación/asset heredados, con respaldo y comprobación de hash |
| `runtime_control.py` | Solicitud local de parada cooperativa ligada al PID del proceso |
| `purge.py` | Vista previa de bajas/retencion; `--apply` confirma el lote. |
| `rag_eval.py` | Golden set del RAG; `--retrieval`, `--full`, `--types` |
| `secrets_scan.py` | Escaneo de secretos con clasificación y supresiones auditables |
| `run_quality_gate.py` | Coordina lint, typing, pruebas, SAST, dependencias, RAG y E2E |
| `smoke_authorization.py` | Matriz `Matrix` vs `MatrixR1` de punta a punta |
| `load_entra_mapping.py` | Carga el mapeo de grupos de Entra ID |
| `local_model_diagnostics.py` | Diagnóstico de modelos; consultar el alcance de cada flag antes de usar inferencia |

## Ejecución

Desde la raíz del proyecto en `cmd.exe`:

```bat
set "PYTHONPATH=%CD%\backend"
.venv\Scripts\python.exe -m scripts.preflight
```

Use `--read-only` para un probe que no aplica migraciones, seeds ni ingesta. En
Windows, `DIAGNOSTICO_MATRIX_RH.bat` ya ejecuta esa variante y añade puertos,
procesos, metadata RAG/modelos y logs. `-ProbarModelos` solicita inferencia
sintética y requiere backend detenido; es una operación activa.

`release_transfer.py` se conserva como herramienta de compatibilidad para
traslados deliberados. La actualización actual utiliza `INSTALAR_MATRIX_RH.bat`
en la misma raíz; no necesita traslado ni otro lanzador.

Con `--dependencies-only` se comprueban Python y los paquetes obligatorios,
sin cargar Settings, leer `.env` o conectar MySQL/Ollama/Qdrant. Si faltan
paquetes, todos los modos devuelven codigo 1 y un resultado accionable antes
de importar la configuracion. `--llm-only` y `--dependencies-only` son excluyentes.

`--llm-only` puede ejecutar embeddings para comprobar dimensión; no confundirlo
con metadatos de sólo lectura. Los comandos de migración, seed, ingesta, traslado,
actualización de configuración y purga aplicada escriben estado. Respaldo y permisos
siguen siendo obligatorios; la existencia del comando no autoriza usarlo en producción.

[Operación](../../docs/RUNBOOK.md) · [Pruebas](../../docs/TESTING.md) ·
[Modelos](../../docs/MODELOS_Y_RENDIMIENTO.md).
