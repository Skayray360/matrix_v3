# Creado por Aldo Garcia.
"""Migracion acotada de ajustes operativos, con respaldo y sin exponer secretos."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

ADDITIONS = {
    "LLM_FAST_TIMEOUT_SECONDS": "180",
    "LLM_DEEP_TIMEOUT_SECONDS": "180",
    "ANSWER_ALLOW_GENERAL_KNOWLEDGE": "true",
    "ANSWER_EVIDENCE_MODE": "cited",
    "LLM_FAST_THINKING": "auto",
    "LLM_DEEP_THINKING": "auto",
    "LLM_STRUCTURED_THINKING": "auto",
    "LLM_COMPLETION_RETRIES": "1",
    "OLLAMA_FAST_MAX_TOKENS": "1536",
    "OLLAMA_DEEP_MAX_TOKENS": "3072",
}
LEGACY_VALUES = {
    "LLM_REQUEST_DEADLINE_SECONDS": ("120", "600"),
    "LLM_FAST_THINKING": ("default", "auto"),
    "LLM_DEEP_THINKING": ("default", "auto"),
    "LLM_STRUCTURED_THINKING": ("default", "auto"),
    "OLLAMA_FAST_MAX_TOKENS": ("768", "1536"),
    "OLLAMA_DEEP_MAX_TOKENS": ("2048", "3072"),
}
OUTPUT_CONTEXTS = {
    "OLLAMA_FAST_MAX_TOKENS": ("OLLAMA_FAST_NUM_CTX", 8192),
    "OLLAMA_DEEP_MAX_TOKENS": ("OLLAMA_DEEP_NUM_CTX", 8192),
}


def _input_room(key: str, output: str, values: dict[str, str]) -> bool:
    """No elevar salida a costa de una ventana personalizada de entrada.

    La reserva cubre los 1400 tokens del agente y un margen de 136 tokens;
    una expresion de contexto que no podemos interpretar conserva el limite.
    """
    context_key, default = OUTPUT_CONTEXTS[key]
    raw = os.environ.get(context_key, values.get(context_key, str(default)))
    raw = raw.split("#", 1)[0].strip().strip("'\"")
    if not re.fullmatch(r"\d+(?:\.0*)?", raw):
        return False
    context = int(raw.split(".", 1)[0])
    return context - int(output) > 1536


def upgrade_configuration(root: Path) -> dict[str, object]:
    target = root / "backend" / "config" / ".env"
    if not target.is_file() or target.is_symlink():
        raise ValueError("Se requiere un archivo .env local regular.")
    original_bytes = target.read_bytes()
    has_bom = original_bytes.startswith(b"\xef\xbb\xbf")
    original = original_bytes.decode("utf-8-sig")
    lines = original.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in original else "\n"
    positions: dict[str, list[int]] = {}
    values: dict[str, str] = {}
    for index, line in enumerate(lines):
        match = re.match(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)", line)
        if match:
            key, value = match.groups()
            positions.setdefault(key.upper(), []).append(index)
            values[key.upper()] = value.strip()
    wanted = {*ADDITIONS, *LEGACY_VALUES}
    if any(len(positions.get(key, [])) > 1 for key in wanted):
        raise ValueError("Hay claves operativas duplicadas en .env; no se modifica.")
    changed = []
    for key, (old, new) in LEGACY_VALUES.items():
        if key not in positions:
            continue
        if key in OUTPUT_CONTEXTS and not _input_room(key, new, values):
            continue
        value_pattern = re.escape(old) + (r"(?:\.0*)?" if old.isdecimal() else "")
        pattern = rf"([ \t]*(?:export[ \t]+)?{key}[ \t]*=[ \t]*)(?:{value_pattern}|'" + value_pattern
        pattern += r"'|\"" + value_pattern + r"\")([ \t]*(?:#.*)?)(\r?\n)?"
        match = re.fullmatch(pattern, lines[positions[key][0]], flags=re.IGNORECASE)
        if match:
            lines[positions[key][0]] = match[1] + new + match[2] + (match[3] or "")
            changed.append(key)
    additions = {**ADDITIONS, "LLM_REQUEST_DEADLINE_SECONDS": "600"}
    for key, value in additions.items():
        if key not in positions:
            if key in OUTPUT_CONTEXTS and not _input_room(key, value, values):
                value = LEGACY_VALUES[key][0]
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline
            lines.append(f"{key}={value}{newline}")
            changed.append(key)
    if not changed:
        return {"changed_keys": [], "backup_created": False}
    backup_dir = root / "knowledge-base" / "backups" / "configuration"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_dir / f"env-before-1.3.0-{stamp}.bak"
    # bytes exactos en el respaldo: no generar valores nuevos para secretos.
    backup.write_bytes(original_bytes)
    os.chmod(backup, 0o600)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=root, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            updated = "".join(lines).encode("utf-8")
            stream.write((b"\xef\xbb\xbf" if has_bom else b"") + updated)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"changed_keys": changed, "backup_created": True}
