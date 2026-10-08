<!-- Creado por Aldo Garcia. -->

# Activación TI de AD mediante OIDC interno

Este checklist configura la integración existente. No afirma que el directorio
corporativo esté conectado ni requiere cambiar el frontend del ZIP.

`app/auth/provider.py` define `IdentityProvider` y `NormalizedIdentity`.
`oidc_provider.py` implementa OIDC interno; la federación con AD pertenece al IdP.
Matrix no hace bind LDAP/LDAPS directo. El flujo usa state, nonce, PKCE y
validación de tokens; después conserva una sesión propia en SQL con hash del token.
Cambiar proveedor no concede categorías ni fuentes SQL automáticamente.

1. Definir IdP interno federado a AD y su soporte de OIDC/claims. Matrix no hace
   LDAP directo. Preparar DNS, TLS y endpoints accesibles para backend y navegador.
2. Registrar cliente confidencial y redirect HTTPS del origen final de Matrix.
3. Configurar `.env` privado:

   ```dotenv
   APP_ENV=production
   AUTH_PROVIDER=oidc
   LOCAL_TEST_AUTH_ENABLED=false
   LOCAL_TEST_SEED_USERS_ENABLED=false
   SESSION_COOKIE_SECURE=true
   OIDC_ISSUER=https://idp.interno.example/realm
   OIDC_AUTHORIZATION_ENDPOINT=https://idp.interno.example/authorize
   OIDC_TOKEN_ENDPOINT=https://idp.interno.example/token
   OIDC_JWKS_URI=https://idp.interno.example/jwks
   OIDC_CLIENT_ID=cliente_configurado_por_ti
   OIDC_CLIENT_SECRET=secreto_configurado_por_ti
   OIDC_REDIRECT_URI=https://matrix.interno.example/api/v1/auth/callback
   LLM_LOCAL_ONLY=true
   ```

   Los endpoints anteriores son ejemplos; usar los exactos del IdP. Configurar
   también `APP_SECRET_KEY`, URL base y SQL con secretos propios.
4. Revisar grupos/roles de `config/authorization/entra-role-mapping.yaml` y la
   taxonomía. Materializar primero con dry-run y después aplicar conscientemente.
5. Probar usuario base, coordinador, administrador limitado y Nómina restringida.
   El catálogo/ingesta no concede permisos por sí mismo.
6. Probar login/logout, expiración, usuario/grupo revocados, callback repetido,
   state inválido, caída del IdP y reautorización durante inferencia.
7. Registrar IdP/config no secreta, resultados y aprobaciones de TI. Repetir ACL
   documental y scope SQL con el directorio real.

No habilitar `local_test` para resolver una caída en producción. La red interna,
certificados y controles del IdP son requisitos de la topología local.

Para pasar desde cuentas de prueba a SSO, seguir también
[Identidad y permisos](../AUTHENTICATION_AUTHORIZATION.md#alta-baja-y-cambio-de-identidad).
Ver [Seguridad](../SECURITY.md) y [Entra opcional](ENTRA_ID_SETUP.md).
