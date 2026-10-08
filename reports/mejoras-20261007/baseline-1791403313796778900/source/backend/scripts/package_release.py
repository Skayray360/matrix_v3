# Creado por Aldo Garcia.
"""Empaquetado del ZIP final de entrega (seccion 39.6).

Incluye fuente, lockfiles, build del frontend, migraciones, seeds sinteticos,
documentos de prueba sinteticos, BAT/PowerShell, guias operativas y pruebas.

Excluye, de forma verificable: ``.env`` real, ``.venv``, ``node_modules``, caches,
logs con datos, tokens, API keys, certificados privados, bases reales y los
volumenes runtime de Qdrant.

Antes de escribir el ZIP se ejecuta el escaner de secretos sobre el contenido
seleccionado. Si aparece un secreto real, **no se genera el paquete**.

Uso:
    python -m scripts.package_release
    python -m scripts.package_release --output ../MatrixRH-final.zip
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from scripts.preflight import OPERATOR_BATCH_FILES, PROJECT_ROOT, PreflightReport, check_integrity, project_version

#: Directorios que nunca entran al paquete.
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
        "var",  # estado runtime: uploads, logs, volumen de Qdrant
        "playwright-report",
        "test-results",
        ".playwright",
        "matrix_rh_backend.egg-info",
        "build",  # artefactos temporales de setuptools, no frontend/dist
        "offline-models",  # pesos importados al runtime; no son fuente del proyecto
        ".ollama",  # blobs sin extension y estado privado del runtime
        ".vscode",
        ".idea",
    }
)

#: Patrones de archivo excluidos. El `.env` real es el mas importante.
EXCLUDED_PATTERNS = (
    ".env",
    ".env.*",
    ".env-*",  # escrituras atomicas incompletas: nunca distribuir configuracion real
    "*.pem",
    "*.pfx",
    "*.key",
    "*.p12",
    "*.crt",
    "*.log",
    "*.pyc",
    "*.sqlite3",
    "*.sqlite",
    "*.db",
    "*.zip",
    "*.7z",
    "*.tar",
    "*.tar.gz",
    "*.bak",
    "*.tmp",
    "*.orig",
    "*.rej",
    "source-history.bundle",
    "Thumbs.db",
    ".DS_Store",
    # Cachés de herramientas: no son fuente y guardan rutas absolutas del equipo
    # donde se ejecutaron, que no tienen sentido en el equipo destino.
    ".coverage",
    ".coverage.*",
    "*.tsbuildinfo",
    # Pesos/artefactos de inferencia locales, incluso fuera del directorio de importacion.
    "*.gguf",
    "*.safetensors",
    "*.onnx",
    "*.pt",
    "*.pth",
    "pytorch_model*.bin",
    "ggml-model*.bin",
)

#: Excepciones a los patrones anteriores.
KEEP_PATTERNS = (".env.example",)

# Solo estas instrucciones de estado viajan en el ZIP; los datos runtime se
# respaldan por separado y nunca se convierten en fuente distribuible.
RUNTIME_READMES = frozenset({
    "var/README.md", "var/logs/README.md", "var/qdrant/README.md", "var/uploads/README.md",
})


@dataclass
class PackageStats:
    files: int = 0
    bytes_uncompressed: int = 0
    skipped_dirs: int = 0
    skipped_files: int = 0


def _is_excluded_file(path: Path) -> bool:
    name = path.name
    if any(fnmatch.fnmatch(name, keep) for keep in KEEP_PATTERNS):
        return False
    return any(fnmatch.fnmatch(name.casefold(), pattern.casefold()) for pattern in EXCLUDED_PATTERNS)


def iter_package_files(root: Path) -> list[Path]:
    """Selecciona los archivos que entran al paquete."""
    seleccionados: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        partes = path.relative_to(root).parts
        relative_name = path.relative_to(root).as_posix()
        if relative_name in RUNTIME_READMES:
            seleccionados.append(path)
            continue
        if any(
            parte in EXCLUDED_DIRS or parte.startswith(".venv.previous-") or parte.endswith(".egg-info")
            for parte in partes
        ):
            continue
        if _is_excluded_file(path):
            continue
        # Las guias mantienen los nombres y la organizacion de matrix_v2.
        # Los informes vigentes viajan con la entrega para que sus resultados
        # puedan revisarse; salidas temporales y auditorias archivadas no.
        if partes[0] == "reports" and (
            path.suffix.lower() != ".md"
            or any(part.casefold() in {"historico", "history", "archived", "archivo"} for part in partes[1:-1])
        ):
            continue
        seleccionados.append(path)
    return seleccionados


def verify_no_secrets(files: list[Path], root: Path) -> list[str]:
    """Ejecuta el escaner de secretos sobre el contenido seleccionado."""
    from scripts.secrets_scan import scan

    relativas = {p.relative_to(root).as_posix() for p in files}
    hallazgos = scan(root)
    return [
        f"{f.file}:{f.line} [{f.rule}]"
        for f in hallazgos
        if f.classification == "real" and f.file in relativas
    ]


def build_zip(root: Path, destino: Path, files: list[Path]) -> PackageStats:
    stats = PackageStats()
    destino.parent.mkdir(parents=True, exist_ok=True)
    # La carpeta instalada es estable aunque cambie la revision interna del codigo.
    raiz_interna = root.name if root.name.startswith("matrix-rh-") else f"matrix-rh-{project_version(root)}"
    checksums: list[str] = []

    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archivo:
        for path in files:
            relativa = path.relative_to(root)
            if relativa.as_posix() == "SHA256SUMS.txt":
                continue  # Se regenera al empaquetar una carpeta previamente extraida.
            contenido = path.read_bytes()
            info = zipfile.ZipInfo.from_file(path, arcname=f"{raiz_interna}/{relativa.as_posix()}")
            archivo.writestr(info, contenido, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            checksums.append(f"{hashlib.sha256(contenido).hexdigest()}  {relativa.as_posix()}")
            stats.files += 1
            stats.bytes_uncompressed += len(contenido)
        manifiesto = ("\n".join(checksums) + "\n").encode("utf-8")
        archivo.writestr(f"{raiz_interna}/SHA256SUMS.txt", manifiesto)
        stats.files += 1
        stats.bytes_uncompressed += len(manifiesto)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Empaqueta el ZIP de entrega de Matrix RH")
    parser.add_argument("--output", default="", help="ruta del ZIP (por defecto, junto al proyecto)")
    parser.add_argument(
        "--skip-secret-scan",
        action="store_true",
        help="omite la verificacion de secretos (NO recomendado)",
    )
    args = parser.parse_args(argv)

    root = PROJECT_ROOT
    package_name = root.name if root.name.startswith("matrix-rh-") else f"matrix-rh-{project_version(root)}"
    destino = Path(args.output) if args.output else root.parent / f"{package_name}.zip"

    integrity = PreflightReport()
    check_integrity(integrity, root=root)
    if not integrity.ok:
        print("SE ABORTA EL EMPAQUETADO: el paquete principal o la fuente operativa son incoherentes.")
        for item in integrity.checks:
            print(f"  - {item.detail}")
        return 1

    print(f"Proyecto: {root}")
    files = [path for path in iter_package_files(root) if path.resolve() != destino.resolve()]
    print(f"Archivos seleccionados: {len(files)}")

    if not args.skip_secret_scan:
        print("Verificando que no viaje ningun secreto real...")
        problemas = verify_no_secrets(files, root)
        if problemas:
            print("SE ABORTA EL EMPAQUETADO: hay secretos reales en el contenido seleccionado.")
            for problema in problemas:
                print(f"  - {problema}")
            return 1
        print("  0 secretos reales.")

    # Comprobaciones de contenido obligatorio.
    relativas = {p.relative_to(root).as_posix() for p in files}
    obligatorios = (
        "README.md",
        ".htaccess",
        ".env.example",
        "docker-compose.yml",
        "INSTALAR_MATRIX_RH.bat",
        "INICIAR_MATRIX_RH.bat",
        "DETENER_MATRIX_RH.bat",
        "DIAGNOSTICO_MATRIX_RH.bat",
        "windows/Upgrade-MatrixRH.ps1",
        "backend/scripts/release_transfer.py",
        "backend/scripts/configuration_upgrade.py",
        "windows/TestModels-MatrixRH.ps1",
        "backend/scripts/runtime_control.py",
        "backend/scripts/clean_legacy_layout.py",
        "backend/migrations/0009_answer_provenance.sql",
        "config/knowledge-layout.yaml",
        "docs/INSTALACION.md",
        f"reports/VALIDACION_{project_version(root)}.md",
        "backend/pyproject.toml",
        "backend/Dockerfile",
        "frontend/package.json",
        # Lockfile canonico (npm): sin el, la instalacion en destino no es
        # reproducible. Se exige para no publicar un ZIP sin lockfile.
        "frontend/package-lock.json",
        "frontend/dist/index.html",
        "backend/uv.lock",
        "docs/README.md",
        "docs/ARCHITECTURE.md",
        "docs/TESTING.md",
        "docs/RUNBOOK.md",
        "docs/RAG_DESIGN.md",
        "docs/SECURITY.md",
        "docs/AUTHENTICATION_AUTHORIZATION.md",
        "docs/TROUBLESHOOTING.md",
        "docs/MODELOS_Y_RENDIMIENTO.md",
        "docs/assets/estructura-matrix-rh.svg",
        "backend/scripts/diagnosticar_rag.py",
        "docs/integrations/README.md",
    )
    faltantes = [nombre for nombre in obligatorios if nombre not in relativas]
    if faltantes:
        print("SE ABORTA EL EMPAQUETADO: faltan archivos obligatorios en la raiz.")
        for nombre in faltantes:
            print(f"  - {nombre}")
        return 1

    batch_files = {name for name in relativas if name.lower().endswith(".bat")}
    if batch_files != OPERATOR_BATCH_FILES:
        print("SE ABORTA EL EMPAQUETADO: deben existir solo los cuatro BAT de operacion en la raiz.")
        return 1

    from scripts.verify_documentation import check_links

    broken_links = check_links(root, files)
    if broken_links:
        print("SE ABORTA EL EMPAQUETADO: hay enlaces locales a contenido no distribuido.")
        for error in broken_links:
            print(f"  - {error}")
        return 1

    prohibidos = [
        r for r in relativas
        if r == ".env" or (r.startswith("var/") and r not in RUNTIME_READMES) or "/node_modules/" in r
    ]
    if prohibidos:
        print("SE ABORTA EL EMPAQUETADO: contenido prohibido seleccionado.")
        for nombre in prohibidos[:10]:
            print(f"  - {nombre}")
        return 1

    # El gestor del frontend (npm) debe fijarse con hash de integridad para que
    # corepack verifique criptograficamente el binario que descarga. Sin el hash,
    # la provision del gestor solo confia en TLS y el registro: no debe publicarse
    # un release asi. Bloqueante a proposito (en desarrollo es solo un aviso).
    from scripts.verify_supply_chain import package_manager_pin

    pm, con_hash = package_manager_pin(root / "frontend")
    if not con_hash:
        print("SE ABORTA EL EMPAQUETADO: el gestor del frontend no fija hash de integridad.")
        print(f"  packageManager actual: {pm or '(ausente)'}")
        print("  Fije 'npm@<version>+sha512.<hash>' en frontend/package.json.")
        print("  Obtenga el hash oficial verificado y ejecute el gestor mediante corepack npm.")
        return 1

    # El frontend compilado es obligatorio arriba; -SkipFrontend permite
    # reutilizarlo en destino cuando no se instala Node/Corepack.

    stats = build_zip(root, destino, files)
    tamano = destino.stat().st_size

    print("")
    print(f"ZIP generado: {destino}")
    print(f"  archivos: {stats.files}")
    print(f"  tamano comprimido: {tamano / (1024 * 1024):.2f} MB")
    print(f"  tamano sin comprimir: {stats.bytes_uncompressed / (1024 * 1024):.2f} MB")
    print("")
    print("Prepare los prerrequisitos y la configuracion indicados en README.md.")
    print("Despues extraiga el paquete y ejecute INSTALAR_MATRIX_RH.bat para instalar e iniciar.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
