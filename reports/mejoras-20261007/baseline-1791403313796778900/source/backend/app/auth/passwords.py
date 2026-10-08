# Creado por Aldo Garcia.
"""Hashing de contrasenas con Argon2id.

Solo se usa para el proveedor local de development/test. En produccion la
autenticacion principal es Microsoft Entra ID y Matrix RH no almacena ninguna
contrasena.

Parametros: se usan los del perfil recomendado por la especificacion de Argon2
(RFC 9106) tal como los expone ``argon2-cffi``, con memoria elevada para que el
hash sea caro de atacar offline aunque la base de datos se filtre.
"""

from __future__ import annotations

from contextlib import suppress

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

#: time_cost/memory_cost elegidos para ~50-100 ms por verificacion en un equipo
#: de escritorio: suficiente para frenar fuerza bruta sin degradar el login.
_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
    type=Type.ID,  # Argon2id
)

#: Hash de una contrasena inexistente. Se verifica contra el cuando el usuario no
#: existe, para que el tiempo de respuesta no revele si la cuenta existe.
_DUMMY_HASH = _HASHER.hash("matrix-rh-dummy-value-for-constant-time")


def hash_password(password: str) -> str:
    """Devuelve el hash PHC de Argon2id."""
    if not password:
        raise ValueError("La contrasena no puede estar vacia.")
    return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Verifica una contrasena. Nunca lanza por credencial incorrecta."""
    try:
        return _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify() -> None:
    """Consume tiempo equivalente a una verificacion real.

    Se invoca cuando el usuario no existe para no filtrar su existencia por un
    canal lateral de temporizacion (gate de la prueba E2E 33).
    """
    with suppress(VerifyMismatchError, VerificationError, InvalidHashError):
        _HASHER.verify(_DUMMY_HASH, "wrong-password")


def needs_rehash(stored_hash: str) -> bool:
    """True si el hash usa parametros antiguos y conviene regenerarlo."""
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def is_argon2id(stored_hash: str) -> bool:
    """Verificacion explicita del algoritmo, usada por las pruebas de seguridad."""
    return stored_hash.startswith("$argon2id$")
