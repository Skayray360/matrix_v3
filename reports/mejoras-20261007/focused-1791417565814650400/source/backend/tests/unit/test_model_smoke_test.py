# Creado por Aldo Garcia.
"""El contrato usa ModelClient real y HTTP simulado: no valida pesos ni GPU."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config.settings import Settings
from app.llm.provider import ModelClient
from app.structured_data.schemas import StructuredQueryPlan
from scripts import model_smoke_test

pytestmark = pytest.mark.unit

MODEL_NAMES = (
    "nemotron-3-nano-omni-30b-a3b-reasoning",
    "gemma-4-31B-it-FP8-dynamic",
    "gemma-4-26B-A4B-it-AWQ-4bit",
)
PLAN = {
    "source": "smoke_demo",
    "entity": "contenedores",
    "fields": ["nombre", "unidades"],
    "filters": [{"field": "nombre", "operator": "eq", "value": "Alfa"}],
    "limit": 1,
}


def configured(monkeypatch, *, model=MODEL_NAMES[0], provider="openai_compatible", fault="", **overrides):
    settings = Settings(
        _env_file=None,
        llm_provider=provider,
        llm_deep_provider=provider,
        llm_embedding_provider=provider,
        ollama_fast_model=model,
        ollama_deep_model=model,
        ollama_embedding_model="embedding-fixture",
        ollama_embedding_dimension=2,
        rag_embedding_dimension=2,
        llm_fast_thinking="disabled",
        llm_deep_thinking="enabled",
        llm_structured_thinking="disabled",
        llm_embedding_revision=overrides.pop("llm_embedding_revision", "PRIVATE_SECRET_FIXTURE_REVISION"),
        **overrides,
    )
    monkeypatch.setattr(model_smoke_test, "get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.ollama_client.get_settings", lambda: settings)
    calls = []

    def transport(request):
        calls.append(request)
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        names = (model, settings.ollama_embedding_model)
        if fault == "inventory_missing":
            names = (settings.ollama_embedding_model,)
        if path in ("/v1/models", "/api/tags"):
            if fault == "inventory_error":
                return httpx.Response(503, json={"message": "PRIVATE_SECRET_SERVER"})
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": name} for name in names]})
            return httpx.Response(200, json={"models": [
                {
                    "name": name,
                    "digest": (
                        "" if fault == "embedding_digest_missing" and name == settings.ollama_embedding_model
                        else "PRIVATE_SECRET_FIXTURE_DIGEST"
                    ),
                }
                for name in names
            ]})
        if path in ("/v1/embeddings", "/api/embed"):
            vectors = [[0.25, 0.75], [0.5, 0.5]]
            if fault == "embedding_dimension":
                vectors = [[0.1], [0.2]]
            elif fault == "embedding_zero":
                vectors = [[0, 0], [0, 0]]
            if path == "/api/embed":
                return httpx.Response(200, json={"embeddings": vectors})
            return httpx.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]})
        if path in ("/v1/chat/completions", "/api/chat"):
            structured = "response_format" in body or "format" in body
            content = json.dumps(PLAN) if structured else "12"
            if structured and fault == "schema_extra":
                content = json.dumps({**PLAN, "raw_sql": "PRIVATE_SECRET_SQL"})
            elif structured and fault == "plan_wrong_source":
                content = json.dumps({**PLAN, "source": "other_source"})
            elif structured and fault == "plan_invalid_identifier":
                content = json.dumps({**PLAN, "entity": "bad.entity"})
            elif structured and fault == "malformed_json":
                content = "PRIVATE_SECRET_NOT_JSON"
            elif not structured and fault == "wrong_number":
                content = "42"
            elif not structured and fault == "reasoning_leak":
                content = "<think>PRIVATE_SECRET_THOUGHT</think>12"
            if fault == "http_error":
                return httpx.Response(500, json={"message": "PRIVATE_SECRET_SERVER"})
            if fault == "timeout":
                raise httpx.ReadTimeout("PRIVATE_SECRET_ENDPOINT", request=request)
            if path == "/api/chat":
                return httpx.Response(
                    200,
                    json={
                        "message": {"content": content, "thinking": "PRIVATE_SECRET_THOUGHT"},
                        "done": True,
                        "done_reason": "length" if fault == "truncated" else "stop",
                        "prompt_eval_count": 25,
                        "eval_count": 3,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "finish_reason": "length" if fault == "truncated" else "stop",
                        "message": {"content": content, "reasoning_content": "PRIVATE_SECRET_THOUGHT"},
                    }],
                    "usage": {"prompt_tokens": 25, "completion_tokens": 3},
                },
            )
        pytest.fail(f"Ruta no esperada: {path}")

    clients = []

    def create_client():
        client = ModelClient(client=httpx.Client(transport=httpx.MockTransport(transport)))
        clients.append(client)
        return client

    monkeypatch.setattr(model_smoke_test, "ModelClient", create_client)
    return settings, calls, clients


@pytest.mark.parametrize("model", MODEL_NAMES)
@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_three_model_names_work_by_configuration_with_real_adapter(monkeypatch, model, provider):
    settings, calls, clients = configured(monkeypatch, model=model, provider=provider)
    report = model_smoke_test.run_smoke_test("fast")
    assert report["passed"]
    assert report["status"] == "completed"
    assert len(calls) == (5 if provider == "ollama" else 4)  # Ollama vuelve a obtener el digest.
    assert clients[0]._client.is_closed
    embedding_payload = json.loads(next(
        request.content for request in calls if request.url.path in ("/api/embed", "/v1/embeddings")
    ))
    assert embedding_payload["model"] == settings.ollama_embedding_model
    assert embedding_payload["input"][0].startswith("task: search result | query:")
    assert embedding_payload["input"][1].startswith("title: Fixture sintetico | text:")
    schema_payload = json.loads(calls[-1].content)
    actual_schema = (
        schema_payload["format"] if provider == "ollama"
        else schema_payload["response_format"]["json_schema"]["schema"]
    )
    assert actual_schema == StructuredQueryPlan.model_json_schema()
    assert "PRIVATE_SECRET" not in json.dumps(report)
    assert not report["corporate_rag_validated"]
    assert not report["concurrency_validated"]


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_all_profiles_keep_own_budget_and_thinking_when_same_model(monkeypatch, provider):
    settings, calls, _ = configured(monkeypatch, provider=provider)
    report = model_smoke_test.run_smoke_test("all")
    assert report["passed"]
    assert len(calls) == (7 if provider == "ollama" else 6)
    payloads = [
        json.loads(request.content) for request in calls
        if request.url.path in ("/api/chat", "/v1/chat/completions")
    ]
    if provider == "ollama":
        assert [payload["think"] for payload in payloads] == [False, False, True, False]
        assert [payload["options"]["num_ctx"] for payload in payloads] == [
            settings.ollama_fast_num_ctx, settings.ollama_fast_num_ctx,
            settings.ollama_deep_num_ctx, settings.ollama_deep_num_ctx,
        ]
        budgets = [payload["options"]["num_predict"] for payload in payloads]
    else:
        assert [payload["chat_template_kwargs"]["enable_thinking"] for payload in payloads] == [False, False, True, False]
        budgets = [payload["max_tokens"] for payload in payloads]
    assert budgets == [
        settings.ollama_fast_max_tokens, settings.llm_planner_max_tokens,
        settings.ollama_deep_max_tokens, settings.llm_planner_max_tokens,
    ]


@pytest.mark.parametrize(
    "fault,code",
    [
        ("embedding_dimension", "embedding_contract_failed"),
        ("embedding_zero", "embedding_shape_or_zero_vector"),
        ("wrong_number", "synthetic_numeric_answer_mismatch"),
        ("reasoning_leak", "synthetic_numeric_answer_mismatch"),
        ("schema_extra", "structured_contract_failed"),
        ("plan_wrong_source", "synthetic_plan_mismatch"),
        ("plan_invalid_identifier", "structured_contract_failed"),
        ("malformed_json", "structured_contract_failed"),
        ("truncated", "chat_contract_failed"),
        ("http_error", "chat_contract_failed"),
        ("timeout", "chat_contract_failed"),
    ],
)
@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_contract_failures_are_explicit_without_response_or_secret(monkeypatch, provider, fault, code):
    _, _, clients = configured(monkeypatch, provider=provider, fault=fault)
    report = model_smoke_test.run_smoke_test()
    assert not report["passed"]
    expected_codes = {code, "chat_contract_failed"} if fault == "reasoning_leak" else {code}
    assert expected_codes.intersection(check["code"] for check in report["checks"])
    assert "PRIVATE_SECRET" not in json.dumps(report)
    assert clients[0]._client.is_closed


@pytest.mark.parametrize("fault", ["inventory_missing", "inventory_error"])
def test_inventory_failure_does_not_start_inference(monkeypatch, fault):
    _, calls, _ = configured(monkeypatch, fault=fault)
    report = model_smoke_test.run_smoke_test()
    assert not report["passed"]
    assert report["status"] == "blocked"
    assert len(calls) == 1


@pytest.mark.parametrize("revision", ["unverified", ""])
def test_compatible_embedding_revision_required_before_inference(monkeypatch, revision):
    _, calls, clients = configured(monkeypatch, llm_embedding_revision=revision)
    report = model_smoke_test.run_smoke_test("all")
    assert not report["passed"]
    assert report["status"] == "blocked"
    assert report["checks"][-1]["code"] == "embedding_revision_unavailable"
    assert all(request.method == "GET" for request in calls)
    assert clients[0]._client.is_closed
    assert "PRIVATE_SECRET" not in json.dumps(report)


def test_ollama_embedding_digest_required_before_inference(monkeypatch):
    _, calls, clients = configured(monkeypatch, provider="ollama", fault="embedding_digest_missing")
    report = model_smoke_test.run_smoke_test("all")
    assert not report["passed"]
    assert report["status"] == "blocked"
    assert report["checks"][-1]["code"] == "embedding_revision_unavailable"
    assert all(request.method == "GET" for request in calls)
    assert clients[0]._client.is_closed
    assert "PRIVATE_SECRET" not in json.dumps(report)


@pytest.mark.parametrize("overrides", [{"llm_local_only": False}, {"llm_deep_provider": "vertex"}])
def test_cloud_rejected_before_client_constructor(monkeypatch, overrides):
    settings = Settings(_env_file=None).model_copy(update=overrides)
    monkeypatch.setattr(model_smoke_test, "get_settings", lambda: settings)
    monkeypatch.setattr(model_smoke_test, "ModelClient", lambda: pytest.fail("no constructor permitido"))
    report = model_smoke_test.run_smoke_test()
    assert report["checks"][0]["code"] == "local_only_required"
    assert not report["passed"]


def test_endpoint_allowlist_rejected_before_http(monkeypatch):
    _, calls, _ = configured(monkeypatch, llm_api_base_url="http://unapproved.internal/v1")
    report = model_smoke_test.run_smoke_test()
    assert not report["passed"]
    assert report["checks"][0]["code"] == "local_adapter_rejected"
    assert not calls


def test_no_opt_in_loads_neither_settings_nor_model(monkeypatch, capsys):
    monkeypatch.setattr(model_smoke_test, "get_settings", lambda: pytest.fail("sin opt-in no config"))
    monkeypatch.setattr(model_smoke_test, "ModelClient", lambda: pytest.fail("sin opt-in no cliente"))
    assert model_smoke_test.main([]) == 2
    assert "INCOMPLETE" in capsys.readouterr().out


def test_cli_writes_report_and_returns_failure_code(monkeypatch, tmp_path, capsys):
    configured(monkeypatch, fault="wrong_number")
    output = tmp_path / "smoke.json"
    assert model_smoke_test.main(["--run-inference", "--output", str(output)]) == 1
    assert not json.loads(output.read_text())["passed"]
    assert "PRIVATE_SECRET" not in capsys.readouterr().out


def test_cli_does_not_overwrite_existing_file(monkeypatch, tmp_path, capsys):
    configured(monkeypatch)
    output = tmp_path / "existing.env"
    output.write_text("keep-existing-data", encoding="utf-8")
    assert model_smoke_test.main(["--run-inference", "--output", str(output)]) == 1
    assert output.read_text() == "keep-existing-data"
    assert json.loads(capsys.readouterr().out)["code"] == "report_write_failed"


def test_cli_success_has_correct_exit_code(monkeypatch, capsys):
    configured(monkeypatch)
    assert model_smoke_test.main(["--run-inference", "--profile", "deep"]) == 0
    assert json.loads(capsys.readouterr().out)["passed"]
