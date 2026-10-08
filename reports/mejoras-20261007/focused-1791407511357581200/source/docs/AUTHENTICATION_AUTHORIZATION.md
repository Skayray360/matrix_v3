<!-- Creado por Aldo Garcia. -->

# Identidad, sesiones y autorización

| Perfil | Proveedor | Uso |
| --- | --- | --- |
| Desarrollo/pruebas | `local_test` | Credenciales locales sintéticas con Argon2id |
| Producción local | `oidc` | IdP interno HTTPS federado a AD |
| Producción con cloud explícito | `entra` | Microsoft Entra OIDC |

No existe fallback automático entre proveedores. `APP_ENV=production` rechaza
`local_test`, flags de usuarios sintéticos y cookies inseguras. OIDC interno
requiere issuer, authorization/token/JWKS endpoints, cliente/secret y redirect
HTTPS configurados. Matrix no implementa bind LDAP directo.

La sesión es del servidor: cookie HttpOnly y hash de token en SQL. El frontend
no recibe tokens de identidad para ejecutar permisos. CSRF se exige en
mutaciones; SameSite y HTTPS deben corresponder a la topología real. Mantener
UI y API en el mismo origen. Logout y expiración invalidan la sesión.

`app/authorization/policy.py` construye `UserContext` firmado e inmutable desde
SQL. Incluye roles, permisos, categorías y concesiones estructuradas. Ni el
navegador, el prompt, los documentos ni el LLM pueden ampliarlo.

El tráfico usa estos controles RBAC, categorías, ownership y alcance SQL.
La tabla `authorization_policies` y `PolicyEngine.evaluate_abac()` están
preparados, pero el evaluador no se invoca en las rutas actuales. Las filas
ALLOW/DENY de esa tabla no constituyen una restricción activa de esta entrega.

## Controles de alcance

- Categorías: concesiones nominales o wildcard elegible; categorías desconocidas
  quedan cerradas. Nómina confidencial requiere concesión explícita.
- Conversaciones/adjuntos: propietario y conversación se verifican antes de
  revelar estado o contenido. Las cargas privadas no se publican en el corpus.
- Memoria/historial: se filtran con permisos actuales y fingerprint de producción.
- Datos estructurados: además de `structured.query`, fuente habilitada y rol,
  se exigen concesiones de entidad y filtros de filas del servidor.
- Cola/respuesta: reautorizar sesión y fingerprint al ejecutar y antes de
  publicar; cancelar/expirar impide una respuesta tardía.
- Administración: `knowledge.admin` se intersecta con la categoría efectiva;
  `diagnostics.read` protege diagnóstico/auditoría correspondientes.

## Activación

Las políticas declarativas están en `config/authorization/`. La ingesta no
concede permisos. Revisar el mapeo primero con `scripts.load_entra_mapping
--dry-run` y aplicar sólo después de validar grupos, roles y taxonomía.

Probar al menos una identidad autorizada y otra denegada por dominio, y revocar
un rol durante inferencia para verificar que el resultado no se publique.
Las pruebas con IdP simulado no acreditan SSO corporativo activo.

Ver [AD/OIDC interno](integrations/AD_ACTIVATION_CHECKLIST.md),
[SQL controlado](integrations/DATABASE_CONNECTORS.md) y [seguridad](SECURITY.md).

## Sesión, proxy y revocación

Los perfiles staging y production rechazan identidad/seed local. Development/test
con identidad local sólo escuchan loopback y no pueden anunciar `APP_BASE_URL`
externo. CLI `serve --host` vuelve a validar esa restricción. El seed nuevo usa
secreto privado generado por el instalador; no reinicia credenciales existentes.

`SESSION_IDLE_MINUTES=30` limita inactividad de solicitudes además del vencimiento
absoluto. CSRF usa comparación constante. Uvicorn interpreta proxy headers sólo
para `FORWARDED_ALLOW_IPS` explícitos; `*` y redes /0 son rechazados. Compose
confía en la IP individual de Nginx, en red separada de SQL/Qdrant.

`PolicyEngine` consulta la versión vigente del registro de categorías, conservando
una vista consistente dentro de cada decisión. Recargar invalida permisos y
memoria previos; YAML inválido deniega acceso. El alcance estructurado incorpora
atributos firmados `user_id`, `username`, `email`; correo federado procede del
IdP validado y persistencia del servidor. El navegador no suministra esos valores.
Ver [DATABASE_CONNECTORS.md](integrations/DATABASE_CONNECTORS.md).

## Perfiles corporativos y cuentas de prueba

El rol base `hcm_emp_basico` / grupo `HCM_EMP_BASICO_MX` habilita documentación
general; los grupos especializados agregan concesiones explícitas y no reemplazan
ese rol base. La fuente efectiva es YAML/SQL, no una lista recibida del navegador.

| Perfil | Alcance |
| --- | --- |
| Empleado base | Documentación general concedida |
| Coordinador de dominio | Documentación especializada de su dominio |
| Administrador de dominio | Categorías y permisos funcionales expresamente asignados |
| Administrador IA Matrix | Categorías elegibles y fuentes declaradas; no acceso universal a datos privados |
| `Matrix` / `matrix_admin_test` | Cuenta de desarrollo y prueba, no perfil productivo |
| `MatrixR1` / `prestaciones_reader_test` | Prueba de lectura de prestaciones, sin administración |

Nómina General y Confidencial son dominios distintos. Las categorías con
`wildcard_eligible: false` necesitan concesión nominal. Ningún wildcard documental
abre una fuente SQL: requiere permiso funcional y concesión sobre fuente,
entidades y filas. Ver [RAG y publicación](RAG_DESIGN.md).

Los usuarios iniciales nuevos usan el secreto generado localmente; no una contraseña
pública del proyecto. El seed es idempotente y no restablece contraseñas existentes.
No ejecutar seed local para resolver un problema de identidad empresarial.

## Alta, baja y cambio de identidad

Para desarrollo, las cuentas de prueba se preparan con el bootstrap y `.env`
privado. Para personas reales, TI gestiona altas/bajas en el IdP y asigna grupos
aprobados. Matrix normaliza esa identidad y materializa roles; autenticar no
autoriza automáticamente todos los dominios. Primero revisar el mapeo con dry-run.
`--prune` elimina mapeos no declarados y sólo debe aplicarse con aprobación.

Al revocar grupos, comprobar sesión, historial, adjuntos y una consulta nueva.
Probar también revocación durante inferencia: una operación aceptada no debe
publicarse si el alcance ya cambió. No reasignar propietario ni hacer visibles
históricos sin huella válida para facilitar un cambio de usuario.

Al pasar de `local_test` a SSO: respaldar SQL/configuración; preparar cliente y
redirects; revisar `identity_links`; validar ownership con datos de aceptación;
desactivar seed/local; probar expiración, caída del IdP y revocación antes del
cambio empresarial. No enlazar identidades sólo por su nombre visible ni copiar
contraseñas/tokens. Un rollback restaura estado compatible, sin ampliar ACL.
