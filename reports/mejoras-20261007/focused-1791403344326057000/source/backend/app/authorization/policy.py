# Creado por Aldo Garcia.
"""Motor de politicas RBAC + atributos de contexto.

Este modulo es el ``Authorization Guard`` de la seccion 3.2. Todas las decisiones
son **deny-by-default**: la ausencia de una regla nunca concede acceso.

Punto critico del diseno (requisito 11): las decisiones se toman *antes* de RAG,
*antes* de la consulta SQL y *antes* de construir el prompt. El resto del sistema
recibe unicamente el conjunto de categorias/fuentes ya autorizadas y las usa como
filtro de recuperacion, no como filtro posterior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.categories import CategoryRegistry, get_registry
from app.authorization.context import (
    PERM_KNOWLEDGE_ADMIN,
    PERM_STRUCTURED_QUERY,
    StructuredSourceGrant,
    UserContext,
)
from app.common.logging import get_logger
from app.config import get_settings
from app.database.models import (
    AuthorizationPolicy,
    CategoryPermission,
    Permission,
    Role,
    RolePermission,
    StructuredSourcePermission,
    User,
    UserRole,
)

logger = get_logger(__name__)


class Effect(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class Decision:
    """Resultado de una evaluacion de politica.

    ``reason`` es para auditoria y depuracion interna. **No** se envia al usuario:
    revelar el motivo exacto de una denegacion permite inferir la existencia de
    recursos restringidos (seccion 6.2).
    """

    effect: Effect
    reason: str
    resource: str = ""

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW

    @classmethod
    def allow(cls, resource: str, reason: str = "policy_grant") -> Decision:
        return cls(effect=Effect.ALLOW, reason=reason, resource=resource)

    @classmethod
    def deny(cls, resource: str, reason: str = "no_explicit_grant") -> Decision:
        return cls(effect=Effect.DENY, reason=reason, resource=resource)


class PolicyEngine:
    """Construye contextos de usuario y evalua permisos efectivos."""

    def __init__(self, registry: CategoryRegistry | None = None) -> None:
        # Solo una inyeccion explicita conserva un registro fijo (pruebas).
        # Las instancias de servicio consultan el registro vigente por decision,
        # incluso si otro objeto conserva una referencia a este motor.
        self._registry_override = registry

    def _current_registry(self) -> CategoryRegistry:
        return self._registry_override if self._registry_override is not None else get_registry()

    # ------------------------------------------------------------------------
    # Construccion del contexto
    # ------------------------------------------------------------------------
    def build_context(
        self,
        session: Session,
        *,
        user: User,
        session_id: str,
        request_id: str,
        groups: frozenset[str] | None = None,
    ) -> UserContext:
        """Calcula los permisos efectivos y devuelve un contexto firmado.

        Se lee siempre desde la base de datos en el momento del request: si un
        administrador revoca un rol, el siguiente request ya no lo tiene, sin
        necesidad de invalidar sesiones.
        """
        role_rows = session.execute(
            select(Role).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user.id)
        ).scalars().all()
        role_names = frozenset(r.name for r in role_rows)
        role_ids = [r.id for r in role_rows]

        permissions: set[str] = set()
        allowed_categories: set[str] = set()
        wildcard = False
        allowed_sources: set[str] = set()
        source_grants: list[StructuredSourceGrant] = []

        if role_ids:
            perm_rows = session.execute(
                select(Permission.name)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id.in_(role_ids))
            ).scalars().all()
            permissions.update(perm_rows)

            cat_rows = session.execute(
                select(CategoryPermission).where(CategoryPermission.role_id.in_(role_ids))
            ).scalars().all()
            for row in cat_rows:
                if row.access != "read":
                    continue
                if row.is_wildcard:
                    wildcard = True
                else:
                    allowed_categories.add(row.category)

            src_rows = session.execute(
                select(StructuredSourcePermission).where(
                    StructuredSourcePermission.role_id.in_(role_ids)
                )
            ).scalars().all()
            allowed_sources.update(row.source_name for row in src_rows)
            source_grants.extend(
                StructuredSourceGrant(
                    source_name=row.source_name,
                    allowed_entities_json=(
                        json.dumps(row.allowed_entities, sort_keys=True, separators=(",", ":"))
                        if row.allowed_entities is not None else None
                    ),
                    row_filter=row.row_filter,
                )
                for row in src_rows
            )

        context = UserContext(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            auth_source=user.auth_source,
            session_id=session_id,
            request_id=request_id,
            email=user.email,
            roles=role_names,
            groups=groups or frozenset(),
            permissions=frozenset(permissions),
            allowed_categories=frozenset(allowed_categories),
            category_wildcard=wildcard,
            allowed_sources=frozenset(allowed_sources),
            structured_source_grants=tuple(source_grants),
        )
        secret = get_settings().app_secret_key.get_secret_value()
        return context.sign(secret)

    # ------------------------------------------------------------------------
    # Categorias documentales
    # ------------------------------------------------------------------------
    def effective_categories(self, ctx: UserContext) -> frozenset[str]:
        """Conjunto **explicito** de categorias que el usuario puede leer.

        Incluso con wildcard se devuelve la lista enumerada de categorias
        conocidas y elegibles: la wildcard se resuelve en la capa de politicas y
        el filtro de metadata que llega a Qdrant siempre es una lista concreta
        (requisito 6.1). Nunca se consulta "sin filtro".
        """
        registry = self._current_registry()
        known = registry.known()
        granted = set(ctx.allowed_categories)
        if ctx.category_wildcard:
            for name in known:
                if registry.is_wildcard_eligible(name):
                    granted.add(name)
        # Solo se devuelven categorias que el registro conoce: una concesion a
        # una categoria inexistente no debe abrir un comodin implicito.
        return frozenset(name for name in granted if name in known)

    def can_read_category(self, ctx: UserContext, category: str) -> Decision:
        resource = f"category:{category}"
        registry = self._current_registry()
        if category not in registry.known():
            return Decision.deny(resource, "unknown_category")
        if category in ctx.allowed_categories:
            return Decision.allow(resource, "explicit_category_grant")
        if ctx.category_wildcard and registry.is_wildcard_eligible(category):
            return Decision.allow(resource, "business_admin_wildcard")
        return Decision.deny(resource, "deny_by_default")

    # ------------------------------------------------------------------------
    # Conversaciones y adjuntos
    # ------------------------------------------------------------------------
    def can_access_conversation(self, ctx: UserContext, owner_user_id: str) -> Decision:
        """Ownership estricto: ni siquiera el administrador lee conversaciones ajenas.

        Una conversacion es correspondencia privada del empleado con el sistema;
        el rol administrativo cubre conocimiento y roles, no chats de terceros.
        """
        resource = "conversation"
        if owner_user_id == ctx.user_id:
            return Decision.allow(resource, "owner")
        return Decision.deny(resource, "not_owner")

    # ------------------------------------------------------------------------
    # Fuentes estructuradas
    # ------------------------------------------------------------------------
    def can_query_source(self, ctx: UserContext, source_name: str) -> Decision:
        resource = f"structured_source:{source_name}"
        if PERM_STRUCTURED_QUERY not in ctx.permissions:
            return Decision.deny(resource, "missing_structured_query_permission")
        if source_name in ctx.allowed_sources:
            return Decision.allow(resource, "explicit_source_grant")
        return Decision.deny(resource, "deny_by_default")

    # ------------------------------------------------------------------------
    # Administracion
    # ------------------------------------------------------------------------
    def can_administer_knowledge(self, ctx: UserContext) -> Decision:
        resource = "admin:knowledge"
        if PERM_KNOWLEDGE_ADMIN in ctx.permissions:
            return Decision.allow(resource, "permission_grant")
        return Decision.deny(resource, "missing_permission")

    def can_read_diagnostics(self, ctx: UserContext) -> Decision:
        """Diagnostico administrativo: expone parametros, nunca secretos."""
        from app.authorization.context import PERM_DIAGNOSTICS_READ

        resource = "admin:diagnostics"
        if PERM_DIAGNOSTICS_READ in ctx.permissions:
            return Decision.allow(resource, "permission_grant")
        return Decision.deny(resource, "missing_permission")

    # ------------------------------------------------------------------------
    # Politicas ABAC adicionales
    # ------------------------------------------------------------------------
    def evaluate_abac(
        self, session: Session, ctx: UserContext, *, resource_kind: str, resource_key: str
    ) -> Decision:
        """Evalua ``authorization_policies``.

        Precedencia: un DENY explicito gana siempre sobre cualquier ALLOW, y la
        ausencia de filas devuelve DENY.
        """
        rows = session.execute(
            select(AuthorizationPolicy).where(
                AuthorizationPolicy.resource_kind == resource_kind,
                AuthorizationPolicy.resource_key.in_([resource_key, "*"]),
            )
        ).scalars().all()

        subjects = {("user", ctx.user_id), *(("role", r) for r in ctx.roles), *(("group", g) for g in ctx.groups)}
        applicable = [row for row in rows if (row.subject_kind, row.subject_key) in subjects]

        resource = f"{resource_kind}:{resource_key}"
        if any(row.effect == Effect.DENY for row in applicable):
            return Decision.deny(resource, "explicit_deny_policy")
        if any(row.effect == Effect.ALLOW for row in applicable):
            return Decision.allow(resource, "explicit_allow_policy")
        return Decision.deny(resource, "no_matching_policy")


_engine: PolicyEngine | None = None


def get_policy_engine() -> PolicyEngine:
    global _engine
    if _engine is None:
        _engine = PolicyEngine()
    return _engine


def reset_policy_engine() -> None:
    """Reinicia el motor tras cambiar el registro de categorias."""
    global _engine
    _engine = None
