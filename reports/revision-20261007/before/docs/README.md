<!-- Creado por Aldo Garcia. -->
# Integraciones y activación

| Guía | Uso |
| --- | --- |
| [AD/OIDC interno](AD_ACTIVATION_CHECKLIST.md) | Identidad empresarial dentro de la red local |
| [Conectores SQL](DATABASE_CONNECTORS.md) | Catálogo read-only y alcance de entidades/filas |
| [Entra ID](ENTRA_ID_SETUP.md) | Opción explícita con dependencia cloud, no perfil local |

| Dependencia | Preparación en el código | Pendiente en cada entorno |
| --- | --- | --- |
| MySQL/MariaDB | ORM, migraciones y comprobación de propiedad | Servicio, DSN, base y cuenta aprobada |
| Qdrant | Embedded/server, ACL y generaciones | Ruta única o servidor/API key internos |
| Ollama | Chat, embeddings, inventario y preflight | Runtime, pesos, revisiones y capacidad reales |
| API local compatible | Adapter aislado del negocio | Endpoint, protocolo, modelos y hosts permitidos |
| AD/OIDC interno | Cliente y normalización de identidad | IdP HTTPS, federación AD y grupos/roles |
| Entra | Adapter opcional | Tenant, cliente, secretos, redirect y login real |
| SQL externo | Plan cerrado y adapter read-only | Driver opcional, vistas, DSN y permisos del motor |
| Vertex | Adapter opcional inactivo | Cambia el perfil a cloud; requiere decisión y configuración explícitas |
| HTTPS | Proxy/cabeceras preparados | DNS, certificados y política de TI |

El catálogo SQL de ejemplo mantiene fuentes deshabilitadas. No acredita conexión
a SAP, Oracle, PostgreSQL o SQL Server por incluir sus nombres. Una integración
implementada o un puerto abierto no demuestran credenciales, esquema ni consultas
autorizadas. Registrar pruebas reales positivas y negativas antes de habilitarla.

Para el perfil local mantener inferencia local y, si hay SSO, OIDC interno. Entra
y Vertex implican dependencia cloud; no se activan para resolver errores de la
instalación local. Ver [seguridad](../SECURITY.md),
[infraestructura](../../infrastructure/README.md) y
[validación vigente](../../reports/VALIDACION_1.3.1.md).
