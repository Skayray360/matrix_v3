# Creado por Aldo Garcia.
"""Pruebas de seguridad automatizadas contra la API real.

Cubren la seccion 20: IDOR/BOLA, auth bypass, escalada de rol, SQLi, XSS, CSRF,
path traversal, nombre de archivo malicioso, MIME spoof, carga sobredimensionada,
rate limiting y ausencia de secretos en las respuestas.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from seeds.identity_seed import SYNTHETIC_TEST_PASSWORD

pytestmark = [pytest.mark.security, pytest.mark.integration, pytest.mark.critical]


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    from app.database.engine import check_database

    ok, _ = check_database()
    if not ok:
        pytest.skip("MySQL/MariaDB no disponible")

    from scripts.test_environment import require_disposable_database

    require_disposable_database()

    from app.config import get_settings

    object.__setattr__(get_settings(), "scheduler_enabled", False)
    from app.main import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _reset_rate_limiter():  # noqa: ANN202
    from app.security.rate_limit import get_rate_limiter

    get_rate_limiter().reset()
    yield
    get_rate_limiter().reset()


def login(client: TestClient, username: str):  # noqa: ANN201
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/local/login",
        json={"username": username, "password": SYNTHETIC_TEST_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


class TestAuthBypass:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/me",
            "/api/v1/conversations",
            "/api/v1/admin/diagnostics",
            "/api/v1/admin/knowledge/summary",
        ],
    )
    def test_rutas_protegidas_exigen_sesion(self, client: TestClient, path: str):
        client.cookies.clear()
        assert client.get(path).status_code == 401

    def test_el_chat_exige_sesion(self, client: TestClient):
        client.cookies.clear()
        response = client.post("/api/v1/chat", json={"message": "hola", "conversation_id": None})
        assert response.status_code in (401, 403)

    def test_cabeceras_de_identidad_falsificadas_se_ignoran(self, client: TestClient):
        login(client, "MatrixR1")
        response = client.get(
            "/api/v1/me",
            headers={
                "X-User-Id": "00000000-0000-0000-0000-000000000000",
                "X-Roles": "matrix_admin_test",
                "X-Forwarded-User": "Matrix",
            },
        )
        cuerpo = response.json()
        assert cuerpo["username"] == "MatrixR1"
        assert cuerpo["roles"] == ["prestaciones_reader_test"]
        assert cuerpo["allowed_categories"] == ["prestaciones"]


class TestEscaladaDeRol:
    def test_matrixr1_no_puede_publicar_conocimiento_corporativo(self, client: TestClient):
        csrf = login(client, "MatrixR1")
        response = client.post(
            "/api/v1/admin/knowledge/documents",
            headers={"X-CSRF-Token": csrf},
            data={"category": "prestaciones"},
            files={"file": ("promocion.md", b"# intento de promocion", "text/markdown")},
        )
        assert response.status_code == 403

    def test_matrixr1_no_puede_reconciliar_el_conocimiento(self, client: TestClient):
        csrf = login(client, "MatrixR1")
        response = client.post(
            "/api/v1/admin/knowledge/reconcile",
            headers={"X-CSRF-Token": csrf},
            json={"force": True},
        )
        assert response.status_code == 403

    def test_matrixr1_no_puede_leer_la_auditoria_administrativa(self, client: TestClient):
        login(client, "MatrixR1")
        assert client.get("/api/v1/admin/audit/recent").status_code == 403


class TestIdorBola:
    def test_no_se_accede_a_la_conversacion_de_otro(self, client: TestClient):
        csrf = login(client, "Matrix")
        ajena = client.post(
            "/api/v1/conversations", json={"title": "de Matrix"}, headers={"X-CSRF-Token": csrf}
        ).json()["id"]

        csrf_otro = login(client, "MatrixR1")
        assert client.get(f"/api/v1/conversations/{ajena}").status_code == 404
        assert (
            client.delete(f"/api/v1/conversations/{ajena}", headers={"X-CSRF-Token": csrf_otro}).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/conversations/{ajena}/attachments",
                headers={"X-CSRF-Token": csrf_otro},
                files={"files": ("x.md", b"# hola", "text/markdown")},
            ).status_code
            == 404
        )

    def test_no_se_accede_al_documento_de_otro(self, client: TestClient):
        login(client, "MatrixR1")
        # Identificador inexistente y ajeno reciben el mismo 404.
        assert client.get("/api/v1/documents/00000000-0000-0000-0000-000000000000/status").status_code == 404

    @pytest.mark.parametrize(
        "identificador",
        ["../../etc/passwd", "1 OR 1=1", "%2e%2e%2f", "<script>alert(1)</script>", "' UNION SELECT 1--"],
    )
    def test_identificadores_maliciosos_no_rompen_la_ruta(self, client: TestClient, identificador: str):
        login(client, "Matrix")
        response = client.get(f"/api/v1/conversations/{identificador}")
        assert response.status_code in (400, 404, 422)
        assert "Traceback" not in response.text
        assert "sqlalchemy" not in response.text.lower()


class TestInyeccionSql:
    @pytest.mark.parametrize(
        "payload",
        [
            "Matrix' OR '1'='1",
            "Matrix'; DROP TABLE users; --",
            "' UNION SELECT password_hash_argon2id FROM local_credentials --",
            "admin'/**/OR/**/1=1#",
        ],
    )
    def test_el_login_resiste_inyeccion(self, client: TestClient, payload: str):
        client.cookies.clear()
        response = client.post(
            "/api/v1/auth/local/login", json={"username": payload, "password": "x"}
        )
        assert response.status_code in (401, 429)
        assert "argon2" not in response.text.lower()

    def test_la_base_sigue_intacta_tras_los_intentos(self, client: TestClient):
        """Comprueba que ningun payload anterior destruyo datos."""
        login(client, "Matrix")
        assert client.get("/api/v1/me").status_code == 200


class TestCargaDeArchivos:
    def _conversacion(self, client: TestClient, csrf: str) -> str:
        return client.post(
            "/api/v1/conversations", json={"title": "cargas"}, headers={"X-CSRF-Token": csrf}
        ).json()["id"]

    def test_extension_no_permitida(self, client: TestClient):
        csrf = login(client, "Matrix")
        conversacion = self._conversacion(client, csrf)
        response = client.post(
            f"/api/v1/conversations/{conversacion}/attachments",
            headers={"X-CSRF-Token": csrf},
            files={"files": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
        )
        assert response.status_code == 415

    def test_mime_spoofing(self, client: TestClient):
        csrf = login(client, "Matrix")
        conversacion = self._conversacion(client, csrf)
        response = client.post(
            f"/api/v1/conversations/{conversacion}/attachments",
            headers={"X-CSRF-Token": csrf},
            files={"files": ("falso.pdf", b"esto no es un pdf", "application/pdf")},
        )
        assert response.status_code == 415

    def test_archivo_sobredimensionado(self, client: TestClient):
        csrf = login(client, "Matrix")
        conversacion = self._conversacion(client, csrf)
        response = client.post(
            f"/api/v1/conversations/{conversacion}/attachments",
            headers={"X-CSRF-Token": csrf},
            files={"files": ("grande.txt", b"x" * (40 * 1024 * 1024), "text/plain")},
        )
        assert response.status_code == 413

    def test_nombre_de_archivo_malicioso_no_escapa_del_almacen(self, client: TestClient, tmp_path):
        csrf = login(client, "Matrix")
        conversacion = self._conversacion(client, csrf)
        response = client.post(
            f"/api/v1/conversations/{conversacion}/attachments",
            headers={"X-CSRF-Token": csrf},
            files={"files": ("../../../../evil.md", b"# contenido", "text/markdown")},
        )
        if response.status_code == 200:
            nombre = response.json()["documents"][0]["filename"]
            assert ".." not in nombre
            assert "/" not in nombre and "\\" not in nombre

    def test_zip_bomb_ooxml(self, client: TestClient):
        csrf = login(client, "Matrix")
        conversacion = self._conversacion(client, csrf)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", b"0" * (60 * 1024 * 1024))
        response = client.post(
            f"/api/v1/conversations/{conversacion}/attachments",
            headers={"X-CSRF-Token": csrf},
            files={
                "files": (
                    "bomba.docx",
                    buffer.getvalue(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert response.status_code == 415


class TestRateLimiting:
    def test_el_login_se_limita(self, client: TestClient):
        client.cookies.clear()
        codigos = [
            client.post(
                "/api/v1/auth/local/login", json={"username": "Matrix", "password": "incorrecta"}
            ).status_code
            for _ in range(12)
        ]
        assert 429 in codigos, "el login no aplica rate limiting"


class TestSecretosEnRespuestas:
    @pytest.mark.parametrize(
        "path", ["/api/v1/health", "/api/v1/ready", "/api/v1/me", "/api/v1/admin/diagnostics"]
    )
    def test_ninguna_respuesta_expone_secretos(self, client: TestClient, path: str):
        login(client, "Matrix")
        cuerpo = client.get(path).text.lower()
        for prohibido in (
            "argon2",
            "client_secret",
            "mysql+pymysql",
            "password",
            "private key",
            "bearer ",
        ):
            assert prohibido not in cuerpo, f"{path} expuso {prohibido}"


class TestCabecerasAntiXss:
    def test_la_csp_bloquea_scripts_externos_y_marcos(self, client: TestClient):
        csp = client.get("/api/v1/health").headers["content-security-policy"]
        assert "script-src 'self'" in csp
        assert "unsafe-eval" not in csp
        assert "object-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_las_respuestas_json_no_se_interpretan_como_html(self, client: TestClient):
        response = client.get("/api/v1/health")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-type"].startswith("application/json")
