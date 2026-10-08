# Creado por Aldo Garcia.
"""La validacion de shell exige una copia descartable antes de tocar servicios/datos."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
SCRIPT = Path(__file__).resolve().parents[3] / "scripts/matrixrh.sh"


@pytest.mark.parametrize("guard_code,quality_code", [(1, 0), (0, 2), (0, 0)])
def test_validate_guarda_antes_de_mutar_y_propaga_gate(tmp_path, guard_code, quality_code):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Este contrato necesita bash")
    log = tmp_path / "llamadas.jsonl"
    # Sustituye los comandos antes de llamar a la funcion del script real.
    # No invoca MySQL, indexadores ni procesos de la instalacion operativa.
    source = r'''
source "$1"
log="$2"
guard="$3"
quality="$4"
py() {
    printf '%s\n' "$*" >> "$log"
    case "$*" in
      '-m scripts.test_environment') return "$guard" ;;
      '-m scripts.run_quality_gate --fast') return "$quality" ;;
      *) return 0 ;;
    esac
}
cmd_stop() { printf '%s\n' 'stop' >> "$log"; }
cmd_validate
'''
    completed = subprocess.run(
        [bash, "-c", source, "matrix-validation-test", str(SCRIPT), str(log), str(guard_code), str(quality_code)],
        capture_output=True, text=True, timeout=10,
    )
    calls = log.read_text().splitlines()
    assert calls[0] == "-m scripts.test_environment"
    if guard_code:
        assert calls == ["-m scripts.test_environment"]
        assert completed.returncode == guard_code
    else:
        assert calls == ["-m scripts.test_environment", "stop", "-m scripts.bootstrap migrate",
                         "-m scripts.bootstrap seed", "-m scripts.bootstrap ingest",
                         "-m scripts.run_quality_gate --fast"]
        assert completed.returncode == quality_code
    # La salida no declara un PASS global ni una validacion completa falsa.
    assert "Validacion completada" not in completed.stdout
    assert "PASS" not in completed.stdout
