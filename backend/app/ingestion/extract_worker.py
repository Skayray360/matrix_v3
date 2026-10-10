# Creado por Aldo Garcia.
"""Proceso descartable de extraccion; stdin, red y secretos no forman parte del contrato."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.common.errors import ExtractionFailedError, MatrixError
from app.ingestion.loaders import extract_document
from app.ingestion.local_ocr import OcrOptions


def main() -> None:
    source, destination, name, max_chars, *ocr = sys.argv[1:]
    try:
        options = OcrOptions(**json.loads(ocr[0])) if ocr else OcrOptions()
        extracted = extract_document(Path(source).read_bytes(), filename=name, ocr_options=options)
        if sum(len(block.text) for block in extracted.blocks) > int(max_chars):
            raise ExtractionFailedError("El texto extraido excede el limite operativo.")
        payload = asdict(extracted)
    except MatrixError as exc:
        # Solo mensajes publicos del catalogo; nunca detalles del parser/rutas.
        payload = {"error": {"code": str(exc.code), "message": exc.message}}
    except Exception:  # noqa: BLE001 - no serializar mensajes arbitrarios del parser
        payload = {"error": {
            "code": "extraction_failed", "message": "El documento no pudo procesarse de forma segura.",
        }}
    Path(destination).write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    main()
