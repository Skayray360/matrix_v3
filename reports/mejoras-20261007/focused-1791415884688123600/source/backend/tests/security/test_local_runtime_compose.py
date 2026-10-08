# Creado por Aldo Garcia.
"""Fronteras del runtime opcional; no sustituye arrancar Docker con una GPU."""

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


def _compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def _default(value: str) -> str:
    """Resuelve unicamente defaults de las dos variables comprobadas aqui."""
    return re.sub(r"\$\{[A-Z_]+:-([^}]+)\}", r"\1", value)


@pytest.mark.parametrize("overlay", [False, True])
def test_endpoint_del_perfil_docker_es_aceptado_solo_por_allowlist(monkeypatch, overlay):
    environment = _compose("docker-compose.yml")["services"]["backend"]["environment"]
    if overlay:
        environment.update(_compose("docker-compose.ollama.yml")["services"]["backend"]["environment"])
    settings = Settings(
        _env_file=None,
        app_env="test",
        ollama_base_url=_default(environment["OLLAMA_BASE_URL"]),
        llm_local_hosts=_default(environment["LLM_LOCAL_HOSTS"]),
        llm_local_only=True,
    )
    monkeypatch.setattr("app.llm.provider.get_settings", lambda: settings)
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No debe enviar HTTP"))) as client:
        ModelClient(client=client)
        settings.ollama_base_url = "https://runtime-no-autorizado.example"
        with pytest.raises(ConfigurationError, match="LLM_LOCAL_HOSTS"):
            ModelClient(client=client)


def test_runtime_no_comparte_red_con_bd_ni_publica_puertos_o_monta_conocimiento():
    base = _compose("docker-compose.yml")
    overlay = _compose("docker-compose.ollama.yml")
    runtime = overlay["services"]["ollama"]
    runtime_networks = set(runtime["networks"])
    assert runtime_networks
    for service in ("mysql", "qdrant", "nginx"):
        assert runtime_networks.isdisjoint(base["services"][service]["networks"])
    assert runtime_networks.issubset(overlay["services"]["backend"]["networks"])
    assert all(overlay["networks"][name]["internal"] is True for name in runtime_networks)
    assert not runtime.get("ports")
    assert not runtime.get("env_file")
    assert runtime["volumes"] == ["ollama_data:/root/.ollama"]
    assert runtime["environment"]["OLLAMA_NO_CLOUD"] == "1"
    assert overlay["services"]["backend"]["environment"]["LLM_LOCAL_ONLY"] == "true"


def test_runtime_exige_imagen_y_ram_seleccionadas_y_reserva_gpu_explicita():
    runtime = _compose("docker-compose.ollama.yml")["services"]["ollama"]
    assert "${OLLAMA_IMAGE:?" in runtime["image"]
    assert "${OLLAMA_MEMORY_LIMIT:?" in runtime["mem_limit"]
    devices = runtime["deploy"]["resources"]["reservations"]["devices"]
    assert all("gpu" in device["capabilities"] for device in devices)
    assert all(not {"count", "device_ids"}.issubset(device) for device in devices)
    assert runtime["pids_limit"] > 0
    assert "ALL" in runtime["cap_drop"]
    assert "no-new-privileges:true" in runtime["security_opt"]


def test_base_precreada_del_perfil_docker_tiene_consentimiento_de_adopcion():
    """Une el perfil de arranque y la guardia SQL; sin el flag un volumen nuevo no arranca."""
    services = _compose("docker-compose.yml")["services"]
    database = services["mysql"]["environment"]["MYSQL_DATABASE"]
    environment = services["backend"]["environment"]
    assert f"/{database}?" in environment["DATABASE_URL"]
    settings = Settings(
        _env_file=None, app_env="test",
        matrix_adopt_existing_database=environment["MATRIX_ADOPT_EXISTING_DATABASE"],
    )
    assert settings.matrix_adopt_existing_database is True
