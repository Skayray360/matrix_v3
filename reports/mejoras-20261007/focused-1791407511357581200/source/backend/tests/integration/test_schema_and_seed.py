# Creado por Aldo Garcia.
"""Esquema, migraciones y seed de identidad contra MySQL real."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, select

from app.auth.passwords import is_argon2id, verify_password
from app.authorization.context import ROLE_BUSINESS_ADMIN, ROLE_PRESTACIONES_READER
from app.database.migrator import discover_migrations, pending_migrations, run_migrations
from app.database.models import Base, CategoryPermission, LocalCredential, Role, User, UserRole
from seeds.identity_seed import SYNTHETIC_TEST_PASSWORD, seed_test_users

pytestmark = pytest.mark.integration


class TestMigraciones:
    def test_no_quedan_migraciones_pendientes(self, require_database):  # noqa: ARG002
        assert pending_migrations() == []

    def test_reejecutar_migraciones_es_idempotente(self, require_database):  # noqa: ARG002
        assert run_migrations() == []

    def test_las_migraciones_estan_bien_nombradas(self):
        versiones = [m.version for m in discover_migrations()]
        assert versiones == sorted(versiones)
        assert len(set(versiones)) == len(versiones)

    def test_el_esquema_real_coincide_con_los_modelos_orm(self, require_database):  # noqa: ARG002
        """Impide que migraciones y ORM se desincronicen con el tiempo."""
        from app.database.engine import get_engine

        inspector = inspect(get_engine())
        tablas_reales = set(inspector.get_table_names())

        for tabla, modelo in Base.metadata.tables.items():
            if tabla == "schema_migrations":
                continue
            assert tabla in tablas_reales, f"falta la tabla {tabla}"
            columnas_reales = {c["name"] for c in inspector.get_columns(tabla)}
            columnas_orm = set(modelo.columns.keys())
            faltantes = columnas_orm - columnas_reales
            assert not faltantes, f"{tabla}: columnas del ORM ausentes en la BD: {faltantes}"


class TestSeedDeIdentidad:
    def test_los_usuarios_sinteticos_existen(self, db_session):
        for username in ("Matrix", "MatrixR1"):
            usuario = db_session.execute(
                select(User).where(User.username == username)
            ).scalar_one_or_none()
            assert usuario is not None, f"falta el usuario {username}"
            assert usuario.is_active is True
            assert usuario.is_synthetic_test is True

    def test_la_contrasena_no_se_guarda_en_texto_plano(self, db_session):
        """Requisito 27.1: `Matrix RH` no debe aparecer como valor almacenado."""
        credenciales = db_session.execute(select(LocalCredential)).scalars().all()
        assert credenciales
        for credencial in credenciales:
            assert credencial.password_hash_argon2id != SYNTHETIC_TEST_PASSWORD
            assert SYNTHETIC_TEST_PASSWORD not in credencial.password_hash_argon2id

    def test_el_hash_es_argon2id_y_verifica(self, db_session):
        usuario = db_session.execute(select(User).where(User.username == "Matrix")).scalar_one()
        credencial = db_session.get(LocalCredential, usuario.id)
        assert is_argon2id(credencial.password_hash_argon2id)
        assert verify_password(credencial.password_hash_argon2id, SYNTHETIC_TEST_PASSWORD)
        assert not verify_password(credencial.password_hash_argon2id, "otra cosa")

    def test_los_roles_asignados_son_los_esperados(self, db_session):
        def roles_de(username: str) -> set[str]:
            usuario = db_session.execute(select(User).where(User.username == username)).scalar_one()
            filas = db_session.execute(
                select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(
                    UserRole.user_id == usuario.id
                )
            ).scalars().all()
            return set(filas)

        assert roles_de("Matrix") == {ROLE_BUSINESS_ADMIN}
        assert roles_de("MatrixR1") == {ROLE_PRESTACIONES_READER}

    def test_el_rol_restringido_solo_tiene_prestaciones(self, db_session):
        rol = db_session.execute(
            select(Role).where(Role.name == ROLE_PRESTACIONES_READER)
        ).scalar_one()
        permisos = db_session.execute(
            select(CategoryPermission).where(CategoryPermission.role_id == rol.id)
        ).scalars().all()
        assert [(p.category, p.is_wildcard) for p in permisos] == [("prestaciones", False)]

    def test_el_rol_administrador_tiene_wildcard(self, db_session):
        rol = db_session.execute(select(Role).where(Role.name == ROLE_BUSINESS_ADMIN)).scalar_one()
        permisos = db_session.execute(
            select(CategoryPermission).where(CategoryPermission.role_id == rol.id)
        ).scalars().all()
        assert any(p.is_wildcard for p in permisos)

    def test_el_seed_es_idempotente(self, require_database):  # noqa: ARG002
        from app.database.engine import session_scope

        with session_scope() as db:
            primera = seed_test_users(db)
        with session_scope() as db:
            segunda = seed_test_users(db)
        assert segunda == [] or segunda == primera

        with session_scope() as db:
            total = db.execute(select(User).where(User.username == "Matrix")).scalars().all()
        assert len(total) == 1
