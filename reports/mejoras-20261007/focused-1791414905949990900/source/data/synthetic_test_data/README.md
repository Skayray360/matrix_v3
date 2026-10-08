<!-- Creado por Aldo Garcia. -->
# Datos sintéticos de prueba

Los seis documentos de `knowledge/` contienen políticas ficticias heredadas del
repositorio de pruebas. No son políticas de Peñoles ni Fresnillo y no se indexan
en una instalación normal. El scanner excluye esta raíz deliberadamente.

Los tests unitarios leen estos archivos directamente para verificar anotaciones,
extracción y chunking. Para ejecutar el golden set con RAG real, usar una instalación
de pruebas y una base desechable: copiar únicamente `knowledge/` a una raíz de
pruebas fuera de esta carpeta reservada y establecer `RAG_KNOWLEDGE_ROOT` allí.
Nunca mezclar estos ejemplos con el corpus de negocio. Ver `docs/TESTING.md`.
