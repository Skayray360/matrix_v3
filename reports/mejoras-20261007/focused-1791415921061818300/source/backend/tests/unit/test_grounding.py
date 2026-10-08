# Creado por Aldo Garcia.
"""Verificador de grounding y allowlist de fuentes."""

from __future__ import annotations

import pytest

from app.rag.grounding import (
    coverage_ratio,
    declares_insufficiency,
    extract_citations,
    filter_answer_citations,
    verify_grounding,
)
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit


def evidence(source_id: str, text: str = "Corresponden 20 dias habiles de vacaciones.") -> Evidence:
    category, rest = source_id.split("/", 1)
    filename = rest.split("#", 1)[0]
    return Evidence(
        source_id=source_id,
        text=text,
        score=0.7,
        category=category,
        filename=filename,
        section="",
        page_or_sheet="",
        document_id="doc-1",
        chunk_id="chunk-1",
    )


EVIDENCIAS = (
    evidence("prestaciones/politica-vacaciones.md#1"),
    evidence("prestaciones/politica-vacaciones.md#2"),
)


class TestExtraccionDeCitas:
    def test_extrae_las_citas_presentes(self):
        texto = "Son 20 dias [[prestaciones/politica-vacaciones.md#1]] segun la tabla."
        assert extract_citations(texto) == ("prestaciones/politica-vacaciones.md#1",)

    def test_elimina_duplicados_conservando_el_orden(self):
        texto = "[[a/b.md#1]] y de nuevo [[a/b.md#1]] mas [[a/c.md#2]]"
        assert extract_citations(texto) == ("a/b.md#1", "a/c.md#2")

    def test_texto_sin_citas(self):
        assert extract_citations("una respuesta sin fuentes") == ()


class TestVerificacion:
    def test_respuesta_bien_citada_pasa(self):
        report = verify_grounding(
            "Corresponden 20 dias habiles de vacaciones [[prestaciones/politica-vacaciones.md#1]].", EVIDENCIAS
        )
        assert report.grounded is True
        assert report.invalid_source_ids == ()

    def test_source_id_inventado_se_rechaza(self):
        report = verify_grounding("Segun [[nomina/secreto-inventado.md#9]] son 30 dias.", EVIDENCIAS)
        assert report.grounded is False
        assert report.has_invalid_citations is True
        assert "nomina/secreto-inventado.md#9" in report.invalid_source_ids

    def test_respuesta_documental_sin_citas_se_rechaza(self):
        report = verify_grounding("Son 20 dias habiles.", EVIDENCIAS)
        assert report.grounded is False
        assert report.reason == "respuesta documental sin ninguna cita"

    def test_afirmar_sin_evidencia_se_rechaza(self):
        report = verify_grounding("La politica dice que son 25 dias.", ())
        assert report.grounded is False

    def test_declarar_insuficiencia_es_valido(self):
        report = verify_grounding("No cuento con informacion documental suficiente.", ())
        assert report.grounded is True
        assert report.declares_insufficiency is True

    def test_declarar_falta_de_acceso_es_valido(self):
        report = verify_grounding("No tiene acceso a esa informacion.", ())
        assert report.grounded is True

    def test_una_cita_invalida_pesa_mas_que_la_insuficiencia(self):
        report = verify_grounding(
            "No hay evidencia, pero segun [[x/y.md#1]] son 30 dias.", EVIDENCIAS
        )
        assert report.grounded is False


class TestCobertura:
    def test_cobertura_total(self):
        texto = "[[prestaciones/politica-vacaciones.md#1]] [[prestaciones/politica-vacaciones.md#2]]"
        assert coverage_ratio(texto, EVIDENCIAS) == 1.0

    def test_cobertura_parcial(self):
        assert coverage_ratio("[[prestaciones/politica-vacaciones.md#1]]", EVIDENCIAS) == 0.5

    def test_sin_evidencia_la_cobertura_es_cero(self):
        assert coverage_ratio("texto", ()) == 0.0


class TestFiltradoDeCitas:
    def test_elimina_solo_las_citas_invalidas(self):
        texto = "Valido [[prestaciones/politica-vacaciones.md#1]] e invalido [[falso/x.md#1]]."
        resultado = filter_answer_citations(texto, EVIDENCIAS)
        assert "prestaciones/politica-vacaciones.md#1" in resultado
        assert "falso/x.md#1" not in resultado


class TestMarcadoresDeInsuficiencia:
    @pytest.mark.parametrize(
        "texto",
        [
            "No cuento con informacion documental suficiente",
            "No tengo informacion sobre eso",
            "La evidencia es insuficiente",
            "No tiene acceso a esa informacion",
            "Esa consulta esta fuera del alcance de Matrix RH",
        ],
    )
    def test_detecta_las_formulas_de_insuficiencia(self, texto: str):
        assert declares_insufficiency(texto) is True

    def test_no_marca_una_respuesta_normal(self):
        assert declares_insufficiency("Corresponden 20 dias habiles de vacaciones.") is False
