<!-- Creado por Aldo Garcia. -->

# reclutamiento/ (compatibilidad)

Corpus heredado de Reclutamiento. Se conserva como categoría
`reclutamiento` para que una actualización no elimine ni duplique conocimiento
ya indexado.

- Entradas: documentación oficial existente del proceso de reclutamiento.
- Salida: chunks corporativos con categoría `reclutamiento`.
- Dependencias: política y grupos autorizados para ese dominio.

La ubicación normativa para contenido nuevo es
`especializadas/reclutamiento/`. No mueva documentos durante una actualización
automática: prepare la migración, reindexe y valide un usuario autorizado y otro
no autorizado. Este `README.md` se excluye de la ingesta.

## Contenido y reglas de la carpeta

| Archivo | Contenido |
| --- | --- |
| `proceso-de-reclutamiento.md` | Archivo funcional del proyecto; revisar su configuracion antes de modificarlo. |

Colocar nuevos archivos de la misma responsabilidad junto al codigo existente; actualizar este README cuando cambie el contenido. No colocar: Codigo, claves, logs y README usados como evidencia. Los README de organizacion no se indexan.
