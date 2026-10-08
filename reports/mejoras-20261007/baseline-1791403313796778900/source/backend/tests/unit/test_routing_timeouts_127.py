# Creado por Aldo Garcia.
"""Router y presupuestos 1.2.7 con reloj/transporte simulado; no usa modelos."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.config.settings import Settings
from app.llm import provider
from app.llm.model_policy import Intent, ModelChoice, ModelPolicy
from app.llm.ollama_client import OllamaClient

pytestmark = pytest.mark.unit


def configuration(monkeypatch, **overrides):
    settings = Settings(_env_file=None, app_env="test", **overrides)
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.model_policy.get_settings", lambda: settings)
    return settings


@pytest.mark.parametrize(
    "question",
    (
        "¿Qué es una prestación laboral?", "Define la prima vacacional",
        "¿Qué es la motivación?", "Explica la cultura organizacional",
        "¿Cómo funciona la memoria humana?", "Define el liderazgo situacional",
        "Explica la fotosíntesis", "¿Qué es el consentimiento informado?",
        "Define un resumen", "Explica las vacaciones en términos generales",
        "¿Cómo funciona la nómina?",
    ),
)
def test_conceptual_questions_have_no_subject_allowlist(monkeypatch, question):
    configuration(monkeypatch)
    assert ModelPolicy().classify_intent(question) is Intent.GENERAL


@pytest.mark.parametrize(
    "question",
    (
        "¿Cuántos días de vacaciones me corresponden?",
        "Explica los requisitos para solicitar las vacaciones",
        "¿Qué es la prestación de nuestra empresa?",
        "¿Qué es mi salario?", "Explica el contenido del archivo adjunto",
        "Explica Python y cuánto me dan si me caso",
        "Define lo que dice el documento de prestaciones",
        "¿Qué es el importe de mi bono?", "¿Y en ese caso?",
        "Explica los beneficios vigentes de Peñoles",
    ),
)
def test_operational_internal_or_ambiguous_questions_keep_retrieval(monkeypatch, question):
    configuration(monkeypatch)
    assert ModelPolicy().classify_intent(question) is Intent.DOCUMENTAL


def test_employee_statistics_keep_structured_route(monkeypatch):
    configuration(monkeypatch)
    assert ModelPolicy().classify_intent("¿Cuántos empleados hay por departamento?") is Intent.STRUCTURED


def test_authorized_scope_and_low_similarity_do_not_force_qwen(monkeypatch):
    configuration(monkeypatch)
    question = "¿Cuál es la política de vacaciones y qué requisitos debo cumplir para solicitarlas?"
    decision = ModelPolicy().route(
        question, categories_in_scope=13, low_retrieval_confidence=True,
        multi_tool=True, verifier_requested_deep=True,
    )
    assert decision.choice is ModelChoice.FAST
    assert decision.model_name == "gemma4:latest"
    assert decision.decisive_signals == ()
    assert "low_retrieval_confidence" in decision.weak_signals
    assert "multiple_authorized_categories" in decision.weak_signals
    audit = decision.as_audit_dict()
    assert audit["routing_decisive_signals"] == []
    assert "multiple_authorized_categories" in audit["routing_weak_signals"]


@pytest.mark.parametrize("question", ("Evalúa el procedimiento", "Explica detalladamente el permiso"))
def test_generic_quality_words_are_not_deep_requests(monkeypatch, question):
    configuration(monkeypatch)
    assert ModelPolicy().route(question).choice is ModelChoice.FAST


@pytest.mark.parametrize("question", ("Define una comparación", "¿Qué es una contradicción?"))
def test_defining_comparison_or_contradiction_is_not_itself_deep_analysis(monkeypatch, question):
    configuration(monkeypatch)
    assert ModelPolicy().route(question).choice is ModelChoice.FAST


@pytest.mark.parametrize(
    "question",
    (
        "Haz un análisis profundo de las prestaciones", "Compara aguinaldo y vacaciones",
        "Analiza a fondo el reglamento", "Realiza una comparación del aguinaldo y las vacaciones",
    ),
)
def test_explicit_deep_tasks_are_auditable(monkeypatch, question):
    configuration(monkeypatch)
    result = ModelPolicy().route(question)
    assert result.choice is ModelChoice.DEEP
    assert result.decisive_signals


def test_only_summary_intent_can_escalate_for_evidence_volume(monkeypatch):
    configuration(monkeypatch)
    policy = ModelPolicy()
    kwargs = {"evidence_count": 20, "evidence_chars": 40_000}
    assert policy.route("Resume los documentos", **kwargs).choice is ModelChoice.DEEP
    assert policy.route("¿Qué requisito aplica?", **kwargs).choice is ModelChoice.FAST


def test_safe_new_settings_and_limits(monkeypatch):
    settings = configuration(monkeypatch)
    assert settings.answer_allow_general_knowledge is True
    assert settings.answer_evidence_mode == "cited"
    assert settings.llm_request_deadline_seconds == 600
    assert settings.llm_fast_timeout_seconds == settings.llm_deep_timeout_seconds == 180
    for name in ("llm_fast_timeout_seconds", "llm_deep_timeout_seconds", "llm_request_deadline_seconds"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, app_env="test", **{name: 601})
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="test", answer_evidence_mode="unverified")


@pytest.mark.parametrize("shared", (False, True))
@pytest.mark.parametrize(("execution_profile", "expected"), (("fast", 25), ("deep", 65)))
def test_timeout_uses_profile_even_when_models_share_name(monkeypatch, shared, execution_profile, expected):
    settings = configuration(
        monkeypatch, ollama_fast_model="local", ollama_deep_model="local" if shared else "local-deep",
        llm_fast_timeout_seconds=25, llm_deep_timeout_seconds=65,
    )
    monkeypatch.setattr(provider.time, "monotonic", lambda: 100)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"message": {"content": "respuesta sintetica"}, "done": True})

    model = settings.ollama_deep_model if execution_profile == "deep" else settings.ollama_fast_model
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = provider.ModelClient(client=http)
        client.chat(model=model, messages=[], execution_profile=execution_profile)
    assert len(requests) == 1
    assert requests[0].extensions["timeout"]["read"] == expected
    assert provider._stage_deadline.get() is None


def test_transport_timeout_remains_an_upper_bound(monkeypatch):
    settings = configuration(monkeypatch, llm_fast_timeout_seconds=180)
    monkeypatch.setattr(provider.time, "monotonic", lambda: 100)
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"message": {"content": "OK"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        provider.ModelClient(client=http, timeout=12).chat(model=settings.ollama_fast_model, messages=[])
    assert timeouts == [12]


def test_remaining_global_budget_is_shared_by_deep_and_fast_stages(monkeypatch):
    settings = configuration(monkeypatch, llm_request_deadline_seconds=240)
    now = [100.0]
    monkeypatch.setattr(provider.time, "monotonic", lambda: now[0])
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        if len(timeouts) == 1:
            now[0] = 290.0
            raise httpx.ReadTimeout("provider-sensitive-message", request=request)
        return httpx.Response(200, json={"message": {"content": "OK"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = provider.ModelClient(client=http)
        with provider.inference_deadline():
            with pytest.raises(InferenceFailureError) as caught:
                client.chat(model=settings.ollama_deep_model, messages=[], execution_profile="deep")
            assert caught.value.failure_kind is InferenceFailureKind.TIMEOUT
            client.chat(model=settings.ollama_fast_model, messages=[], execution_profile="fast")
    assert timeouts == [180, 50]
    assert provider._deadline.get() is None
    assert provider._stage_deadline.get() is None


def test_nested_global_context_cannot_extend_existing_deadline(monkeypatch):
    settings = configuration(monkeypatch, llm_request_deadline_seconds=40)
    now = [100.0]
    monkeypatch.setattr(provider.time, "monotonic", lambda: now[0])
    with provider.inference_deadline():
        original = provider._deadline.get()
        now[0] = 105
        settings.llm_request_deadline_seconds = 600
        with provider.inference_deadline():
            assert provider._deadline.get() == original == 140
    assert provider._deadline.get() is None


def test_expired_global_budget_rejects_before_post(monkeypatch):
    settings = configuration(monkeypatch, llm_request_deadline_seconds=30)
    now = [100.0]
    monkeypatch.setattr(provider.time, "monotonic", lambda: now[0])

    def forbidden(_request):
        raise AssertionError("No debe abrir POST con presupuesto agotado")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as http:
        client = provider.ModelClient(client=http)
        with provider.inference_deadline():
            now[0] = 140
            with pytest.raises(InferenceFailureError) as caught:
                client.chat(model=settings.ollama_fast_model, messages=[])
    assert caught.value.failure_kind is InferenceFailureKind.DEADLINE


@pytest.mark.parametrize("facade", (False, True))
def test_read_timeout_has_safe_cause_and_no_http_retry(monkeypatch, caplog, facade):
    settings = configuration(monkeypatch, llm_max_transient_retries=2)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("SECRET_FROM_PROVIDER", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = provider.ModelClient(client=http) if facade else OllamaClient(client=http)
        with pytest.raises(InferenceFailureError) as caught:
            client.chat(model=settings.ollama_fast_model, messages=[{"role": "user", "content": "SECRET_PROMPT"}])
    assert len(calls) == 1
    assert caught.value.detail == "timeout"
    assert "SECRET" not in str(caught.value)
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize(
    ("body", "kind"),
    (
        ({"message": {"content": "incompleto"}, "done_reason": "length"}, InferenceFailureKind.INCOMPLETE),
        ({"message": {"content": " "}}, InferenceFailureKind.EMPTY),
        ({"message": {"content": "<think>razonamiento</think>"}}, InferenceFailureKind.REASONING),
        ({"message": {"content": None}}, InferenceFailureKind.FORMAT),
        ([], InferenceFailureKind.FORMAT),
    ),
)
def test_generation_validation_has_stable_failure_kind(monkeypatch, body, kind):
    settings = configuration(monkeypatch)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))) as http,
        pytest.raises(InferenceFailureError) as caught,
    ):
        provider.ModelClient(client=http).chat(model=settings.ollama_fast_model, messages=[])
    assert caught.value.failure_kind is kind
    assert caught.value.detail == str(kind)


@pytest.mark.parametrize(("status", "kind"), ((400, "http"), (404, "http"), (503, "busy")))
def test_http_failures_preserve_only_status_and_cause(monkeypatch, status, kind):
    settings = configuration(monkeypatch)
    with (
        httpx.Client(transport=httpx.MockTransport(
            lambda _r: httpx.Response(status, json={"error": "SENSITIVE_PROVIDER_ERROR"}),
        )) as http,
        pytest.raises(InferenceFailureError) as caught,
    ):
        provider.ModelClient(client=http).chat(model=settings.ollama_fast_model, messages=[])
    assert caught.value.detail == kind
    assert caught.value.http_status == status
    assert "SENSITIVE" not in str(caught.value.__dict__)


def test_schema_failure_is_distinct_from_transport(monkeypatch):
    settings = configuration(monkeypatch)
    body = {"message": {"content": json.dumps({"unexpected": True})}}
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))) as http,
        pytest.raises(InferenceFailureError) as caught,
    ):
        provider.ModelClient(client=http).chat(
            model=settings.ollama_fast_model, messages=[],
            response_schema={"type": "object", "required": ["answer"]},
        )
    assert caught.value.failure_kind is InferenceFailureKind.SCHEMA
