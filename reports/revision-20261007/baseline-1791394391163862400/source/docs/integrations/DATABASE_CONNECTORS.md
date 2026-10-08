<!-- Creado por Aldo Garcia. -->

# Conectores SQL y alcance efectivo

`config/data_sources/sources.yaml` contiene catálogo, allowlists y `secret_ref`.
El DSN vive en la variable privada indicada, usando una cuenta read-only.
Las fuentes de ejemplo están deshabilitadas; el adapter no acredita conexión.

| Fuente de ejemplo | Motor | Referencia de secreto |
| --- | --- | --- |
| `rh_demo` | MySQL | `MATRIX_DEMO_DB_DSN` |
| `sap_hcm` | SQL Server | `MATRIX_SAP_HCM_DSN` |
| `oracle_hcm` | Oracle | `MATRIX_ORACLE_HCM_DSN` |
| `postgres_analytics` | PostgreSQL | `MATRIX_PG_ANALYTICS_DSN` |

Los drivers externos son extras opcionales del backend. Una consulta requiere
fuente habilitada, rol permitido, `structured.query` y concesión SQL del servidor.
`StructuredSourcePermission` puede acotar `allowed_entities_json` y `row_filter`.

## Contrato de concesión

- `allowed_entities_json`: NULL o `["*"]` conserva todas las entidades declaradas;
  una lista acota nombres y `[]` no concede ninguna.
- `row_filter`: NULL no añade esa restricción; en caso contrario requiere JSON
  cerrado con `filters`. No acepta expresiones SQL ni placeholders de usuario.
- El AND agrupa filtros de una concesión; los grupos de roles aplicables se unen
  por OR. Los filtros de catálogo y de la pregunta siguen restringiendo el resultado.
- JSON inválido, campos fuera de allowlist, operadores/valores inválidos o una
  concesión ausente provocan rechazo, no acceso completo.

Ejemplo de filtro del servidor:

```json
{"filters":[{"field":"centro_trabajo","operator":"eq","value":"MX01"}]}
```

`QueryPlanner` produce un plan JSON. `QueryPolicyValidator` verifica entidad,
columnas de proyección/filtros/agrupación/orden, límites y scope. El compilador
parametriza y verifica el AST; el adapter ejecuta con timeout/read-only. El modelo
no produce SQL ejecutable ni controla credenciales.

## Activación

1. TI prepara vista y cuenta de mínimo privilegio en el motor real.
2. Instalar driver opcional correspondiente con el mismo lock.
3. Declarar entidad/columnas/límites y configurar DSN privado; revisar concesiones.
4. Habilitar fuente, comprobar health y probar consultas positivas y negativas.
5. Probar scope por filas/entidades, filtros inválidos, timeout y ausencia de
   permisos. Registrar motor, esquema y resultados sin revelar DSN o datos reales.

La aplicación usa un adapter SQL sobre vistas declaradas; no incorpora APIs
nativas de SAP/Oracle por el nombre del ejemplo. La cuenta read-only real y las
vistas corporativas siguen siendo controles de TI.

Ver [../AUTHENTICATION_AUTHORIZATION.md](../AUTHENTICATION_AUTHORIZATION.md) y
[modelo de datos](../ARCHITECTURE.md#persistencia-y-migraciones).

## Alcance obligatorio del usuario

`row_scope: user` es el default. Cada entidad necesita `required_user_filters`
que referencia exclusivamente `user.user_id`, `user.username` o `user.email`
del contexto firmado. Ejemplo de configuración del catálogo:

```yaml
row_scope: user
# Dentro de una entidad cuya columna empleado_id esté permitida:
required_user_filters:
  empleado_id: user.username
```

La igualdad se parametriza y se agrega por AND al scope de roles, filtros
estáticos y pregunta. Un atributo vacío, referencia no soportada, falta de
mapeo o conflicto rechaza el plan. Este ejemplo sólo es válido si la vista
real usa ese mismo identificador; no asumir equivalencia con número de empleado.
Atributos como centro/área requieren un contrato de identidad adicional validado.

La alternativa `row_scope: role_view` exige que la tabla esté declarada en
`approved_security_views` de esa fuente. TI debe acreditar aislamiento de la
vista y cuenta read-only; una aprobación de nombre no demuestra su semántica.

Las cuatro fuentes incluidas siguen `enabled: false`. Plantillas corporativas
sin mapping permanecen cerradas aunque se cambie ese flag. Antes de habilitarlas,
validar identidad, columnas y población con dos usuarios de alcance distinto.
Una recarga del catálogo invalida adapters/huellas previas y una consulta en
curso no acepta un contrato que cambió entre validación y ejecución.
