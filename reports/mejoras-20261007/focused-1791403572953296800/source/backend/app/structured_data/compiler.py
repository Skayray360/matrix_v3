# Creado por Aldo Garcia.
"""Compilador de planes a SQL parametrizado + verificacion por AST.

Dos garantias independientes:

1. **Construccion segura.** Todo identificador (tabla, columna) proviene de la
   allowlist de la configuracion, nunca del texto del usuario ni del modelo.
   Todo valor viaja como parametro nombrado. No hay concatenacion de valores.
2. **Verificacion posterior.** El SQL generado se vuelve a parsear con
   ``sqlglot`` y se comprueba que sea una unica sentencia ``SELECT``, sin DDL/DML,
   sin multiples statements y que solo toque la tabla autorizada. Es defensa en
   profundidad: si un dia alguien introduce una concatenacion, esta verificacion
   lo detecta.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from app.common.errors import StructuredQueryRejectedError
from app.common.logging import get_logger
from app.structured_data.schemas import ComparisonOperator, QueryFilter
from app.structured_data.validator import ValidatedPlan

logger = get_logger(__name__)

#: Motor de la fuente -> dialecto de sqlglot.
_DIALECTS = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgresql": "postgres",
    "sqlserver": "tsql",
    "oracle": "oracle",
    "sqlite": "sqlite",
}

#: Nodos que jamas pueden aparecer en una consulta compilada.
_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Command,
    exp.Merge,
    exp.TruncateTable,
)

_OPERATOR_SQL = {
    ComparisonOperator.EQ: "=",
    ComparisonOperator.NEQ: "<>",
    ComparisonOperator.GT: ">",
    ComparisonOperator.GTE: ">=",
    ComparisonOperator.LT: "<",
    ComparisonOperator.LTE: "<=",
    ComparisonOperator.LIKE: "LIKE",
}


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """SQL listo para ejecutar y sus parametros.

    ``limit`` es el maximo de filas que se devuelven al usuario. El SQL lleva
    ``limit + 1`` (``sql_limit``) para que el adapter pueda distinguir "hay
    exactamente N filas" de "hay mas de N y el resultado quedo truncado". Sin esa
    fila extra, el indicador ``truncated`` nunca se activaria y el usuario podria
    creer que esta viendo el conjunto completo.
    """

    sql: str
    parameters: dict[str, Any]
    dialect: str
    limit: int
    sql_limit: int


def _quote(identifier: str, dialect: str) -> str:
    """Cita un identificador ya validado contra la allowlist.

    Aunque el identificador provenga de la configuracion (no del usuario), se
    cita igualmente para no depender de que no colisione con palabras reservadas.
    """
    if dialect == "mysql":
        return f"`{identifier}`"
    if dialect == "tsql":
        return f"[{identifier}]"
    return f'"{identifier}"'


def compile_plan(validated: ValidatedPlan) -> CompiledQuery:
    """Genera SQL parametrizado a partir de un plan ya validado."""
    plan = validated.plan
    dialect = _DIALECTS.get(validated.source.engine, "mysql")
    table = _quote(validated.entity.table, dialect)

    # --- proyeccion --------------------------------------------------------
    projections: list[str] = [_quote(f, dialect) for f in plan.fields]
    for aggregation in plan.aggregations:
        target = "*" if aggregation.field == "*" else _quote(aggregation.field, dialect)
        projections.append(
            f"{aggregation.function.upper()}({target}) AS {_quote(aggregation.alias, dialect)}"
        )
    if not projections:
        raise StructuredQueryRejectedError("El plan no proyecta ninguna columna.")

    # --- filtros -----------------------------------------------------------
    where_parts: list[str] = []
    parameters: dict[str, Any] = {}

    def compile_condition(condition: QueryFilter, param: str) -> str:
        column = _quote(condition.field, dialect)
        if condition.operator is ComparisonOperator.IS_NULL:
            return f"{column} IS NULL"
        if condition.operator is ComparisonOperator.IS_NOT_NULL:
            return f"{column} IS NOT NULL"
        if condition.operator is ComparisonOperator.IN:
            values = condition.value if isinstance(condition.value, list) else [condition.value]
            if not values:
                # IN vacio: se fuerza una condicion falsa en lugar de omitir el
                # filtro, que ampliaria el conjunto de resultados.
                return "1 = 0"
            placeholders = []
            for offset, value in enumerate(values):
                key = f"{param}_{offset}"
                parameters[key] = value
                placeholders.append(f":{key}")
            return f"{column} IN ({', '.join(placeholders)})"

        operator = _OPERATOR_SQL.get(condition.operator)
        if operator is None:  # pragma: no cover - enum cerrado
            raise StructuredQueryRejectedError("Operador no soportado.")
        parameters[param] = condition.value
        return f"{column} {operator} :{param}"

    for index, condition in enumerate(validated.all_filters()):
        where_parts.append(compile_condition(condition, f"p{index}"))

    # Una concesion sin filtro cubre todas las filas de ESA entidad. De lo
    # contrario cada grupo permanece unido: (rol A: pais AND centro) OR
    # (rol B: pais AND centro), nunca pais IN (...) AND centro IN (...).
    groups = validated.authorization_filter_groups
    if groups and all(groups):
        alternatives = [
            "(" + " AND ".join(
                compile_condition(condition, f"r{group_index}_{condition_index}")
                for condition_index, condition in enumerate(group)
            ) + ")"
            for group_index, group in enumerate(groups)
        ]
        where_parts.append("(" + " OR ".join(alternatives) + ")")

    # --- ensamblado --------------------------------------------------------
    # bandit B608 / ruff S608: el SQL se construye por interpolacion porque los
    # identificadores (tabla y columnas) no pueden viajar como parametros en
    # ningun motor. La mitigacion es de tres capas y esta cubierta por pruebas:
    #   1. cada identificador proviene de config/data_sources/sources.yaml y ya
    #      paso la allowlist del QueryPolicyValidator;
    #   2. cada identificador cumple IDENTIFIER_RE (^[A-Za-z_][A-Za-z0-9_]{0,63}$)
    #      y se cita segun el dialecto;
    #   3. TODOS los valores viajan como parametros nombrados, y el SQL final se
    #      reanaliza con sqlglot para exigir un unico SELECT sobre la tabla
    #      autorizada (verify_sql_is_readonly).
    # Riesgo residual documentado en reports/security/cybersecurity.md.
    sql = f"SELECT {', '.join(projections)} FROM {table}"  # noqa: S608  # nosec B608
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if plan.group_by:
        sql += " GROUP BY " + ", ".join(_quote(f, dialect) for f in plan.group_by)
    if plan.sort:
        sql += " ORDER BY " + ", ".join(
            f"{_quote(s.field, dialect)} {s.direction.upper()}" for s in plan.sort
        )

    limit = validated.effective_limit
    # Se pide una fila de mas para poder detectar truncamiento (ver CompiledQuery).
    sql_limit = limit + 1
    if dialect == "tsql":
        # T-SQL usa TOP; se reescribe la proyeccion.
        sql = sql.replace("SELECT ", f"SELECT TOP {sql_limit} ", 1)
    elif dialect == "oracle":
        sql += f" FETCH FIRST {sql_limit} ROWS ONLY"
    else:
        sql += f" LIMIT {sql_limit}"

    verify_sql_is_readonly(sql, dialect=dialect, allowed_table=validated.entity.table)
    return CompiledQuery(
        sql=sql, parameters=parameters, dialect=dialect, limit=limit, sql_limit=sql_limit
    )


def verify_sql_is_readonly(sql: str, *, dialect: str, allowed_table: str) -> None:
    """Verificacion por AST del SQL final.

    Rechaza: multiples sentencias, cualquier cosa que no sea ``SELECT``, nodos
    DDL/DML, y referencias a tablas fuera de la autorizada. Tambien rechaza
    comentarios SQL, que son el vehiculo clasico para intentar evadir un
    validador basado en texto.
    """
    if "--" in sql or "/*" in sql or "#" in sql:
        raise StructuredQueryRejectedError("La consulta contiene comentarios SQL.")

    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001
        raise StructuredQueryRejectedError(
            "La consulta generada no es analizable.", detail=str(exc)
        ) from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise StructuredQueryRejectedError("Solo se permite una sentencia por consulta.")

    statement = statements[0]
    if not isinstance(statement, exp.Select):
        raise StructuredQueryRejectedError("Solo se permiten sentencias SELECT.")

    for node_type in _FORBIDDEN_NODES:
        if list(statement.find_all(node_type)):
            raise StructuredQueryRejectedError("La consulta contiene operaciones no permitidas.")

    tables = {t.name for t in statement.find_all(exp.Table)}
    if tables - {allowed_table}:
        logger.warning(
            "structured.table_not_allowed",
            extra={"referenced_tables": sorted(tables), "allowed_table": allowed_table},
        )
        raise StructuredQueryRejectedError("La consulta referencia tablas no autorizadas.")

    # Subconsultas y uniones no forman parte del contrato del plan.
    has_union = any(True for _node in statement.find_all(exp.Union))
    has_subquery = any(True for _node in statement.find_all(exp.Subquery))
    if has_union or has_subquery:
        raise StructuredQueryRejectedError("La consulta contiene construcciones no permitidas.")
