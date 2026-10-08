# Creado por Aldo Garcia.
"""Cliente Ollama contra un transporte simulado.

Se usa ``httpx.MockTransport``: permite ejercitar reintentos, timeouts, errores
4xx/5xx y la verificacion de dimension **sin** depender de que Ollama este
instalado, y de forma determinista.
"""

from __future__ import annotations

import httpx
import pytest

from app.common.errors import EmbeddingDimensionMismatchError, OllamaUnavailableError
from app.llm.ollama_client import OllamaClient

pytestmark = pytest.mark.unit


def build_client(handler, *, timeout: float = 5.0) -> OllamaClient:  # noqa: ANN001
    transport = httpx.MockTransport(handler)
    return OllamaClient(client=httpx.Client(transport=transport, timeout=timeout))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):  # noqa: ANN001, ANN202
    """Los reintentos usan backoff; en pruebas no se espera de verdad."""
    monkeypatch.setattr("app.llm.ollama_client.time.sleep", lambda _s: None)


class TestPingEInventario:
    def test_ping_ok(self):
        client = build_client(lambda _r: httpx.Response(200, json={"models": []}))
        assert client.ping() is True

    def test_ping_falla_sin_excepcion(self):
        def handler(_request):  # noqa: ANN001, ANN202
            raise httpx.ConnectError("sin conexion")

        assert build_client(handler).ping() is False

    def test_lista_los_modelos(self):
        client = build_client(
            lambda _r: httpx.Response(
                200,
                json={"models": [{"name": "gemma4:latest"}, {"name": "qwen3.6:latest"}]},
            )
        )
        inventory = client.list_models()
        assert inventory.has("gemma4:latest") is True
        assert inventory.has("inexistente:latest") is False

    def test_error_http_en_tags_se_traduce(self):
        client = build_client(lambda _r: httpx.Response(500))
        with pytest.raises(OllamaUnavailableError):
            client.list_models()


class TestChat:
    def test_devuelve_contenido_y_latencia(self):
        client = build_client(
            lambda _r: httpx.Response(200, json={"message": {"content": "  respuesta  "}})
        )
        result = client.chat(model="gemma4:latest", messages=[{"role": "user", "content": "hola"}])
        assert result.content == "respuesta"
        assert result.model == "gemma4:latest"
        assert result.latency_ms >= 0

    def test_envia_las_opciones_configuradas(self):
        capturado: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            capturado.update(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": "ok"}})

        client = build_client(handler)
        client.chat(
            model="gemma4:latest",
            messages=[{"role": "user", "content": "x"}],
            temperature=0.3,
            max_tokens=128,
            stop=["FIN"],
        )
        assert capturado["stream"] is False
        assert capturado["options"]["temperature"] == 0.3
        assert capturado["options"]["num_predict"] == 128
        assert capturado["options"]["stop"] == ["FIN"]
        assert capturado["keep_alive"]


class TestReintentos:
    def test_saturacion_no_se_reintenta(self):
        intentos = {"n": 0}

        def handler(_request):  # noqa: ANN001, ANN202
            intentos["n"] += 1
            if intentos["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"message": {"content": "ok"}})

        client = build_client(handler)
        with pytest.raises(OllamaUnavailableError):
            client.chat(model="gemma4:latest", messages=[])
        assert intentos["n"] == 1

    def test_un_4xx_no_se_reintenta(self):
        """Un 4xx indica una peticion incorrecta: reintentar esconde el diagnostico."""
        intentos = {"n": 0}

        def handler(_request):  # noqa: ANN001, ANN202
            intentos["n"] += 1
            return httpx.Response(404, json={"error": "model not found"})

        client = build_client(handler)
        with pytest.raises(OllamaUnavailableError, match="rechazada"):
            client.chat(model="inexistente:latest", messages=[])
        assert intentos["n"] == 1

    def test_los_reintentos_estan_acotados(self):
        intentos = {"n": 0}

        def handler(_request):  # noqa: ANN001, ANN202
            intentos["n"] += 1
            raise httpx.ConnectTimeout("timeout")

        client = build_client(handler)
        with pytest.raises(OllamaUnavailableError):
            client.chat(model="gemma4:latest", messages=[])
        assert intentos["n"] == 1


class TestEmbeddings:
    def _handler(self, vectors):  # noqa: ANN001, ANN202
        return lambda _r: httpx.Response(200, json={"embeddings": vectors})

    def test_dimension_correcta(self):
        client = build_client(self._handler([[0.1] * 768]))
        assert len(client.embed_one("texto")) == 768

    def test_dimension_incorrecta_falla_sin_truncar(self):
        client = build_client(self._handler([[0.1] * 512]))
        with pytest.raises(EmbeddingDimensionMismatchError, match="no coincide") as excinfo:
            client.embed(["texto"])
        # El detalle tecnico deja constancia de que no se adapta el vector.
        assert "No se trunca ni se rellena" in (excinfo.value.detail or "")
        assert "real=512" in (excinfo.value.detail or "")

    def test_numero_de_vectores_distinto_al_de_textos(self):
        client = build_client(self._handler([[0.1] * 768]))
        with pytest.raises(EmbeddingDimensionMismatchError):
            client.embed(["uno", "dos"])

    def test_lista_vacia_no_llama_al_servicio(self):
        def handler(_request):  # noqa: ANN001, ANN202
            raise AssertionError("no deberia llamarse")

        assert build_client(handler).embed([]) == []

    def test_embed_one_cachea_por_hash_sin_repetir_http(self):
        calls = {"count": 0}

        def handler(_request):  # noqa: ANN001, ANN202
            calls["count"] += 1
            return httpx.Response(200, json={"embeddings": [[0.1] * 768]})

        client = build_client(handler)
        first = client.embed_one("pregunta repetida")
        second = client.embed_one("pregunta repetida")
        assert first == second
        assert calls["count"] == 1

    def test_sonda_de_dimension_reporta_el_valor_real(self):
        """La sonda debe poder REPORTAR una discrepancia, no fallar por ella."""
        client = build_client(self._handler([[0.1] * 1024]))
        assert client.probe_embedding_dimension() == 1024

    def test_sonda_sin_vectores_falla(self):
        client = build_client(self._handler([]))
        with pytest.raises(EmbeddingDimensionMismatchError):
            client.probe_embedding_dimension()
