# Creado por Aldo Garcia.
"""Lectura local de diapositivas OOXML; sin Office, macros, enlaces ni ejecucion."""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree

from app.common.errors import ExtractionFailedError
from app.rag.chunking import TextBlock

MAX_SLIDES = 500
MAX_SHAPES_PER_SLIDE = 2000
MAX_TABLE_ROWS = 5000
MAX_TABLE_COLUMNS = 100
MAX_XML_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000
_NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def _xml(archive: zipfile.ZipFile, name: str):
    entry = archive.getinfo(name)
    if entry.file_size > MAX_XML_BYTES:
        raise ExtractionFailedError("Una parte XML de la presentacion excede el limite permitido.")
    raw = archive.read(entry)
    # ElementTree no consulta recursos externos, pero una entidad interna puede
    # expandir memoria. OOXML no necesita DTD; rechazarla tambien en UTF-16.
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw.replace(b"\x00", b""), re.IGNORECASE):
        raise ExtractionFailedError("La presentacion contiene declaraciones XML no permitidas.")
    return ElementTree.fromstring(raw)  # noqa: S314 - DTD/entidades rechazadas arriba


def _paragraphs(body) -> list[str]:
    result = []
    for paragraph in body.findall("a:p", _NS):
        pieces = []
        for element in paragraph.iter():
            if element.tag == f"{{{_NS['a']}}}t" and element.text:
                pieces.append(element.text)
            elif element.tag == f"{{{_NS['a']}}}br":
                pieces.append("\n")
        text = "".join(pieces).strip()
        if text:
            result.append(text)
    return result


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def extract_pptx_blocks(data: bytes) -> tuple[list[TextBlock], list[str]]:
    """Conserva el orden de presentation.xml, que puede diferir de slideN.xml."""
    blocks: list[TextBlock] = []
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if (len(entries) > MAX_ARCHIVE_ENTRIES
                    or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES
                    or sum(item.file_size for item in entries) / max(1, len(data)) > 120):
                raise ExtractionFailedError("La presentacion excede los limites del contenedor OOXML.")
            if len({item.filename for item in entries}) != len(entries):
                raise ExtractionFailedError("La presentacion contiene partes duplicadas ambiguas.")
            presentation = _xml(archive, "ppt/presentation.xml")
            relationships = _xml(archive, "ppt/_rels/presentation.xml.rels")
            slide_paths: dict[str, str] = {}
            for relation in relationships:
                if not relation.get("Type", "").endswith("/slide"):
                    continue
                target = relation.get("Target", "")
                if (relation.get("TargetMode", "").casefold() == "external"
                        or target.startswith(("/", "\\")) or ":" in target or "\\" in target):
                    raise ExtractionFailedError("La presentacion referencia una diapositiva externa o no valida.")
                name = posixpath.normpath(posixpath.join("ppt", target))
                if not name.startswith("ppt/slides/") or not name.endswith(".xml"):
                    raise ExtractionFailedError("La ruta de una diapositiva no es valida.")
                relation_id = relation.get("Id", "")
                if not relation_id or relation_id in slide_paths:
                    raise ExtractionFailedError("La presentacion contiene relaciones de diapositiva ambiguas.")
                slide_paths[relation_id] = name
            slides = presentation.findall("p:sldIdLst/p:sldId", _NS)
            if len(slides) > MAX_SLIDES:
                raise ExtractionFailedError(f"La presentacion excede el limite de {MAX_SLIDES} diapositivas.")
            for number, slide in enumerate(slides, 1):
                relation_id = slide.get(f"{{{_NS['r']}}}id", "")
                root = _xml(archive, slide_paths[relation_id])
                locator = f"diapositiva {number}"
                section = locator
                shapes = root.findall(".//p:sp", _NS)
                tables = root.findall(".//a:tbl", _NS)
                if len(shapes) + len(tables) > MAX_SHAPES_PER_SLIDE:
                    raise ExtractionFailedError("Una diapositiva excede el limite de elementos.")
                for shape in shapes:
                    placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", _NS)
                    body = shape.find("p:txBody", _NS)
                    if (body is not None and placeholder is not None
                            and placeholder.get("type") in {"title", "ctrTitle"}):
                        section = " ".join(_paragraphs(body)) or locator
                        break
                before = len(blocks)
                # El orden del arbol conserva cuadros y tablas; no materializa
                # imagenes, notas privadas, objetos OLE o relaciones externas.
                for element in root.iter():
                    if element.tag == f"{{{_NS['p']}}}sp":
                        body = element.find("p:txBody", _NS)
                        if body is None:
                            continue
                        paragraphs = _paragraphs(body)
                        placeholder = element.find("p:nvSpPr/p:nvPr/p:ph", _NS)
                        title = placeholder is not None and placeholder.get("type") in {"title", "ctrTitle"}
                        if paragraphs:
                            text = "\n".join(paragraphs)
                            blocks.append(TextBlock(
                                f"## {text}" if title else text, section, locator,
                                "heading" if title else "paragraph", level=2 if title else 0,
                            ))
                    elif element.tag == f"{{{_NS['a']}}}tbl":
                        rows = element.findall("a:tr", _NS)
                        if len(rows) > MAX_TABLE_ROWS:
                            raise ExtractionFailedError("Una tabla de la presentacion excede el limite de filas.")
                        lines = []
                        if any(
                            cell.get("gridSpan", "1") != "1" or cell.get("rowSpan", "1") != "1"
                            for cell in element.findall("a:tr/a:tc", _NS)
                        ):
                            warnings.append(
                                f"Diapositiva {number}: tabla con celdas combinadas; "
                                "revisar su estructura contra el original."
                            )
                        for row in rows:
                            cells = row.findall("a:tc", _NS)
                            if len(cells) > MAX_TABLE_COLUMNS:
                                raise ExtractionFailedError(
                                    "Una tabla de la presentacion excede el limite de columnas."
                                )
                            values = []
                            for cell in cells:
                                body = cell.find("a:txBody", _NS)
                                values.append(_cell(" ".join(_paragraphs(body))) if body is not None else "")
                            if any(values):
                                lines.append("| " + " | ".join(values) + " |")
                        if lines:
                            blocks.append(TextBlock("\n".join(lines), section, locator, "table"))
                if len(blocks) == before:
                    warnings.append(f"Diapositiva {number}: sin texto extraible; imagenes y notas no se indexan.")
    except ExtractionFailedError:
        raise
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError, OSError, RuntimeError, ValueError) as exc:
        raise ExtractionFailedError("La presentacion no pudo abrirse (protegida o corrupta).") from exc
    return blocks, warnings
