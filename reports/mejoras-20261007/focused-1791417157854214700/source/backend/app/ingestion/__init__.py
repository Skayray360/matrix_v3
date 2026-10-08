# Creado por Aldo Garcia.
"""Ingestion: extraccion, chunking, embeddings, upsert y reconciliacion incremental."""

from app.ingestion.loaders import ExtractedDocument, extract_document, supported_extensions

__all__ = ["ExtractedDocument", "extract_document", "supported_extensions"]
