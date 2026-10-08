# Creado por Aldo Garcia.
"""Flujo OIDC de Entra ID completo con mocks criptograficamente coherentes.

La especificacion pide probar el callback de Entra ID "mediante mocks
criptograficamente coherentes en test". Eso es exactamente lo que hace este
modulo: se genera un par RSA local, se firma un ``id_token`` real, y el token
endpoint se simula con ``httpx.MockTransport``. La validacion criptografica del
adapter se ejercita de verdad, sin ningun atajo.

Lo unico que no se prueba aqui es la conectividad real con
``login.microsoftonline.com``; por eso el estado sigue siendo
``PREPARED_NOT_CONNECTED``.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app.auth.entra_provider import EntraIdentityProvider
from app.common.errors import UnauthorizedError
from app.common.ids import utcnow_naive
from app.config import get_settings
from app.database.models import OidcLoginState

pytestmark = pytest.mark.integration

TENANT = "tenant-de-prueba"
CLIENT = "client-de-prueba"
REDIRECT = "https://matrixrh.local/api/v1/auth/callback"


@pytest.fixture(scope="module")
def rsa_key():  # noqa: ANN201
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def entra_config(monkeypatch):  # noqa: ANN001, ANN202
    settings = get_settings()
    monkeypatch.setattr(settings, "entra_tenant_id", TENANT, raising=False)
    monkeypatch.setattr(settings, "entra_client_id", CLIENT, raising=False)
    monkeypatch.setattr(settings, "entra_redirect_uri", REDIRECT, raising=False)
    monkeypatch.setattr(settings, "entra_allowed_groups", "", raising=False)
    return settings


def build_id_token(rsa_key, nonce: str, **overrides) -> str:  # noqa: ANN001
    ahora = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": CLIENT,
        "sub": "sub-abc",
        "oid": "oid-xyz",
        "iat": ahora,
        "exp": ahora + 900,
        "nonce": nonce,
        "preferred_username": "empleado.demo@corporativo.mx",
        "name": "Empleado Demo",
        "email": "empleado.demo@corporativo.mx",
        "groups": ["grupo-prestaciones"],
    }
    claims.update(overrides)
    return jwt.encode(claims, rsa_key, algorithm="RS256")


class TestFlujoCompleto:
    def test_start_login_persiste_state_nonce_y_pkce(self, db_session, entra_config):  # noqa: ARG002
        provider = EntraIdentityProvider()
        challenge = provider.start_login(db_session, redirect_after="/inicio")

        assert challenge.authorization_url.startswith(
            f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?"
        )
        # PKCE S256 obligatorio y redirect exacto.
        assert "code_challenge_method=S256" in challenge.authorization_url
        assert "response_type=code" in challenge.authorization_url
        assert f"client_id={CLIENT}" in challenge.authorization_url

        record = db_session.execute(
            select(OidcLoginState).where(OidcLoginState.state == challenge.state)
        ).scalar_one()
        assert record.nonce
        assert record.code_verifier
        assert record.redirect_after == "/inicio"
        assert record.consumed_at is None
        # El verifier NUNCA viaja al navegador.
        assert record.code_verifier not in challenge.authorization_url

    def test_callback_completo_devuelve_identidad_normalizada(
        self, db_session, entra_config, rsa_key, monkeypatch
    ):  # noqa: ARG002
        provider = EntraIdentityProvider()
        challenge = provider.start_login(db_session)
        record = db_session.execute(
            select(OidcLoginState).where(OidcLoginState.state == challenge.state)
        ).scalar_one()

        id_token = build_id_token(rsa_key, record.nonce)

        def token_endpoint(request: httpx.Request) -> httpx.Response:
            assert str(request.url).endswith("/oauth2/v2.0/token")
            cuerpo = request.content.decode()
            # El canje debe incluir el verifier y el redirect exacto.
            assert "code_verifier=" in cuerpo
            assert "grant_type=authorization_code" in cuerpo
            return httpx.Response(200, json={"id_token": id_token, "access_token": "no-usado"})

        provider._http = httpx.Client(transport=httpx.MockTransport(token_endpoint))
        monkeypatch.setattr(provider, "_signing_key", lambda _t: rsa_key.public_key())

        identity = provider.authenticate(db_session, code="codigo-de-autorizacion", state=challenge.state)

        assert identity.subject_id == "oid-xyz"
        assert identity.auth_source == "entra"
        assert identity.username == "empleado.demo@corporativo.mx"
        assert identity.groups == ("grupo-prestaciones",)

        db_session.refresh(record)
        assert record.consumed_at is not None, "el state debe quedar consumido"

    def test_el_state_no_se_puede_reutilizar(self, db_session, entra_config, rsa_key, monkeypatch):  # noqa: ARG002
        provider = EntraIdentityProvider()
        challenge = provider.start_login(db_session)
        record = db_session.execute(
            select(OidcLoginState).where(OidcLoginState.state == challenge.state)
        ).scalar_one()
        id_token = build_id_token(rsa_key, record.nonce)

        provider._http = httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"id_token": id_token}))
        )
        monkeypatch.setattr(provider, "_signing_key", lambda _t: rsa_key.public_key())

        provider.authenticate(db_session, code="codigo", state=challenge.state)
        with pytest.raises(UnauthorizedError, match="estado"):
            provider.authenticate(db_session, code="codigo", state=challenge.state)

    def test_un_state_desconocido_se_rechaza(self, db_session, entra_config):  # noqa: ARG002
        with pytest.raises(UnauthorizedError, match="estado"):
            EntraIdentityProvider().authenticate(
                db_session, code="codigo", state="state-que-nunca-existio"
            )

    def test_un_state_expirado_se_rechaza(self, db_session, entra_config):  # noqa: ARG002
        from datetime import timedelta

        provider = EntraIdentityProvider()
        challenge = provider.start_login(db_session)
        record = db_session.execute(
            select(OidcLoginState).where(OidcLoginState.state == challenge.state)
        ).scalar_one()
        record.expires_at = utcnow_naive() - timedelta(minutes=1)
        db_session.flush()

        with pytest.raises(UnauthorizedError, match="estado"):
            provider.authenticate(db_session, code="codigo", state=challenge.state)

    def test_faltan_code_o_state(self, db_session, entra_config):  # noqa: ARG002
        with pytest.raises(UnauthorizedError, match="incompleta"):
            EntraIdentityProvider().authenticate(db_session, code="", state="")

    def test_un_token_endpoint_que_falla_se_traduce(self, db_session, entra_config):  # noqa: ARG002
        provider = EntraIdentityProvider()
        challenge = provider.start_login(db_session)
        provider._http = httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(400, json={"error": "invalid_grant"}))
        )
        with pytest.raises(UnauthorizedError, match="canje"):
            provider.authenticate(db_session, code="codigo", state=challenge.state)

    def test_una_respuesta_sin_id_token_se_rechaza(self, db_session, entra_config):  # noqa: ARG002
        provider = EntraIdentityProvider()
        challenge = provider.start_login(db_session)
        provider._http = httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"access_token": "x"}))
        )
        with pytest.raises(UnauthorizedError, match="id_token"):
            provider.authenticate(db_session, code="codigo", state=challenge.state)

    def test_un_error_de_red_en_el_canje_se_traduce(self, db_session, entra_config):  # noqa: ARG002
        provider = EntraIdentityProvider()
        challenge = provider.start_login(db_session)

        def sin_red(_request):  # noqa: ANN001, ANN202
            raise httpx.ConnectError("sin conexion")

        provider._http = httpx.Client(transport=httpx.MockTransport(sin_red))
        with pytest.raises(UnauthorizedError, match="contactar"):
            provider.authenticate(db_session, code="codigo", state=challenge.state)


class TestClienteConfidencial:
    def test_el_secreto_se_envia_solo_si_esta_configurado(
        self, db_session, entra_config, rsa_key, monkeypatch
    ):
        from pydantic import SecretStr

        provider = EntraIdentityProvider()
        challenge = provider.start_login(db_session)
        record = db_session.execute(
            select(OidcLoginState).where(OidcLoginState.state == challenge.state)
        ).scalar_one()
        id_token = build_id_token(rsa_key, record.nonce)

        capturado: dict[str, str] = {}

        def token_endpoint(request: httpx.Request) -> httpx.Response:
            capturado["cuerpo"] = request.content.decode()
            return httpx.Response(200, json={"id_token": id_token})

        # Sin secreto configurado: cliente publico con PKCE.
        monkeypatch.setattr(entra_config, "entra_client_secret", SecretStr("replace_me"), raising=False)
        provider = EntraIdentityProvider()
        provider._http = httpx.Client(transport=httpx.MockTransport(token_endpoint))
        monkeypatch.setattr(provider, "_signing_key", lambda _t: rsa_key.public_key())
        provider.authenticate(db_session, code="codigo", state=challenge.state)
        assert "client_secret" not in capturado["cuerpo"]
