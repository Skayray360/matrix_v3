# Creado por Aldo Garcia.
"""Adopcion de bases de datos preexistentes.

Regresion del segundo fallo real en el equipo destino. La guardia de propiedad
(``test_database_ownership.py``) protege una base que *contiene tablas ajenas*,
pero no cubria el caso que de verdad ocurrio: una base llamada ``matrix_rh``
creada por **otro proyecto** que en ese momento estaba **vacia** -- su
herramienta de migraciones habia hecho un ``downgrade`` y habia borrado todas
las tablas.

Vacia y adoptable parecen lo mismo y no lo son. Matrix RH se habria apropiado
del nombre de una base ajena, y la siguiente vez que el otro proyecto migrara,
las dos aplicaciones estarian escribiendo en el mismo sitio.

El criterio nuevo es quien creo la base, no que hay dentro: si Matrix RH no la
creo y no tiene migraciones registradas, no la usa salvo permiso explicito
(``MATRIX_ADOPT_EXISTING_DATABASE=true``).

Estas pruebas crean bases temporales reales y las eliminan al terminar.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.common.errors import ConfigurationError
from app.config import get_settings
from app.database.migrator import ensure_database_exists, list_tables, run_migrations

pytestmark = pytest.mark.integration


@pytest.fixture()
def servidor(require_database) -> Iterator[object]:  # noqa: ARG001, ANN201
    """Engine contra el servidor MySQL, sin base seleccionada."""
    url = make_url(get_settings().database_url.get_secret_value())
    engine = create_engine(url.set(database=""), future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def nombre_libre(servidor) -> Iterator[str]:  # noqa: ANN001
    """Un nombre de base que no existe. Se elimina al terminar, exista o no."""
    nombre = f"matrix_rh_adopt_{uuid.uuid4().hex[:12]}"
    try:
        yield nombre
    finally:
        with servidor.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{nombre}`"))
            conn.commit()


def crear_base(servidor, nombre: str) -> None:  # noqa: ANN001
    with servidor.connect() as conn:
        conn.execute(text(f"CREATE DATABASE `{nombre}` CHARACTER SET utf8mb4"))
        conn.commit()


def apuntar_a(monkeypatch: pytest.MonkeyPatch, url, nombre: str) -> None:  # noqa: ANN001
    """Redirige la configuracion a ``nombre`` y descarta el engine cacheado.

    Se usa ``render_as_string(hide_password=False)`` y no ``str(url)``: este
    ultimo enmascara la contrasena con ``***``, y el DSN resultante produce un
    ``Access denied for user 'root'@'localhost' (using password: YES)`` que
    parece un problema de permisos y no lo es.
    """
    from app.database import engine as engine_module

    dsn = url.set(database=nombre).render_as_string(hide_password=False)
    monkeypatch.setattr(get_settings(), "database_url", SecretStr(dsn), raising=False)
    engine_module.dispose_engine()


@pytest.fixture()
def apuntado(monkeypatch: pytest.MonkeyPatch, servidor):  # noqa: ANN001, ANN201
    """Devuelve una funcion que apunta la configuracion a una base dada.

    Al terminar restaura el engine para no dejar el pool colgado de una base
    temporal ya eliminada.
    """
    from app.database import engine as engine_module

    url = make_url(get_settings().database_url.get_secret_value())
    try:
        yield lambda nombre: apuntar_a(monkeypatch, url, nombre)
    finally:
        engine_module.dispose_engine()


def engine_de(servidor, nombre: str):  # noqa: ANN001, ANN201
    url = make_url(get_settings().database_url.get_secret_value())
    return create_engine(url.set(database=nombre), future=True)


class TestQuienCreoLaBase:
    def test_crear_una_base_nueva_reporta_que_la_creamos(self, apuntado, nombre_libre):  # noqa: ANN001
        apuntado(nombre_libre)

        nombre, la_creamos = ensure_database_exists()

        assert nombre == nombre_libre
        assert la_creamos is True

    def test_una_base_preexistente_reporta_que_no_la_creamos(
        self, apuntado, servidor, nombre_libre
    ):  # noqa: ANN001
        crear_base(servidor, nombre_libre)
        apuntado(nombre_libre)

        _nombre, la_creamos = ensure_database_exists()

        assert la_creamos is False

    def test_la_segunda_llamada_ya_no_la_crea(self, apuntado, nombre_libre):  # noqa: ANN001
        apuntado(nombre_libre)

        assert ensure_database_exists()[1] is True
        assert ensure_database_exists()[1] is False


class TestAdopcionDeBaseAjena:
    def test_no_adopta_una_base_preexistente_aunque_este_vacia(
        self, apuntado, servidor, nombre_libre
    ):  # noqa: ANN001
        """El caso real: base ajena vaciada por el `downgrade` de otro proyecto."""
        crear_base(servidor, nombre_libre)
        apuntado(nombre_libre)

        with pytest.raises(ConfigurationError) as excinfo:
            run_migrations()

        mensaje = excinfo.value.message
        assert "ya existia" in mensaje
        assert nombre_libre in mensaje

    def test_el_mensaje_dice_las_dos_salidas(self, apuntado, servidor, nombre_libre):  # noqa: ANN001
        """Un error de instalacion debe decir QUE hacer, no solo que fallo."""
        crear_base(servidor, nombre_libre)
        apuntado(nombre_libre)

        with pytest.raises(ConfigurationError) as excinfo:
            run_migrations()

        mensaje = excinfo.value.message
        assert "DATABASE_URL" in mensaje
        assert "MATRIX_ADOPT_EXISTING_DATABASE" in mensaje

    def test_al_rechazarla_no_deja_ninguna_tabla_dentro(
        self, apuntado, servidor, nombre_libre
    ):  # noqa: ANN001
        """Negarse a usar la base no puede escribir en ella."""
        crear_base(servidor, nombre_libre)
        apuntado(nombre_libre)

        with pytest.raises(ConfigurationError):
            run_migrations()

        engine = engine_de(servidor, nombre_libre)
        try:
            assert list_tables(engine) == set()
        finally:
            engine.dispose()

    def test_con_la_bandera_explicita_si_la_adopta(
        self, apuntado, monkeypatch, servidor, nombre_libre
    ):  # noqa: ANN001
        """El operador puede autorizarlo; lo que no vale es hacerlo en silencio."""
        crear_base(servidor, nombre_libre)
        apuntado(nombre_libre)
        monkeypatch.setattr(
            get_settings(), "matrix_adopt_existing_database", True, raising=False
        )

        aplicadas = run_migrations()

        assert aplicadas  # se aplico al menos una migracion
        engine = engine_de(servidor, nombre_libre)
        try:
            assert "schema_migrations" in list_tables(engine)
        finally:
            engine.dispose()


class TestInstalacionNormal:
    def test_una_base_creada_por_matrix_rh_se_migra_sin_bandera(
        self, apuntado, servidor, nombre_libre
    ):  # noqa: ANN001
        """La instalacion limpia no debe pedirle nada al operador."""
        apuntado(nombre_libre)

        aplicadas = run_migrations()

        assert aplicadas
        engine = engine_de(servidor, nombre_libre)
        try:
            assert "schema_migrations" in list_tables(engine)
        finally:
            engine.dispose()

    def test_reinstalar_sobre_una_base_propia_no_pide_permiso(
        self, apuntado, nombre_libre
    ):  # noqa: ANN001
        """La segunda ejecucion ya no la crea, pero tiene migraciones nuestras."""
        apuntado(nombre_libre)
        run_migrations()

        assert run_migrations() == []  # idempotente y sin error

    def test_una_base_ajena_con_tablas_falla_por_propiedad(
        self, apuntado, servidor, nombre_libre
    ):  # noqa: ANN001
        """La guardia de propiedad actua antes: su mensaje es mas concreto."""
        crear_base(servidor, nombre_libre)
        engine = engine_de(servidor, nombre_libre)
        try:
            with engine.connect() as conn:
                conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
                conn.commit()
        finally:
            engine.dispose()
        apuntado(nombre_libre)

        with pytest.raises(ConfigurationError) as excinfo:
            run_migrations()

        assert "otra aplicacion" in excinfo.value.message


class TestDsnDePruebas:
    def test_el_dsn_redirigido_conserva_la_contrasena(self):
        """Regresion del `***` de ``str(URL)``.

        ``str(make_url(...))`` enmascara la contrasena. Usarlo para redirigir la
        conexion produce un `Access denied ... (using password: YES)` que hace
        perder el tiempo buscando un problema de permisos inexistente.
        """
        url = make_url("mysql+pymysql://usuario:secreto@localhost:3306/origen")

        enmascarado = str(url.set(database="destino"))
        real = url.set(database="destino").render_as_string(hide_password=False)

        assert "***" in enmascarado
        assert "***" not in real
        assert "secreto" in real
