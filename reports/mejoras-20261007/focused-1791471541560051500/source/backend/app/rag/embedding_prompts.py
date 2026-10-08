# Creado por Aldo Garcia.
"""Prefijos de tarea para ``embeddinggemma:latest``.

EmbeddingGemma es un modelo *instruido*: espera que la consulta y el documento se
presenten con prefijos distintos. Sin ellos, consulta y documento caen en
regiones distintas del espacio vectorial y la similitud coseno se hunde.

Sintoma observado durante la construccion: la pregunta "con que frecuencia se
realizan los simulacros" no recuperaba el parrafo que dice literalmente
"los simulacros se realizan de forma trimestral" -- el score quedaba por debajo
del umbral de 0.35. Con los prefijos correctos el mismo par supera holgadamente
el umbral.

Estos prefijos forman parte del contrato de indexacion: si se cambian, hay que
reindexar (por eso ``INGESTION_VERSION`` los acompana).
"""

from __future__ import annotations

#: Prefijo de consulta recomendado por el modelo para busqueda semantica.
QUERY_TEMPLATE = "task: search result | query: {text}"
#: Prefijo de documento. ``title`` mejora el anclaje cuando el chunk viene de una
#: seccion con encabezado; se usa "none" cuando no hay titulo, como indica el
#: propio formato del modelo.
DOCUMENT_TEMPLATE = "title: {title} | text: {text}"


def format_query(text: str) -> str:
    """Prepara una consulta para embeber."""
    from app.config import get_settings

    return get_settings().llm_query_template.format(text=text.strip())


def format_document(text: str, *, title: str = "") -> str:
    """Prepara un chunk de documento para embeber."""
    clean_title = (title or "none").strip() or "none"
    from app.config import get_settings

    return get_settings().llm_document_template.format(title=clean_title, text=text.strip())
