# Creado por Aldo Garcia.
"""Chunking estructural."""

from __future__ import annotations

import pytest

from app.rag.chunking import (
    HEADING_BREAK_MIN_TOKENS,
    TextBlock,
    chunk_blocks,
    chunk_text,
    estimate_tokens,
    split_into_blocks,
)

pytestmark = pytest.mark.unit


DOCUMENTO = """# Politica de Vacaciones

## 1. Objetivo
Establecer las reglas de otorgamiento de vacaciones para el personal.

## 2. Dias por antiguedad

| Antiguedad | Dias |
| --- | --- |
| 1 anio | 12 |
| 2 anios | 14 |

## 3. Procedimiento
1. Solicitar con 15 dias de anticipacion.
2. El jefe autoriza.
3. RH registra el periodo.
"""


class TestSplitIntoBlocks:
    def test_detecta_encabezados_con_su_nivel(self):
        blocks = split_into_blocks(DOCUMENTO)
        headings = [b for b in blocks if b.kind == "heading"]
        assert headings[0].level == 1
        assert headings[1].level == 2
        assert headings[1].section == "1. Objetivo"

    def test_agrupa_la_tabla_en_un_solo_bloque(self):
        tables = [b for b in split_into_blocks(DOCUMENTO) if b.kind == "table"]
        assert len(tables) == 1
        assert "1 anio" in tables[0].text and "2 anios" in tables[0].text

    def test_agrupa_la_lista_en_un_solo_bloque(self):
        lists = [b for b in split_into_blocks(DOCUMENTO) if b.kind == "list"]
        assert len(lists) == 1
        assert lists[0].text.count("\n") == 2


class TestChunking:
    def test_los_encabezados_abren_chunk_nuevo(self):
        # Cada seccion supera el minimo, asi que no se fusionan en un solo chunk.
        largo = "\n\n".join(
            f"## Seccion {i}\n" + ("contenido relevante de la seccion " * 40) for i in range(1, 4)
        )
        chunks = chunk_text(largo, chunk_size_tokens=900, overlap_tokens=120)
        assert len(chunks) >= 3
        assert {c.section for c in chunks} >= {"Seccion 1", "Seccion 2", "Seccion 3"}

    def test_no_genera_chunks_que_solo_contienen_titulos(self):
        chunks = chunk_text(
            "# A\n## B\n## C\n\ntexto real del documento\n", chunk_size_tokens=900, overlap_tokens=120
        )
        assert all(estimate_tokens(c.text) > 0 for c in chunks)
        # Los titulos consecutivos sin contenido no se cortan por separado.
        assert len(chunks) == 1

    def test_una_lista_corta_no_se_parte(self):
        blocks = split_into_blocks("## Procedimiento\n1. uno\n2. dos\n3. tres\n")
        chunks = chunk_blocks(blocks, chunk_size_tokens=900, overlap_tokens=120)
        combined = "\n".join(c.text for c in chunks)
        assert combined.count("uno") == 1
        assert "uno" in chunks[0].text and "tres" in chunks[0].text

    def test_respeta_el_tamano_maximo(self):
        texto = "palabra " * 4000
        chunks = chunk_text(texto, chunk_size_tokens=300, overlap_tokens=50)
        assert len(chunks) > 1
        assert all(estimate_tokens(c.text) <= 300 for c in chunks)

    def test_solape_comparte_el_presupuesto_del_bloque_entrante(self):
        blocks = [
            TextBlock("a " * 600, "S"),
            TextBlock("b " * 80, "S"),
            TextBlock("c " * 620, "S"),
        ]
        chunks = chunk_blocks(blocks, chunk_size_tokens=900, overlap_tokens=120)
        assert all(estimate_tokens(c.text) <= 900 for c in chunks)
        assert "a" in chunks[0].text and "b" in chunks[0].text
        assert "c" in chunks[-1].text

    def test_redondeo_de_bloques_pequenos_no_acumula_exceso(self):
        blocks = [TextBlock("palabra", "S") for _ in range(100)]
        chunks = chunk_blocks(blocks, chunk_size_tokens=30, overlap_tokens=3)
        assert all(estimate_tokens(c.text) <= 30 for c in chunks)

    @pytest.mark.parametrize("labels", [("pagina 1", "pagina 2"), ("Hoja A", "Hoja B")])
    def test_cada_chunk_conserva_una_sola_pagina_o_hoja(self, labels):
        blocks = [
            TextBlock("contenido primero", "S", labels[0]),
            TextBlock("contenido segundo", "S", labels[1]),
        ]
        chunks = chunk_blocks(blocks, chunk_size_tokens=900, overlap_tokens=120)
        assert [(c.page_or_sheet, c.text) for c in chunks] == [
            (labels[0], "contenido primero"), (labels[1], "contenido segundo")
        ]

    def test_los_indices_son_consecutivos(self):
        chunks = chunk_text(DOCUMENTO, chunk_size_tokens=120, overlap_tokens=20)
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_overlap_mayor_que_el_tamano_es_error(self):
        with pytest.raises(ValueError, match="mayor"):
            chunk_blocks(split_into_blocks("hola"), chunk_size_tokens=50, overlap_tokens=50)

    def test_texto_vacio_produce_cero_chunks(self):
        assert chunk_text("   \n\n  ", chunk_size_tokens=900, overlap_tokens=120) == []

    def test_umbral_de_corte_por_encabezado_es_explicito(self):
        assert HEADING_BREAK_MIN_TOKENS > 0


class TestEstimacionDeTokens:
    def test_es_monotona(self):
        assert estimate_tokens("una palabra") < estimate_tokens("una palabra mas larga aun")

    def test_cadena_vacia_es_cero(self):
        assert estimate_tokens("") == 0
