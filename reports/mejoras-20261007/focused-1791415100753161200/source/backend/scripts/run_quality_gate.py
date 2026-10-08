# Creado por Aldo Garcia.
"""Quality gate unico de Matrix RH (seccion 21).

Coordina, en este orden:

1. lint (``ruff``)
2. typing (``mypy``)
3. pruebas unitarias
4. pruebas de integracion
5. pruebas de seguridad
6. auditoria de dependencias (``pip-audit``)
7. analisis estatico de seguridad (``bandit``)
8. escaneo de secretos (propio + ``detect-secrets`` si esta disponible)
9. pruebas del frontend (``vitest``)
10. Playwright E2E (solo con ``--with-e2e`` y el backend arriba)
11. evaluacion del RAG
12. resumen de gates

Una herramienta ausente se reporta como ``SKIPPED`` y no se disfraza de PASS.
Ningun paso se "aprueba" ignorando hallazgos.

Uso:
    python -m scripts.run_quality_gate
    python -m scripts.run_quality_gate --fast          # sin RAG completo ni E2E
    python -m scripts.run_quality_gate --with-e2e
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
import subprocess  # noqa: S404 - se invocan herramientas de calidad conocidas
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import PROJECT_ROOT

BACKEND_ROOT = PROJECT_ROOT / "backend"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
REPORTS = PROJECT_ROOT / "reports"
TEST_REPORTS = REPORTS / "tests"
SECURITY_REPORTS = REPORTS / "security"

# Estados de gate. El linter marca S105 por el nombre "PASS"; no es una
# contrasena sino el veredicto de una comprobacion.
PASS = "PASS"  # noqa: S105
FAIL = "FAIL"
SKIPPED = "SKIPPED"
INCOMPLETE = "INCOMPLETE"


@dataclass
class GateResult:
    name: str
    status: str
    exit_code: int = 0
    duration_s: float = 0.0
    detail: str = ""
    blocking: bool = True


def _run(
    command: list[str], *, cwd: Path, name: str, blocking: bool = True,
    require_complete_junit: Path | None = None,
) -> GateResult:
    """Ejecuta un comando y traduce su codigo de salida a un resultado de gate."""
    started = time.perf_counter()
    print(f"\n=== {name} ===")
    print("  $ " + " ".join(command))
    if require_complete_junit is not None:
        require_complete_junit.unlink(missing_ok=True)
    try:
        completed = subprocess.run(command, cwd=str(cwd), check=False)  # noqa: S603
    except FileNotFoundError:
        return GateResult(name=name, status=SKIPPED, detail="herramienta no instalada", blocking=blocking)
    duration = round(time.perf_counter() - started, 2)
    status = PASS if completed.returncode == 0 else FAIL
    detail = ""
    if status == PASS and require_complete_junit is not None:
        try:
            root = ET.parse(require_complete_junit).getroot()  # noqa: S314 - reporte local de pytest
            suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
            count = sum(int(suite.get("tests", "0")) for suite in suites)
            skipped = sum(int(suite.get("skipped", "0")) for suite in suites)
            if skipped or not count:
                status = INCOMPLETE
                detail = f"{count} casos recopilados, {skipped} omitidos; no certifica la suite completa"
        except (OSError, ET.ParseError, ValueError):
            status = FAIL
            detail = "falta un reporte JUnit valido de esta ejecucion"
    return GateResult(
        name=name,
        status=status,
        exit_code=completed.returncode,
        duration_s=duration,
        detail=detail,
        blocking=blocking,
    )


def _python() -> str:
    return sys.executable


def _resolve_tool(name: str) -> str | None:
    """Ruta del ejecutable de una herramienta.

    Se busca **primero en el entorno virtual del proyecto** y solo despues en el
    PATH. Motivo: el gate se invoca con la ruta absoluta del interprete
    (``.venv\\Scripts\\python.exe``), sin activar el venv, asi que su carpeta
    ``Scripts`` no esta en el PATH. ``detect-secrets`` estaba instalado como
    dependencia de desarrollo del propio proyecto y aun asi el gate lo daba por
    ausente.
    """
    scripts_dir = Path(sys.executable).parent
    encontrado = shutil.which(name, path=str(scripts_dir))
    return encontrado or shutil.which(name)


def _tool_available(name: str) -> bool:
    return _resolve_tool(name) is not None


def _launcher(name: str) -> list[str] | None:
    """Comando ejecutable para una herramienta que puede ser un script de shell.

    En Windows ``npm`` es ``npm.cmd``. ``shutil.which`` lo encuentra, pero
    ``subprocess.run(["npm", ...])`` no lo puede lanzar: ``CreateProcess`` no
    ejecuta ``.cmd`` ni ``.bat`` sin interprete, y falla con ``FileNotFoundError``.

    El efecto era peor que un fallo: ``_run`` traducia esa excepcion a
    ``SKIPPED: herramienta no instalada`` y el gate informaba de que npm no
    estaba, en un equipo donde si estaba. Un gate que se salta una comprobacion
    y lo llama "no instalado" miente sobre lo que ha verificado.
    """
    resolved = _resolve_tool(name)
    if resolved is None:
        return None
    if resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", resolved]
    return [resolved]


def _write_installed_dependencies(destination: Path) -> None:
    """Fija cada tercero instalado sin enviar el paquete privado Matrix a PyPI.

    pip-audit 2.7.3 con --strict falla ante el propio paquete editable aun con
    --skip-editable. Un inventario completo permite --no-deps --disable-pip:
    el auditor consulta versiones exactas sin resolver ni instalar nada.
    """
    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "")
        version = distribution.version
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("Distribucion instalada sin nombre valido")
        canonical = re.sub(r"[-_.]+", "-", name).casefold()
        if canonical == "matrix-rh-backend":
            continue
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+!_-]*", version):
            raise ValueError("Distribucion instalada sin version valida")
        if canonical in installed and installed[canonical] != version:
            raise ValueError("Dos versiones distintas de la misma distribucion instalada")
        installed[canonical] = version
    if not installed:
        raise ValueError("No se pudo inventariar ninguna dependencia instalada")
    destination.write_text(
        "".join(f"{name}=={version}\n" for name, version in sorted(installed.items())), encoding="utf-8",
    )


def _audit_dependencies(py: str) -> GateResult:
    requirements = SECURITY_REPORTS / "dependencies-installed.txt"
    try:
        _write_installed_dependencies(requirements)
    except (OSError, ValueError) as error:
        return GateResult(
            name="auditoria de dependencias (pip-audit)", status=FAIL, exit_code=1,
            detail=f"No se pudo construir el inventario de terceros ({type(error).__name__})",
        )
    return _run(
        [py, "-m", "pip_audit", "--strict", "--requirement", str(requirements),
         "--no-deps", "--disable-pip", "--progress-spinner", "off",
         "--format", "json", "--output", str(SECURITY_REPORTS / "pip-audit.json")],
        cwd=BACKEND_ROOT, name="auditoria de dependencias (pip-audit)",
    )


def build_gates(args: argparse.Namespace) -> list[GateResult]:
    TEST_REPORTS.mkdir(parents=True, exist_ok=True)
    SECURITY_REPORTS.mkdir(parents=True, exist_ok=True)
    results: list[GateResult] = []
    py = _python()

    # 1. lint
    results.append(_run([py, "-m", "ruff", "check", "app", "scripts", "seeds", "tests"],
                        cwd=BACKEND_ROOT, name="lint (ruff)"))

    # 2. typing
    results.append(_run([py, "-m", "mypy", "app"], cwd=BACKEND_ROOT, name="typing (mypy)",
                        blocking=False))

    # 2b. encabezados de autoria (requisito 16)
    results.append(_run([py, "-m", "scripts.verify_headers"], cwd=BACKEND_ROOT,
                        name="encabezados de autoria"))

    results.append(_run([py, "-m", "scripts.verify_documentation"], cwd=BACKEND_ROOT,
                        name="enlaces de documentacion distribuida"))

    # 3. unit
    results.append(_run(
        [py, "-m", "pytest", "tests/unit", "-q",
         "--junitxml", str(TEST_REPORTS / "unit-junit.xml"),
         "--cov=app", "--cov-report", f"xml:{TEST_REPORTS / 'coverage-unit.xml'}"],
        cwd=BACKEND_ROOT, name="pruebas unitarias",
        require_complete_junit=TEST_REPORTS / "unit-junit.xml"))

    # 4. integration
    results.append(_run(
        [py, "-m", "pytest", "tests/integration", "-q",
         "--junitxml", str(TEST_REPORTS / "integration-junit.xml")],
        cwd=BACKEND_ROOT, name="pruebas de integracion",
        require_complete_junit=TEST_REPORTS / "integration-junit.xml"))

    # 5. security tests
    results.append(_run(
        [py, "-m", "pytest", "tests/security", "-q",
         "--junitxml", str(TEST_REPORTS / "security-junit.xml")],
        cwd=BACKEND_ROOT, name="pruebas de seguridad",
        require_complete_junit=TEST_REPORTS / "security-junit.xml"))

    # 6. Todas las dependencias instaladas (runtime y desarrollo) son bloqueantes.
    # El codigo propio se comprueba con las suites, Bandit y el escaner de secretos.
    results.append(_audit_dependencies(py))

    # 7. SAST
    results.append(_run(
        [py, "-m", "bandit", "-q", "-r", "app", "-x", "tests",
         "-f", "json", "-o", str(SECURITY_REPORTS / "bandit.json")],
        cwd=BACKEND_ROOT, name="SAST (bandit)"))

    # 8. secretos
    results.append(_run(
        [py, "-m", "scripts.secrets_scan", "--allow-env",
         "--output", str(SECURITY_REPORTS / "secrets_scan.json")],
        cwd=BACKEND_ROOT, name="escaneo de secretos"))

    # 8b. cadena de suministro: lista de bloqueo, scripts de instalacion,
    # indicadores de compromiso y reproducibilidad. Bloqueante.
    supply_gate = _run(
        [py, "-m", "scripts.verify_supply_chain",
         "--output", str(SECURITY_REPORTS / "supply_chain.json")],
        cwd=BACKEND_ROOT, name="cadena de suministro (npm fijado)")
    results.append(supply_gate)
    frontend_trusted = supply_gate.status == PASS

    detect = _launcher("detect-secrets")
    if detect:
        # Se excluyen los arboles de dependencias y el estado runtime: no son
        # codigo del proyecto y multiplican el tiempo del gate por decenas.
        results.append(_run([*detect, "scan", "--all-files", "--exclude-files",
                             r"(^|[\\/])(\.venv|node_modules|\.git|var|dist|__pycache__)([\\/]|$)"],
                            cwd=PROJECT_ROOT, name="detect-secrets", blocking=False))
    else:
        results.append(GateResult(name="detect-secrets", status=SKIPPED,
                                  detail="no instalado", blocking=False))

    # 9. frontend
    # npm utiliza el package-lock.json real, igual que el instalador Windows.
    corepack = _launcher("corepack")
    if (FRONTEND_ROOT / "node_modules").is_dir() and corepack and frontend_trusted:
        npm = [*corepack, "npm"]
        # Vulnerabilidades en lo que realmente se despliega: bloqueante.
        prod_audit = _run([*npm, "audit", "--omit=dev", "--audit-level", "low"],
                          cwd=FRONTEND_ROOT, name="npm audit (produccion)")
        results.append(prod_audit)
        # Vite/Vitest se EJECUTAN en el host de build: dev tambien es bloqueante.
        audit_gate = _run(
            [*npm, "audit", "--audit-level=low", "--include=dev", "--include=optional", "--include=peer"],
            cwd=FRONTEND_ROOT, name="npm audit (incluye dev)",
        )
        results.append(audit_gate)
        frontend_trusted = audit_gate.status == PASS and prod_audit.status == PASS
        for command, name in (
            ("test", "pruebas de frontend (vitest)"),
            ("build", "build del frontend (tsc + vite)"),
        ):
            if frontend_trusted:
                results.append(_run([*npm, "--silent", "run", command], cwd=FRONTEND_ROOT, name=name))
            else:
                results.append(GateResult(name=name, status=SKIPPED, detail="audit de suministro no aprobado"))
    else:
        for name in (
            "npm audit (produccion)",
            "npm audit (incluye dev)",
            "pruebas de frontend (vitest)",
            "build del frontend (tsc + vite)",
        ):
            results.append(GateResult(name=name, status=SKIPPED,
                                      detail="arbol/gestor ausente o cadena de suministro no aprobada"))

    # 10. E2E
    corepack_e2e = _launcher("corepack")
    if args.with_e2e and corepack_e2e and frontend_trusted:
        results.append(_run([*corepack_e2e, "npm", "exec", "--no", "--", "playwright", "test"],
                            cwd=FRONTEND_ROOT, name="Playwright E2E"))
    elif args.with_e2e:
        results.append(GateResult(name="Playwright E2E", status=SKIPPED,
                                  detail="Corepack no disponible o cadena de suministro no aprobada"))
    else:
        results.append(GateResult(name="Playwright E2E", status=SKIPPED,
                                  detail="use --with-e2e con el backend arriba"))

    # 11. RAG
    rag_args = [py, "-m", "scripts.rag_eval", "--output", str(TEST_REPORTS / "rag_eval.json")]
    rag_args.append("--retrieval" if args.fast else "--full")
    rag_result = _run(rag_args, cwd=BACKEND_ROOT, name="evaluacion RAG (golden set)")
    if args.fast and rag_result.status == PASS:
        rag_result.status = INCOMPLETE
        rag_result.detail = "solo recuperacion; generacion con modelos reales no evaluada"
    results.append(rag_result)

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quality gate de Matrix RH")
    parser.add_argument("--fast", action="store_true",
                        help="RAG solo en modo recuperacion (sin generacion)")
    parser.add_argument("--with-e2e", action="store_true",
                        help="incluye Playwright (requiere el backend arriba)")
    parser.add_argument("--output", default=str(REPORTS / "quality-gate.json"))
    args = parser.parse_args(argv)

    results = build_gates(args)

    blocking_failures = [r for r in results if r.status == FAIL and r.blocking]
    incomplete_gates = [r for r in results if r.status in (SKIPPED, INCOMPLETE) and r.blocking]
    advisory_failures = [r for r in results if r.status == FAIL and not r.blocking]

    payload = {
        "project": "Matrix RH",
        "overall": FAIL if blocking_failures else INCOMPLETE if incomplete_gates else PASS,
        "blocking_failures": [r.name for r in blocking_failures],
        "incomplete_gates": [r.name for r in incomplete_gates],
        "advisory_failures": [r.name for r in advisory_failures],
        "gates": [asdict(r) for r in results],
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 72)
    print("  RESUMEN DEL QUALITY GATE")
    print("=" * 72)
    for result in results:
        marca = {PASS: "[ OK ]", FAIL: "[FAIL]", SKIPPED: "[SKIP]", INCOMPLETE: "[PART]"}[result.status]
        sufijo = "" if result.blocking else "  (informativo)"
        print(f"  {marca} {result.name:42s} {result.duration_s:>7.2f}s{sufijo}")
    print("=" * 72)
    print(f"  Reporte: {out}")
    print(f"  RESULTADO: {payload['overall']}")
    if advisory_failures:
        print("  Hallazgos informativos que requieren revision del Agente de Ciberseguridad:")
        for result in advisory_failures:
            print(f"    - {result.name}")
    print()

    return 1 if blocking_failures else 2 if incomplete_gates else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
