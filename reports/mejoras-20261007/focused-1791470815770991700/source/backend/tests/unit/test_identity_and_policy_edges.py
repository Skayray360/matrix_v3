# Creado por Aldo Garcia.
"""Casos de borde de identidad, sesiones, politicas ABAC, categorias y extractores.

Incluye la prueba de contrato del adapter de Entra ID: se genera un par RSA
local, se firma un ``id_token`` sintetico y se comprueba que la validacion
criptografica (firma, issuer, audience, expiracion y nonce) funciona de verdad
**sin** necesidad de un tenant real.
"""

from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.entra_provider import PLACEHOLDER_VALUES, EntraIdentityProvider
from app.auth.provider import IntegrationStatus, NormalizedIdentity
from app.authorization.categories import (
    CategoryPolicy,
    build_registry,
    discover_filesystem_categories,
    load_category_policy_file,
)
from app.common.errors import ConfigurationError, UnauthorizedError
from app.config import get_settings

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Adapter de Entra ID: contrato criptografico sin tenant real
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rsa_key():  # noqa: ANN201
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def provider(monkeypatch) -> EntraIdentityProvider:  # noqa: ANN001
    settings = get_settings()
    monkeypatch.setattr(settings, "entra_tenant_id", "tenant-de-prueba", raising=False)
    monkeypatch.setattr(settings, "entra_client_id", "client-de-prueba", raising=False)
    monkeypatch.setattr(
        settings, "entra_redirect_uri", "https://matrixrh/api/v1/auth/callback", raising=False
    )
    return EntraIdentityProvider()


def firmar(rsa_key, provider: EntraIdentityProvider, **overrides):  # noqa: ANN001, ANN201
    ahora = int(time.time())
    claims = {
        "iss": provider.issuer,
        "aud": "client-de-prueba",
        "sub": "sub-123",
        "oid": "oid-456",
        "iat": ahora,
        "exp": ahora + 600,
        "nonce": "nonce-correcto",
        "preferred_username": "empleado@corporativo.mx",
        "name": "Empleado Demo",
        "groups": ["grupo-prestaciones"],
    }
    claims.update(overrides)
    return jwt.encode(claims, rsa_key, algorithm="RS256")


class TestValidacionDeIdToken:
    def test_un_token_correcto_se_acepta(self, rsa_key, provider):
        token = firmar(rsa_key, provider)
        claims = provider.validate_id_token(
            token, expected_nonce="nonce-correcto", signing_key=rsa_key.public_key()
        )
        assert claims["oid"] == "oid-456"

    def test_una_firma_de_otra_clave_se_rechaza(self, rsa_key, provider):
        otra = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = firmar(otra, provider)
        with pytest.raises(UnauthorizedError):
            provider.validate_id_token(
                token, expected_nonce="nonce-correcto", signing_key=rsa_key.public_key()
            )

    def test_un_issuer_distinto_se_rechaza(self, rsa_key, provider):
        token = firmar(rsa_key, provider, iss="https://atacante.example/v2.0")
        with pytest.raises(UnauthorizedError):
            provider.validate_id_token(
                token, expected_nonce="nonce-correcto", signing_key=rsa_key.public_key()
            )

    def test_una_audiencia_distinta_se_rechaza(self, rsa_key, provider):
        token = firmar(rsa_key, provider, aud="otra-aplicacion")
        with pytest.raises(UnauthorizedError):
            provider.validate_id_token(
                token, expected_nonce="nonce-correcto", signing_key=rsa_key.public_key()
            )

    def test_un_token_expirado_se_rechaza(self, rsa_key, provider):
        ahora = int(time.time())
        token = firmar(rsa_key, provider, iat=ahora - 7200, exp=ahora - 3600)
        with pytest.raises(UnauthorizedError):
            provider.validate_id_token(
                token, expected_nonce="nonce-correcto", signing_key=rsa_key.public_key()
            )

    def test_un_nonce_distinto_se_rechaza(self, rsa_key, provider):
        """Protege frente a replay del token en otra sesion de login."""
        token = firmar(rsa_key, provider)
        with pytest.raises(UnauthorizedError):
            provider.validate_id_token(
                token, expected_nonce="otro-nonce", signing_key=rsa_key.public_key()
            )

    def test_un_token_sin_subject_se_rechaza(self, rsa_key, provider):
        token = firmar(rsa_key, provider)
        claims = provider.validate_id_token(
            token, expected_nonce="nonce-correcto", signing_key=rsa_key.public_key()
        )
        claims.pop("oid")
        claims.pop("sub")
        with pytest.raises(UnauthorizedError, match="subject"):
            provider.normalize_identity(claims)


class TestNormalizacionDeIdentidad:
    def test_produce_la_identidad_comun(self, rsa_key, provider):
        claims = provider.validate_id_token(
            firmar(rsa_key, provider),
            expected_nonce="nonce-correcto",
            signing_key=rsa_key.public_key(),
        )
        identity = provider.normalize_identity(claims)
        assert isinstance(identity, NormalizedIdentity)
        assert identity.subject_id == "oid-456"
        assert identity.auth_source == "entra"
        assert identity.groups == ("grupo-prestaciones",)
        assert identity.to_dict()["auth_source"] == "entra"

    def test_un_grupo_no_habilitado_se_rechaza(self, rsa_key, provider, monkeypatch):
        monkeypatch.setattr(get_settings(), "entra_allowed_groups", "grupo-permitido", raising=False)
        claims = provider.validate_id_token(
            firmar(rsa_key, provider),
            expected_nonce="nonce-correcto",
            signing_key=rsa_key.public_key(),
        )
        with pytest.raises(UnauthorizedError, match="habilitada"):
            provider.normalize_identity(claims)


class TestEstadoDelProveedorEntra:
    def test_sin_configurar_queda_preparado_no_conectado(self, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "entra_tenant_id", "replace_me", raising=False)
        monkeypatch.setattr(settings, "entra_client_id", "replace_me", raising=False)
        monkeypatch.setattr(settings, "entra_redirect_uri", "", raising=False)
        assert EntraIdentityProvider().status() is IntegrationStatus.PREPARED_NOT_CONNECTED

    def test_exige_configuracion_para_iniciar_el_login(self, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "entra_tenant_id", "replace_me", raising=False)
        with pytest.raises(ConfigurationError, match="ENTRA_"):
            EntraIdentityProvider()._require_configured()

    def test_la_url_de_logout_requiere_tenant(self, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "entra_tenant_id", "replace_me", raising=False)
        assert EntraIdentityProvider().logout_url() is None

    def test_los_marcadores_estan_centralizados(self):
        assert "replace_me" in PLACEHOLDER_VALUES
        assert "" in PLACEHOLDER_VALUES

    def test_los_endpoints_apuntan_al_tenant(self, provider):
        assert "tenant-de-prueba" in provider.authorization_endpoint
        assert "tenant-de-prueba" in provider.token_endpoint
        assert provider.jwks_uri.endswith("/discovery/v2.0/keys")
        assert provider.logout_url() is not None


# ---------------------------------------------------------------------------
# Seleccion de proveedor
# ---------------------------------------------------------------------------
class TestSeleccionDeProveedor:
    def test_en_test_devuelve_el_proveedor_local(self):
        from app.auth.provider import get_identity_provider

        assert get_identity_provider().name == "local_test"

    def test_el_proveedor_local_no_redirige(self):
        from app.auth.provider import get_identity_provider

        challenge = get_identity_provider().start_login(None)  # type: ignore[arg-type]
        assert challenge.authorization_url == "/login"

    def test_el_proveedor_local_reporta_su_estado(self):
        from app.auth.provider import get_identity_provider

        assert get_identity_provider().status() is IntegrationStatus.CONNECTED_AND_VALIDATED

    def test_el_proveedor_local_no_ofrece_logout_federado(self):
        from app.auth.provider import get_identity_provider

        assert get_identity_provider().logout_url() is None


# ---------------------------------------------------------------------------
# Registro de categorias
# ---------------------------------------------------------------------------
class TestRegistroDeCategorias:
    def test_carga_el_archivo_real(self):
        policy_file = load_category_policy_file()
        nombres = {c.name for c in policy_file.categories}
        assert {"prestaciones", "nomina", "salud_ambiental"} <= nombres

    def test_una_categoria_no_declarada_usa_los_valores_por_defecto(self):
        registry = build_registry(extra_categories=["categoria_descubierta"])
        policy = registry.get("categoria_descubierta")
        assert policy.name == "categoria_descubierta"
        assert "automaticamente" in policy.description

    def test_las_semillas_siempre_estan_registradas(self):
        conocidas = build_registry().known()
        assert {"prestaciones", "nomina", "reclutamiento"} <= conocidas

    def test_una_categoria_no_elegible_no_entra_en_el_wildcard(self):
        assert build_registry().is_wildcard_eligible("investigaciones_internas") is False

    def test_una_categoria_nueva_es_deny_by_default_incluso_para_wildcard(self):
        assert build_registry(extra_categories=["categoria_nueva"]).is_wildcard_eligible(
            "categoria_nueva"
        ) is False

    def test_rechaza_una_sensibilidad_invalida(self):
        with pytest.raises(ValueError, match="sensitivity"):
            CategoryPolicy(name="x", sensitivity="ultrasecreta")

    def test_rechaza_un_nombre_inseguro(self):
        with pytest.raises(ValueError, match="invalido"):
            CategoryPolicy(name="../escape")

    def test_un_archivo_de_politica_invalido_falla(self, tmp_path: Path):
        malo = tmp_path / "categories.yaml"
        malo.write_text("categories:\n  - name: MAYUSCULAS\n", encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_category_policy_file(malo)

    def test_un_archivo_inexistente_devuelve_politica_vacia(self, tmp_path: Path):
        assert load_category_policy_file(tmp_path / "no-existe.yaml").categories == []

    def test_descubre_las_carpetas_del_knowledge_root(self, tmp_path: Path):
        (tmp_path / "prestaciones").mkdir()
        (tmp_path / "general").mkdir()
        (tmp_path / "especializadas" / "nomina_confidencial").mkdir(parents=True)
        (tmp_path / "Area Invalida").mkdir()
        (tmp_path / ".oculta").mkdir()
        (tmp_path / "suelto.md").write_text("x", encoding="utf-8")

        descubiertas = discover_filesystem_categories(tmp_path)
        assert descubiertas == ["general", "nomina_confidencial", "prestaciones"]

    def test_un_root_inexistente_no_rompe(self, tmp_path: Path):
        assert discover_filesystem_categories(tmp_path / "no-existe") == []


# ---------------------------------------------------------------------------
# Politicas ABAC
# ---------------------------------------------------------------------------
class TestPoliticasAbac:
    def test_un_deny_explicito_gana_sobre_un_allow(self, db_session, admin_context):
        from app.authorization.policy import PolicyEngine
        from app.common.ids import new_id, utcnow_naive
        from app.database.models import AuthorizationPolicy

        recurso = f"recurso-{new_id()[:8]}"
        for efecto, sujeto in (("ALLOW", "matrix_admin_test"), ("DENY", admin_context.user_id)):
            db_session.add(
                AuthorizationPolicy(
                    id=new_id(),
                    subject_kind="role" if efecto == "ALLOW" else "user",
                    subject_key=sujeto,
                    resource_kind="prueba",
                    resource_key=recurso,
                    effect=efecto,
                    created_at=utcnow_naive(),
                )
            )
        db_session.flush()

        decision = PolicyEngine().evaluate_abac(
            db_session, admin_context, resource_kind="prueba", resource_key=recurso
        )
        assert decision.allowed is False
        assert decision.reason == "explicit_deny_policy"

    def test_sin_politicas_se_deniega(self, db_session, admin_context):
        from app.authorization.policy import PolicyEngine

        decision = PolicyEngine().evaluate_abac(
            db_session, admin_context, resource_kind="prueba", resource_key="inexistente"
        )
        assert decision.allowed is False
        assert decision.reason == "no_matching_policy"

    def test_un_allow_por_rol_concede(self, db_session, admin_context):
        from app.authorization.policy import PolicyEngine
        from app.common.ids import new_id, utcnow_naive
        from app.database.models import AuthorizationPolicy

        recurso = f"recurso-{new_id()[:8]}"
        db_session.add(
            AuthorizationPolicy(
                id=new_id(),
                subject_kind="role",
                subject_key="matrix_admin_test",
                resource_kind="prueba",
                resource_key=recurso,
                effect="ALLOW",
                created_at=utcnow_naive(),
            )
        )
        db_session.flush()

        decision = PolicyEngine().evaluate_abac(
            db_session, admin_context, resource_kind="prueba", resource_key=recurso
        )
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Extractores: caminos de error y limites
# ---------------------------------------------------------------------------
class TestExtractoresLimites:
    def test_un_txt_demasiado_grande_se_rechaza(self):
        from app.common.errors import ExtractionFailedError
        from app.ingestion.loaders import MAX_TEXT_BYTES, extract_txt

        with pytest.raises(ExtractionFailedError, match="limite"):
            extract_txt(b"x" * (MAX_TEXT_BYTES + 1))

    def test_un_markdown_demasiado_grande_se_rechaza(self):
        from app.common.errors import ExtractionFailedError
        from app.ingestion.loaders import MAX_TEXT_BYTES, extract_markdown

        with pytest.raises(ExtractionFailedError):
            extract_markdown(b"x" * (MAX_TEXT_BYTES + 1))

    def test_el_xlsx_trunca_las_filas_y_avisa(self, monkeypatch):
        openpyxl = pytest.importorskip("openpyxl")
        import io

        from app.ingestion import loaders

        monkeypatch.setattr(loaders, "MAX_XLSX_ROWS_PER_SHEET", 3)

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["columna"])
        for i in range(20):
            sheet.append([f"valor{i}"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        resultado = loaders.extract_xlsx(buffer.getvalue())
        assert any("truncada" in aviso for aviso in resultado.warnings)

    def test_un_csv_sin_encabezado_util_no_rompe(self):
        from app.ingestion.loaders import extract_csv

        resultado = extract_csv(b"\n\n\n")
        assert resultado.is_empty

    def test_el_mime_de_una_extension_desconocida_es_generico(self):
        from app.ingestion.loaders import mime_for_extension

        assert mime_for_extension(".desconocida") == "application/octet-stream"


# ---------------------------------------------------------------------------
# Chunking: caminos de bloque sobredimensionado
# ---------------------------------------------------------------------------
class TestChunkingBloquesGrandes:
    def test_una_frase_gigante_se_parte_por_palabras(self):
        from app.rag.chunking import chunk_text, estimate_tokens

        texto = "palabra " * 2000  # una sola "frase" sin puntuacion
        chunks = chunk_text(texto, chunk_size_tokens=100, overlap_tokens=10)
        assert len(chunks) > 5
        assert all(estimate_tokens(c.text) <= 130 for c in chunks)

    def test_varias_frases_largas_se_agrupan_por_frase(self):
        from app.rag.chunking import chunk_text

        frase = "esta es una frase de longitud media con varias palabras dentro. "
        chunks = chunk_text(frase * 60, chunk_size_tokens=120, overlap_tokens=20)
        assert len(chunks) > 1
        assert all(c.text.strip() for c in chunks)

    def test_una_tabla_gigante_no_pierde_contenido(self):
        from app.rag.chunking import chunk_text

        filas = "\n".join(f"| fila{i} | valor{i} |" for i in range(400))
        chunks = chunk_text(f"## Tabla\n{filas}\n", chunk_size_tokens=200, overlap_tokens=20)
        combinado = " ".join(c.text for c in chunks)
        assert "fila0" in combinado
        assert "fila399" in combinado
