<!-- Creado por Aldo Garcia. -->

# documentacion_empresarial/

Categoría efectiva: `documentacion_empresarial`.

| Nivel | Grupo |
|---|---|
| Administrador | `HCM_ADM_DOCEMPRESARIAL_MX` |
| Coordinador | `HCM_COORD_DOCEMPRESARIAL_MX` |

Dominio propio para documentación corporativa general de la empresa. Sensibilidad
`internal`, pero de acceso **nominal** (`wildcard_eligible: false`): no lo cubre el
comodín del administrador de negocio; sólo lo ve quien tenga su grupo/rol, además
de `HCM_EMP_BASICO_MX`.

Coloca aquí tus archivos (`.docx`, `.md`, `.pdf`, `.txt`, `.xlsx`, `.csv`). Este
`README.md` no se indexa. Tras copiar archivos, ejecuta la reconciliación
(`scripts.bootstrap ingest`).

## Contenido y reglas de la carpeta

Esta carpeta no distribuye archivos funcionales adicionales a este README.

Colocar nuevos archivos de la misma responsabilidad junto al codigo existente; actualizar este README cuando cambie el contenido. No colocar: Codigo, claves, logs y README usados como evidencia. Los README de organizacion no se indexan.
