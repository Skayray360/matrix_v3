# Creado por Aldo Garcia.
"""Validacion de archivos subidos (seccion 17).

Controles, en orden:

1. allowlist de extensiones (no denylist: una denylist siempre se queda corta);
2. verificacion de **magic bytes** frente a la extension declarada, para detectar
   spoofing de MIME;
3. tamano maximo configurable;
4. nombre interno UUID -- el ``filename`` del cliente jamas se usa como ruta;
5. normalizacion y bloqueo de ``..``, separadores y rutas absolutas;
6. almacenamiento fuera del web root;
7. limites anti-zip-bomb para formatos OOXML.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.etree import ElementTree

from app.common.errors import FileTooLargeError, UnsupportedFileError, ValidationFailedError
from app.common.ids import new_id, sha256_bytes
from app.common.logging import get_logger
from app.config import get_settings
from app.ingestion.loaders import mime_for_extension

logger = get_logger(__name__)

#: Firmas de archivo por extension. Un ``.pdf`` que no empieza por %PDF- se
#: rechaza aunque el navegador declare ``application/pdf``.
_MAGIC_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
}

#: Formatos OOXML: contenedores ZIP sujetos a limites anti-bomba.
_OOXML_PARTS = {
    ".docx": (
        "word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    ),
    ".xlsx": (
        "xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    ),
    ".pptx": (
        "ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    ),
}
_OOXML_EXTENSIONS = frozenset(_OOXML_PARTS)
_CONTENT_TYPES_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"

#: Ratio maximo de descompresion tolerado.
MAX_COMPRESSION_RATIO = 120
#: Tamano maximo total descomprimido de un OOXML.
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")


@dataclass(frozen=True, slots=True)
class UploadValidation:
    """Resultado de validar un archivo entrante."""

    original_filename: str
    safe_display_name: str
    #: Nombre con el que se guarda en disco: UUID + extension validada.
    internal_filename: str
    extension: str
    mime_type: str
    size_bytes: int
    sha256: str


def sanitize_display_name(filename: str) -> str:
    """Nombre seguro para mostrar y almacenar en BD.

    Se toma solo el ultimo componente de la ruta y se eliminan caracteres que
    puedan alterar rutas o inyectarse en el HTML de la UI.
    """
    candidate = PurePosixPath(str(filename or "").replace("\\", "/")).name
    candidate = _SAFE_NAME_RE.sub("_", candidate).strip(" .")
    if not candidate:
        candidate = "documento"
    if len(candidate) > 200:
        # El limite es del nombre visible, no una conversion de tipo. Un nombre
        # largo de Windows debe conservar .pdf/.docx al validar y al reindexar.
        suffix = Path(candidate).suffix
        if suffix and len(suffix) < 20:
            return candidate[:200 - len(suffix)] + suffix
    return candidate[:200]


def assert_safe_relative_path(relative_path: str) -> str:
    """Valida una ruta relativa dentro del knowledge root.

    Rechaza rutas absolutas, ``..`` y separadores de Windows disfrazados. Es la
    barrera contra path traversal en la ingesta corporativa.
    """
    raw = str(relative_path or "").replace("\\", "/")
    if not raw.strip():
        raise ValidationFailedError("Ruta relativa vacia.")
    # Se comprueba ANTES de normalizar: quitar la barra inicial convertiria
    # "/etc/passwd" en una ruta relativa aparentemente valida.
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValidationFailedError("Ruta relativa no permitida.")

    normalized = raw.strip("/")
    if not normalized:
        raise ValidationFailedError("Ruta relativa vacia.")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        raise ValidationFailedError("Ruta relativa no permitida.")
    # Win32 reconoce streams alternativos (archivo:stream), dispositivos y
    # componentes con puntos/espacios finales que PurePosixPath no normaliza.
    # Rechazarlos antes de acceder a disco evita alias de la ruta validada.
    if any(
        re.search(r'[<>:"|?*\x00-\x1f]', part)
        or part != part.rstrip(" .")
        or PureWindowsPath(part).is_reserved()
        for part in path.parts
    ):
        raise ValidationFailedError("Ruta relativa no permitida.")
    return str(path)


def resolve_within(root: Path, relative_path: str) -> Path:
    """Resuelve una ruta garantizando que no escapa de ``root``.

    Se comparan las rutas ya resueltas: es la unica comprobacion fiable frente a
    enlaces simbolicos y a normalizaciones inesperadas del sistema de archivos.
    """
    safe_relative = assert_safe_relative_path(relative_path)
    root_resolved = root.resolve()
    target = (root_resolved / safe_relative).resolve()
    if not target.is_relative_to(root_resolved):
        logger.warning("security.path_traversal_blocked")
        raise ValidationFailedError("Ruta fuera del directorio permitido.")
    return target


def _check_ooxml_bomb(data: bytes, extension: str) -> None:
    """Limites anti-zip-bomb para contenedores OOXML."""
    if extension not in _OOXML_EXTENSIONS:
        return
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise UnsupportedFileError("El archivo contiene demasiadas entradas internas.")
            total_uncompressed = sum(info.file_size for info in entries)
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise UnsupportedFileError("El archivo se expande por encima del limite permitido.")
            if data and total_uncompressed / max(1, len(data)) > MAX_COMPRESSION_RATIO:
                raise UnsupportedFileError("Ratio de compresion sospechoso; archivo rechazado.")
            for info in entries:
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in PurePosixPath(name).parts:
                    raise UnsupportedFileError("El archivo contiene rutas internas no permitidas.")
            names = [info.filename for info in entries]
            if len(names) != len(set(names)):
                raise UnsupportedFileError("El archivo contiene entradas internas duplicadas.")
            main_part, content_type = _OOXML_PARTS[extension]
            if main_part not in names or "[Content_Types].xml" not in names:
                raise UnsupportedFileError("El contenedor no corresponde al formato Office declarado.")
            manifest = archive.getinfo("[Content_Types].xml")
            if manifest.file_size > 1024 * 1024:
                raise UnsupportedFileError("El manifiesto Office excede el limite permitido.")
            raw = archive.read(manifest)
            # Quitar ceros solo para detectar declaraciones tambien en UTF-16
            # o UTF-32; el parser sigue recibiendo el XML original.
            if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw.replace(b"\x00", b""), re.I):
                raise UnsupportedFileError("El archivo Office contiene declaraciones XML no permitidas.")
            root = ElementTree.fromstring(raw)  # noqa: S314 - XML <=1MB, DTD/entidades rechazadas arriba
            if root.tag != f"{_CONTENT_TYPES_NS}Types" or not any(
                item.tag == f"{_CONTENT_TYPES_NS}Override"
                and item.get("PartName") == f"/{main_part}"
                and item.get("ContentType") == content_type
                for item in root
            ):
                raise UnsupportedFileError("El contenido Office no corresponde a su extension.")
            if any("vbaproject" in name.lower() for name in names) or b"macroenabled" in raw.lower():
                raise UnsupportedFileError("Convierta el archivo a un formato Office sin macros.")
    except (zipfile.BadZipFile, ElementTree.ParseError, RuntimeError, NotImplementedError, EOFError) as exc:
        raise UnsupportedFileError("El archivo no es un contenedor OOXML valido.", detail=type(exc).__name__) from exc


def validate_upload(data: bytes, *, filename: str) -> UploadValidation:
    """Valida un archivo entrante y devuelve sus metadatos seguros."""
    settings = get_settings()

    if not data:
        raise ValidationFailedError("El archivo esta vacio.")
    if len(data) > settings.upload_max_bytes:
        raise FileTooLargeError(
            f"El archivo excede el maximo de {settings.upload_max_bytes // (1024 * 1024)} MB."
        )

    display_name = sanitize_display_name(filename)
    extension = Path(display_name).suffix.lower()
    if extension in {".doc", ".ppt"}:
        raise UnsupportedFileError(
            "Formato Office antiguo no compatible. Convierta localmente a DOCX o PPTX y vuelva a cargarlo."
        )
    if extension not in settings.allowed_upload_extensions:
        raise UnsupportedFileError(f"Extension no permitida: {extension or '(sin extension)'}")

    signatures = _MAGIC_SIGNATURES.get(extension)
    if signatures and not any(data.startswith(sig) for sig in signatures):
        logger.warning("security.mime_spoof_blocked", extra={"file_extension": extension})
        raise UnsupportedFileError("El contenido del archivo no corresponde a su extension.")

    _check_ooxml_bomb(data, extension)

    return UploadValidation(
        original_filename=str(filename),
        safe_display_name=display_name,
        internal_filename=f"{new_id()}{extension}",
        extension=extension,
        mime_type=mime_for_extension(extension),
        size_bytes=len(data),
        sha256=sha256_bytes(data),
    )


def read_bounded(stream, *, max_bytes: int | None = None) -> bytes:
    """Limita la lectura aunque no exista Content-Length o se omita el proxy."""
    limit = max_bytes if max_bytes is not None else get_settings().upload_max_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(64 * 1024, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise FileTooLargeError()
        chunks.append(chunk)
