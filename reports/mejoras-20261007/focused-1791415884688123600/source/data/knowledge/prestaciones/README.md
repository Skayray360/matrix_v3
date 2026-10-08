<!-- Creado por Aldo Garcia. -->

# prestaciones/ (compatibilidad)

Corpus heredado de prestaciones, vacaciones, aguinaldo y fondo de ahorro. La
reconciliación conserva la categoría efectiva `prestaciones` para no romper una
instalación existente.

- Entradas: `.docx`, `.md`, `.pdf`, `.txt`, `.xlsx` o `.csv` oficiales.
- Salida: chunks corporativos con categoría `prestaciones` y filtro RBAC.
- Dependencias: política `prestaciones` y roles vigentes en el backend.

No agregue aquí documentación nueva si ya existe una clasificación aprobada en
`general/` o `especializadas/<dominio>/`. Migre sólo mediante un cambio
controlado de política, pruebas positivas y negativas y reindexación; mover un
archivo cambia su categoría efectiva. Este `README.md` se excluye de la ingesta.

## Contenido y reglas de la carpeta

| Archivo | Contenido |
| --- | --- |
| `fondo-de-ahorro-y-aguinaldo.md` | Archivo funcional del proyecto; revisar su configuracion antes de modificarlo. |
| `politica-vacaciones.md` | Archivo funcional del proyecto; revisar su configuracion antes de modificarlo. |

Colocar nuevos archivos de la misma responsabilidad junto al codigo existente; actualizar este README cuando cambie el contenido. No colocar: Codigo, claves, logs y README usados como evidencia. Los README de organizacion no se indexan.
