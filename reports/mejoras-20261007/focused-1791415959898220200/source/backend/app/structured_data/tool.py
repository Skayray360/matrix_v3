# Creado por Aldo Garcia.
"""``StructuredDataTool``: la unica puerta de entrada a datos estructurados.

Flujo completo (seccion 12):

``plan JSON -> Pydantic -> QueryPolicyValidator -> compilador parametrizado ->
verificacion AST -> timeout + row limit -> credencial read-only -> evidencia``

El resultado se devuelve como **evidencia estructurada**, con la fuente, la
entidad y las columnas consultadas, para que la respuesta sea trazable a una
consulta validada y no a una invencion del modelo.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from app.authorization.context import UserContext
from app.authorization.policy import PolicyEngine
from app.common.errors import StructuredQueryRejectedError
from app.common.logging import get_logger
from app.structured_data.adapters import QueryOutcome, ReadOnlySourceAdapter
from app.structured_data.compiler import compile_plan
from app.structured_data.schemas import StructuredQueryPlan
from app.structured_data.sources import SourceCatalog, get_source_catalog
from app.structured_data.validator import QueryPolicyValidator

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StructuredEvidence:
    """Evidencia estructurada entregada al agente de conocimiento."""

    source_id: str
    source: str
    entity: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    truncated: bool

    def as_markdown_table(self, max_rows: int = 25) -> str:
        """Representacion compacta para el prompt.

        Se limita el numero de filas: volcar cientos de filas en el prompt
        degrada la calidad de la sintesis y consume contexto sin aportar.
        """
        if not self.columns:
            return "(sin resultados)"
        header = "| " + " | ".join(self.columns) + " |"
        separator = "| " + " | ".join("---" for _ in self.columns) + " |"
        body = [
            "| " + " | ".join("" if v is None else str(v) for v in row) + " |"
            for row in self.rows[:max_rows]
        ]
        table = "\n".join([header, separator, *body])
        if len(self.rows) > max_rows:
            table += f"\n(se muestran {max_rows} de {self.row_count} filas)"
        return table

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source": self.source,
            "entity": self.entity,
            "columns": list(self.columns),
            "rows": [list(r) for r in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
        }


class StructuredDataTool:
    """Tool de datos estructurados con validacion de politicas incorporada."""

    def __init__(
        self,
        *,
        catalog: SourceCatalog | None = None,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self._catalog_override = catalog
        self._validator = QueryPolicyValidator(catalog=catalog, policy_engine=policy_engine)
        self._adapters: dict[str, ReadOnlySourceAdapter] = {}
        self._adapter_contracts: dict[str, str] = {}

    @property
    def _catalog(self) -> SourceCatalog:
        return self._catalog_override if self._catalog_override is not None else get_source_catalog()

    def _adapter(self, source_name: str) -> ReadOnlySourceAdapter:
        source = self._catalog.get(source_name)
        if source is None:
            raise StructuredQueryRejectedError("La fuente solicitada no esta disponible.")
        contract = sha256((source.model_dump_json() + (source.dsn() or "")).encode()).hexdigest()
        if source_name not in self._adapters or self._adapter_contracts.get(source_name) != contract:
            if source_name in self._adapters:
                self._adapters[source_name].dispose()
            self._adapters[source_name] = ReadOnlySourceAdapter(source)
            self._adapter_contracts[source_name] = contract
        return self._adapters[source_name]

    def available_entities(self, ctx: UserContext) -> list[dict[str, Any]]:
        """Catalogo visible para el usuario: solo fuentes que tiene concedidas.

        Se filtra por permisos tambien aqui para que ni el listado revele la
        existencia de fuentes que el rol no puede consultar.
        """
        visible: list[dict[str, Any]] = []
        for name in self._catalog.enabled_names():
            if name not in ctx.allowed_sources:
                continue
            source = self._catalog.get(name)
            if source is None:  # pragma: no cover
                continue
            entities = self._validator.visible_entities(source, ctx=ctx)
            if not entities:
                continue
            visible.append(
                {
                    "source": source.name,
                    "description": source.description,
                    "entities": [
                        {
                            "name": entity.name,
                            "description": entity.description,
                            "columns": list(entity.allowed_columns),
                        }
                        for entity in entities
                    ],
                }
            )
        return visible

    def run(self, plan: StructuredQueryPlan, *, ctx: UserContext) -> StructuredEvidence:
        """Valida, compila y ejecuta el plan. Devuelve evidencia estructurada."""
        validated = self._validator.validate(plan, ctx=ctx)
        compiled = compile_plan(validated)
        # Una recarga entre autorizacion y compilacion no ejecuta el alcance
        # anterior contra un adapter de la fuente nueva.
        current = self._catalog.get(validated.source.name)
        if current is None or current.model_dump() != validated.source.model_dump():
            raise StructuredQueryRejectedError("La politica de la fuente cambio; repita la consulta.")

        logger.info(
            "structured.query_authorized",
            extra={
                "user_opaque_id": ctx.user_id,
                "source_name": validated.source.name,
                "entity_name": validated.entity.name,
                "authorization_decision": "ALLOW",
                "row_limit": compiled.limit,
            },
        )

        outcome: QueryOutcome = self._adapter(validated.source.name).execute(
            compiled, entity_name=validated.entity.name
        )
        return StructuredEvidence(
            source_id=f"db:{outcome.source}/{outcome.entity}",
            source=outcome.source,
            entity=outcome.entity,
            columns=outcome.columns,
            rows=outcome.rows,
            row_count=outcome.row_count,
            truncated=outcome.truncated,
        )

    def health_report(self) -> list[dict[str, str]]:
        """Estado real de cada fuente para ``/ready`` y diagnostico."""
        report: list[dict[str, str]] = []
        for name in self._catalog.names():
            adapter = self._adapter(name)
            problems = adapter.validate_configuration()
            report.append(
                {
                    "source": name,
                    "status": str(adapter.status()),
                    "configuration_problems": "; ".join(problems) if problems else "",
                }
            )
        return report
