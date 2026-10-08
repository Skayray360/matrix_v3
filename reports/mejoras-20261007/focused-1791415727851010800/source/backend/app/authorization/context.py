# Creado por Aldo Garcia.
"""``UserContext``: la unica fuente de verdad sobre quien pregunta y que puede ver.

Reglas de la seccion 3.2 que este modulo materializa:

* El contexto se construye **una sola vez por request**, a partir de la sesion de
  servidor y de las politicas de la base de datos.
* Es inmutable (``frozen=True``): ninguna capa posterior -- orquestador, agente,
  tool RAG, tool SQL -- puede ampliarlo.
* Va firmado con HMAC-SHA256 usando ``APP_SECRET_KEY``. Cualquier componente que
  reciba un contexto puede exigir ``verify()``; un contexto fabricado o mutado
  por deserializacion no pasa la verificacion.
* **Nunca** se construye a partir de ``user_id``, ``role`` o ``groups`` enviados
  por el navegador. El unico insumo del cliente es la cookie de sesion opaca.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field, replace
from hashlib import sha256

from app.common.errors import ForbiddenError

#: Rol logico con acceso administrativo de negocio (wildcard de categorias).
ROLE_BUSINESS_ADMIN = "matrix_admin_test"
#: Rol logico restringido a prestaciones.
ROLE_PRESTACIONES_READER = "prestaciones_reader_test"
#: Perfil base obligatorio de la matriz corporativa HCM.
ROLE_HCM_BASE = "hcm_emp_basico"

#: Permisos internos con nombre estable.
PERM_KNOWLEDGE_ADMIN = "knowledge.admin"
PERM_USERS_ADMIN = "users.admin"
PERM_STRUCTURED_QUERY = "structured.query"
PERM_DIAGNOSTICS_READ = "diagnostics.read"


@dataclass(frozen=True, slots=True)
class StructuredSourceGrant:
    """Copia inmutable de una concesion SQL leida exclusivamente del servidor.

    El JSON se conserva como texto para que listas/dicts mutables no puedan
    ampliar el alcance despues de firmar el contexto. El tool valida su contrato
    antes de mostrar entidades, generar planes o ejecutar consultas.
    """

    source_name: str
    allowed_entities_json: str | None = None
    row_filter: str | None = None


@dataclass(frozen=True, slots=True)
class UserContext:
    """Identidad y permisos efectivos, inmutables y firmados."""

    user_id: str
    username: str
    display_name: str
    auth_source: str
    session_id: str
    request_id: str
    #: Atributo persistido por el servidor desde la identidad autenticada.
    #: Nunca se obtiene de un plan SQL, formulario o payload de chat.
    email: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)
    groups: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)
    #: Categorias documentales explicitamente concedidas.
    allowed_categories: frozenset[str] = field(default_factory=frozenset)
    #: True si algun rol tiene wildcard de categorias de negocio.
    category_wildcard: bool = False
    #: Fuentes estructuradas concedidas.
    allowed_sources: frozenset[str] = field(default_factory=frozenset)
    #: None conserva constructores internos antiguos; build_context siempre
    #: entrega una tupla explicita, incluso vacia. Nunca se toma del navegador.
    structured_source_grants: tuple[StructuredSourceGrant, ...] | None = None
    signature: str = ""

    # ------------------------------------------------------------------ firma
    def _canonical(self) -> str:
        """Representacion canonica y estable de los campos firmados."""
        payload = {
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "auth_source": self.auth_source,
            "session_id": self.session_id,
            "roles": sorted(self.roles),
            "groups": sorted(self.groups),
            "permissions": sorted(self.permissions),
            "allowed_categories": sorted(self.allowed_categories),
            "category_wildcard": self.category_wildcard,
            "allowed_sources": sorted(self.allowed_sources),
            "structured_source_grants": (
                sorted(
                    (
                        [grant.source_name, grant.allowed_entities_json, grant.row_filter]
                        for grant in self.structured_source_grants
                    ),
                    key=lambda grant: json.dumps(grant, sort_keys=True),
                )
                if self.structured_source_grants is not None else None
            ),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def sign(self, secret: str) -> UserContext:
        """Devuelve una copia firmada. El original queda intacto."""
        mac = hmac.new(secret.encode("utf-8"), self._canonical().encode("utf-8"), sha256).hexdigest()
        return replace(self, signature=mac)

    def verify(self, secret: str) -> bool:
        """Comparacion en tiempo constante de la firma."""
        if not self.signature:
            return False
        expected = hmac.new(
            secret.encode("utf-8"), self._canonical().encode("utf-8"), sha256
        ).hexdigest()
        return hmac.compare_digest(expected, self.signature)

    def require_valid(self, secret: str) -> None:
        """Falla cerrado si el contexto no esta firmado correctamente."""
        if not self.verify(secret):
            raise ForbiddenError(detail="UserContext con firma invalida o ausente")

    # ------------------------------------------------------------- consultas
    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def is_knowledge_admin(self) -> bool:
        return PERM_KNOWLEDGE_ADMIN in self.permissions

    @property
    def role_set_hash(self) -> str:
        """Huella estable del conjunto de roles para logs de auditoria."""
        return sha256(",".join(sorted(self.roles)).encode("utf-8")).hexdigest()[:32]

    def public_profile(self) -> dict[str, object]:
        """Datos que ``/me`` puede devolver al frontend.

        Se exponen roles y categorias porque la UI los usa para etiquetas
        informativas, pero el backend jamas confia en lo que el cliente devuelva:
        toda decision se recalcula desde este contexto de servidor.
        """
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "auth_source": self.auth_source,
            "roles": sorted(self.roles),
            "permissions": sorted(self.permissions),
            "allowed_categories": sorted(self.allowed_categories),
            "category_wildcard": self.category_wildcard,
        }
