<!-- Creado por Aldo Garcia. -->

# Documentos para Matrix RH

El corpus original vive en `data/knowledge`. Se conserva al actualizar y se
consulta junto con la ruta personalizada `RAG_KNOWLEDGE_ROOT` del `.env`.

La version 1.2.8 tambien reconoce estas carpetas directas dentro de `data`:

| Carpeta | Categoria aplicada | Tratamiento |
| --- | --- | --- |
| Prestaciones | prestaciones | Beneficios y politicas de prestaciones |
| Nomina / Nómina | nomina | Categoria legacy; no juntar nomina confidencial |
| Compensaciones | compensaciones | Politicas del dominio, sin ampliar permisos |
| Seguridad | salud_ambiental | Salud ocupacional, seguridad, higiene y ambiente |
| Gastos medicos / Gastos médicos | prestaciones | Beneficios de seguro y reembolso; no expedientes clinicos personales |
| ACR | Sin definir | Se omite y avisa hasta aprobar su categoria |

El mapeo se configura en [knowledge-layout.yaml](../config/knowledge-layout.yaml).
No distingue mayusculas ni acentos del nombre de carpeta. Cada categoria destino
debe estar declarada en [categories.yaml](../config/authorization/categories.yaml).
Asignar una carpeta a una categoria **no concede acceso** a usuarios o roles.
Los permisos de consulta se mantienen en la politica de autorizacion existente.

Los PDF pueden estar directamente en `data/Prestaciones/` o en sus subcarpetas.
No es necesario moverlos a `data/knowledge` ni cambiar el `.env` para que esta
version los encuentre. Despues de copiar documentos hay que ejecutar la ingesta
o esperar la reconciliacion programada; copiar un PDF por si solo no lo indexa.
El inventario informa archivos admitidos, PDF y categorias, pero no prueba que
un PDF tenga texto. La extraccion lo comprueba en un proceso limitado: los PDF
escaneados sin texto requieren OCR previo, que Matrix no ejecuta.

Se admiten PDF, TXT, MD, DOCX, XLSX y CSV. Se omiten README, carpetas ocultas,
enlaces simbolicos/junctions y formatos no compatibles. `synthetic_test_data`,
`var/uploads` y carpetas desconocidas de `data` no se incluyen como documentos
corporativos. Los adjuntos privados mantienen su namespace por usuario y chat.

La reconciliacion conserva el identificador de documentos sin cambios y no crea
otra version al adoptar una carpeta legacy con los mismos bytes. Si una raiz no
esta disponible o no se puede recorrer completa, conserva sus documentos y
emite un aviso; si un archivo desaparece de una raiz disponible, retira su indice.

Subcarpetas del paquete: [knowledge/](knowledge/README.md) y
[synthetic_test_data/](synthetic_test_data/README.md).
