# Creado por Aldo Garcia.
"""OIDC de produccion para un proveedor alojado por TI dentro de la red."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.auth.entra_provider import EntraIdentityProvider
from app.auth.provider import IntegrationStatus, NormalizedIdentity
from app.common.ids import sha256_text


class OidcIdentityProvider(EntraIdentityProvider):
    name = "oidc"

    @property
    def authority(self) -> str:
        return self._settings.oidc_issuer.rstrip("/")

    @property
    def issuer(self) -> str:
        return self._settings.oidc_issuer

    @property
    def authorization_endpoint(self) -> str:
        return self._settings.oidc_authorization_endpoint

    @property
    def token_endpoint(self) -> str:
        return self._settings.oidc_token_endpoint

    @property
    def jwks_uri(self) -> str:
        return self._settings.oidc_jwks_uri

    @property
    def client_id(self) -> str:
        return self._settings.oidc_client_id

    @property
    def redirect_uri(self) -> str:
        return self._settings.oidc_redirect_uri

    @property
    def client_secret(self) -> str:
        return self._settings.oidc_client_secret.get_secret_value()

    @property
    def allowed_groups(self) -> tuple[str, ...]:
        return tuple(g.strip() for g in self._settings.oidc_allowed_groups.split(",") if g.strip())

    def _require_configured(self) -> None:
        # Settings valida HTTPS, client y secreto antes de construir el adapter.
        pass

    def normalize_identity(self, claims: dict[str, Any]) -> NormalizedIdentity:
        identity = super().normalize_identity(claims)
        return replace(identity, subject_id=sha256_text(f"{self.issuer}|{claims['sub']}"))

    def logout_url(self) -> str | None:
        # /auth/logout revoca la sesion Matrix, no la sesion del IdP.
        return None

    def status(self) -> IntegrationStatus:
        return super().status()
