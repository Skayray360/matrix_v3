# Creado por Aldo Garcia.
"""Empaquetado del ZIP autocontenido de entrega.

Incluye fuente, lockfiles, build del frontend, migraciones, corpus original,
fixtures sinteticos, cuatro BAT, controlador PowerShell, un README y pruebas.

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
        "reports",  # snapshots e informes historicos; no forman parte de la entrega
        "playwright-report",
        "test-results",
        ".playwright",
        "matrix_rh_backend.egg-info",
        "build",  # artefactos temporales de setuptools, no frontend/dist
        "runtime", "logs", "secrets", "state", "models", "backups",
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
    "*.env",  # incluye backend/config/docker.env, generado para el perfil opcional
    "*.env.*",
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

GENERATED_CONFIGURATION = frozenset({
    "backend/config/mysql.ini", "backend/config/mysql-initialized.json",
    "backend/config/apache/matrix-rh.conf",
})

PACKAGE_ROOT_DIRS = frozenset({"knowledge-base", "backend", "frontend"})
PACKAGE_ROOT_FILES = OPERATOR_BATCH_FILES | {"README.md", "docker-compose.yml", ".htaccess"}


@dataclass
class PackageStats:
    files: int = 0
    bytes_uncompressed: int = 0
    skipped_dirs: int = 0
    skipped_files: int = 0


def _is_excluded_file(path: Path) -> bool:
    name = path.name
    return any(fnmatch.fnmatch(name.casefold(), pattern.casefold()) for pattern in EXCLUDED_PATTERNS)


def iter_package_files(root: Path) -> list[Path]:
    """Selecciona los archivos que entran al paquete."""
    seleccionados: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            continue
        if path.relative_to(root).as_posix() in GENERATED_CONFIGURATION:
            continue
        partes = path.relative_to(root).parts
        if any(
            parte in EXCLUDED_DIRS or parte.startswith(".venv.previous-") or parte.endswith(".egg-info")
            for parte in partes
        ):
            continue
        if _is_excluded_file(path):
            continue
        seleccionados.append(path)
    return seleccionados


def verify_release_layout(files: list[Path], root: Path) -> list[str]:
    """Rechaza estructuras antiguas sin borrar ni ocultar documentos del operador."""
    errors: list[str] = []
    for path in files:
        relative = path.relative_to(root)
        name = relative.as_posix()
        if ((len(relative.parts) == 1 and name not in PACKAGE_ROOT_FILES)
                or (len(relative.parts) > 1 and relative.parts[0] not in PACKAGE_ROOT_DIRS)):
            errors.append(f"Ruta fuera de la estructura de entrega: {name}")
        if path.name.casefold().startswith("readme") and name != "README.md":
            errors.append(f"README adicional: {name}; revise su contenido antes de consolidarlo")
        if path.suffix.casefold() == ".bat" and name not in OPERATOR_BATCH_FILES:
            errors.append(f"BAT adicional: {name}")
    return errors


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
            if relativa.as_posix() == "backend/release/SHA256SUMS.txt":
                continue  # Se regenera al empaquetar una carpeta previamente extraida.
            contenido = path.read_bytes()
            info = zipfile.ZipInfo.from_file(path, arcname=f"{raiz_interna}/{relativa.as_posix()}")
            archivo.writestr(info, contenido, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            checksums.append(f"{hashlib.sha256(contenido).hexdigest()}  {relativa.as_posix()}")
            stats.files += 1
            stats.bytes_uncompressed += len(contenido)
        manifiesto = ("\n".join(checksums) + "\n").encode("utf-8")
        archivo.writestr(f"{raiz_interna}/backend/release/SHA256SUMS.txt", manifiesto)
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
    layout_errors = verify_release_layout(files, root)
    if layout_errors:
        print("SE ABORTA EL EMPAQUETADO: la estructura no corresponde a la entrega limpia.")
        for problem in layout_errors[:20]:
            print(f"  - {problem}")
        return 1

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
        "README.md", ".htaccess", "backend/config/env.example", "docker-compose.yml",
        "instalar.bat", "iniciar.bat", "detener.bat", "diagnosticar.bat",
        "backend/scripts/windows/MatrixRH.ps1", "backend/scripts/bootstrap.py",
        "backend/scripts/windows/launch_process.py", "backend/config/runtime-manifest.json",
        "backend/config/apache/matrix-rh.conf.template",
        "backend/scripts/preflight.py", "backend/scripts/runtime_control.py",
        "backend/scripts/local_identity.py", "backend/config/knowledge-layout.yaml",
        "backend/pyproject.toml", "backend/uv.lock", "backend/Dockerfile",
        "backend/LICENSE", "frontend/package.json", "frontend/package-lock.json",
        "frontend/dist/index.html",
    )
    faltantes = [nombre for nombre in obligatorios if nombre not in relativas]
    if faltantes:
        print("SE ABORTA EL EMPAQUETADO: faltan archivos obligatorios de la entrega.")
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

    prohibidos = [name for name in relativas
                 if _is_excluded_file(Path(name)) or any(part in EXCLUDED_DIRS for part in Path(name).parts)]
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

    # El frontend compilado es obligatorio; la instalacion nativa no necesita Node/Corepack.

    stats = build_zip(root, destino, files)
    tamano = destino.stat().st_size

    print("")
    print(f"ZIP generado: {destino}")
    print(f"  archivos: {stats.files}")
    print(f"  tamano comprimido: {tamano / (1024 * 1024):.2f} MB")
    print(f"  tamano sin comprimir: {stats.bytes_uncompressed / (1024 * 1024):.2f} MB")
    print("")
    print("Prepare los prerrequisitos y la configuracion indicados en README.md.")
    print("Despues extraiga el paquete y ejecute instalar.bat para instalar e iniciar.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
