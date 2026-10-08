# Creado por Aldo Garcia.
"""Sintesis util, origen explicito y fallback acotado sin datos reales."""

from __future__ import annotations

import pytest

from app.agents.knowledge_agent import KnowledgeAgent, SynthesisResult
from app.agents.orchestrator import Orchestrator
from app.agents.prompts import build_answer_messages
from app.common.answers import GENERAL_HEADING
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.config import get_settings
from app.llm.model_policy import Intent, ModelChoice, ModelPolicy
from app.llm.ollama_client import ChatResult
from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit
SOURCE = "prestaciones/vacaciones.pdf#1"


def evidence() -> tuple[Evidence, ...]:
    return (Evidence(
        source_id=SOURCE, text="La prestacion otorga 20 dias habiles, previa autorizacion del supervisor.",
        score=0.8, category="prestaciones", filename="vacaciones.pdf", section="Vacaciones",
        page_or_sheet="pagina 1", document_id="documento-sintetico", chunk_id="chunk-sintetico",
    ),)


def test_parafrasis_conserva_cita_y_no_simula_verificacion_semantica():
    answer = f"Se pueden solicitar 20 dias habiles con autorizacion del supervisor. [[{SOURCE}]]"
    report = verify_grounding(answer, evidence(), mode="cited")
    assert report.grounded and report.citations_valid
    assert report.cited_source_ids == (SOURCE,)
    assert not report.factual_verified and not report.extractive_verified
    assert not verify_grounding(answer, evidence(), mode="extractive").grounded


def test_respuesta_mixta_separa_el_conocimiento_general():
    answer = (f"### Información documentada\nLa prestacion es de 20 dias habiles con autorizacion. [[{SOURCE}]]"
              f"\n\n{GENERAL_HEADING}\nPlanificar el descanso puede ayudar a coordinar el trabajo.")
    report = verify_grounding(answer, evidence(), mode="cited", allow_general_knowledge=True)
    assert report.grounded and report.cited_source_ids == (SOURCE,)
    assert not verify_grounding(answer, evidence(), mode="cited", allow_general_knowledge=False).grounded


@pytest.mark.parametrize("answer", [
    f"La prestacion es de 99 dias. [[{SOURCE}]]",
    "La prestacion es de 20 dias. [[prestaciones/inventado.pdf#9]]",
    f"La prestacion es de 20 dias. [[{SOURCE}]] Ademas incluye un bono.",
    f"{GENERAL_HEADING}\nToda empresa concede 20 dias. [[{SOURCE}]]",
])
def test_no_se_aprobaran_cifras_o_citas_sin_respaldo(answer):
    assert not verify_grounding(answer, evidence(), mode="cited", allow_general_knowledge=True).grounded


def test_prompt_permite_sintesis_y_distingue_datos_internos(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    monkeypatch.setattr(get_settings(), "answer_allow_general_knowledge", True)
    messages = build_answer_messages(question="Como puedo solicitar vacaciones?", evidences=evidence())
    assert "Puedes complementar" in messages[0]["content"]
    assert "unica fuente" in messages[0]["content"]
    assert GENERAL_HEADING in messages[0]["content"]


class FailingDeepClient:
    def __init__(self, failure=InferenceFailureKind.TIMEOUT):
        self.calls = []
        self.failure = failure

    def chat(self, *, model, messages, **kwargs):
        self.calls.append((model, messages, kwargs))
        if kwargs["execution_profile"] == "deep":
            raise InferenceFailureError(self.failure)
        return ChatResult(
            content=f"La prestacion es de 20 dias habiles, previa autorizacion. [[{SOURCE}]]",
            model=model, latency_ms=12,
        )


@pytest.mark.parametrize("kind", [InferenceFailureKind.TIMEOUT, InferenceFailureKind.INCOMPLETE])
def test_modelo_distinto_fallido_tiene_un_intento_gemma_con_evidencia_autorizada(monkeypatch, kind):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    monkeypatch.setattr(get_settings(), "ollama_deep_model", "synthetic-deep-generator")
    policy = ModelPolicy()
    client = FailingDeepClient(kind)
    answer = KnowledgeAgent(llm=client, policy=policy).synthesize(
        question="Compara en profundidad las condiciones", evidences=evidence(),
        model_name=policy.deep_model, choice=ModelChoice.DEEP,
    )
    assert [call[0] for call in client.calls] == [policy.deep_model, policy.fast_model]
    assert answer.model == policy.fast_model
    assert answer.cited_source_ids == (SOURCE,)
    assert answer.answer_basis == "documented"
    assert client.calls[1][2]["num_ctx"] == get_settings().ollama_fast_num_ctx


def test_fallo_gemma_no_entra_en_un_bucle_de_modelos():
    class BrokenClient:
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            raise InferenceFailureError(InferenceFailureKind.TIMEOUT)

    client = BrokenClient()
    policy = ModelPolicy()
    with pytest.raises(InferenceFailureError):
        KnowledgeAgent(llm=client, policy=policy).synthesize(
            question="Explica las vacaciones", evidences=evidence(), model_name=policy.fast_model,
            choice=ModelChoice.FAST,
        )
    assert client.calls == 1


def test_resumen_jerarquico_conserva_evidencia_si_reduce_agota_su_etapa(monkeypatch):
    policy = ModelPolicy()
    client = FailingDeepClient()
    agent = KnowledgeAgent(llm=client, policy=policy)
    partial = f"La prestacion otorga 20 dias habiles, previa autorizacion del supervisor. [[{SOURCE}]]"
    verified = verify_grounding(partial, evidence(), mode="cited")
    monkeypatch.setattr(agent, "synthesize", lambda **kwargs: SynthesisResult(
        answer=partial, model=policy.fast_model, latency_ms=12, grounding=verified, cited_source_ids=(SOURCE,),
    ))
    result = agent._synthesize_hierarchical_summary(
        question="Resume todas las partes", evidences=evidence(), model_name=policy.fast_model,
        choice=ModelChoice.FAST, deep_model_name=policy.deep_model, scope_note="", evidence_truncated=False,
    )
    assert [call[0] for call in client.calls] == [policy.deep_model]
    assert result.hierarchical and result.grounding.grounded
    assert result.cited_source_ids == (SOURCE,)
    assert evidence()[0].text in result.answer


def test_resumen_no_acepta_solo_teoria_general_cuando_hay_documento(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")
    monkeypatch.setattr(get_settings(), "answer_allow_general_knowledge", True)

    class GeneralOnlyClient:
        calls = 0

        def chat(self, *, model, **kwargs):
            self.calls += 1
            return ChatResult(content=f"{GENERAL_HEADING}\nEl descanso ayuda al bienestar.", model=model, latency_ms=1)

    client = GeneralOnlyClient()
    policy = ModelPolicy()
    result = KnowledgeAgent(llm=client, policy=policy).synthesize(
        question="Resume el archivo de vacaciones", evidences=evidence(), model_name=policy.fast_model,
        choice=ModelChoice.FAST, intent=Intent.DOCUMENT_SUMMARY,
    )
    assert client.calls == 2
    assert result.grounding.grounded and result.grounding.extractive_verified
    assert result.cited_source_ids == (SOURCE,)
    assert evidence()[0].text in result.answer


@pytest.mark.parametrize("question", [
    "Cual es mi salario?", "Dime el sueldo de Juan", "Resume este adjunto",
    "Que dice el reglamento?", "Como son las prestaciones en nuestra empresa?",
    "Hola, como entrego la incapacidad del IMSS?", "Cuanto se entrega por el apoyo de casamiento?",
    "Y en ese caso?",
])
def test_sin_evidencia_no_se_inventa_un_dato_interno(question):
    assert not Orchestrator._allow_general_fallback(question, Intent.DOCUMENTAL, frozenset({"prestaciones"}))


def test_orientacion_abierta_sin_fuentes_tiene_ruta_general():
    assert Orchestrator._allow_general_fallback(
        "Como organizar un descanso laboral?", Intent.DOCUMENTAL, frozenset({"prestaciones"}),
    )
