# Creado por Aldo Garcia.
"""Contrato de sustitucion local y rollback sobre configuracion sintetica aislada."""

from __future__ import annotations

import json

import httpx
import pytest

from app.common.errors import ConfigurationError
from app.config import Settings
from app.llm.model_configuration import validate_generator
from app.llm.provider import ModelClient
from scripts.model_change import apply_plan, build_plan, rollback, validate_candidate

pytestmark = pytest.mark.unit

def env_path(root):
    path = root / "backend" / "config" / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


BASE = (
    "# Configuracion sintetica: conservar comentarios\nAPP_ENV=test\n"
    "APP_SECRET_KEY=synthetic-private-value-must-not-appear-in-plan\n"
    "OLLAMA_FAST_MODEL=gemma4:latest\nOLLAMA_DEEP_MODEL=gemma4:latest\n"
    f"LLM_FAST_DIGEST={'a' * 64}\nLLM_DEEP_DIGEST={'a' * 64}\n"
    "OLLAMA_EMBEDDING_MODEL=embeddinggemma:latest\nOLLAMA_EMBEDDING_DIMENSION=768\n"
).encode()
CANDIDATE = BASE.replace(b"gemma4:latest", b"future-local:verified").replace(b"a" * 64, b"b" * 64)


def validator(*, capabilities=None, digest="b" * 64, seen=None):
    seen = seen if seen is not None else []

    def validate(settings):
        def handler(request):
            data = json.loads(request.content) if request.content else {}
            seen.append((request.url.path, data))
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [
                    {"name": settings.ollama_fast_model, "digest": digest},
                ]})
            if request.url.path == "/api/show":
                return httpx.Response(200, json={
                    "capabilities": capabilities if capabilities is not None else ["completion"],
                    "model_info": {"fixture.context_length": 16384},
                })
            assert request.url.path == "/api/chat", "No debe inferir embeddings ni modificar el indice"
            content = '{"ok":true}' if "format" in data else "Hola."
            return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": content}})

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            return validate_candidate(settings, client=ModelClient(client=http, settings=settings))

    return validate


def test_change_apply_restore_keeps_embeddings_and_secrets_exact(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.model_change.assert_stopped", lambda root: None)
    env, backup = env_path(tmp_path), tmp_path / "private/env-before.bak"
    env.write_bytes(BASE)
    seen = []
    validate = validator(seen=seen)
    plan = build_plan(BASE, CANDIDATE, validate=validate)
    serialized = json.dumps(plan)
    assert "synthetic-private-value" not in serialized
    assert plan["embedding_changed"] is False and plan["reindex_required"] is False
    assert all(item["text_and_json_contract"] == "passed_synthetic" for item in plan["validation"])
    applied = apply_plan(tmp_path, plan, backup, validate=validate)
    assert applied["applied"] and backup.read_bytes() == BASE
    assert b"future-local:verified" in env.read_bytes()
    assert b"OLLAMA_EMBEDDING_MODEL=embeddinggemma:latest" in env.read_bytes()
    assert b"# Configuracion sintetica" in env.read_bytes()
    assert {payload.get("model") for path, payload in seen if path == "/api/chat"} == {"future-local:verified"}
    assert rollback(tmp_path, plan, backup)["restored"]
    assert env.read_bytes() == BASE


@pytest.mark.parametrize("alteration", [
    b"OLLAMA_EMBEDDING_MODEL=other:latest\n",
    b"OLLAMA_BASE_URL=http://127.0.0.1:11435\n",
    b"AUTH_PROVIDER=oidc\n",
])
def test_generator_plan_rejects_embedding_endpoint_or_other_changes(alteration):
    candidate = CANDIDATE
    if alteration.startswith(b"OLLAMA_EMBEDDING_MODEL"):
        candidate = candidate.replace(b"OLLAMA_EMBEDDING_MODEL=embeddinggemma:latest\n", alteration)
    else:
        candidate += alteration
    with pytest.raises(ValueError):
        build_plan(BASE, candidate, validate=lambda settings: pytest.fail("No debe contactar el runtime"))


@pytest.mark.parametrize("capabilities,digest", [(["embedding"], "b" * 64), (["completion"], "c" * 64)])
def test_plan_rejects_wrong_capability_or_changed_weights(capabilities, digest):
    with pytest.raises(ConfigurationError):
        build_plan(BASE, CANDIDATE, validate=validator(capabilities=capabilities, digest=digest))


def test_apply_and_rollback_never_overwrite_intervening_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.model_change.assert_stopped", lambda root: None)
    env, backup = env_path(tmp_path), tmp_path / "before.bak"
    plan = build_plan(BASE, CANDIDATE, validate=validator())
    env.write_bytes(BASE + b"# newer edit\n")
    with pytest.raises(ValueError, match="cambio desde el plan"):
        apply_plan(tmp_path, plan, backup, validate=validator())
    assert not backup.exists()
    env.write_bytes(BASE)
    apply_plan(tmp_path, plan, backup, validate=validator())
    env.write_bytes(env.read_bytes() + b"# later unrelated edit\n")
    with pytest.raises(ValueError, match="edicion posterior"):
        rollback(tmp_path, plan, backup)
    assert env.read_bytes().endswith(b"# later unrelated edit\n")


def test_candidate_settings_do_not_inherit_console_model_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_FAST_MODEL", "unintended-model")
    plan = build_plan(BASE, CANDIDATE, validate=validator())
    assert plan["validation"][0]["model"] == "future-local:verified"


def test_compatible_local_contract_changes_generator_without_embedding_calls():
    settings = Settings(_env_file=None, app_env="test", llm_provider="openai_compatible",
                        llm_deep_provider="openai_compatible", ollama_fast_model="future-local:1",
                        ollama_deep_model="future-local:1", llm_fast_digest="b" * 64, llm_deep_digest="b" * 64)
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "future-local:1", "digest": "b" * 64}]})
        assert request.url.path == "/v1/chat/completions"
        data = json.loads(request.content)
        content = '{"ok":true}' if data.get("response_format") else "Hola."
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = validate_candidate(settings, client=ModelClient(client=http, settings=settings))
    assert all(row["text_and_json_contract"] == "passed_synthetic" for row in result)
    assert all(row["capabilities"] is None for row in result), "No inventar metadata de un adapter compatible"
    assert set(paths) == {"/v1/models", "/v1/chat/completions"}


def test_context_larger_than_declared_model_limit_fails():
    settings = Settings(_env_file=None, app_env="test", ollama_fast_num_ctx=32768)

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": settings.ollama_fast_model, "digest": "a" * 64}]})
        return httpx.Response(200, json={"capabilities": ["completion"], "model_info": {"fixture.context_length": 8192}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http, pytest.raises(ConfigurationError, match="contexto"):
        validate_generator(ModelClient(client=http, settings=settings), profile="fast")


@pytest.mark.parametrize("environment", ["development", "test"])
@pytest.mark.parametrize("provider,local", [("ollama", False), ("openai_compatible", False),
                                          ("vertex", False), ("vertex", True)])
def test_product_never_contacts_cloud_even_with_legacy_opt_in(monkeypatch, environment, provider, local):
    settings = Settings(_env_file=None, app_env=environment, llm_provider=provider, llm_deep_provider=provider,
                        llm_local_only=local, llm_vertex_base_url="https://cloud.example.test/models")
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("Network client must not be created"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: pytest.fail("Network client must not be created"))
    with pytest.raises(ConfigurationError, match="todos los entornos"):
        ModelClient(settings=settings)


def test_edited_endpoint_is_rejected_before_transport_after_client_created():
    settings = Settings(_env_file=None, app_env="test")
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("No red externa"))) as http:
        client = ModelClient(settings=settings, client=http)
        with pytest.raises(ConfigurationError, match="hosts locales"):
            client._post("https://external.example.test/chat", {"model": "synthetic"})
        settings.llm_local_only = False
        with pytest.raises(ConfigurationError, match="inferencia local"):
            client._post("/api/chat", {"model": "synthetic"})


def test_bootstrap_rejects_foreign_project_marker_before_preflight(tmp_path, monkeypatch, capsys):
    from scripts import bootstrap

    monkeypatch.setattr(bootstrap, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "check_integrity", lambda report: pytest.fail("No debe ejecutar la copia equivocada"))
    assert bootstrap.main(["serve", "--project-root", str(tmp_path / "foreign")]) == 1
    assert "no corresponde" in capsys.readouterr().out


def test_change_lock_blocks_bootstrap_and_preserves_existing_lock(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts import bootstrap
    from scripts.model_change import configuration_lock

    monkeypatch.setattr(bootstrap, "PROJECT_ROOT", tmp_path)
    with configuration_lock(tmp_path):
        with pytest.raises(ValueError, match="bloqueo"), configuration_lock(tmp_path):
            pytest.fail("Dos cambios no deben entrar a la vez")
        assert (tmp_path / "knowledge-base/state/run/configuration-change.lock").is_file()
        assert bootstrap.cmd_serve(SimpleNamespace()) == 1
    assert not (tmp_path / "knowledge-base/state/run/configuration-change.lock").exists()
