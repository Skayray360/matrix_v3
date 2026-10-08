# Creado por Aldo Garcia.
"""Guardia previa a suites que pueden borrar y confirmar datos sinteticos."""

from __future__ import annotations

import os
import re

from sqlalchemy.engine import make_url

from app.config import AppEnv, QdrantMode, get_settings
from app.config.settings import PROJECT_ROOT


def require_disposable_database() -> None:
    """Exige entorno, nombre y consentimiento explicitos antes de cualquier escritura.

    No abre conexiones ni crea bases. El nombre es una segunda barrera: activar
    la variable de consentimiento nunca autoriza usar la base habitual Matrix.
    """
    settings = get_settings()
    database = make_url(settings.database_url.get_secret_value()).database or ""
    errors = []
    if settings.app_env is not AppEnv.TEST:
        errors.append("APP_ENV debe ser test")
    if not re.fullmatch(r"matrix_rh_test(?:_[a-z0-9]+)*", database):
        errors.append("DATABASE_URL debe seleccionar matrix_rh_test o matrix_rh_test_<sufijo>")
    if os.environ.get("MATRIX_TEST_ALLOW_DESTRUCTIVE", "").lower() != "true":
        errors.append("MATRIX_TEST_ALLOW_DESTRUCTIVE=true debe autorizar una base descartable")
    test_root = PROJECT_ROOT / "var" / "tests"
    if test_root.resolve() != test_root.absolute():
        errors.append("var/tests no debe redirigir mediante enlaces a almacenamiento externo")
    if not settings.upload_storage_path.resolve().is_relative_to(test_root.absolute()):
        errors.append("UPLOAD_STORAGE_ROOT debe estar dentro de var/tests")
    if settings.qdrant_mode is QdrantMode.EMBEDDED:
        if not settings.qdrant_storage_path.resolve().is_relative_to(test_root.absolute()):
            errors.append("QDRANT_PATH debe estar dentro de var/tests")
    elif not all(
        re.fullmatch(r"matrix_rh_test_[a-z0-9_]+", name)
        for name in (settings.rag_collection_corporate, settings.rag_collection_private)
    ):
        errors.append("las colecciones Qdrant servidor deben empezar por matrix_rh_test_")
    if errors:
        raise ValueError(
            "Validacion bloqueada antes de modificar datos. " + "; ".join(errors)
            + ". Use una base exclusiva de pruebas sin informacion real; nunca renombre la base operativa."
        )


def main() -> int:
    try:
        require_disposable_database()
    except ValueError as error:
        print(str(error))
        return 1
    print("Entorno de pruebas descartable autorizado por configuracion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
