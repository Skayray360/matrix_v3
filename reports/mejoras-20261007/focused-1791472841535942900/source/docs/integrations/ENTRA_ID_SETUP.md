<!-- Creado por Aldo Garcia. -->

# Activación opcional de Entra ID

El adapter `app/auth/entra_provider.py` está preparado para OIDC Entra.
Seleccionarlo implica dependencia cloud; el perfil local usa OIDC interno.
Pruebas con tokens/HTTP simulados no acreditan tenant real conectado.

1. TI registra una aplicación Web/confidencial con redirect HTTPS del origen
   de Matrix y define grupos/app-roles autorizados.
2. Configurar `.env` privado con `AUTH_PROVIDER=entra`, `ENTRA_TENANT_ID`,
   `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ENTRA_REDIRECT_URI`, logout y grupos
   según el ejemplo de configuración. No poner secreto en YAML ni frontend.
3. Para producción: `APP_ENV=production`, cookie segura, secreto de aplicación
   y flags de usuarios locales/sintéticos desactivados.
4. Revisar `config/authorization/entra-role-mapping.yaml` y materialización SQL.
   La identidad normalizada no recibe permisos por mera autenticación.
5. Validar firma/JWKS, issuer, audience, expiración, nonce/state/PKCE, redirect,
   sesión, CSRF, logout y revocación con el tenant real.
6. Probar positivo/negativo por dominio, Nómina, administración y fuentes SQL.

No hay fallback a local si Entra falla. Conservar una vía operativa de TI para
recuperación sin sustituir el mecanismo de autenticación productivo.

Antes de cambiar cuentas existentes, aplicar el procedimiento de
[cambio de identidad](../AUTHENTICATION_AUTHORIZATION.md#alta-baja-y-cambio-de-identidad):
respaldos, enlaces de identidad, ownership y pruebas de revocación. Para conservar
la identidad dentro de la red local usar [AD/OIDC interno](AD_ACTIVATION_CHECKLIST.md).
