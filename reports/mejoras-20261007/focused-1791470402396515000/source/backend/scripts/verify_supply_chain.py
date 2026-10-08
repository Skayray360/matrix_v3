# Creado por Aldo Garcia.
"""Verificacion de cadena de suministro del arbol de dependencias.

Motivado por los gusanos autopropagables de npm: paquetes comprometidos que
ejecutan codigo en el ciclo de vida de instalacion, roban credenciales del
entorno y republican paquetes del mantenedor afectado.

Comprueba cuatro cosas sobre el arbol REALMENTE instalado, no sobre el
manifiesto declarado:

1. **Lista de bloqueo** -- ningun paquete/version de
   ``config/supply_chain_denylist.yaml`` esta presente, a ninguna profundidad.
2. **Scripts de instalacion** -- ningun paquete declara ``preinstall``,
   ``install`` o ``postinstall``. Es el vector por el que se propaga el gusano.
3. **Indicadores de compromiso** -- nombres de archivo y patrones de contenido
   asociados a exfiltracion de credenciales.
4. **Instalacion reproducible** -- existe el lockfile del gestor declarado
   (npm en la entrega consolidada) y ``.npmrc`` declara ``ignore-scripts=true``.

Las comprobaciones 1-3 recorren el arbol ``node_modules`` REAL y son agnosticas
al gestor: funcionan igual con pnpm o npm. Solo la comprobacion 4 es especifica
del lockfile.

Uso:
    python -m scripts.verify_supply_chain
    python -m scripts.verify_supply_chain --json
    python -m scripts.verify_supply_chain --allow-install-scripts esbuild
"""

from __future__ import annotations

import argparse
import codecs
import fnmatch
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from app.common.errors import ConfigurationError

# Este gate tambien corre ANTES del build Docker, sin importar la configuracion
# de la aplicacion ni requerir sus servicios. Solo necesita Python y PyYAML.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DENYLIST_PATH = PROJECT_ROOT / "config" / "supply_chain_denylist.yaml"
FRONTEND = PROJECT_ROOT / "frontend"
NODE_MODULES = FRONTEND / "node_modules"

#: Extensiones que se inspeccionan en busca de patrones de exfiltracion.
SCANNED_SUFFIXES = frozenset({
    ".js", ".cjs", ".mjs", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
    ".sh", ".ps1", ".bat", ".cmd", ".py", "",
})
# Tamano de bloque, NO limite de archivo: el codigo minificado tambien puede
# contener IOC. El solapamiento cubre los patrones acotados de nuestra denylist.
MAX_SCAN_BYTES = 2 * 1024 * 1024
SCAN_OVERLAP = 4096


@dataclass
class Finding:
    severity: str
    kind: str
    detail: str
    location: str = ""


@dataclass
class Report:
    checked_packages: int = 0
    findings: list[Finding] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "critical"]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checked_packages": self.checked_packages,
            "checks": self.checks,
            "blocking": [asdict(f) for f in self.blocking],
            "warnings": [asdict(f) for f in self.findings if f.severity != "critical"],
        }


def load_denylist(path: Path | None = None) -> dict:
    target = path or DENYLIST_PATH
    if not target.exists():
        raise ConfigurationError(f"No existe la lista de bloqueo: {target}")
    try:
        return yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(
            f"La lista de bloqueo es invalida: {target.name}", detail=str(exc)
        ) from exc


def iter_installed_packages(root: Path, report: Report | None = None):  # noqa: ANN201
    """Genera ``(nombre, version, ruta)`` de cada paquete instalado.

    Se recorre ``node_modules`` completo, incluidos los anidados: un paquete
    comprometido puede aparecer como dependencia transitiva profunda y no en el
    primer nivel.
    """
    if not root.is_dir():
        return
    for manifest in root.rglob("package.json"):
        # Solo raices reales: un paquete SI puede llamarse "lib" o "test".
        parent = manifest.parent
        if not (parent.parent.name == "node_modules" or (
            parent.parent.name.startswith("@") and parent.parent.parent.name == "node_modules"
        )):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"]:
                raise ValueError("manifiesto sin identidad valida")
        except (OSError, UnicodeError, ValueError) as exc:
            if report is not None:
                report.findings.append(Finding(
                    "critical", "manifiesto_instalado_invalido", type(exc).__name__, str(manifest)
                ))
            continue
        name = data.get("name")
        if not isinstance(name, str) or not name:
            continue
        yield name, str(data.get("version", "")), manifest.parent, data


def _version_matches(version: str, allowed) -> bool:  # noqa: ANN001
    if allowed == "*" or allowed is None:
        return True
    if isinstance(allowed, str):
        return version == allowed
    return version in set(allowed)


def check_denylist(report: Report, denylist: dict, packages: list[tuple]) -> None:
    """Comprueba que ningun paquete comprometido este presente."""
    bloqueados = 0
    for incident in denylist.get("incidents", []):
        incident_id = incident.get("id", "sin-id")
        for entry in incident.get("packages", []):
            nombre = entry.get("name")
            versiones = entry.get("versions", "*")
            for pkg_name, pkg_version, pkg_path, _ in packages:
                if pkg_name == nombre and _version_matches(pkg_version, versiones):
                    bloqueados += 1
                    report.findings.append(
                        Finding(
                            severity="critical",
                            kind="paquete_en_lista_de_bloqueo",
                            detail=f"{pkg_name}@{pkg_version} ({incident_id})",
                            location=str(pkg_path),
                        )
                    )
    report.checks["lista_de_bloqueo"] = "OK" if bloqueados == 0 else f"FAIL ({bloqueados})"


def check_install_scripts(report: Report, packages: list[tuple], allowed: set[str]) -> None:
    """Detecta paquetes con scripts de ciclo de vida de instalacion."""
    encontrados = []
    for pkg_name, pkg_version, pkg_path, data in packages:
        scripts = data.get("scripts") or {}
        if not isinstance(scripts, dict):
            continue
        presentes = [s for s in ("preinstall", "install", "postinstall") if scripts.get(s)]
        if not presentes:
            continue
        if pkg_name in allowed:
            report.findings.append(
                Finding(
                    severity="info",
                    kind="script_de_instalacion_permitido",
                    detail=f"{pkg_name}@{pkg_version}: {', '.join(presentes)}",
                    location=str(pkg_path),
                )
            )
            continue
        encontrados.append(pkg_name)
        report.findings.append(
            Finding(
                # No es critico por si mismo: es critico que se EJECUTE. Con
                # ignore-scripts=true no se ejecuta. Se reporta como aviso para
                # que quede a la vista en cada instalacion.
                severity="warning",
                kind="script_de_instalacion",
                detail=f"{pkg_name}@{pkg_version}: {', '.join(presentes)}",
                location=str(pkg_path),
            )
        )
    report.checks["scripts_de_instalacion"] = (
        "OK (ninguno)" if not encontrados else f"REVISAR ({len(encontrados)})"
    )


def check_indicators(report: Report, denylist: dict, root: Path) -> None:
    """Busca indicadores de compromiso en el arbol instalado."""
    indicadores = denylist.get("indicators", {})
    patrones_nombre = indicadores.get("filenames", [])
    patrones_contenido = [re.compile(p) for p in indicadores.get("content_patterns", [])]

    coincidencias = 0

    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
                coincidencias += 1
                report.findings.append(Finding("critical", "enlace_fuera_del_arbol", path.name, str(path)))
                continue
            if not path.is_file():
                continue
            if any(fnmatch.fnmatch(path.name.lower(), p.lower()) for p in patrones_nombre):
                coincidencias += 1
                report.findings.append(
                    Finding(
                        severity="critical",
                        kind="ioc_nombre_de_archivo",
                        detail=path.name,
                        location=str(path),
                    )
                )
                continue
            if path.suffix.lower() not in SCANNED_SUFFIXES:
                continue
            try:
                with path.open("rb") as stream:
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
                    previous = ""
                    while raw := stream.read(MAX_SCAN_BYTES):
                        contenido = previous + decoder.decode(raw)
                        matched = next((p for p in patrones_contenido if p.search(contenido)), None)
                        if matched:
                            coincidencias += 1
                            report.findings.append(Finding(
                                "critical", "ioc_contenido", matched.pattern, str(path)
                            ))
                            break
                        previous = contenido[-SCAN_OVERLAP:]
            except OSError as exc:
                coincidencias += 1
                report.findings.append(Finding(
                    "critical", "archivo_no_inspeccionado", type(exc).__name__, str(path)
                ))

    report.checks["indicadores_de_compromiso"] = (
        "OK" if coincidencias == 0 else f"FAIL ({coincidencias})"
    )


def check_reproducible_install(report: Report) -> None:
    """Verifica lockfile y que los scripts esten bloqueados por configuracion.

    El lockfile debe corresponder al packageManager real. Se admite pnpm para
    verificar arboles historicos, sin confundirlo con el npm de esta entrega.
    """
    manager, _ = package_manager_pin()
    lock_name = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml"}.get(manager.split("@", 1)[0])
    if lock_name and (FRONTEND / lock_name).is_file():
        report.checks["lockfile"] = f"OK ({lock_name})"
    else:
        report.checks["lockfile"] = "FAIL"
        report.findings.append(
            Finding(
                severity="critical",
                kind="sin_lockfile",
                detail=f"falta el lockfile del gestor declarado ({manager or 'ausente'}): "
                       "la instalacion no es reproducible",
            )
        )

    npmrc = FRONTEND / ".npmrc"
    contenido = npmrc.read_text(encoding="utf-8") if npmrc.exists() else ""
    declared = re.findall(r"^\s*ignore-scripts\s*=\s*([^\r\n;#]*)", contenido, re.MULTILINE)
    if len(declared) == 1 and declared[0].strip().lower() == "true":
        report.checks["ignore_scripts"] = "OK"
    else:
        report.checks["ignore_scripts"] = "FAIL"
        report.findings.append(
            Finding(
                severity="critical",
                kind="scripts_de_instalacion_habilitados",
                detail="frontend/.npmrc no declara ignore-scripts=true",
            )
        )


#: Regex del campo ``packageManager`` con hash de integridad de corepack, p. ej.
#: ``pnpm@9.15.9+sha512.<hash>``. El sufijo ``+sha...`` es lo que hace que
#: corepack VERIFIQUE criptograficamente el binario descargado.
_PM_WITH_HASH = re.compile(
    r"^(?:npm|pnpm)@\d+\.\d+\.\d+\+(?:sha224\.[a-fA-F0-9]{56}|"
    r"sha256\.[a-fA-F0-9]{64}|sha384\.[a-fA-F0-9]{96}|sha512\.[a-fA-F0-9]{128})$"
)


def package_manager_pin(frontend: Path | None = None) -> tuple[str, bool]:
    """Devuelve ``(valor_packageManager, tiene_hash_de_integridad)``.

    Reutilizable por el empaquetado de release, que exige el hash de forma
    bloqueante.
    """
    manifest = (frontend or FRONTEND) / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", False
    pm = data.get("packageManager")
    if not isinstance(pm, str) or not pm:
        return "", False
    return pm, bool(_PM_WITH_HASH.match(pm))


def check_package_manager_pin(report: Report) -> None:
    """El gestor debe fijarse en package.json con hash de integridad.

    corepack descarga el binario del gestor la primera vez. Sin el hash
    (``npm@x.y.z+sha512...``) la descarga solo se apoya en TLS y el registro;
    con el hash, corepack lo verifica criptograficamente. Se reporta como AVISO
    (no rompe desarrollo). El empaquetado de release lo exige de forma
    bloqueante: no debe publicarse sin integridad del gestor fijada.
    """
    pm, con_hash = package_manager_pin()
    if not pm:
        report.checks["gestor_fijado"] = "REVISAR (sin packageManager)"
        report.findings.append(
            Finding(
                severity="warning",
                kind="gestor_no_fijado",
                detail="frontend/package.json no declara packageManager (fije npm@x.y.z+sha512...)",
            )
        )
    elif not con_hash:
        report.checks["gestor_fijado"] = "REVISAR (sin hash de integridad)"
        report.findings.append(
            Finding(
                severity="warning",
                kind="gestor_sin_hash_integridad",
                detail=f"packageManager={pm} sin sufijo +sha; corepack no verifica el binario descargado",
            )
        )
    else:
        report.checks["gestor_fijado"] = "OK (con hash de integridad)"


def check_npm_lock(report: Report, denylist: dict, packages: list[tuple], *, lock_only: bool) -> None:
    """Bloquea versiones comprometidas ANTES de descargar y contrasta identidad.

    npm ci verifica SRI de los tarballs; este control NO certifica cada byte del
    arbol ya extraido. No se aceptan links ni dependencias sin HTTPS/SRI en esta
    distribucion. Las dependencias opcionales de otra plataforma pueden faltar.
    """
    manager, _ = package_manager_pin()
    if not manager.startswith("npm@"):
        report.checks["lock_contenido"] = "REVISAR (historico pnpm, no valida lock npm)"
        return
    target = FRONTEND / "package-lock.json"
    before = len(report.blocking)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if data.get("lockfileVersion") not in (2, 3) or not isinstance(data.get("packages"), dict):
            raise ValueError("se requiere lock npm v2/v3 con packages")
        entries = data["packages"]
        manifest = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
        for group in ("dependencies", "devDependencies", "optionalDependencies"):
            if entries.get("", {}).get(group, {}) != manifest.get(group, {}):
                raise ValueError(f"{group} difiere entre package.json y lockfile")
    except (OSError, ValueError, AttributeError) as exc:
        report.findings.append(Finding("critical", "lock_invalido", str(exc), str(target)))
        report.checks["lock_contenido"] = "FAIL"
        return
    locked = []
    installed = {path.relative_to(FRONTEND).as_posix(): (name, version) for name, version, path, _ in packages}
    for location, entry in entries.items():
        if not location:
            continue
        if not isinstance(entry, dict) or not location.startswith("node_modules/") or ".." in Path(location).parts:
            report.findings.append(Finding("critical", "lock_entrada_invalida", location, str(target)))
            continue
        slot = location.rsplit("node_modules/", 1)[-1]
        name = entry.get("name", slot)
        version = entry.get("version", "")
        resolved = entry.get("resolved", "")
        integrity = entry.get("integrity", "")
        if (entry.get("link") or not isinstance(version, str) or not version
                or not isinstance(resolved, str) or not resolved.startswith("https://")
                or not isinstance(integrity, str)
                or not re.fullmatch(r"sha(?:256|384|512)-[A-Za-z0-9+/]+={0,2}", integrity)):
            report.findings.append(Finding("critical", "lock_sin_integridad", location, str(target)))
        path = FRONTEND / location
        locked.append((name, version, path, entry))
        if not lock_only:
            if path.is_symlink():
                report.findings.append(Finding("critical", "paquete_enlazado", location, str(path)))
            actual = installed.get(location)
            if actual is not None and actual != (name, version):
                report.findings.append(Finding(
                    "critical", "identidad_difiere_del_lock", f"{location}: {actual} != {(name, version)}", str(path)
                ))
            elif actual is None and not entry.get("optional"):
                report.findings.append(Finding("critical", "paquete_del_lock_ausente", location, str(path)))
    if not lock_only:
        for location in installed.keys() - entries.keys():
            report.findings.append(Finding("critical", "paquete_fuera_del_lock", location, str(FRONTEND / location)))
    lock_report = Report()
    check_denylist(lock_report, denylist, locked)
    report.findings.extend(lock_report.findings)
    report.checks["lista_de_bloqueo_lock"] = lock_report.checks["lista_de_bloqueo"]
    report.checks["lock_contenido"] = f"OK ({len(locked)} paquetes)" if len(report.blocking) == before else "FAIL"


def run(*, allowed_script_packages: set[str] | None = None, lock_only: bool = False) -> Report:
    report = Report()
    denylist = load_denylist()

    check_reproducible_install(report)
    check_package_manager_pin(report)
    if not package_manager_pin()[1]:
        report.findings.append(Finding(
            "critical", "gestor_sin_integridad_verificable", "el gate requiere packageManager con hash completo"
        ))
    if lock_only:
        report.checks["arbol_instalado"] = "REVISAR (solo lock; pendiente inspeccionar tras npm ci)"
        check_npm_lock(report, denylist, [], lock_only=True)
        return report

    if NODE_MODULES.is_symlink():
        report.findings.append(Finding("critical", "arbol_enlazado", "node_modules no debe ser un enlace"))
        return report

    packages = list(iter_installed_packages(NODE_MODULES, report))
    report.checked_packages = len(packages)

    if not packages:
        report.checks["arbol_instalado"] = "AUSENTE (ejecute 'npm ci --ignore-scripts' en frontend/)"
        report.findings.append(Finding(
            "critical", "arbol_instalado_ausente", "no se puede aprobar un arbol no inspeccionado"
        ))
    else:
        report.checks["arbol_instalado"] = f"OK ({len(packages)} manifiestos)"

    check_denylist(report, denylist, packages)
    check_install_scripts(report, packages, allowed_script_packages or set())
    check_indicators(report, denylist, NODE_MODULES)
    check_npm_lock(report, denylist, packages, lock_only=False)
    return report


def render(report: Report) -> str:
    lineas = ["", "=== CADENA DE SUMINISTRO (npm) ===", ""]
    for nombre, estado in report.checks.items():
        marca = "[ OK ]" if estado.startswith("OK") else "[FAIL]"
        if estado.startswith(("AUSENTE", "REVISAR")):
            marca = "[WARN]"
        lineas.append(f"{marca} {nombre:28s} {estado}")

    avisos_scripts = [f for f in report.findings if f.severity == "warning" and f.kind == "script_de_instalacion"]
    if avisos_scripts:
        lineas.append("")
        lineas.append(f"Paquetes con script de instalacion ({len(avisos_scripts)}):")
        for aviso in avisos_scripts[:20]:
            lineas.append(f"    {aviso.detail}")
        lineas.append("    (no se ejecutan: ignore-scripts=true)")

    otros_avisos = [f for f in report.findings if f.severity == "warning" and f.kind != "script_de_instalacion"]
    if otros_avisos:
        lineas.append("")
        lineas.append(f"Avisos ({len(otros_avisos)}):")
        for aviso in otros_avisos[:20]:
            lineas.append(f"    [{aviso.kind}] {aviso.detail}")

    if report.blocking:
        lineas.append("")
        lineas.append("HALLAZGOS BLOQUEANTES:")
        for hallazgo in report.blocking:
            lineas.append(f"    [{hallazgo.kind}] {hallazgo.detail}")
            if hallazgo.location:
                lineas.append(f"        {hallazgo.location}")

    lineas.append("")
    outcome = "SIN HALLAZGOS BLOQUEANTES EN LOS CONTROLES EJECUTADOS" if report.ok else "BLOQUEADO: REQUIERE REVISION"
    lineas.append("RESULTADO: " + outcome)
    lineas.append("")
    return "\n".join(lineas)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verificacion de cadena de suministro de Matrix RH")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--lock-only", action="store_true", help="verifica el lock antes de descargar; no certifica node_modules"
    )
    parser.add_argument("--frontend", type=Path, help="directorio de frontend a inspeccionar")
    parser.add_argument(
        "--allow-install-scripts",
        nargs="*",
        default=[],
        help="paquetes cuyo script de instalacion se acepta explicitamente",
    )
    args = parser.parse_args(argv)
    if args.frontend:
        global FRONTEND, NODE_MODULES
        FRONTEND = args.frontend.resolve()
        NODE_MODULES = FRONTEND / "node_modules"
    report = run(allowed_script_packages=set(args.allow_install_scripts), lock_only=args.lock_only)
    payload = report.as_dict()

    if args.output:
        destino = Path(args.output)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else render(report))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
