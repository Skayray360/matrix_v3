# Creado por Aldo Garcia.
"""Proveedor de identidad local para development/test.

Existe para que el modelo de identidad y autorizacion se pueda validar de punta a
punta *antes* de disponer de un tenant real de Microsoft Entra ID. Usa
exactamente el mismo contrato ``IdentityProvider`` y produce la misma
``NormalizedIdentity``, de modo que las politicas probadas con `Matrix` y
`MatrixR1` se reutilizan tal cual al activar Entra.

Controles aplicados:

* solo se instancia con ``APP_ENV`` development o test (validado en configuracion
  y de nuevo aqui, defensa en profundidad);
* la contrasena se verifica con Argon2id contra la base interna;
* bloqueo temporal por intentos fallidos y verificacion dummy para no revelar si
  el usuario existe;
* el resultado del login (exito o fallo) se audita sin registrar la contrasena.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.passwords import dummy_verify, verify_password
from app.auth.provider import (
    IdentityProvider,
    IntegrationStatus,
    LoginChallenge,
    NormalizedIdentity,
)
from app.common.errors import ConfigurationError, RateLimitedError, UnauthorizedError
from app.common.ids import utcnow_naive
from app.common.logging import get_logger
from app.config import get_settings
from app.database.models import LocalCredential, User

logger = get_logger(__name__)

#: Tras este numero de intentos fallidos consecutivos la cuenta queda bloqueada.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 5


class LocalTestIdentityProvider(IdentityProvider):
    """Autentica contra ``users`` + ``local_credentials``."""

    name = "local_test"

    def __init__(self) -> None:
        settings = get_settings()
        # Defensa en profundidad: aunque la configuracion ya lo valida, este
        # adapter se niega a existir fuera de development/test.
        if not settings.is_local_auth_allowed:
            raise ConfigurationError(
                "LocalTestIdentityProvider no puede instanciarse en este entorno."
            )

    def start_login(self, session: Session, *, redirect_after: str | None = None) -> LoginChallenge:
        """El proveedor local no redirige: la UI muestra un formulario propio."""
        return LoginChallenge(authorization_url="/login", state="")

    def authenticate(self, session: Session, **kwargs: object) -> NormalizedIdentity:
        """Verifica usuario y contrasena.

        El mensaje de error es identico para "usuario inexistente" y "contrasena
        incorrecta" (gate E2E 33): la respuesta no debe permitir enumerar cuentas.
        """
        username = str(kwargs.get("username") or "").strip()
        password = str(kwargs.get("password") or "")

        if not username or not password:
            raise UnauthorizedError("Usuario o contrasena incorrectos.")

        user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if user is None or not user.is_active:
            # Se consume tiempo equivalente para no filtrar la existencia.
            dummy_verify()
            logger.warning("auth.local.login_failed", extra={"auth_reason": "unknown_user"})
            raise UnauthorizedError("Usuario o contrasena incorrectos.")

        credential = session.get(LocalCredential, user.id)
        if credential is None:
            dummy_verify()
            logger.warning("auth.local.login_failed", extra={"auth_reason": "no_local_credential"})
            raise UnauthorizedError("Usuario o contrasena incorrectos.")

        now = utcnow_naive()
        if credential.locked_until is not None and credential.locked_until > now:
            logger.warning(
                "auth.local.login_locked",
                extra={"auth_reason": "locked", "user_opaque_id": user.id},
            )
            raise RateLimitedError("Cuenta bloqueada temporalmente por intentos fallidos.")

        if not verify_password(credential.password_hash_argon2id, password):
            credential.failed_attempts += 1
            if credential.failed_attempts >= MAX_FAILED_ATTEMPTS:
                credential.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                credential.failed_attempts = 0
            session.flush()
            logger.warning(
                "auth.local.login_failed",
                extra={"auth_reason": "bad_password", "user_opaque_id": user.id},
            )
            raise UnauthorizedError("Usuario o contrasena incorrectos.")

        credential.failed_attempts = 0
        credential.locked_until = None
        session.flush()

        logger.info("auth.local.login_ok", extra={"user_opaque_id": user.id})
        return NormalizedIdentity(
            subject_id=user.id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            auth_source=self.name,
            groups=(),
            roles=(),
        )

    def logout_url(self) -> str | None:
        return None

    def status(self) -> IntegrationStatus:
        settings = get_settings()
        return (
            IntegrationStatus.CONNECTED_AND_VALIDATED
            if settings.is_local_auth_allowed
            else IntegrationStatus.DISABLED
        )
