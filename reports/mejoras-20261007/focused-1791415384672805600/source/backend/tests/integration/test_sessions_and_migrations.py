# Creado por Aldo Garcia.
"""Sesiones de servidor, proveedor local y utilidades de migracion."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.auth.local_provider import MAX_FAILED_ATTEMPTS, LocalTestIdentityProvider
from app.auth.sessions import (
    cookie_parameters,
    create_session,
    purge_expired_sessions,
    resolve_session,
    revoke_all_user_sessions,
    revoke_session,
)
from app.common.errors import ConfigurationError, RateLimitedError, UnauthorizedError
from app.common.ids import sha256_text, utcnow_naive
from app.database.migrator import (
    discover_migrations,
    ensure_database_exists,
    split_statements,
)
from app.database.models import LocalCredential, SessionRecord, User
from seeds.identity_seed import SYNTHETIC_TEST_PASSWORD

pytestmark = pytest.mark.integration


@pytest.fixture()
def usuario(db_session):  # noqa: ANN001, ANN201
    return db_session.execute(select(User).where(User.username == "Matrix")).scalar_one()


class TestSesiones:
    def test_crear_y_resolver_una_sesion(self, db_session, usuario):
        issued = create_session(db_session, user=usuario, auth_source="local_test")
        record, resuelto = resolve_session(db_session, issued.session_token)
        assert resuelto.id == usuario.id
        assert record.id == issued.session_id

    def test_la_base_guarda_el_hash_no_el_token(self, db_session, usuario):
        issued = create_session(db_session, user=usuario, auth_source="local_test")
        record = db_session.get(SessionRecord, issued.session_id)
        assert record.session_token_hash == sha256_text(issued.session_token)
        assert issued.session_token not in record.session_token_hash

    def test_cada_login_emite_un_identificador_nuevo(self, db_session, usuario):
        """Sin esto seria posible la fijacion de sesion."""
        primera = create_session(db_session, user=usuario, auth_source="local_test")
        segunda = create_session(db_session, user=usuario, auth_source="local_test")
        assert primera.session_token != segunda.session_token
        assert primera.session_id != segunda.session_id

    def test_un_token_ausente_o_desconocido_se_rechaza(self, db_session):
        with pytest.raises(UnauthorizedError):
            resolve_session(db_session, None)
        with pytest.raises(UnauthorizedError):
            resolve_session(db_session, "token-inventado")

    def test_una_sesion_revocada_se_rechaza(self, db_session, usuario):
        issued = create_session(db_session, user=usuario, auth_source="local_test")
        revoke_session(db_session, issued.session_id)
        with pytest.raises(UnauthorizedError):
            resolve_session(db_session, issued.session_token)

    def test_una_sesion_expirada_se_rechaza(self, db_session, usuario):
        issued = create_session(db_session, user=usuario, auth_source="local_test")
        record = db_session.get(SessionRecord, issued.session_id)
        record.expires_at = utcnow_naive() - timedelta(minutes=1)
        db_session.flush()
        with pytest.raises(UnauthorizedError, match="expiro"):
            resolve_session(db_session, issued.session_token)

    def test_un_usuario_desactivado_no_tiene_sesion_valida(self, db_session, usuario):
        issued = create_session(db_session, user=usuario, auth_source="local_test")
        usuario.is_active = False
        db_session.flush()
        try:
            with pytest.raises(UnauthorizedError):
                resolve_session(db_session, issued.session_token)
        finally:
            usuario.is_active = True
            db_session.flush()

    def test_revocar_todas_las_sesiones_de_un_usuario(self, db_session, usuario):
        for _ in range(3):
            create_session(db_session, user=usuario, auth_source="local_test")
        revocadas = revoke_all_user_sessions(db_session, usuario.id)
        assert revocadas >= 3

    def test_purgar_sesiones_caducadas(self, db_session, usuario):
        issued = create_session(db_session, user=usuario, auth_source="local_test")
        record = db_session.get(SessionRecord, issued.session_id)
        record.expires_at = utcnow_naive() - timedelta(days=1)
        db_session.flush()
        assert purge_expired_sessions(db_session) >= 1

    def test_la_cookie_es_httponly(self):
        params = cookie_parameters()
        assert params["httponly"] is True
        assert params["path"] == "/"
        assert params["samesite"] in ("lax", "strict", "none")


class TestProveedorLocal:
    def test_autentica_con_la_credencial_sintetica(self, db_session):
        identity = LocalTestIdentityProvider().authenticate(
            db_session, username="Matrix", password=SYNTHETIC_TEST_PASSWORD
        )
        assert identity.username == "Matrix"
        assert identity.auth_source == "local_test"

    def test_rechaza_una_contrasena_incorrecta(self, db_session):
        with pytest.raises(UnauthorizedError, match="incorrectos"):
            LocalTestIdentityProvider().authenticate(
                # secrets-scan: allow (valor deliberadamente incorrecto de la prueba)
                db_session, username="Matrix", password="incorrecta"
            )

    def test_rechaza_un_usuario_inexistente_con_el_mismo_mensaje(self, db_session):
        provider = LocalTestIdentityProvider()
        try:
            provider.authenticate(db_session, username="NoExiste", password="x")
        except UnauthorizedError as inexistente:
            mensaje_inexistente = inexistente.message
        try:
            provider.authenticate(db_session, username="Matrix", password="x")
        except UnauthorizedError as incorrecta:
            mensaje_incorrecta = incorrecta.message
        assert mensaje_inexistente == mensaje_incorrecta

    def test_rechaza_credenciales_vacias(self, db_session):
        with pytest.raises(UnauthorizedError):
            LocalTestIdentityProvider().authenticate(db_session, username="", password="")

    def test_bloquea_tras_varios_intentos_fallidos(self, db_session):
        provider = LocalTestIdentityProvider()
        usuario = db_session.execute(select(User).where(User.username == "MatrixR1")).scalar_one()
        credencial = db_session.get(LocalCredential, usuario.id)
        credencial.failed_attempts = 0
        credencial.locked_until = None
        db_session.flush()

        for _ in range(MAX_FAILED_ATTEMPTS):
            with pytest.raises(UnauthorizedError):
                # secrets-scan: allow (valor deliberadamente incorrecto de la prueba)
                provider.authenticate(db_session, username="MatrixR1", password="incorrecta")

        with pytest.raises(RateLimitedError, match="bloqueada"):
            provider.authenticate(
                db_session, username="MatrixR1", password=SYNTHETIC_TEST_PASSWORD
            )

        # Se restaura el estado para no afectar a otras pruebas.
        credencial.failed_attempts = 0
        credencial.locked_until = None
        db_session.flush()

    def test_un_login_correcto_reinicia_el_contador(self, db_session):
        provider = LocalTestIdentityProvider()
        usuario = db_session.execute(select(User).where(User.username == "Matrix")).scalar_one()
        credencial = db_session.get(LocalCredential, usuario.id)
        credencial.failed_attempts = 2
        db_session.flush()

        provider.authenticate(db_session, username="Matrix", password=SYNTHETIC_TEST_PASSWORD)
        assert credencial.failed_attempts == 0
        assert credencial.locked_until is None


class TestMigrator:
    def test_descubre_las_migraciones_en_orden(self):
        versiones = [m.version for m in discover_migrations()]
        assert versiones == sorted(versiones)
        assert "0001" in versiones

    def test_cada_migracion_tiene_checksum_estable(self):
        migraciones = discover_migrations()
        assert all(len(m.checksum) == 64 for m in migraciones)
        assert discover_migrations()[0].checksum == migraciones[0].checksum

    def test_rechaza_un_directorio_inexistente(self, tmp_path: Path):
        with pytest.raises(ConfigurationError, match="No existe"):
            discover_migrations(tmp_path / "no-existe")

    def test_rechaza_un_nombre_de_migracion_invalido(self, tmp_path: Path):
        (tmp_path / "migracion-mal-nombrada.sql").write_text("SELECT 1;", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="Nombre de migracion invalido"):
            discover_migrations(tmp_path)

    def test_divide_las_sentencias_ignorando_comentarios(self):
        sql = """
        -- comentario con punto y coma; dentro
        CREATE TABLE a (id INT);
        CREATE TABLE b (id INT);
        """
        sentencias = split_statements(sql)
        assert len(sentencias) == 2
        assert all(s.startswith("CREATE TABLE") for s in sentencias)

    def test_la_base_configurada_existe(self, require_database):  # noqa: ARG002
        """En un entorno ya instalado la base existe y no la creamos ahora."""
        nombre, la_creamos = ensure_database_exists()
        assert nombre
        assert la_creamos is False
