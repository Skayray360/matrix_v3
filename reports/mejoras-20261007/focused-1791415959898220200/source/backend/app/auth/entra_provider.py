# Creado por Aldo Garcia.
"""Adapter de Microsoft Entra ID (OIDC Authorization Code + PKCE, patron BFF).

Estado por defecto en este entorno: ``PREPARED_NOT_CONNECTED``. El codigo es
real y ejecutable -- no pseudocodigo -- pero mientras no existan tenant y client
configurados no se declara conectado (requisitos 37 y 38).

Patron Backend-for-Frontend: el ``id_token`` y el ``access_token`` **nunca** salen
del backend. El navegador recibe unicamente una cookie de sesion opaca,
``HttpOnly``. Esto elimina de raiz la clase de fugas por ``localStorage``.

Validaciones obligatorias implementadas (seccion 5.2): issuer, audience,
expiracion, nonce, state, firma via JWKS, redirect URI exacta y PKCE.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.provider import (
    IdentityProvider,
    IntegrationStatus,
    LoginChallenge,
    NormalizedIdentity,
)
from app.common.errors import ConfigurationError, UnauthorizedError
from app.common.ids import new_opaque_token, utcnow_naive
from app.common.logging import get_logger
from app.config import get_settings
from app.database.models import OidcLoginState

logger = get_logger(__name__)

#: Ventana de vida del ``state``: suficiente para completar un login humano y lo
#: bastante corta para limitar la reutilizacion.
STATE_TTL_MINUTES = 10
REQUESTED_SCOPES = "openid profile email"

#: Valores que significan "sin configurar" en el .env de ejemplo. Se centralizan
#: para no comparar contra literales dispersos por el adapter.
PLACEHOLDER_VALUES = frozenset({"", "replace_me"})


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class EntraIdentityProvider(IdentityProvider):
    """Implementacion OIDC contra el endpoint v2.0 de Microsoft Entra ID."""

    name = "entra"

    def __init__(self, *, http_client: httpx.Client | None = None) -> None:
        self._settings = get_settings()
        self._http = http_client
        self._jwks_client: PyJWKClient | None = None

    # ------------------------------------------------------------- metadatos
    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self._settings.entra_tenant_id}/v2.0"

    @property
    def issuer(self) -> str:
        return self.authority

    @property
    def authorization_endpoint(self) -> str:
        return (
            f"https://login.microsoftonline.com/{self._settings.entra_tenant_id}"
            "/oauth2/v2.0/authorize"
        )

    @property
    def token_endpoint(self) -> str:
        return (
            f"https://login.microsoftonline.com/{self._settings.entra_tenant_id}"
            "/oauth2/v2.0/token"
        )

    @property
    def jwks_uri(self) -> str:
        return f"https://login.microsoftonline.com/{self._settings.entra_tenant_id}/discovery/v2.0/keys"

    @property
    def client_id(self) -> str:
        return self._settings.entra_client_id

    @property
    def redirect_uri(self) -> str:
        return self._settings.entra_redirect_uri

    @property
    def client_secret(self) -> str:
        return self._settings.entra_client_secret.get_secret_value()

    @property
    def allowed_groups(self) -> tuple[str, ...]:
        return self._settings.entra_allowed_group_list

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=20.0)
        return self._http

    def _require_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("ENTRA_TENANT_ID", self._settings.entra_tenant_id),
                ("ENTRA_CLIENT_ID", self.client_id),
                ("ENTRA_REDIRECT_URI", self.redirect_uri),
            )
            if not value or value == "replace_me"
        ]
        if missing:
            raise ConfigurationError("Entra ID no esta configurado. Faltan: " + ", ".join(missing))

    # ------------------------------------------------------------------ flujo
    def start_login(self, session: Session, *, redirect_after: str | None = None) -> LoginChallenge:
        """Genera state, nonce y PKCE, y construye la URL de autorizacion."""
        self._require_configured()

        state = new_opaque_token(24)
        nonce = new_opaque_token(24)
        # PKCE S256: el verifier nunca viaja al navegador.
        code_verifier = _b64url(secrets.token_bytes(48))
        code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())

        now = utcnow_naive()
        session.add(
            OidcLoginState(
                state=state,
                nonce=nonce,
                code_verifier=code_verifier,
                redirect_after=redirect_after,
                created_at=now,
                expires_at=now + timedelta(minutes=STATE_TTL_MINUTES),
            )
        )
        session.flush()

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "response_mode": "query",
            "scope": REQUESTED_SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        url = f"{self.authorization_endpoint}?{httpx.QueryParams(params)}"
        logger.info("auth.entra.login_started")
        return LoginChallenge(authorization_url=url, state=state)

    def authenticate(self, session: Session, **kwargs: object) -> NormalizedIdentity:
        """Canjea el ``code`` y valida el ``id_token``."""
        self._require_configured()
        code = str(kwargs.get("code") or "")
        state = str(kwargs.get("state") or "")
        if not code or not state:
            raise UnauthorizedError("Respuesta de autenticacion incompleta.")

        record = self._consume_state(session, state)
        token_response = self._exchange_code(code, record.code_verifier)
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise UnauthorizedError("El proveedor no devolvio un id_token.")

        claims = self.validate_id_token(id_token, expected_nonce=record.nonce)
        return self.normalize_identity(claims)

    def _consume_state(self, session: Session, state: str) -> OidcLoginState:
        """Valida y marca el state como usado (proteccion CSRF de login y replay)."""
        record = session.execute(select(OidcLoginState).where(OidcLoginState.state == state)).scalar_one_or_none()
        now = utcnow_naive()
        if record is None or record.consumed_at is not None or record.expires_at < now:
            logger.warning("auth.entra.state_rejected")
            raise UnauthorizedError("El estado de autenticacion no es valido.")
        consumed = session.execute(
            update(OidcLoginState)
            .where(
                OidcLoginState.id == record.id, OidcLoginState.consumed_at.is_(None), OidcLoginState.expires_at >= now
            )
            .values(consumed_at=now)
        )
        if consumed.rowcount != 1:
            raise UnauthorizedError("El estado de autenticacion ya fue utilizado.")
        # Consumir incluso si falla el canje posterior; libera SQL antes de HTTP.
        session.commit()
        return record

    def _exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Canje del authorization code en el token endpoint."""
        data = {
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
            "scope": REQUESTED_SCOPES,
        }
        # El secreto se lee de la configuracion en tiempo de ejecucion y solo
        # vive en memoria del backend; nunca esta incrustado en el codigo.
        client_secret = self.client_secret  # noqa: S105
        if client_secret not in PLACEHOLDER_VALUES:
            # Cliente confidencial. El secreto vive solo en memoria del backend.
            data["client_secret"] = client_secret
        try:
            response = self._client().post(self.token_endpoint, data=data)
        except httpx.HTTPError as exc:
            raise UnauthorizedError("No fue posible contactar al proveedor de identidad.", detail=str(exc)) from exc
        if response.status_code != 200:
            # No se propaga el cuerpo: puede contener detalles del tenant.
            logger.warning("auth.entra.token_exchange_failed", extra={"http_status": response.status_code})
            raise UnauthorizedError("El canje del codigo de autorizacion fallo.")
        return response.json()

    def _signing_key(self, id_token: str) -> Any:
        if self._jwks_client is None:
            self._jwks_client = PyJWKClient(self.jwks_uri, cache_keys=True)
        return self._jwks_client.get_signing_key_from_jwt(id_token).key

    def validate_id_token(
        self,
        id_token: str,
        *,
        expected_nonce: str,
        signing_key: Any | None = None,
        algorithms: tuple[str, ...] = ("RS256",),
    ) -> dict[str, Any]:
        """Valida firma, issuer, audience, expiracion y nonce.

        ``signing_key`` se inyecta en las pruebas de contrato con un par de
        claves generado localmente, de modo que la validacion criptografica se
        ejercita de verdad sin tenant real.
        """
        try:
            key = signing_key if signing_key is not None else self._signing_key(id_token)
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=list(algorithms),
                audience=self.client_id,
                issuer=self.issuer,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
                leeway=60,
            )
        except jwt.PyJWTError as exc:
            logger.warning("auth.entra.id_token_invalid", extra={"jwt_error": type(exc).__name__})
            raise UnauthorizedError("El token de identidad no es valido.") from exc

        if claims.get("nonce") != expected_nonce:
            logger.warning("auth.entra.nonce_mismatch")
            raise UnauthorizedError("El token de identidad no es valido.")
        return claims

    def normalize_identity(self, claims: dict[str, Any]) -> NormalizedIdentity:
        """Traduce claims de Entra ID a la identidad comun de Matrix RH."""
        subject = str(claims.get("oid") or claims.get("sub") or "")
        if not subject:
            raise UnauthorizedError("El token de identidad no contiene un subject.")
        username = str(claims.get("preferred_username") or claims.get("upn") or claims.get("email") or subject)
        if claims.get("hasgroups") or "groups" in (claims.get("_claim_names") or {}):
            raise UnauthorizedError("El token excede el limite de grupos; TI debe ajustar los claims.")
        if not isinstance(claims.get("groups", []), list) or not isinstance(claims.get("roles", []), list):
            raise UnauthorizedError("Formato de grupos o roles no valido.")
        groups = tuple(str(g) for g in (claims.get("groups") or []))
        roles = tuple(str(r) for r in (claims.get("roles") or []))

        allowed = self.allowed_groups
        if allowed and not (set(groups) & set(allowed)):
            # El usuario se autentico correctamente pero no pertenece a ningun
            # grupo habilitado para Matrix RH.
            logger.warning("auth.entra.group_not_allowed")
            raise UnauthorizedError("La cuenta no esta habilitada para Matrix RH.")

        return NormalizedIdentity(
            subject_id=subject,
            username=username,
            display_name=str(claims.get("name") or username),
            email=claims.get("email") or claims.get("preferred_username"),
            auth_source=self.name,
            groups=groups,
            roles=roles,
        )

    def logout_url(self) -> str | None:
        if not self._settings.entra_tenant_id or self._settings.entra_tenant_id == "replace_me":
            return None
        post_logout = self._settings.entra_post_logout_redirect_uri
        base = (
            f"https://login.microsoftonline.com/{self._settings.entra_tenant_id}"
            "/oauth2/v2.0/logout"
        )
        return f"{base}?post_logout_redirect_uri={post_logout}" if post_logout else base

    def status(self) -> IntegrationStatus:
        """Conectividad de metadata/JWKS; no equivale a un login empresarial validado."""
        if any(value in PLACEHOLDER_VALUES for value in (self.client_id, self.redirect_uri)):
            return IntegrationStatus.PREPARED_NOT_CONNECTED
        try:
            response = self._client().get(f"{self.authority}/.well-known/openid-configuration")
            response.raise_for_status()
            metadata = response.json()
            expected = {
                "issuer": self.issuer,
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint,
                "jwks_uri": self.jwks_uri,
            }
            if any(metadata.get(key) != value for key, value in expected.items()):
                return IntegrationStatus.ERROR
            keys = self._client().get(self.jwks_uri)
            keys.raise_for_status()
            if not keys.json().get("keys"):
                return IntegrationStatus.ERROR
            return IntegrationStatus.ENDPOINTS_REACHABLE
        except (httpx.HTTPError, ValueError):
            return IntegrationStatus.ERROR
