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
import re
import sys
from pathlib import Path

import pytest

from app.common.errors import ConfigurationError, DatabaseUnavailableError
from app.config.settings import PROJECT_ROOT
from scripts.bootstrap import main

pytestmark = pytest.mark.unit

WINDOWS_DIR = PROJECT_ROOT / "backend/scripts/windows"


def _leer() -> str:
    ruta = WINDOWS_DIR / "MatrixRH.ps1"
    assert ruta.is_file(), f"Falta {ruta}"
    return ruta.read_text(encoding="utf-8")


def _funcion(nombre: str) -> str:
    match = re.search(rf"(?ms)^function {re.escape(nombre)}\b.*?(?=^function |^try \{{|\Z)", _leer())
    assert match, f"Falta la funcion operativa {nombre}"
    return match.group(0)


def _forzar(monkeypatch: pytest.MonkeyPatch, excepcion: BaseException) -> None:
    """Hace que ``bootstrap status`` levante la excepcion indicada."""

    def explotar(_: argparse.Namespace) -> int:
        raise excepcion

    # La entrega verificada es una precondicion de estas pruebas de mensajes;
    # la integridad real y su rechazo tienen sus propios fixtures aislados.
    monkeypatch.setattr("scripts.bootstrap.check_integrity",
                        lambda report: report.add("integridad_codigo", "PASS", "Entrega sintetica verificada."))
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
        """El controlador explica que falta su Python antes de ejecutar modulos."""
        contenido = _leer()
        invoke = _funcion("Invoke-Python")

        assert "'venv\\Scripts\\python.exe'" in contenido
        assert "No existe el Python propio. Ejecute instalar.bat." in invoke
        assert invoke.index("Test-Path -LiteralPath $script:Python") < invoke.index("Invoke-Checked $script:Python")

    def test_la_salida_nativa_se_conserva_y_el_error_indica_su_codigo(self):
        """El helper transmite stdout/stderr sin capturarlos como objetos PowerShell."""
        invoke = _funcion("Invoke-Checked")

        assert "& $Executable @Arguments" in invoke
        assert "2>&1" not in invoke and "Out-Null" not in invoke
        assert "$LASTEXITCODE -ne 0" in invoke
        assert "(codigo " in invoke and "Consulte el mensaje anterior." in invoke

    def test_la_guardia_de_base_solo_corre_tras_instalar_dependencias(self):
        """Una dependencia faltante detiene uv antes de consultar la base propia."""
        install = _funcion("Install-Stack")

        assert "Invoke-Checked $uv @('sync'" in install
        assert "dependencias fijadas por uv.lock" in install
        assert install.index("Invoke-Checked $uv @('sync'") < install.index("'--guard-install'")

    def test_base_propia_sin_respuesta_se_distingue_del_puerto_ajeno(self):
        """Los mensajes distinguen un proceso propio caido de una colision ajena."""
        start = _funcion("Start-MySql")
        complete = _funcion("Complete-MySqlBootstrap")

        assert "Complete-MySqlBootstrap" in start
        assert "MySQL propio no responde al control autenticado." in complete
        assert "Se conserva el bootstrap pendiente para reintentar." in complete
        assert "ocupado por otro proceso. No se detiene." in start
        assert "El datadir MySQL no esta vacio pero tampoco completo. No se reinicializa." in start
        assert start.index("Owned-Process 'mysql'") < start.index("Port-Free $port")

    def test_la_comprobacion_de_base_va_despues_de_instalar_dependencias(self):
        """Nunca anunciar instalacion completa antes de la guardia y migraciones."""
        install = _funcion("Install-Stack")
        dependencies = install.index("Instalando Python propio")
        guard = install.index("'--guard-install'")
        migrate = install.index("'scripts.bootstrap','migrate'")
        complete = install.index("INSTALACION TERMINADA")
        assert dependencies < guard < migrate < complete

    def test_el_diagnostico_sin_venv_lo_dice_en_lugar_de_reventar(self):
        diagnose = _funcion("Diagnose")
        check = _funcion("Check")
        invoke = _funcion("Invoke-Python")
        assert "Check 'Python propio' { Invoke-Python" in diagnose
        assert "No existe el Python propio. Ejecute instalar.bat." in invoke
        assert "catch" in check and "$script:CheckFailures++" in check
        assert "'[FALLA] ' + $Name + ': ' + $_.Exception.Message" in check
        assert "'Resultado: ' + $script:CheckFailures + ' FALLA(S).'" in diagnose
        assert "Diagnostico con fallos. Compare los componentes anteriores." in diagnose


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
