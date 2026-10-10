# Creado por Aldo Garcia.
"""Fronteras del perfil Docker Linux opcional; no acredita ejecucion de los contenedores."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import yaml

from app.common.errors import ConfigurationError
from app.config.settings import Settings
from app.llm.provider import ModelClient

ROOT = Path(__file__).resolve().parents[3]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_endpoint_del_perfil_docker_es_aceptado_solo_por_allowlist(monkeypatch):
    environment = _compose()["services"]["backend"]["environment"]
    settings = Settings(
        _env_file=None, app_env="test", ollama_base_url=environment["OLLAMA_BASE_URL"],
        llm_local_hosts=environment["LLM_LOCAL_HOSTS"], llm_local_only=True,
    )
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No debe enviar HTTP"))) as client:
        ModelClient(client=client)
        settings.ollama_base_url = "https://runtime-no-autorizado.example"
        with pytest.raises(ConfigurationError, match="LLM_LOCAL_HOSTS"):
            ModelClient(client=client)


def test_runtime_no_comparte_red_con_bd_ni_publica_puertos_o_monta_conocimiento():
    compose = _compose()
    services = compose["services"]
    runtime = services["ollama"]
    runtime_networks = set(runtime["networks"])
    assert runtime_networks
    for name in ("mysql", "qdrant", "nginx"):
        assert runtime_networks.isdisjoint(services[name]["networks"])
    assert runtime_networks.issubset(services["backend"]["networks"])
    assert all(compose["networks"][name]["internal"] is True for name in runtime_networks)
    assert not runtime.get("ports")
    assert not runtime.get("env_file")
    assert [(v["source"], v["target"]) for v in runtime["volumes"]] == [
        ("./knowledge-base/state/docker/ollama", "/models"),
    ]
    assert runtime["environment"]["OLLAMA_NO_CLOUD"] == "1"
    assert services["backend"]["environment"]["LLM_LOCAL_ONLY"] == "true"
    assert all(compose["networks"][name]["internal"] for name in services["backend"]["networks"])


def test_runtime_fija_imagen_y_limites_sin_exigir_gpu_ajena_al_equipo():
    runtime = _compose()["services"]["ollama"]
    assert re.fullmatch(r"ollama/ollama:[^@]+@sha256:[0-9a-f]{64}", runtime["image"])
    assert "${OLLAMA_MEMORY_LIMIT:-" in runtime["mem_limit"]
    assert runtime["cpus"] and runtime["pids_limit"] > 0
    assert "ALL" in runtime["cap_drop"]
    assert "no-new-privileges:true" in runtime["security_opt"]
    assert not runtime.get("deploy") and not runtime.get("devices")
    assert runtime["read_only"] is True


def test_descarga_es_opt_in_y_no_recibe_documentos_ni_secretos():
    compose = _compose()
    service = compose["services"]["model-download"]
    assert service["profiles"] == ["prepare"]
    assert not service.get("ports") and not service.get("env_file") and not service.get("secrets")
    assert set(service["networks"]).isdisjoint(compose["services"]["ollama"]["networks"])
    assert [v["target"] for v in service["volumes"]] == ["/models"]
    assert all(not compose["networks"][name].get("internal", False) for name in service["networks"])
    assert "ollama show" in service["command"][0]  # No reemplazar tags ya descargados al repetir.
    assert service["environment"]["OLLAMA_NO_CLOUD"] == "1"


def test_base_precreada_del_perfil_docker_tiene_consentimiento_de_adopcion():
    services = _compose()["services"]
    environment = services["backend"]["environment"]
    assert services["mysql"]["environment"]["MYSQL_DATABASE"] == "matrix_rh"
    assert services["mysql"]["environment"]["MYSQL_USER"] == "matrixrh"
    assert "MYSQL_ROOT_PASSWORD" not in environment
    assert services["mysql"]["environment"]["MYSQL_ROOT_PASSWORD_FILE"] == "/run/secrets/mysql-root"
    assert services["backend"]["env_file"] == ["./backend/config/docker.env"]
    settings = Settings(
        _env_file=None, app_env="test",
        matrix_adopt_existing_database=environment["MATRIX_ADOPT_EXISTING_DATABASE"],
    )
    assert settings.matrix_adopt_existing_database is True


def test_unico_puerto_loopback_tls_y_sin_contenedores_globales():
    compose = _compose()
    services = compose["services"]
    assert [(name, service["ports"]) for name, service in services.items() if service.get("ports")] == [
        ("nginx", ["127.0.0.1:8443:8443"]),
    ]
    assert all("container_name" not in service for service in services.values())
    assert compose["name"] == "matrixrh-${MATRIX_DOCKER_PROJECT_ID:?ejecute docker_prepare}"
    assert not compose.get("volumes")  # Estado en binds dentro de esta copia Linux.
    for name in ("mysql", "qdrant", "ollama", "backend", "nginx"):
        assert services[name]["healthcheck"]
        assert services[name]["restart"] == "unless-stopped"
        assert services[name]["mem_limit"] and services[name]["pids_limit"] > 0
    environment = services["backend"]["environment"]
    assert environment["APP_ENV"] == "development"
    assert environment["APP_BASE_URL"] == "https://127.0.0.1:8443"
    assert environment["SESSION_COOKIE_SECURE"] == "true"
    assert environment["AUTH_PROVIDER"] == "local"
    assert environment["LOCAL_TEST_AUTH_ENABLED"] == "false"
    assert environment["LOCAL_TEST_SEED_USERS_ENABLED"] == "false"


def test_imagenes_fijadas_y_build_sin_secretos_ni_estado():
    compose = _compose()
    for service in compose["services"].values():
        if "build" not in service:
            assert re.search(r"@sha256:[0-9a-f]{64}$", service["image"])
    dockerfile = (ROOT / "backend/Dockerfile").read_text()
    for line in dockerfile.splitlines():
        if line.startswith("FROM "):
            assert re.search(r"@sha256:[0-9a-f]{64}(?: AS \w+)?$", line)
    assert "uv==0.12.23" in dockerfile
    assert "--frozen --no-dev --no-editable" in dockerfile
    assert "npm ci --ignore-scripts" in dockerfile
    assert "USER matrixrh" in dockerfile
    exclusions = (ROOT / "backend/Dockerfile.dockerignore").read_text().splitlines()
    for relative in ("knowledge-base", "backend/config/secrets", "**/docker.env", "**/.env", "backend/runtime"):
        assert relative in exclusions
