<!-- Creado por Aldo Garcia. -->

# nomina/ (compatibilidad)

Corpus heredado de Nómina. La reconciliación mantiene la categoría efectiva
`nomina` exclusivamente para compatibilidad con instalaciones anteriores.

- Entradas: documentos de Nómina General ya existentes.
- Salida: chunks corporativos con categoría legacy `nomina`.
- Dependencias: política legacy y grupos de Nómina General.

No publique aquí contenido nuevo ni información confidencial. Los documentos
nuevos deben clasificarse físicamente en
`especializadas/nomina_general/` o
`especializadas/nomina_confidencial/`, con políticas y grupos independientes.
Una migración requiere autorización, pruebas ACL y reindexación. Este
`README.md` se excluye de la ingesta.

## Contenido y reglas de la carpeta

| Archivo | Contenido |
| --- | --- |
| `politica-de-nomina.md` | Archivo funcional del proyecto; revisar su configuracion antes de modificarlo. |

Colocar nuevos archivos de la misma responsabilidad junto al codigo existente; actualizar este README cuando cambie el contenido. No colocar: Codigo, claves, logs y README usados como evidencia. Los README de organizacion no se indexan.
