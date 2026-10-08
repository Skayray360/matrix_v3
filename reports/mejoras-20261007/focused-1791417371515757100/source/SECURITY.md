<!-- Creado por Aldo Garcia. -->

# Seguridad de Matrix RH

Responsable declarado: Aldo Garcia. Revisión: 2026-10-01. Reporte vulnerabilidades
por el canal interno acordado con el responsable; no publique datos personales,
credenciales ni detalles de explotación. Incluya componente, versión, impacto,
reproducción con datos sintéticos y prueba de regresión cuando sea posible.

## Controles y configuración

- `.env` y claves se configuran localmente y no forman parte del ZIP.
- Las cuentas sintéticas pertenecen a desarrollo/pruebas. Producción rechaza
  `AUTH_PROVIDER=local_test` y los flags que las habilitan.
- El backend aplica sesión, CSRF y permisos; los recursos privados verifican
  propietario y el RAG filtra categorías antes de recuperar.
- `LLM_LOCAL_ONLY=true` restringe los adaptadores a hosts autorizados. Un proveedor
  externo requiere configurar explícitamente el perfil.
- Las fuentes empresariales requieren credenciales de solo lectura y alcances
  aprobados. No reutilice una base ajena para Matrix RH.

## Verificación

Desde `backend`, con herramientas de desarrollo instaladas:

```text
python -m scripts.secrets_scan
python -m ruff check app scripts seeds
python -m pytest tests/security
```

Consulte [controles y amenazas](docs/SECURITY.md) y
[validación vigente](reports/VALIDACION_1.3.1.md)
para evidencia y pendientes. Los informes no acreditan cumplimiento formal ni
el despliegue real de TLS, AD/OIDC, GPU o protección de bases en su equipo.

Los valores de fixtures son exclusivamente sintéticos. Cada supresión de
analizador debe incluir justificación concreta; no desactive controles para
conseguir resultados de pruebas aprobados.
