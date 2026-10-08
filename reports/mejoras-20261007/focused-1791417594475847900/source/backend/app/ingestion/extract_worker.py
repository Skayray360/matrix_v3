# Creado por Aldo Garcia.
"""Proceso descartable de extraccion; stdin, red y secretos no forman parte del contrato."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.ingestion.loaders import extract_document


def main() -> None:
    source, destination, name, max_chars = sys.argv[1:]
    extracted = extract_document(Path(source).read_bytes(), filename=name)
    if sum(len(block.text) for block in extracted.blocks) > int(max_chars):
        raise ValueError("El texto extraido excede el limite operativo")
    Path(destination).write_text(json.dumps(asdict(extracted)), encoding="utf-8")


if __name__ == "__main__":
    main()
