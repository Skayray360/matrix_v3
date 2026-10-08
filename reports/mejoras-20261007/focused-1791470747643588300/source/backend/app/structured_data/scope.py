# Creado por Aldo Garcia.
"""Restricciones SQL por rol: contrato declarativo cerrado, nunca SQL libre.

Una concesion aplica sus filtros con AND. Concesiones de varios roles se unen
con OR, manteniendo juntos los campos de cada concesion. Los filtros del plan
y los required_filters del catalogo siguen siendo obligatorios para todos.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.authorization.context import StructuredSourceGrant
from app.common.errors import StructuredQueryRejectedError
from app.structured_data.schemas import (
    IDENTIFIER_RE,
    MAX_FILTERS,
    ComparisonOperator,
    QueryFilter,
)


@dataclass(frozen=True, slots=True)
class ParsedSourceGrant:
    allowed_entities: frozenset[str] | None
    filters: tuple[QueryFilter, ...]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Clave duplicada en la restriccion")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Constante JSON no admitida: {value}")


def _read_json(value: str) -> Any:
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def parse_source_grant(grant: StructuredSourceGrant) -> ParsedSourceGrant:
    """NULL significa concesion sin esa restriccion; datos invalidos se rechazan.

    allowed_entities: NULL/todos, ["*"]/todos o lista de nombres; []/ninguno.
    row_filter: NULL/sin filtro o {"filters": [{"field": ..., "operator":
    "eq", "value": ...}]}. No admite placeholders ni expresiones SQL.
    """
    try:
        entities = None
        if grant.allowed_entities_json is not None:
            raw_entities = _read_json(grant.allowed_entities_json)
            if not isinstance(raw_entities, list):
                raise ValueError("allowed_entities debe ser una lista")
            if raw_entities != ["*"]:
                if any(not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name) for name in raw_entities):
                    raise ValueError("Entidad no valida")
                entities = frozenset(raw_entities)

        filters: tuple[QueryFilter, ...] = ()
        if grant.row_filter is not None:
            if not isinstance(grant.row_filter, str) or len(grant.row_filter) > 512:
                raise ValueError("row_filter debe ser JSON de hasta 512 caracteres")
            raw = _read_json(grant.row_filter)
            if not isinstance(raw, dict) or set(raw) != {"filters"}:
                raise ValueError("Se exige un objeto filters")
            conditions = raw["filters"]
            if not isinstance(conditions, list) or not 1 <= len(conditions) <= MAX_FILTERS:
                raise ValueError("Se exige al menos un filtro acotado")
            parsed: list[QueryFilter] = []
            for condition in conditions:
                current = QueryFilter.model_validate(condition)
                value = current.value
                if current.operator is ComparisonOperator.IN:
                    if not isinstance(value, list):
                        raise ValueError("IN requiere una lista")
                elif current.operator in (ComparisonOperator.IS_NULL, ComparisonOperator.IS_NOT_NULL):
                    if value is not None:
                        raise ValueError("El operador de nulidad no admite valor")
                elif value is None or isinstance(value, list):
                    raise ValueError("El operador requiere un valor escalar no nulo")
                values = value if isinstance(value, list) else [value]
                if any(isinstance(item, float) and not math.isfinite(item) for item in values):
                    raise ValueError("Numero no finito")
                if any(isinstance(item, str) and len(item) > 512 for item in values):
                    raise ValueError("Valor demasiado largo")
                parsed.append(current)
            filters = tuple(parsed)
        return ParsedSourceGrant(allowed_entities=entities, filters=filters)
    except (ValueError, TypeError, ValidationError) as exc:
        raise StructuredQueryRejectedError(
            "La configuracion de permisos de la fuente no es valida.",
            detail="structured_role_scope_invalid",
        ) from exc
