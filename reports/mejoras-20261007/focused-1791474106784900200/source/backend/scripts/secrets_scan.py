# Creado por Aldo Garcia.
"""Escaner de secretos del repositorio (Agente 6).

Recorre el arbol del proyecto buscando credenciales reales en codigo, plantillas,
configuracion, Dockerfiles, documentacion, frontend compilado y logs.

Distingue tres clases de hallazgo:

* ``real``        -- posible secreto productivo. Cualquiera bloquea la entrega.
* ``synthetic``   -- la credencial sintetica de prueba declarada en la
  especificacion (``Matrix RH``), permitida solo en documentacion, seed y
  fixtures marcados.
* ``placeholder`` -- valores ficticios de ``.env.example`` (``replace_me``).

No sustituye a ``gitleaks``/``detect-secrets``: los complementa con reglas
especificas de este proyecto y funciona sin red ni Git, que es lo que necesita el
instalador Windows.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import PROJECT_ROOT

#: Directorios que no se escanean (generados o de terceros).
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
        "qdrant",
        ".playwright",
        "playwright-report",
        "test-results",
    }
)

#: Extensiones binarias que no se leen como texto.
BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".docx", ".xlsx", ".zip", ".pyc", ".woff", ".woff2"}
)

MAX_FILE_BYTES = 3 * 1024 * 1024

#: Credencial sintetica declarada por la especificacion (no es un secreto real).
# noqa S105: es la credencial sintetica declarada por la especificacion, y este
# escaner necesita conocerla precisamente para clasificarla como no productiva.
SYNTHETIC_PASSWORD = "Matrix RH"  # noqa: S105

#: Archivos donde la credencial sintetica esta permitida por diseno.
SYNTHETIC_ALLOWED = (
    "seeds/identity_seed.py",
    "tests/",
    "docs/",
    "README.md",
    "frontend/tests/",
    "scripts/secrets_scan.py",
    "scripts/smoke_authorization.py",
)

#: Salidas del propio escaner. Sin esta exclusion, cada ejecucion detectaria los
#: extractos redactados de la ejecucion anterior y el reporte creceria solo.
#: El resto de `reports/` SI se escanea: un secreto filtrado a un reporte de
#: pruebas o de auditoria debe detectarse.
SELF_OUTPUT_PATHS = frozenset({"reports/security/secrets_scan.json", "reports/security/bandit.json"})

#: Valores que son evidentemente marcadores, no credenciales.
PLACEHOLDERS = frozenset(
    {
        "replace_me",
        "changeme",
        "your_secret_here",
        "xxx",
        "",
        "none",
        # Palabras genericas usadas en plantillas de documentacion.
        "clave",
        "password",
        "pass",
        "usuario",
        "user",
        "contrasena",
        "tu_clave",
        "secreto",
    }
)

#: Referencias a variables (``${VAR}``, ``%VAR%``) y marcadores de documentacion
#: (``<clave>``). No son valores: son huecos que el operador rellena.
PLACEHOLDER_SHAPE = re.compile(r"^(\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|%[^%]+%|<[^>]+>|\{\{[^}]+\}\})$")

#: Marcador de supresion inline. Debe ir acompanado de una justificacion:
#:     algo = "valor"  # secrets-scan: allow (fixture sintetico de la prueba X)
#: Cada supresion queda registrada en el reporte para que el Agente de
#: Ciberseguridad pueda revisarla una por una.
ALLOW_MARKER = re.compile(r"secrets-scan:\s*allow\s*(?:\((?P<reason>[^)]*)\))?")

#: Un valor que es codigo (una lectura de configuracion, no un literal) no es un
#: secreto incrustado. La lista de prefijos es cerrada a proposito: una regla
#: laxa clasificaba como "codigo" cualquier cadena con un punto, incluido un JWT.
CODE_EXPRESSION = re.compile(
    r"^(self|cls|settings|config|conf|options|args|os\.environ|process\.env|import\.meta\.env"
    r"|get_settings|Settings)\b[\w.\[\]()\"']*$"
)


@dataclass
class Finding:
    file: str
    line: int
    rule: str
    severity: str
    classification: str
    excerpt: str


#: (nombre, patron, severidad)
RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
        "critical",
    ),
    (
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "critical",
    ),
    (
        "generic_api_key",
        re.compile(r"(?i)\b(api[_-]?key|apikey|secret[_-]?key)\b\s*[:=]\s*[\"']([^\"'\s]{16,})[\"']"),
        "high",
    ),
    (
        "client_secret",
        re.compile(r"(?i)\bclient[_-]?secret\b\s*[:=]\s*[\"']?([^\"'\s#]{8,})"),
        "critical",
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
        "high",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
        "high",
    ),
    (
        "connection_string_with_password",
        re.compile(r"\b[a-z0-9+.\-]+://[^\s:/@\"']+:([^\s@\"']{3,})@[^\s\"']+"),
        "critical",
    ),
    (
        "hardcoded_password_assignment",
        re.compile(r"(?i)\b(password|passwd|pwd)\b\s*[:=]\s*[\"']([^\"'\n]{4,})[\"']"),
        "high",
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
        "critical",
    ),
)


def _iter_files(root: Path):  # noqa: ANN202
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


def _classify(
    match_value: str, relative: str, rule: str, line: str = "", previous_line: str = ""
) -> str:
    """Clasifica un hallazgo como real, sintetico, placeholder o suprimido.

    El marcador de supresion se admite en la propia linea o en la inmediatamente
    anterior, porque una cadena larga suele ocupar la linea entera y el
    comentario no cabe al final.
    """
    if ALLOW_MARKER.search(line) or ALLOW_MARKER.search(previous_line):
        return "allowlisted"

    value = (match_value or "").strip().strip("\"'")

    # Una asignacion cuyo valor es una expresion (lectura de configuracion,
    # variable, llamada) no incrusta ningun secreto en el codigo.
    if CODE_EXPRESSION.match(value):
        return "code_reference"

    if (
        value.lower() in PLACEHOLDERS
        or "replace_me" in value.lower()
        or PLACEHOLDER_SHAPE.match(value)
    ):
        return "placeholder"
    if value == SYNTHETIC_PASSWORD and any(allowed in relative for allowed in SYNTHETIC_ALLOWED):
        return "synthetic"
    # Un DSN sin contrasena (root:@host) no expone nada.
    if rule == "connection_string_with_password" and value in ("", "@"):
        return "placeholder"
    # Las plantillas de ejemplo son declaradamente ficticias.
    if relative.endswith(".env.example"):
        return "placeholder"
    return "real"


def scan(root: Path | None = None) -> list[Finding]:
    """Ejecuta todas las reglas sobre el arbol del proyecto."""
    base = root or PROJECT_ROOT
    findings: list[Finding] = []

    for path in _iter_files(base):
        relative = path.relative_to(base).as_posix()
        if relative in SELF_OUTPUT_PATHS:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        lines = content.splitlines()
        for index, line in enumerate(lines):
            line_number = index + 1
            if len(line) > 4000:
                continue
            previous_line = lines[index - 1] if index > 0 else ""
            for rule_name, pattern, severity in RULES:
                match = pattern.search(line)
                if not match:
                    continue
                captured = match.group(match.lastindex) if match.lastindex else match.group(0)
                classification = _classify(captured, relative, rule_name, line, previous_line)
                findings.append(
                    Finding(
                        file=relative,
                        line=line_number,
                        rule=rule_name,
                        severity=severity,
                        classification=classification,
                        # Nunca se imprime el valor: solo el contexto redactado.
                        excerpt=line.strip()[:60].replace(captured, "[MATCH]")[:80],
                    )
                )

        # El .env real jamas debe formar parte del paquete.
        if relative == ".env":
            findings.append(
                Finding(
                    file=relative,
                    line=0,
                    rule="env_file_present",
                    severity="critical",
                    classification="real",
                    excerpt="el archivo .env real no debe distribuirse",
                )
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Escaneo de secretos de Matrix RH")
    parser.add_argument("--root", default="", help="raiz a escanear (por defecto, el proyecto)")
    parser.add_argument("--output", default="", help="ruta del reporte JSON")
    parser.add_argument(
        "--allow-env", action="store_true", help="no marcar la presencia de .env (uso local)"
    )
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else PROJECT_ROOT
    findings = scan(root)
    if args.allow_env:
        findings = [f for f in findings if f.rule != "env_file_present"]

    real = [f for f in findings if f.classification == "real"]
    synthetic = [f for f in findings if f.classification == "synthetic"]
    placeholders = [f for f in findings if f.classification == "placeholder"]
    allowlisted = [f for f in findings if f.classification == "allowlisted"]
    code_refs = [f for f in findings if f.classification == "code_reference"]

    payload = {
        "root": str(root),
        "total_findings": len(findings),
        "real_secrets": len(real),
        "synthetic_credentials": len(synthetic),
        "placeholders": len(placeholders),
        "allowlisted_suppressions": len(allowlisted),
        "code_references": len(code_refs),
        "verdict": "PASS" if not real else "FAIL",
        "real": [asdict(f) for f in real],
        "synthetic": [asdict(f) for f in synthetic[:20]],
        # Las supresiones se publican integras: el Agente de Ciberseguridad debe
        # poder revisar una por una que su justificacion sea legitima.
        "allowlisted": [asdict(f) for f in allowlisted],
    }

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({k: v for k, v in payload.items() if k not in ("real", "synthetic")}, indent=2))
    for finding in real:
        print(f"  SECRETO REAL: {finding.file}:{finding.line} [{finding.rule}] {finding.excerpt}")
    print("\nESCANEO DE SECRETOS:", payload["verdict"])
    return 0 if not real else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
