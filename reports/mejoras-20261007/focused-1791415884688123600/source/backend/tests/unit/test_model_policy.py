# Creado por Aldo Garcia.
"""Politica de los perfiles Gemma: clasificacion de intencion y routing."""

from __future__ import annotations

import pytest

from app.llm.model_policy import FORBIDDEN_MODEL_PATTERN, Intent, ModelChoice, ModelPolicy

pytestmark = pytest.mark.unit


@pytest.fixture()
def policy() -> ModelPolicy:
    return ModelPolicy()


class TestClasificacionDeIntencion:
    def test_pregunta_documental(self, policy: ModelPolicy):
        assert policy.classify_intent("Cual es la politica de vacaciones?") is Intent.DOCUMENTAL

    def test_pregunta_estructurada(self, policy: ModelPolicy):
        assert policy.classify_intent("Cuantos empleados hay por departamento") is Intent.STRUCTURED

    def test_pregunta_mixta(self, policy: ModelPolicy):
        intent = policy.classify_intent(
            "Cuantos empleados hay por departamento y que dice la politica de nomina?"
        )
        assert intent is Intent.MIXED

    def test_saludo_es_conversacional(self, policy: ModelPolicy):
        assert policy.classify_intent("hola") is Intent.CONVERSATIONAL

    @pytest.mark.parametrize(
        "question",
        (
            "Hola, ¿cuántos días de vacaciones me corresponden?",
            "Buenos días, ¿qué dice la política de permisos?",
            "Gracias, ¿y con diez años de antigüedad?",
        ),
    )
    def test_saludo_no_desvia_pregunta_documental(self, policy: ModelPolicy, question: str):
        assert policy.classify_intent(question) is Intent.DOCUMENTAL

    @pytest.mark.parametrize("question", ("Hola", "Buenos días", "Buenas tardes", "Gracias", "Hasta luego"))
    def test_saludo_solo_conserva_ruta_conversacional(self, policy: ModelPolicy, question: str):
        assert policy.classify_intent(question) is Intent.CONVERSATIONAL

    @pytest.mark.parametrize(
        ("question", "expected"),
        (
            ("Hola, ¿quién eres?", Intent.IDENTITY),
            ("Hola, resume el documento adjunto.", Intent.DOCUMENT_SUMMARY),
            ("Hola, ¿cuántos empleados hay por departamento?", Intent.STRUCTURED),
            ("Hola, ¿cuántos empleados hay y qué dice la política?", Intent.MIXED),
            ("¿Y con diez años de antigüedad?", Intent.DOCUMENTAL),
            ("¿Y en ese caso?", Intent.DOCUMENTAL),
        ),
    )
    def test_saludo_conserva_intenciones_y_seguimientos(
        self, policy: ModelPolicy, question: str, expected: Intent
    ):
        assert policy.classify_intent(question) is expected

    def test_texto_vacio_no_rompe(self, policy: ModelPolicy):
        assert policy.classify_intent("   ") is Intent.CONVERSATIONAL


class TestRouting:
    def test_consulta_simple_usa_ruta_rapida(self, policy: ModelPolicy):
        decision = policy.route("Cuantos dias de vacaciones me corresponden?")
        assert decision.choice is ModelChoice.FAST
        assert decision.model_name == "gemma4:latest"

    def test_peticion_de_analisis_profundo_escala(self, policy: ModelPolicy):
        decision = policy.route("Necesito un analisis profundo de la politica de prestaciones")
        assert decision.choice is ModelChoice.DEEP
        assert decision.model_name == "gemma4:latest"
        assert "deep_marker_in_query" in decision.signals

    def test_comparativa_explicita_escala(self, policy: ModelPolicy):
        decision = policy.route("Diferencias entre prestaciones y nomina", categories_in_scope=2)
        assert decision.choice is ModelChoice.DEEP
        assert "explicit_comparison" in decision.decisive_signals
        assert "multiple_authorized_categories" in decision.weak_signals

    def test_flujo_multiherramienta_no_implica_analisis_profundo(self, policy: ModelPolicy):
        decision = policy.route("Dame el dato y su politica", multi_tool=True)
        assert decision.choice is ModelChoice.FAST
        assert "multi_tool_flow" in decision.signals

    def test_baja_confianza_de_recuperacion_no_requiere_perfil_profundo(self, policy: ModelPolicy):
        decision = policy.route("Que dice el reglamento?", low_retrieval_confidence=True)
        assert decision.choice is ModelChoice.FAST
        assert "low_retrieval_confidence" in decision.signals

    def test_verificador_solo_es_observacion(self, policy: ModelPolicy):
        decision = policy.route("Que dice el reglamento?", verifier_requested_deep=True)
        assert decision.choice is ModelChoice.FAST

    def test_consulta_muy_larga_conserva_ruta_rapida(self, policy: ModelPolicy):
        decision = policy.route("politica " * 60)
        assert decision.choice is ModelChoice.FAST
        assert "long_query" in decision.signals

    def test_varias_preguntas_sin_complejidad_no_escalan(self, policy: ModelPolicy):
        decision = policy.route("Que? Como? Cuando? Donde?")
        assert decision.choice is ModelChoice.FAST

    def test_la_decision_es_auditable(self, policy: ModelPolicy):
        audit = policy.route("Cuantos dias de vacaciones?").as_audit_dict()
        assert audit["selected_model"] == "gemma4:latest"
        assert audit["routing_signals"]


class TestModelosProhibidos:
    """Requisito 4: no sustituir por modelos cloud."""

    def test_los_modelos_configurados_no_son_cloud(self, policy: ModelPolicy):
        for model in (policy.fast_model, policy.deep_model, policy.embedding_model):
            assert FORBIDDEN_MODEL_PATTERN.search(model) is None

    @pytest.mark.parametrize(
        "model", ["gpt-4o", "claude-3-opus", "gemini-1.5-pro", "azure-openai-deployment"]
    )
    def test_el_patron_detecta_modelos_cloud(self, model: str):
        assert FORBIDDEN_MODEL_PATTERN.search(model) is not None
