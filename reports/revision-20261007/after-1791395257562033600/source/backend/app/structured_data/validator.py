# Creado por Aldo Garcia.
"""``QueryPolicyValidator``: valida un plan contra permisos y esquema.

Se ejecuta **antes** de compilar SQL. Comprueba, en este orden:

1. que la fuente exista, este habilitada y el rol la tenga concedida;
2. que la entidad este declarada en la configuracion;
3. que **cada** columna referenciada (proyeccion, filtro, agrupacion, orden,
   agregacion) este en la allowlist de la entidad;
4. que el limite no exceda el maximo de la entidad/fuente;
5. que los filtros organizacionales obligatorios se apliquen.

Deny-by-default: cualquier duda termina en rechazo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.authorization.context import PERM_STRUCTURED_QUERY, StructuredSourceGrant, UserContext
from app.authorization.policy import PolicyEngine
from app.common.errors import ForbiddenError, StructuredQueryRejectedError
from app.common.logging import get_logger
from app.config import get_settings
from app.structured_data.schemas import ComparisonOperator, QueryFilter, StructuredQueryPlan
from app.structured_data.scope import parse_source_grant
from app.structured_data.sources import EntityConfig, SourceCatalog, SourceConfig, get_source_catalog

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ValidatedPlan:
    """Plan aprobado, con la configuracion resuelta y los filtros obligatorios."""

    plan: StructuredQueryPlan
    source: SourceConfig
    entity: EntityConfig
    effective_limit: int
    injected_filters: tuple[QueryFilter, ...] = field(default_factory=tuple)
    #: AND dentro de cada concesion; OR entre concesiones de roles distintos.
    authorization_filter_groups: tuple[tuple[QueryFilter, ...], ...] = field(default_factory=tuple)

    def all_filters(self) -> tuple[QueryFilter, ...]:
        return tuple(self.plan.filters) + self.injected_filters


class QueryPolicyValidator:
    """Valida planes de consulta contra politicas y esquema declarado."""

    def __init__(
        self,
        *,
        catalog: SourceCatalog | None = None,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self._catalog_override = catalog
        self._policies = policy_engine

    @property
    def _catalog(self) -> SourceCatalog:
        return self._catalog_override if self._catalog_override is not None else get_source_catalog()

    def user_scope(self, source: SourceConfig, entity: EntityConfig, *, ctx: UserContext) -> tuple[QueryFilter, ...]:
        """Resuelve atributos cerrados y tipados despues de verificar la firma.

        Un atributo ausente/incorrecto niega; ningun valor del plan reemplaza el
        alcance. Las igualdades resultantes se aplican con AND a todos los roles.
        """
        ctx.require_valid(get_settings().app_secret_key.get_secret_value())
        if source.row_scope == "user" and not entity.required_user_filters:
            raise StructuredQueryRejectedError("La entidad no tiene alcance por usuario configurado.")
        if source.row_scope == "role_view" and entity.table not in source.approved_security_views:
            raise StructuredQueryRejectedError("La entidad no declara una vista de seguridad aprobada.")
        attributes = {"user.user_id": ctx.user_id, "user.username": ctx.username, "user.email": ctx.email}
        limits = {"user.user_id": 36, "user.username": 128, "user.email": 256}
        filters: list[QueryFilter] = []
        for column, reference in entity.required_user_filters.items():
            if column not in entity.allowed_columns:
                raise StructuredQueryRejectedError("El filtro de identidad referencia una columna no permitida.")
            value = attributes[reference]
            if not isinstance(value, str) or not value or value != value.strip() or len(value) > limits[reference]:
                raise ForbiddenError(detail="structured_user_attribute_unavailable")
            filters.append(QueryFilter(field=column, operator=ComparisonOperator.EQ, value=value))
        return tuple(filters)

    def _authorize_source(self, source: SourceConfig, *, ctx: UserContext) -> None:
        ctx.require_valid(get_settings().app_secret_key.get_secret_value())
        if not source.enabled or PERM_STRUCTURED_QUERY not in ctx.permissions or source.name not in ctx.allowed_sources:
            raise ForbiddenError()
        if self._policies is not None:
            if not self._policies.can_query_source(ctx, source.name).allowed:
                raise ForbiddenError()
        elif source.allowed_roles and not (set(source.allowed_roles) & set(ctx.roles)):
            raise ForbiddenError()

    def entity_scope(
        self, source: SourceConfig, entity: EntityConfig, *, ctx: UserContext
    ) -> tuple[tuple[QueryFilter, ...], ...]:
        """Mismo control para catalogo visible, plan y ejecucion final."""
        self._authorize_source(source, ctx=ctx)
        self.user_scope(source, entity, ctx=ctx)
        # Los contextos de produccion siempre contienen la tupla explicita de
        # filas de BD; una tupla vacia nunca se convierte en acceso completo.
        grants = (
            tuple(grant for grant in ctx.structured_source_grants if grant.source_name == source.name)
            if ctx.structured_source_grants is not None
            else (StructuredSourceGrant(source_name=source.name),)
        )
        parsed = tuple(parse_source_grant(grant) for grant in grants)
        applicable = tuple(
            grant for grant in parsed
            if grant.allowed_entities is None or entity.name in grant.allowed_entities
        )
        if not applicable:
            raise ForbiddenError()
        allowed_columns = set(entity.allowed_columns)
        if any(condition.field not in allowed_columns for grant in applicable for condition in grant.filters):
            raise StructuredQueryRejectedError(
                "La configuracion de permisos exige un filtro sobre una columna no permitida."
            )
        return tuple(grant.filters for grant in applicable)

    def visible_entities(self, source: SourceConfig, *, ctx: UserContext) -> tuple[EntityConfig, ...]:
        """No revela entidades cuya concesion sea ausente, invalida o restringida."""
        visible: list[EntityConfig] = []
        for entity in source.entities:
            try:
                self.entity_scope(source, entity, ctx=ctx)
            except (ForbiddenError, StructuredQueryRejectedError):
                continue
            visible.append(entity)
        return tuple(visible)

    def validate(self, plan: StructuredQueryPlan, *, ctx: UserContext) -> ValidatedPlan:
        source = self._catalog.get(plan.source)
        if source is None:
            logger.warning("structured.unknown_source", extra={"source_name": plan.source})
            raise StructuredQueryRejectedError("La fuente solicitada no esta disponible.")
        if not source.enabled:
            raise StructuredQueryRejectedError("La fuente solicitada no esta habilitada.")
        self._authorize_source(source, ctx=ctx)

        # --- entidad -------------------------------------------------------
        entity = source.entity(plan.entity)
        if entity is None:
            logger.warning(
                "structured.unknown_entity",
                extra={"source_name": source.name, "entity_name": plan.entity},
            )
            raise StructuredQueryRejectedError("La entidad solicitada no esta disponible.")

        # --- autorizacion de fuente, entidad y filas ------------------------
        authorization_filter_groups = self.entity_scope(source, entity, ctx=ctx)

        allowed_columns = set(entity.allowed_columns)
        if not allowed_columns:
            raise StructuredQueryRejectedError("La entidad no tiene columnas habilitadas.")

        # --- columnas ------------------------------------------------------
        referenced = plan.referenced_columns()
        forbidden = sorted(referenced - allowed_columns)
        if forbidden:
            logger.warning(
                "structured.column_denied",
                extra={"source_name": source.name, "entity_name": entity.name, "denied_count": len(forbidden)},
            )
            raise StructuredQueryRejectedError("La consulta referencia campos no autorizados.")

        if not plan.fields and not plan.aggregations:
            raise StructuredQueryRejectedError("El plan debe proyectar campos o agregaciones.")

        # Una agregacion sin group_by no puede mezclarse con campos sueltos: el
        # resultado seria ambiguo y depende del motor.
        if plan.aggregations and plan.fields and not plan.group_by:
            raise StructuredQueryRejectedError(
                "Una consulta con agregaciones requiere group_by para los campos proyectados."
            )
        if plan.group_by and not set(plan.group_by).issubset(set(plan.fields)):
            raise StructuredQueryRejectedError("group_by debe estar contenido en los campos proyectados.")

        # --- limites -------------------------------------------------------
        effective_limit = min(plan.limit, entity.max_rows, source.max_rows)

        # --- filtros organizacionales obligatorios --------------------------
        injected: list[QueryFilter] = list(self.user_scope(source, entity, ctx=ctx))
        declared_fields = {f.field for f in plan.filters}
        for column, value in entity.required_filters.items():
            if column not in allowed_columns:
                raise StructuredQueryRejectedError(
                    "La configuracion de la entidad exige un filtro sobre una columna no permitida."
                )
            if column in declared_fields:
                # El plan ya filtra por esa columna: se conserva el filtro
                # obligatorio ademas del suyo, nunca en su lugar.
                pass
            injected.append(
                QueryFilter.model_validate({"field": column, "operator": "eq", "value": value})
            )

        return ValidatedPlan(
            plan=plan,
            source=source,
            entity=entity,
            effective_limit=effective_limit,
            injected_filters=tuple(injected),
            authorization_filter_groups=authorization_filter_groups,
        )
