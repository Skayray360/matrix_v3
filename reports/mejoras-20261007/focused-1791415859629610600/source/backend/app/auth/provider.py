# Creado por Aldo Garcia.
"""Puerto ``IdentityProvider`` e identidad normalizada.

La autorizacion de Matrix RH **no depende** de como se autentico el usuario. El
resto del sistema consume unicamente :class:`NormalizedIdentity` y las politicas
internas, de modo que sustituir ``local_test`` por Entra ID no obliga a reescribir
RAG, agentes, frontend ni politicas (seccion 5.5).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.orm import Session

from app.common.errors import ConfigurationError
from app.config import AuthProvider, get_settings


class IntegrationStatus(StrEnum):
    """Estados permitidos para una integracion externa (seccion 37)."""

    CONNECTED_AND_VALIDATED = "CONNECTED_AND_VALIDATED"
    ENDPOINTS_REACHABLE = "ENDPOINTS_REACHABLE"
    PREPARED_NOT_CONNECTED = "PREPARED_NOT_CONNECTED"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class NormalizedIdentity:
    """Identidad comun a todos los proveedores."""

    subject_id: str
    username: str
    display_name: str
    email: str | None = None
    auth_source: str = "local_test"
    groups: tuple[str, ...] = field(default_factory=tuple)
    roles: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "subject_id": self.subject_id,
            "username": self.username,
            "display_name": self.display_name,
            "email": self.email,
            "auth_source": self.auth_source,
            "groups": list(self.groups),
            "roles": list(self.roles),
        }


@dataclass(frozen=True, slots=True)
class LoginChallenge:
    """Respuesta de ``start_login`` para proveedores basados en redireccion."""

    authorization_url: str
    state: str


class IdentityProvider(ABC):
    """Contrato comun: iniciar login, autenticar, cerrar sesion, normalizar."""

    name: str

    @abstractmethod
    def start_login(self, session: Session, *, redirect_after: str | None = None) -> LoginChallenge:
        """Inicia el flujo de autenticacion."""

    @abstractmethod
    def authenticate(self, session: Session, **kwargs: object) -> NormalizedIdentity:
        """Completa la autenticacion y devuelve la identidad normalizada."""

    @abstractmethod
    def logout_url(self) -> str | None:
        """URL de cierre de sesion federado, si el proveedor la ofrece."""

    @abstractmethod
    def status(self) -> IntegrationStatus:
        """Estado operativo real del proveedor."""


def get_identity_provider() -> IdentityProvider:
    """Selecciona el adapter segun ``AUTH_PROVIDER``.

    No existe fallback automatico: si Entra ID falla en produccion, la aplicacion
    devuelve error, **nunca** cae al proveedor local (requisito 5.3).
    """
    from app.auth.entra_provider import EntraIdentityProvider
    from app.auth.local_provider import LocalTestIdentityProvider

    settings = get_settings()
    if settings.auth_provider is AuthProvider.OIDC:
        from app.auth.oidc_provider import OidcIdentityProvider

        return OidcIdentityProvider()
    if settings.auth_provider is AuthProvider.ENTRA:
        return EntraIdentityProvider()
    if settings.auth_provider is AuthProvider.LOCAL_TEST:
        if not settings.is_local_auth_allowed:
            raise ConfigurationError("El proveedor local de pruebas solo puede usarse con APP_ENV=development o test.")
        return LocalTestIdentityProvider()
    raise ConfigurationError(f"AUTH_PROVIDER desconocido: {settings.auth_provider}")
