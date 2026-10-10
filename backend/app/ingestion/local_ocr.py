# Creado por Aldo Garcia.
"""OCR optativo con ejecutables locales existentes, sin descarga ni servicio web."""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - ejecutables locales fijos, sin shell
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.common.errors import ExtractionFailedError


@dataclass(frozen=True, slots=True)
class OcrOptions:
    enabled: bool = False
    language: str = "spa"
    dpi: int = 150

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_+-]+", self.language) or not 100 <= self.dpi <= 300:
            raise ExtractionFailedError("La configuracion del OCR local no es valida.")


def ocr_pdf_page(data: bytes, page_number: int, options: OcrOptions) -> str:
    """Rasteriza una sola pagina acotada y lee su texto en el worker aislado."""
    renderer = shutil.which("pdftoppm")
    engine = shutil.which("tesseract")
    if not renderer or not engine:
        raise ExtractionFailedError("El OCR local requiere pdftoppm y Tesseract instalados en PATH.")
    if page_number < 1:
        raise ExtractionFailedError("El numero de pagina OCR no es valido.")
    try:
        languages = subprocess.run(  # noqa: S603 - ruta encontrada del binario fijo
            [engine, "--list-langs"], check=True, timeout=5,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ).stdout.decode("utf-8", errors="replace").splitlines()
        if not set(options.language.split("+")).issubset({line.strip() for line in languages}):
            raise ExtractionFailedError("El idioma solicitado para OCR no esta instalado en Tesseract.")
        with tempfile.TemporaryDirectory(prefix="matrix-ocr-") as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "page"
            source.write_bytes(data)
            # El limite de lado impide rasterizar lienzos PDF arbitrariamente
            # grandes; el watchdog externo cuenta tambien RAM de estos hijos.
            subprocess.run(  # noqa: S603 - argumentos de datos, nunca shell
                [renderer, "-f", str(page_number), "-l", str(page_number),
                 "-r", str(options.dpi), "-scale-to", "3500", "-singlefile", "-png", str(source), str(output)],
                check=True, timeout=30, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            result = subprocess.run(  # noqa: S603 - argumentos de datos, nunca shell
                [engine, str(output.with_suffix(".png")), "stdout", "-l", options.language, "--psm", "3"],
                check=True, timeout=30, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            if len(result.stdout) > 8 * 1024 * 1024:
                raise ExtractionFailedError("La salida OCR excede el limite permitido.")
            return result.stdout.decode("utf-8", errors="replace")
    except ExtractionFailedError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExtractionFailedError("La pagina no pudo procesarse con el OCR local dentro de sus limites.") from exc
