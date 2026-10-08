# Creado por Aldo Garcia.
"""Regresiones sinteticas para los rechazos observados el 6 de octubre.

No llama a Ollama: comprueba el transporte de citas y el contrato, sin atribuir
a los dobles de prueba la capacidad de razonamiento del modelo instalado.
"""

from __future__ import annotations

import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.prompts import UNVERIFIED_ANSWER, build_answer_messages
from app.config import get_settings
from app.llm.model_policy import ModelPolicy
from app.llm.ollama_client import ChatResult
from app.rag.citation_aliases import citation_aliases, expand_citation_aliases
from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence
from app.structured_data.tool import StructuredEvidence

pytestmark = pytest.mark.unit
LONG_SOURCE = "data-alias/prestaciones/PLAN DE PRUEBA JUBILACI#U00d3N DICIEMBRE 2090.pdf#8"


def evidence(source: str = LONG_SOURCE, text: str = "Se permite un prestamo por ejercicio.") -> Evidence:
    return Evidence(
        source_id=source, text=text, score=0.8, category="prestaciones",
        filename=source.rsplit("/", 1)[-1].rsplit("#", 1)[0], section="",
        page_or_sheet="pagina 9", document_id=source, chunk_id=source,
    )


class Client:
    def __init__(self, *answers: str) -> None:
        self.answers = iter(answers)
        self.calls: list[dict] = []

    def chat(self, **kwargs) -> ChatResult:
        self.calls.append(kwargs)
        return ChatResult(content=next(self.answers), model=kwargs["model"], latency_ms=1)


@pytest.fixture(autouse=True)
def cited_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    monkeypatch.setattr(get_settings(), "answer_allow_general_knowledge", True)


def synthesize(client: Client, items: tuple[Evidence, ...] = (), **kwargs):
    policy = ModelPolicy()
    return KnowledgeAgent(llm=client, policy=policy).synthesize(
        question="Segun el documento, puedo solicitar 9 prestamos?",
        evidences=items or (evidence(),), model_name=policy.fast_model, **kwargs,
    )


def test_long_filename_is_resolved_exactly_and_never_changed_in_public_citations():
    client = Client("Se permite 1 prestamo por ejercicio. [[E1]]")
    result = synthesize(client)
    assert result.grounding.grounded
    assert result.answer == f"Se permite 1 prestamo por ejercicio. [[{LONG_SOURCE}]]"
    assert result.cited_source_ids == (LONG_SOURCE,)
    assert len(client.calls) == 1
    assert "[cita: [[E1]]]" in client.calls[0]["messages"][1]["content"]
    assert not result.grounding.factual_verified


@pytest.mark.parametrize("citation", ["E99", "e1", "E01", "E1-extra", "otro/plan.pdf#8"])
def test_unknown_aliases_are_not_guessed_or_discarded(citation):
    answer = f"Se permite 1 prestamo. [[{citation}]]"
    client = Client(answer, answer)
    result = synthesize(client)
    assert result.answer == UNVERIFIED_ANSWER
    assert result.grounding.has_invalid_citations
    assert len(client.calls) == 2


def test_valid_alias_cannot_turn_user_numbers_into_documentary_evidence():
    answer = "Se permiten 9 prestamos. [[E1]]"
    result = synthesize(Client(answer, answer))
    assert result.answer == UNVERIFIED_ANSWER
    assert result.grounding.citations_valid
    assert "numerica" in result.grounding.reason


def test_each_claim_is_checked_against_its_own_cited_source():
    items = (evidence(), evidence("prestaciones/otro.txt#1", "Se permiten 9 prestamos."))
    answer = "Se permiten 9 prestamos. [[E1]]"
    result = synthesize(Client(answer, answer), items)
    assert result.answer == UNVERIFIED_ANSWER
    assert "numerica" in result.grounding.reason


def test_alias_does_not_bypass_general_section_validation():
    answer = "### Orientación general\nSe permite 1 prestamo. [[E1]]"
    result = synthesize(Client(answer, answer))
    assert result.answer == UNVERIFIED_ANSWER
    assert result.grounding.reason == "orientacion general atribuida a fuentes documentales"


def test_section_retry_is_specific_documentary_and_still_fully_verified():
    client = Client(
        "### Orientación general\nSe permite 1 prestamo. [[E1]]",
        "### Información documentada\nSe permite 1 prestamo por ejercicio. [[E1]]",
    )
    result = synthesize(client)
    assert result.grounding.grounded and result.regenerated
    assert result.answer_basis == "documented"
    retry_system, retry_user = client.calls[1]["messages"]
    assert "Puedes complementar" not in retry_system["content"]
    assert "conocimiento general esta deshabilitada" in retry_system["content"]
    assert "mezclo la procedencia" in retry_user["content"]
    assert "No basta cambiar el titulo" in retry_user["content"]
    assert get_settings().answer_allow_general_knowledge is True


def test_alias_for_evidence_discarded_by_context_budget_stays_invalid(monkeypatch):
    items = (evidence(), evidence("prestaciones/excluido.txt#1", "Se permiten 9 prestamos."))
    client = Client("Se permiten 9 prestamos. [[E2]]", "Se permiten 9 prestamos. [[E2]]")
    agent = KnowledgeAgent(llm=client, policy=ModelPolicy())
    monkeypatch.setattr(agent, "_pack_answer_context", lambda **kwargs: (items[:1], (), None, True))
    result = agent.synthesize(question="Cuantos prestamos?", evidences=items, model_name="gemma4:latest")
    assert result.answer == UNVERIFIED_ANSWER
    assert result.grounding.invalid_source_ids == ("E2",)
    assert all("excluido.txt" not in call["messages"][1]["content"] for call in client.calls)


def test_retry_alias_map_is_rebuilt_from_the_retry_prompt(monkeypatch):
    first = evidence()
    retained = evidence("prestaciones/retenido.txt#1", "Se permiten dos prestamos.")
    client = Client("Se permite 1 prestamo. [[E99]]", "Se permiten 2 prestamos. [[E1]]")
    agent = KnowledgeAgent(llm=client, policy=ModelPolicy())
    packs = iter([((first, retained), (), None, False), ((retained,), (), None, True)])
    monkeypatch.setattr(agent, "_pack_answer_context", lambda **kwargs: next(packs))
    result = agent.synthesize(question="Cuantos prestamos?", evidences=(first, retained), model_name="gemma4:latest")
    assert result.grounding.grounded
    assert result.cited_source_ids == (retained.source_id,)
    assert LONG_SOURCE not in result.answer


def test_structured_and_document_aliases_have_different_namespaces():
    table = StructuredEvidence(
        source_id="sql:fuente:conteo:abc", source="fuente", entity="conteo",
        columns=("total",), rows=((27,),), row_count=1, truncated=False,
    )
    aliases = citation_aliases((evidence(),), (table,))
    assert aliases == {"E1": LONG_SOURCE, "S1": table.source_id}
    result = synthesize(Client("El total es 27. [[S1]]"), structured=(table,))
    assert result.grounding.grounded
    assert result.cited_source_ids == (table.source_id,)


def test_aliases_do_not_make_duplicate_canonical_sources_unambiguous():
    items = (evidence(), evidence(text="Se permiten dos prestamos."))
    answer = "Se permite 1 prestamo. [[E1]]"
    result = synthesize(Client(answer, answer), items)
    assert result.answer == UNVERIFIED_ANSWER
    assert "ambiguo" in result.grounding.reason


def test_canonical_source_id_cannot_be_reinterpreted_as_an_alias():
    items = (evidence(), evidence("E1", "Otro documento."))
    aliases = citation_aliases(items)
    assert "E1" not in aliases
    assert expand_citation_aliases("Texto [[E1]]", aliases) == "Texto [[E1]]"


def test_transport_alias_alone_is_never_accepted_by_the_grounding_allowlist():
    result = verify_grounding("Se permite 1 prestamo. [[E1]]", (evidence(),), mode="cited")
    assert result.has_invalid_citations and not result.grounded


def test_extractive_mode_still_requires_the_complete_original_unit(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "extractive")
    item = evidence(text="Se permite un prestamo. Requiere autorizacion.")
    good = synthesize(Client(f"{item.text} [[E1]]"), (item,))
    assert good.grounding.extractive_verified
    truncated = "Se permite un prestamo. [[E1]]"
    bad = synthesize(Client(truncated, truncated), (item,))
    assert bad.answer == UNVERIFIED_ANSWER


def test_general_capability_remains_optional_in_initial_prompt():
    messages = build_answer_messages(question="Explica el concepto", evidences=(evidence(),))
    assert "Puedes complementar" in messages[0]["content"]
    assert "no necesitas una seccion de orientacion general" in messages[1]["content"]
