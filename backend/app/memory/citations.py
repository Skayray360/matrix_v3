# Creado por Aldo Garcia.
"""Metadatos de citas generados por el servidor, sin texto de evidencia."""

from __future__ import annotations

from math import isfinite
from typing import Any

# Solo estos campos forman parte del contrato publico SourceRef. No persistir
# texto recuperado, filas SQL, URLs arbitrarias ni propiedades del modelo.
_TEXT_FIELDS = {
    "category": 64, "filename": 200, "section": 1024,
    "page_or_sheet": 512, "label": 1024, "scope": 32,
}


def citation_metadata(source_ids: Any, details: Any) -> list[dict[str, Any]]:
    """Conserva IDs citados y amplia solo coincidencias inequívocas de metadata.

    Un historial anterior sin localizadores sigue mostrando el ID conocido; no
    se infiere una pagina a partir del indice del fragmento ni del nombre.
    """
    identifiers = list(dict.fromkeys(
        sid for sid in (source_ids if isinstance(source_ids, list | tuple) else ())
        if isinstance(sid, str) and 0 < len(sid) <= 2048
    ))
    matched: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for detail in (details if isinstance(details, list) else ()):
        if not isinstance(detail, dict) or detail.get("source_id") not in identifiers:
            continue
        sid = detail["source_id"]
        item: dict[str, Any] = {"source_id": sid}
        for key, limit in _TEXT_FIELDS.items():
            value = detail.get(key)
            if isinstance(value, str) and len(value) <= limit:
                item[key] = value
        score = detail.get("score")
        if type(score) in (int, float) and -1e308 <= score <= 1e308 and isfinite(score):
            item["score"] = score
        if sid in matched and matched[sid] != item:
            ambiguous.add(sid)
        matched[sid] = item
    return [matched[sid] if sid in matched and sid not in ambiguous else {"source_id": sid}
            for sid in identifiers]
