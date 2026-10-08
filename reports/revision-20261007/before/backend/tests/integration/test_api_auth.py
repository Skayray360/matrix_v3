# Creado por Aldo Garcia.
"""Integracion FastAPI + MySQL: sesion, CSRF, ownership y bloqueos de entorno.

Estas pruebas usan la aplicacion real y la base real. No tocan Qdrant ni Ollama,
de modo que se pueden ejecutar aunque el backend este arriba en modo embedded.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from seeds.identity_seed import SYNTHETIC_TEST_PASSWORD

pytestmark = [pytest.mark.integration, pytest.mark.critical]


@pytest.fixture(scope="module")
def client(): # noqa: ANN201
    """Cliente de pruebas con el scheduler desactivado."""
    from app.database.engine import check_database

    ok, _ = check_database()
    if not ok:
        pytest.skip("MySQL/MariaDB no disponible")

    from scripts.test_environment import require_disposable_database

    require_disposable_database()

    from app.config import get_settings

    settings = get_settings()
    object.__setattr__(settings, "scheduler_enabled", False)

    from app.main import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _reset_rate_limiter():  # noqa: ANN202
    """El limitador de login (5/min por IP) es un control real y activo.

    La bateria de pruebas hace muchos mas logins que un humano en un minuto, asi
    que se reinicia el contador antes de cada prueba. El control en si se valida
    en ``tests/security/test_rate_limiting.py``.
    """
    from app.security.rate_limit import get_rate_limiter

    get_rate_limiter().reset()
    yield
    get_rate_limiter().reset()


def login(client: TestClient, username: str, password: str = SYNTHETIC_TEST_PASSWORD):  # noqa: ANN201
    return client.post("/api/v1/auth/local/login", json={"username": username, "password": password})


class TestSalud:
    def test_health_no_depende_de_dependencias(self, client: TestClient):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_ready_enumera_componentes(self, client: TestClient):
        response = client.get("/api/v1/ready")
        assert response.status_code in (200, 503)
        nombres = {c["name"] for c in response.json()["components"]}
        assert "database" in nombres
        assert "ollama" in nombres

    def test_health_tambien_esta_en_la_raiz(self, client: TestClient):
        assert client.get("/health").status_code == 200


class TestLoginLocal:
    def test_login_matrix_correcto(self, client: TestClient):
        response = login(client, "Matrix")
        assert response.status_code == 200
        body = response.json()
        assert body["username"] == "Matrix"
        assert body["category_wildcard"] is True
        assert body["csrf_token"]
        client.cookies.clear()

    def test_login_matrixr1_correcto(self, client: TestClient):
        response = login(client, "MatrixR1")
        assert response.status_code == 200
        body = response.json()
        assert body["allowed_categories"] == ["prestaciones"]
        assert body["category_wildcard"] is False
        assert body["permissions"] == []
        client.cookies.clear()

    def test_contrasena_incorrecta_y_usuario_inexistente_dan_el_mismo_mensaje(
        self, client: TestClient
    ):
        malo = login(client, "Matrix", "contrasena-incorrecta")
        inexistente = login(client, "UsuarioInexistente", "cualquiera")
        assert malo.status_code == inexistente.status_code == 401
        assert malo.json()["message"] == inexistente.json()["message"]

    def test_la_cookie_de_sesion_es_httponly(self, client: TestClient):
        response = login(client, "Matrix")
        cookie_header = response.headers.get("set-cookie", "")
        assert "httponly" in cookie_header.lower()
        assert "samesite" in cookie_header.lower()
        client.cookies.clear()

    def test_la_respuesta_no_contiene_el_hash_ni_la_contrasena(self, client: TestClient):
        cuerpo = login(client, "Matrix").text
        assert "argon2" not in cuerpo.lower()
        assert SYNTHETIC_TEST_PASSWORD not in cuerpo
        client.cookies.clear()


class TestSesionYCsrf:
    def test_me_requiere_sesion(self, client: TestClient):
        client.cookies.clear()
        assert client.get("/api/v1/me").status_code == 401

    def test_peticion_mutante_sin_csrf_se_rechaza(self, client: TestClient):
        login(client, "Matrix")
        response = client.post("/api/v1/conversations", json={"title": "sin csrf"})
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"
        client.cookies.clear()

    def test_peticion_mutante_con_csrf_correcto_funciona(self, client: TestClient):
        csrf = login(client, "Matrix").json()["csrf_token"]
        response = client.post(
            "/api/v1/conversations", json={"title": "con csrf"}, headers={"X-CSRF-Token": csrf}
        )
        assert response.status_code == 200
        client.cookies.clear()

    def test_csrf_de_otra_sesion_no_sirve(self, client: TestClient):
        csrf_admin = login(client, "Matrix").json()["csrf_token"]
        client.cookies.clear()
        login(client, "MatrixR1")
        response = client.post(
            "/api/v1/conversations", json={"title": "x"}, headers={"X-CSRF-Token": csrf_admin}
        )
        assert response.status_code == 403
        client.cookies.clear()

    def test_logout_invalida_la_sesion(self, client: TestClient):
        csrf = login(client, "Matrix").json()["csrf_token"]
        assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.get("/api/v1/me").status_code == 401
        client.cookies.clear()

    def test_una_cookie_falsificada_no_abre_sesion(self, client: TestClient):
        client.cookies.clear()
        client.cookies.set("matrixrh_session", "token-inventado-por-un-atacante")
        assert client.get("/api/v1/me").status_code == 401
        client.cookies.clear()


class TestOwnershipDeConversaciones:
    def test_un_usuario_no_ve_la_conversacion_de_otro(self, client: TestClient):
        csrf = login(client, "Matrix").json()["csrf_token"]
        creada = client.post(
            "/api/v1/conversations", json={"title": "privada"}, headers={"X-CSRF-Token": csrf}
        ).json()
        client.cookies.clear()

        login(client, "MatrixR1")
        response = client.get(f"/api/v1/conversations/{creada['id']}")
        # 404 y no 403: un 403 confirmaria que el identificador existe.
        assert response.status_code == 404
        client.cookies.clear()

    def test_un_usuario_no_puede_borrar_la_conversacion_de_otro(self, client: TestClient):
        csrf = login(client, "Matrix").json()["csrf_token"]
        creada = client.post(
            "/api/v1/conversations", json={"title": "privada 2"}, headers={"X-CSRF-Token": csrf}
        ).json()
        client.cookies.clear()

        csrf_otro = login(client, "MatrixR1").json()["csrf_token"]
        response = client.delete(
            f"/api/v1/conversations/{creada['id']}", headers={"X-CSRF-Token": csrf_otro}
        )
        assert response.status_code == 404
        client.cookies.clear()

    def test_el_listado_solo_muestra_conversaciones_propias(self, client: TestClient):
        csrf = login(client, "Matrix").json()["csrf_token"]
        client.post(
            "/api/v1/conversations", json={"title": "solo de Matrix"}, headers={"X-CSRF-Token": csrf}
        )
        client.cookies.clear()

        login(client, "MatrixR1")
        titulos = [c["title"] for c in client.get("/api/v1/conversations").json()]
        assert "solo de Matrix" not in titulos
        client.cookies.clear()


class TestRutasAdministrativas:
    def test_matrixr1_no_accede_a_administracion(self, client: TestClient):
        login(client, "MatrixR1")
        assert client.get("/api/v1/admin/knowledge/summary").status_code == 403
        assert client.get("/api/v1/admin/diagnostics").status_code == 403
        client.cookies.clear()

    def test_matrix_accede_al_diagnostico_sin_secretos(self, client: TestClient):
        login(client, "Matrix")
        response = client.get("/api/v1/admin/diagnostics")
        assert response.status_code == 200
        cuerpo = response.text.lower()
        for prohibido in ("password", "client_secret", "mysql+pymysql", "argon2"):
            assert prohibido not in cuerpo
        client.cookies.clear()

    def test_el_diagnostico_expone_los_parametros_del_rag(self, client: TestClient):
        login(client, "Matrix")
        rag = client.get("/api/v1/admin/diagnostics").json()["rag"]
        assert rag["RAG_TOP_K"] == 6
        assert rag["RAG_FETCH_K"] == 24
        assert rag["RAG_CHUNK_SIZE_TOKENS"] == 900
        assert rag["OLLAMA_EMBEDDING_MODEL"] == "embeddinggemma:latest"
        client.cookies.clear()


class TestCabecerasYErrores:
    def test_cabeceras_de_seguridad(self, client: TestClient):
        headers = client.get("/api/v1/health").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["referrer-policy"] == "no-referrer"

    def test_cada_respuesta_lleva_request_id(self, client: TestClient):
        assert client.get("/api/v1/health").headers.get("x-request-id")

    def test_los_errores_no_filtran_detalles_internos(self, client: TestClient):
        login(client, "Matrix")
        response = client.get("/api/v1/conversations/id-inexistente")
        assert response.status_code == 404
        cuerpo = response.text
        assert "Traceback" not in cuerpo
        assert "sqlalchemy" not in cuerpo.lower()
        assert response.json()["code"] == "not_found"
        client.cookies.clear()

    def test_un_payload_invalido_no_hace_eco_de_los_valores(self, client: TestClient):
        response = client.post(
            "/api/v1/auth/local/login", json={"username": "x", "password": "y", "extra": "ClaveFiltrada"}
        )
        assert response.status_code == 422
        assert "ClaveFiltrada" not in response.text
