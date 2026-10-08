# Creado por Aldo Garcia.
"""Regresiones sinteticas: equivalencias numericas y sus limites de unidad."""

from __future__ import annotations

import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.config import get_settings
from app.llm.model_policy import ModelPolicy
from app.llm.ollama_client import ChatResult
from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit
SOURCE = "prestaciones/ejemplo-numerico.txt#1"
TABLE = "Antigüedad %\n0 – 4.99 0\n5 – 5.99 50\n7 – 7.99 70\n10 en adelante 100"


def evidence(text: str, source: str = SOURCE) -> Evidence:
    return Evidence(
        source_id=source, text=text, score=0.8, category="prestaciones",
        filename="ejemplo-numerico.txt", section="", page_or_sheet="pagina 1",
        document_id="documento-sintetico", chunk_id=source,
    )


def report(source: str, answer: str):
    return verify_grounding(f"{answer} [[{SOURCE}]]", (evidence(source),), mode="cited")


@pytest.mark.parametrize(("source", "answer"), [
    ("Se entrega cada quinquenio de antigüedad.", "Se entrega cada 5 años de antigüedad."),
    ("Se entrega cada quinquenio.", "Se entrega cada cinco años."),
    ("Se entrega cada 5 años.", "Se entrega cada quinquenio."),
    ("Puede solicitar un préstamo por ejercicio.", "Puede solicitar 1 préstamo por ejercicio."),
    ("Puede solicitar 1 préstamo.", "Puede solicitar un préstamo."),
    ("Puede solicitar dos préstamos.", "Puede solicitar 2 préstamos."),
    ("Puede solicitar un préstamo.", "Puede solicitar **1 préstamo**."),
    ("Puede solicitar un préstamo.", "Puede solicitar 1 **préstamo**."),
    (TABLE, "Para el tramo 7 – 7.99 corresponde el 70%."),
    (TABLE, "Para el tramo 7 – 7.99 corresponde el **70 %**."),
    (TABLE, "Para el tramo 7 – 7.99 corresponde el 70 por ciento."),
    ("El porcentaje es 70 %.", "Corresponde el 70%."),
    ("El porcentaje es 70%.", "Corresponde el 70 por ciento."),
    ("| Antigüedad | % |\n| --- | ---: |\n| 7 – 7.99 | 70 |", "Para el tramo 7 – 7.99 corresponde el 70%."),
    ("Puede solicitar un préstamo.", "1. Puede solicitar un préstamo."),
    ("Puede solicitar un préstamo.", "27) Puede solicitar un préstamo."),
    ("Corresponden 20 días hábiles.", "Corresponden veinte días hábiles."),
    ("El resultado es 27.", "El resultado es 27."),
])
def test_supported_numeric_representations_keep_citations_and_units(source, answer):
    result = report(source, answer)
    assert result.grounded, result.reason
    assert result.citations_valid
    assert not result.factual_verified
    assert not result.extractive_verified


@pytest.mark.parametrize(("source", "answer"), [
    ("Se entrega cada quinquenio.", "Se entrega cada 6 años."),
    ("Se entrega cada quinquenio.", "Corresponden 5 préstamos."),
    ("Se entrega cada medio quinquenio.", "Se entrega cada 5 años."),
    ("Se entrega a la mitad de cada quinquenio.", "Se entrega cada 5 años."),
    ("Se entrega cada 5 años.", "Se entrega cada medio quinquenio."),
    ("El intervalo dura un quinquenio y medio.", "El intervalo dura 5 años."),
    ("El intervalo dura un quinquenio más dos meses.", "El intervalo dura 5 años."),
    ("El intervalo dura 5 años.", "El intervalo dura un quinquenio y medio."),
    ("Puede solicitar un préstamo.", "Puede solicitar dos préstamos."),
    ("Puede solicitar un préstamo.", "Puede solicitar 2 préstamos."),
    ("Tengo un plan.", "El plazo es 1 año."),
    ("Puede solicitar un préstamo.", "Corresponde el 1%."),
    ("Puede solicitar 1 préstamo.", "El plazo es 1 año."),
    ("Puede solicitar 1 préstamo.", "Corresponde 1 peso."),
    ("CHUNK 5: reconocimiento a la lealtad.", "Se entrega cada 5 años."),
    (TABLE, "Corresponde el 71%."),
    (TABLE, "Corresponde el 70%."),
    (TABLE, "Para el tramo 5 – 5.99 corresponde el 70%."),
    (TABLE, "Corresponde el 7%."),
    (TABLE, "Corresponde el 4.99%."),
    (TABLE, "Corresponden 70 años."),
    ("El valor de la columna es 70.", "Corresponde el 70%."),
    ("Antigüedad\n7 – 7.99 70", "Corresponde el 70%."),
    ("Antigüedad %\nEsta tabla no está disponible.\n7 – 7.99 70", "Corresponde el 70%."),
    ("Antigüedad %\n7 – 7.99 70 900", "Corresponde el 70%."),
    ("La tasa es 10%.\nEl saldo es 70 pesos.", "Corresponde el 70%."),
    ("Puede solicitar treinta y dos préstamos.", "Puede solicitar 2 préstamos."),
    ("Puede solicitar dos préstamos.", "Puede solicitar treinta y dos préstamos."),
    ("Se requieren 2 años.", "Se requieren 24 meses."),
    ("Se requieren 7 años.", "Se requieren 7.5 años."),
    ("El resultado es 27.", "El resultado es 900."),
    ("No aparece un valor numérico.", "El código es 900Z."),
    ("La tasa es -5%.", "La tasa es 5%."),
    ("La tasa es .5%.", "La tasa es 5%."),
    ("El plazo es 5 años.", "El plazo es -5 años."),
])
def test_unbacked_values_units_and_inferences_stay_rejected(source, answer):
    result = report(source, answer)
    assert not result.grounded
    assert result.citations_valid
    assert "numerica" in result.reason


def test_header_cannot_type_a_row_from_a_different_cited_document():
    other = "prestaciones/otro.txt#1"
    items = (evidence("Antigüedad %"), evidence("7 – 7.99 70", other))
    result = verify_grounding(f"Corresponde el 70%. [[{SOURCE}]][[{other}]]", items, mode="cited")
    assert not result.grounded


def test_uncited_evidence_cannot_provide_numeric_support():
    other = "prestaciones/otro.txt#1"
    items = (evidence("La antigüedad importa."), evidence(TABLE, other))
    assert not verify_grounding(f"Corresponde el 70%. [[{SOURCE}]]", items, mode="cited").grounded


def test_unknown_citation_is_still_rejected():
    result = verify_grounding("Corresponde el 70%. [[falso/a.txt#1]]", (evidence(TABLE),), mode="cited")
    assert not result.grounded
    assert result.has_invalid_citations


def test_list_numbers_do_not_hide_fabricated_values():
    result = report("Se requiere un año.", "1. Se requieren 900 años.")
    assert not result.grounded


def test_extractive_contract_still_requires_the_original_complete_unit():
    item = evidence("Se entrega cada quinquenio.")
    result = verify_grounding(f"Se entrega cada 5 años. [[{SOURCE}]]", (item,), mode="extractive")
    assert not result.grounded
    assert verify_grounding(f"{item.text} [[{SOURCE}]]", (item,), mode="extractive").extractive_verified


@pytest.mark.parametrize(("source", "answer"), [
    ("Se entrega cada quinquenio en los eventos de cierre anual.",
     "Se entrega cada 5 años en los eventos de cierre anual."),
    ("Puede solicitar un préstamo por ejercicio, sin intereses.",
     "Puede solicitar 1 préstamo por ejercicio, sin intereses."),
    (TABLE, "Para el tramo 7 – 7.99 corresponde el 70%."),
])
def test_valid_generation_is_not_replaced_by_insufficient_answer(monkeypatch, source, answer):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")

    class Client:
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            return ChatResult(content=f"{answer} [[{SOURCE}]]", model=kwargs["model"], latency_ms=1)

    client = Client()
    policy = ModelPolicy()
    result = KnowledgeAgent(llm=client, policy=policy).synthesize(
        question="Según el documento, responde la consulta.",
        evidences=(evidence(source),), model_name=policy.fast_model,
    )
    assert result.answer == f"{answer} [[{SOURCE}]]"
    assert result.grounding.grounded
    assert client.calls == 1
    assert not result.regenerated
