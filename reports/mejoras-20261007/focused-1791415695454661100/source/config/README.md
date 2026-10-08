<!-- Creado por Aldo Garcia. -->
# Configuración declarativa

| Archivo | Responsabilidad |
| --- | --- |
| `authorization/categories.yaml` | Taxonomía, sensibilidad, propietario y elegibilidad para wildcard |
| `authorization/entra-role-mapping.yaml` | Roles internos y grupos/app roles externos |
| `data_sources/sources.yaml` | Fuentes SQL, entidades/columnas, filtros y referencias a secretos |
| `knowledge-layout.yaml` | Alias de carpetas documentales; `ACR` no se indexa sin clasificación |
| `model-manifest.json` | Inventario y estado de procedencia de modelos |
| `supply_chain_denylist.yaml` | Restricciones declarativas de dependencias |

Los YAML se leen como datos mediante cargador seguro y validación de esquema.
No colocar DSN, passwords ni tokens: `secret_ref` es el nombre de la variable privada,
no su valor. Un alias o categoría no concede acceso por sí mismo.

El perfil corporativo exige rol base más grupos especializados. Una categoría con
`wildcard_eligible: false` requiere concesión nominal. El mapeo distribuido usa
valores de app role `HCM_*`; si TI usa grupos, debe configurar object IDs reales
y `external_kind: group`, no confundirlos con nombres visibles.

Desde la raíz, revisar antes de modificar SQL:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
& .\.venv\Scripts\python.exe -m scripts.load_entra_mapping --dry-run
```

Aplicar el mapeo sin `--dry-run` sólo tras aprobarlo. `--prune` retira mapeos ya no
declarados y requiere revisar impacto; el login también sincroniza roles obsoletos.

Las fuentes SQL de ejemplo están deshabilitadas. Requieren cuenta read-only real,
concesión del servidor y alcance `row_scope: user` con atributos firmados, o una
vista nominal aprobada `row_scope: role_view`. No basta con `enabled: true`.

[Permisos](../docs/AUTHENTICATION_AUTHORIZATION.md) ·
[Conectores SQL](../docs/integrations/DATABASE_CONNECTORS.md) ·
[Ingesta y categorías](../docs/RAG_DESIGN.md) ·
[Modelos](../docs/MODELOS_Y_RENDIMIENTO.md).
