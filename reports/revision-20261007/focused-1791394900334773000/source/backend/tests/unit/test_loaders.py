# Creado por Aldo Garcia.
"""Extractores por formato. Fixtures 100% sinteticos generados en memoria."""

from __future__ import annotations

import io

import pytest

from app.common.errors import ExtractionFailedError, UnsupportedFileError
from app.ingestion.loaders import (
    extract_csv,
    extract_document,
    extract_markdown,
    extract_txt,
    supported_extensions,
)

pytestmark = pytest.mark.unit


class TestFormatosSoportados:
    def test_los_formatos_obligatorios_estan_presentes(self):
        soportados = supported_extensions()
        for extension in (".docx", ".md", ".pdf", ".txt", ".xlsx"):
            assert extension in soportados

    def test_csv_es_una_mejora_soportada(self):
        assert ".csv" in supported_extensions()

    def test_extension_desconocida_se_rechaza(self):
        with pytest.raises(UnsupportedFileError):
            extract_document(b"contenido", filename="archivo.exe")

    def test_sin_extension_se_rechaza(self):
        with pytest.raises(UnsupportedFileError):
            extract_document(b"contenido", filename="archivo")


class TestTextoPlano:
    def test_extrae_utf8(self):
        resultado = extract_txt(b"Politica de vacaciones\n\nSon 12 dias.")
        assert "vacaciones" in resultado.plain_text()

    def test_detecta_cp1252(self):
        resultado = extract_txt("Nómina y antigüedad".encode("cp1252"))
        assert resultado.plain_text().strip() != ""

    def test_bom_utf8_no_contamina(self):
        resultado = extract_txt("﻿Contenido".encode())
        assert not resultado.plain_text().startswith("﻿")

    def test_bytes_invalidos_no_rompen(self):
        resultado = extract_txt(b"texto valido \xff\xfe mas texto")
        assert "texto valido" in resultado.plain_text()


class TestMarkdown:
    def test_preserva_encabezados_y_listas(self):
        contenido = "# Titulo\n\n## Seccion\n\n- uno\n- dos\n"
        resultado = extract_markdown(contenido.encode())
        kinds = {block.kind for block in resultado.blocks}
        assert "heading" in kinds
        assert "list" in kinds

    def test_registra_la_seccion_actual(self):
        resultado = extract_markdown(b"## Prestaciones\n\nTexto de la seccion.\n")
        assert any(block.section == "Prestaciones" for block in resultado.blocks)


class TestCsv:
    def test_convierte_filas_a_pares_columna_valor(self):
        contenido = b"departamento,personas\nOperaciones,120\nCalidad,35\n"
        resultado = extract_csv(contenido)
        texto = resultado.plain_text()
        assert "departamento: Operaciones" in texto
        assert "personas: 120" in texto

    def test_detecta_punto_y_coma_como_separador(self):
        resultado = extract_csv(b"a;b\n1;2\n")
        assert "a: 1" in resultado.plain_text()


class TestDocx:
    def test_extrae_titulos_parrafos_y_tablas(self):
        docx = pytest.importorskip("docx")
        document = docx.Document()
        document.add_heading("Politica de vacaciones", level=1)
        document.add_paragraph("Se otorgan segun la antiguedad.")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Antiguedad"
        table.cell(0, 1).text = "Dias"
        table.cell(1, 0).text = "1 anio"
        table.cell(1, 1).text = "12"
        buffer = io.BytesIO()
        document.save(buffer)

        resultado = extract_document(buffer.getvalue(), filename="politica.docx")
        texto = resultado.plain_text()
        assert "Politica de vacaciones" in texto
        assert "Se otorgan segun la antiguedad." in texto
        assert "1 anio" in texto and "12" in texto
        assert any(block.kind == "table" for block in resultado.blocks)

    def test_un_docx_corrupto_falla_de_forma_controlada(self):
        with pytest.raises(ExtractionFailedError):
            extract_document(b"PK\x03\x04 basura que no es docx", filename="roto.docx")


class TestXlsx:
    def test_conserva_hoja_encabezados_y_filas(self):
        openpyxl = pytest.importorskip("openpyxl")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Plantilla"
        sheet.append(["departamento", "personas"])
        sheet.append(["Operaciones", 120])
        sheet.append(["Calidad", 35])
        buffer = io.BytesIO()
        workbook.save(buffer)

        resultado = extract_document(buffer.getvalue(), filename="plantilla.xlsx")
        texto = resultado.plain_text()
        assert "Hoja: Plantilla" in texto
        assert "departamento: Operaciones" in texto
        assert any(block.page_or_sheet == "Plantilla" for block in resultado.blocks)

    def test_no_evalua_formulas(self):
        """``data_only=True``: se lee el valor cacheado, nunca se ejecuta la formula."""
        openpyxl = pytest.importorskip("openpyxl")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["calculo"])
        sheet.append(["=1+1"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        resultado = extract_document(buffer.getvalue(), filename="formulas.xlsx")
        assert "=1+1" not in resultado.plain_text()


class TestPdf:
    def test_pdf_invalido_falla_de_forma_controlada(self):
        with pytest.raises(ExtractionFailedError):
            extract_document(b"%PDF-1.4 pero el resto es basura", filename="roto.pdf")

    def test_pdf_sin_texto_avisa_en_lugar_de_inventar(self):
        pypdf = pytest.importorskip("pypdf")
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buffer = io.BytesIO()
        writer.write(buffer)

        resultado = extract_document(buffer.getvalue(), filename="escaneado.pdf")
        assert resultado.is_empty
        assert any("sin texto extraible" in warning for warning in resultado.warnings)
