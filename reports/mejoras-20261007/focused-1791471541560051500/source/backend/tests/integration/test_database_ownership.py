# Creado por Aldo Garcia.
"""Propiedad de la base de datos interna.

Regresion de un fallo real en la instalacion del equipo destino: el ``.env``
generado apuntaba a una base ``matrix_rh`` que pertenecia a **otra aplicacion**.
La guardia existia, pero:

* ``pending_migrations()`` creaba ``schema_migrations`` en esa base ajena antes
  de que la guardia pudiera actuar -- es decir, ensuciaba precisamente lo que
  pretendia proteger;
* el mensaje accionable se perdia dentro de un traceback;
* el preflight reportaba un ``DatabaseUnavailableError`` generico en lugar de
  nombrar la causa.

Estas pruebas usan bases temporales reales y las eliminan al terminar.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.common.errors import ConfigurationError
from app.config import get_settings
from app.database.migrator import (
    applied_versions,
    assert_database_is_ours,
    discover_migrations,
    inspect_ownership,
    list_tables,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def base_temporal(require_database) -> Iterator[tuple[str, object]]:  # noqa: ARG001, ANN201
    """Crea una base vacia y devuelve ``(nombre, engine)``. La elimina al final."""
    nombre = f"matrix_rh_test_{uuid.uuid4().hex[:12]}"
    url = make_url(get_settings().database_url.get_secret_value())
    servidor = create_engine(url.set(database=""), future=True)

    with servidor.connect() as conn:
        conn.execute(text(f"CREATE DATABASE `{nombre}` CHARACTER SET utf8mb4"))
        conn.commit()

    engine = create_engine(url.set(database=nombre), future=True)
    try:
        yield nombre, engine
    finally:
        engine.dispose()
        with servidor.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{nombre}`"))
            conn.commit()
        servidor.dispose()


def crear_tabla_ajena(engine, nombre: str = "alembic_version") -> None:  # noqa: ANN001
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE `{nombre}` (version_num VARCHAR(32) NOT NULL)"))
        conn.commit()


class TestInspeccionDePropiedad:
    def test_una_base_vacia_es_adoptable(self, base_temporal):
        _nombre, engine = base_temporal
        es_nuestra, ajenas = inspect_ownership(engine)
        assert es_nuestra is True
        assert ajenas == set()

    def test_una_base_con_tablas_ajenas_no_es_adoptable(self, base_temporal):
        _nombre, engine = base_temporal
        crear_tabla_ajena(engine)
        crear_tabla_ajena(engine, "users")

        es_nuestra, ajenas = inspect_ownership(engine)
        assert es_nuestra is False
        assert ajenas == {"alembic_version", "users"}

    def test_la_inspeccion_NO_crea_la_tabla_de_control(self, base_temporal):
        """El defecto original: comprobar la propiedad ensuciaba la base ajena."""
        _nombre, engine = base_temporal
        crear_tabla_ajena(engine)

        inspect_ownership(engine)

        assert "schema_migrations" not in list_tables(engine)

    def test_la_guardia_lanza_un_mensaje_accionable(self, base_temporal):
        _nombre, engine = base_temporal
        crear_tabla_ajena(engine)

        with pytest.raises(ConfigurationError) as excinfo:
            assert_database_is_ours(engine)

        mensaje = excinfo.value.message
        assert "otra aplicacion" in mensaje
        assert "alembic_version" in mensaje
        # Debe decir QUE hacer, no solo que fallo.
        assert "DATABASE_URL" in mensaje

    def test_la_guardia_no_ensucia_la_base_ajena(self, base_temporal):
        _nombre, engine = base_temporal
        crear_tabla_ajena(engine)

        with pytest.raises(ConfigurationError):
            assert_database_is_ours(engine)

        assert list_tables(engine) == {"alembic_version"}

    def test_una_base_vacia_pasa_la_guardia(self, base_temporal):
        _nombre, engine = base_temporal
        assert_database_is_ours(engine)  # no debe lanzar


class TestConsultaDeMigraciones:
    def test_consultar_no_crea_la_tabla(self, base_temporal):
        """Consultar el estado no debe modificar la base."""
        _nombre, engine = base_temporal

        assert applied_versions(engine) == {}
        assert "schema_migrations" not in list_tables(engine)

    def test_solo_con_create_se_crea_la_tabla(self, base_temporal):
        _nombre, engine = base_temporal

        assert applied_versions(engine, create=True) == {}
        assert "schema_migrations" in list_tables(engine)

    def test_una_base_con_migraciones_registradas_es_nuestra(self, base_temporal):
        """Reinstalar sobre una instalacion previa de Matrix RH es correcto."""
        _nombre, engine = base_temporal
        applied_versions(engine, create=True)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, checksum, applied_at) "
                    "VALUES ('0001', :checksum, NOW(6))"
                ),
                {"checksum": discover_migrations()[0].checksum},
            )
            conn.commit()
        # Aunque existan tablas propias, la base sigue siendo nuestra.
        crear_tabla_ajena(engine, "users")

        es_nuestra, ajenas = inspect_ownership(engine)
        assert es_nuestra is True
        assert ajenas == set()
        assert_database_is_ours(engine)

    def test_la_tabla_de_control_vacia_no_reclama_la_base(self, base_temporal):
        """Una `schema_migrations` vacia junto a tablas ajenas NO es nuestra."""
        _nombre, engine = base_temporal
        applied_versions(engine, create=True)
        crear_tabla_ajena(engine)

        es_nuestra, ajenas = inspect_ownership(engine)
        assert es_nuestra is False
        assert "alembic_version" in ajenas


class TestDiagnosticoDePropiedad:
    def test_la_base_configurada_del_proyecto_es_propia(self, require_database):  # noqa: ARG002
        from app.database.migrator import database_ownership_problem

        assert database_ownership_problem() is None
