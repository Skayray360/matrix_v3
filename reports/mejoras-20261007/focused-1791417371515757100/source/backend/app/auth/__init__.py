# Creado por Aldo Garcia.
"""Autenticacion: puerto ``IdentityProvider`` y sus adapters (local_test / Entra ID)."""

from app.auth.provider import IdentityProvider, NormalizedIdentity, get_identity_provider

__all__ = ["IdentityProvider", "NormalizedIdentity", "get_identity_provider"]
