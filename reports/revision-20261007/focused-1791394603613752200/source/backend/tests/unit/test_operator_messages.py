# Creado por Aldo Garcia.
"""Lo que el operador ve cuando algo falla.

Estas pruebas no verifican logica de negocio: verifican **mensajes**. Existen
porque las instalaciones fallidas en el equipo destino no fallaron por el motivo
que decia la pantalla.

Tres casos reales:

1. ``run_migrations`` levantaba un ``ConfigurationError`` con la solucion escrita
   dentro, y el `.bat` mostraba un traceback de Python de veinte lineas donde esa
   frase quedaba sepultada.
2. PowerShell convertia la salida por stderr de Python en un ``NativeCommandError``
   y el mensaje real desaparecia detras del envoltorio de la excepcion.
3. El diagnostico moria con ``ModuleNotFoundError: No module named 'pydantic'``
   cuando lo unico que pasaba era que Matrix RH no estaba instalado.

Un instalador que no sabe explicar por que fallo no esta terminado, asi que el
formato de estos mensajes se prueba igual que cualquier otra funcionalidad.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from app.common.errors import ConfigurationError, DatabaseUnavailableError
from app.config.settings import PROJECT_ROOT
from scripts.bootstrap import main

pytestmark = pytest.mark.unit

WINDOWS_DIR = PROJECT_ROOT / "windows"


def _leer(nombre: str) -> str:
    ruta = WINDOWS_DIR / nombre
    assert ruta.is_file(), f"Falta {ruta}"
    return ruta.read_text(encoding="utf-8")


def _forzar(monkeypatch: pytest.MonkeyPatch, excepcion: BaseException) -> None:
    """Hace que ``bootstrap status`` levante la excepcion indicada."""

    def explotar(_: argparse.Namespace) -> int:
        raise excepcion

    monkeypatch.setattr("scripts.bootstrap.cmd_status", explotar)


class TestSalidaDelBootstrap:
    def test_un_error_tipado_se_muestra_como_mensaje_y_no_como_traceback(
        self, monkeypatch, capsys
    ):
        _forzar(
            monkeypatch,
            ConfigurationError("La base 'matrix_rh' ya existia y no fue creada por Matrix RH."),
        )

        code = main(["status"])
        salida = capsys.readouterr().out

        assert code == 1
        assert "ERROR [configuration_error]" in salida
        assert "ya existia" in salida
        assert "Traceback" not in salida

    def test_el_tipo_tecnico_se_muestra_sin_revelar_detalles_privados(self, monkeypatch, capsys):
        """El operador conserva el tipo; DSN y parametros no llegan a pantalla."""
        _forzar(
            monkeypatch,
            DatabaseUnavailableError(
                "No fue posible conectar con MySQL.", detail="OperationalError(2003): password=PRIVADO"
            ),
        )

        assert main(["status"]) == 1
        salida = capsys.readouterr().out

        assert "ERROR [database_unavailable] No fue posible conectar con MySQL." in salida
        assert "Tipo: DatabaseUnavailableError" in salida
        assert "PRIVADO" not in salida and "OperationalError(2003)" not in salida

    def test_interrumpir_con_ctrl_c_no_es_un_fallo_del_sistema(self, monkeypatch, capsys):
        _forzar(monkeypatch, KeyboardInterrupt())

        code = main(["status"])
        salida = capsys.readouterr().out

        assert code == 130  # convencion POSIX: 128 + SIGINT
        assert "Interrumpido por el operador" in salida
        assert "Traceback" not in salida


class TestScriptsDeWindows:
    def test_no_se_invoca_python_sin_entorno_virtual(self):
        """Sin `.venv` cualquier `python -m scripts...` es un ModuleNotFoundError."""
        contenido = _leer("Common-MatrixRH.ps1")
        invoke = contenido.split("function Invoke-MatrixPython", 1)[-1]

        assert "Test-MatrixVenv" in invoke
        assert "INSTALAR_MATRIX_RH.bat" in invoke

    def test_la_salida_nativa_no_se_convierte_en_excepcion_de_powershell(self):
        """`$ErrorActionPreference = 'Stop'` envuelve stderr en NativeCommandError."""
        contenido = _leer("Common-MatrixRH.ps1")

        assert contenido.count("$ErrorActionPreference = 'Continue'") >= 2

    def test_la_sonda_de_base_sobrevive_a_la_falta_de_dependencias(self):
        """La sonda corre antes de que el venv este completo en algunos flujos."""
        contenido = _leer("Common-MatrixRH.ps1")

        assert "DESCONOCIDO:SinDependencias" in contenido
        # Los imports deben estar dentro del try, no en el nivel del modulo:
        # de lo contrario el ImportError sale como traceback y no como veredicto.
        assert "except ImportError:" in contenido

    def test_un_veredicto_inesperado_no_se_interpreta_como_ocupada(self):
        """Antes, cualquier salida rara marcaba la base como ocupada."""
        contenido = _leer("Common-MatrixRH.ps1")

        assert "$veredictos = @('LIBRE', 'PROPIA', 'EXISTE_VACIA')" in contenido
        assert "EXISTE_VACIA" in contenido

    def test_la_comprobacion_de_base_va_despues_de_instalar_dependencias(self):
        """El orden es el defecto: la sonda usa SQLAlchemy y necesita el venv."""
        contenido = _leer("Install-MatrixRH.ps1")

        dependencias = contenido.index("Dependencias de Python")
        base = contenido.index("Base de datos destino")

        assert dependencias < base

    def test_el_diagnostico_sin_venv_lo_dice_en_lugar_de_reventar(self):
        contenido = _leer("Diagnose-MatrixRH.ps1")

        assert "DIAGNOSTICO INCOMPLETO" in contenido
        assert "no esta instalado en este equipo" in contenido
        # La guardia debe ir antes de la primera llamada a Python.
        guardia = contenido.index("Test-MatrixVenv")
        primera_llamada = contenido.index("Invoke-MatrixPython")
        assert guardia < primera_llamada


class TestQualityGate:
    """Un gate que se salta una comprobacion no puede llamarlo "no instalado"."""

    def test_una_herramienta_cmd_de_windows_se_lanza_con_su_interprete(self, monkeypatch):
        """`npm` es `npm.cmd`: CreateProcess no lo ejecuta sin `cmd /c`."""
        from scripts import run_quality_gate

        monkeypatch.setattr(
            run_quality_gate,
            "_resolve_tool",
            lambda _n: r"C:\Program Files\nodejs\npm.CMD",
        )

        assert run_quality_gate._launcher("npm") == [
            "cmd",
            "/c",
            r"C:\Program Files\nodejs\npm.CMD",
        ]

    def test_una_herramienta_ausente_se_distingue_de_una_no_lanzable(self, monkeypatch):
        from scripts import run_quality_gate

        monkeypatch.setattr(run_quality_gate, "_resolve_tool", lambda _n: None)

        assert run_quality_gate._launcher("npm") is None

    def test_las_herramientas_del_venv_se_buscan_antes_que_las_del_path(self, monkeypatch):
        """El gate corre sin activar el venv: su `Scripts` no esta en el PATH."""
        from scripts import run_quality_gate

        scripts_dir = str(Path(sys.executable).parent)
        llamadas: list[str | None] = []

        def which(_nombre, path=None):  # noqa: ANN001, ANN202
            llamadas.append(path)
            return r"C:\venv\Scripts\detect-secrets.exe" if path == scripts_dir else None

        monkeypatch.setattr(run_quality_gate.shutil, "which", which)

        assert run_quality_gate._resolve_tool("detect-secrets") == (
            r"C:\venv\Scripts\detect-secrets.exe"
        )
        assert llamadas[0] == scripts_dir  # el venv primero
