# Creado por Aldo Garcia.
"""Cambio de generador local: plan validado, aplicacion y reversion sin reindexar."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psutil
from dotenv import dotenv_values
from pydantic import ValidationError

from app.common.errors import MatrixError
from app.config import Settings
from app.config.settings import PROJECT_ROOT
from app.llm.model_configuration import validate_generator
from app.llm.provider import ModelClient
from app.llm.request_control import inference_control

GENERATOR_KEYS = frozenset({
    "LLM_PROVIDER", "LLM_DEEP_PROVIDER", "OLLAMA_FAST_MODEL", "OLLAMA_DEEP_MODEL",
    "LLM_FAST_DIGEST", "LLM_DEEP_DIGEST", "OLLAMA_BASE_URL", "LLM_API_BASE_URL",
    "OLLAMA_FAST_NUM_CTX", "OLLAMA_DEEP_NUM_CTX", "OLLAMA_FAST_MAX_TOKENS", "OLLAMA_DEEP_MAX_TOKENS",
    "LLM_FAST_TIMEOUT_SECONDS", "LLM_DEEP_TIMEOUT_SECONDS", "LLM_FAST_THINKING", "LLM_DEEP_THINKING",
    "LLM_STRUCTURED_THINKING", "LLM_TEMPERATURE", "LLM_GENERAL_TEMPERATURE", "LLM_TOP_P",
    "LLM_FAST_TOP_K", "LLM_DEEP_TOP_K",
})


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _values(data: bytes) -> dict[str, str]:
    import io

    decoded = data.decode("utf-8-sig")
    keys = re.findall(r"(?m)^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", decoded)
    if len({key.upper() for key in keys}) != len(keys):
        raise ValueError("El archivo contiene claves duplicadas; no se modifica.")
    values = dotenv_values(stream=io.StringIO(decoded), interpolate=False)
    if any(value is None or "${" in value for value in values.values()):
        raise ValueError("Use valores explicitos en .env, sin expansion de variables.")
    return {key.upper(): value for key, value in values.items()}


def _settings(values: dict[str, str]) -> Settings:
    # Prevalencia explicita para TODAS las claves: una variable de esta consola
    # no debe convertir la validacion aislada en otro perfil efectivo.
    defaults = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    defaults.update({key.lower(): value for key, value in values.items()})
    return Settings(_env_file=None, **defaults)


def _generation_values(values: dict[str, str]) -> dict[str, str]:
    return {key: values[key] for key in sorted(GENERATOR_KEYS) if key in values}


def _embedding_identity(settings: Settings) -> tuple:
    return (
        settings.llm_embedding_provider, settings.ollama_embedding_model, settings.llm_embedding_digest,
        settings.llm_embedding_revision, settings.ollama_embedding_dimension, settings.rag_embedding_dimension,
        settings.llm_query_template, settings.llm_document_template,
        settings.ollama_base_url if settings.llm_embedding_provider == "ollama" else settings.llm_api_base_url,
    )


def _candidate(before: bytes, changes: dict[str, str]) -> tuple[bytes, Settings]:
    values = _values(before)
    old = _settings(values)
    if not isinstance(changes, dict) or not set(changes).issubset(GENERATOR_KEYS):
        raise ValueError("El plan intenta cambiar claves ajenas al generador.")
    if any(not isinstance(value, str) or any(char in value for char in "\r\n\0") for value in changes.values()):
        raise ValueError("Los valores del plan no son validos.")
    # Escribir un valor quoted conserva # y espacios sin convertirlos en sintaxis.
    quoted = {key: "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'" for key, value in changes.items()}
    text = before.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    written = set()
    lines = []
    for line in text.splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match[1].upper() if match else ""
        lines.append(f"{key}={quoted[key]}" if key in changes else line)
        if key in changes:
            written.add(key)
    lines.extend(f"{key}={quoted[key]}" for key in sorted(set(changes) - written))
    updated = (newline.join(lines) + newline).encode("utf-8")
    if before.startswith(b"\xef\xbb\xbf"):
        updated = b"\xef\xbb\xbf" + updated
    new = _settings(_values(updated))
    if not new.llm_local_only or "vertex" in (new.llm_provider, new.llm_deep_provider):
        raise ValueError("Solo se permite un generador local; no se habilita cloud.")
    if _embedding_identity(old) != _embedding_identity(new):
        raise ValueError("El cambio afecta al embedding o su endpoint; requiere proceso de reindexacion separado.")
    return updated, new


def validate_candidate(settings: Settings, *, client: ModelClient | None = None) -> list[dict]:
    own = client is None
    client = client or ModelClient(settings=settings)
    reports = []
    try:
        for profile in ("fast", "deep"):
            with inference_control(min(60, settings.llm_request_deadline_seconds)):
                report = validate_generator(client, profile=profile)
                if not report["observed_digest"]:
                    raise ValueError("El runtime no acredita digest completo; no se autoriza el cambio.")
                if not report["expected_digest"]:
                    raise ValueError("Fije LLM_FAST_DIGEST/LLM_DEEP_DIGEST al digest observado antes del cambio.")
                text = client.chat(
                    model=report["model"], messages=[{"role": "user", "content": "Responde un saludo breve."}],
                    execution_profile=profile, num_ctx=report["context_tokens"], max_tokens=128, temperature=0,
                )
                if not text.content.strip():
                    raise ValueError("El candidato no cumple el contrato de texto.")
                schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                          "required": ["ok"], "additionalProperties": False}
                client.chat(
                    model=report["model"], messages=[{"role": "user", "content": 'Devuelve {"ok":true}.'}],
                    execution_profile=profile, num_ctx=report["context_tokens"], max_tokens=128, temperature=0,
                    response_schema=schema,
                )
                report["text_and_json_contract"] = "passed_synthetic"
                reports.append(report)
    finally:
        if own:
            client.close()
    return reports


def build_plan(before: bytes, candidate: bytes, *, validate=validate_candidate) -> dict:
    old, new = _values(before), _values(candidate)
    changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    if changed - GENERATOR_KEYS or any(key not in new for key in changed):
        raise ValueError("El candidato solo puede modificar valores explicitos de generacion.")
    changes = {key: new[key] for key in changed}
    if not changes:
        raise ValueError("No hay cambios de generacion.")
    updated, settings = _candidate(before, changes)
    return {
        "schema_version": 1, "created_at_utc": datetime.now(UTC).isoformat(),
        "before_sha256": _sha(before), "after_sha256": _sha(updated),
        "before": _generation_values(old), "after": _generation_values(_values(updated)),
        "changes": changes, "validation": validate(settings),
        "embedding_changed": False, "reindex_required": False,
        "limitation": "Synthetic protocol validation; RH quality and hardware capacity remain separate gates.",
    }


def assert_stopped(root: Path) -> None:
    root = root.resolve()
    for process in psutil.process_iter(["pid", "cmdline", "cwd", "exe"]):
        args = process.info.get("cmdline") or []
        command = " ".join(args)
        if not ("scripts.bootstrap serve" in command or "uvicorn app.main:app" in command):
            continue
        cwd, exe = process.info.get("cwd"), process.info.get("exe")
        own = cwd and Path(cwd).resolve() in {root, root / "backend"}
        own = own or (exe and (root / "backend" / "runtime" / "venv") in Path(exe).resolve().parents)
        if own:
            raise ValueError("Detenga Matrix de esta carpeta antes de aplicar o revertir el generador.")


@contextmanager
def configuration_lock(root: Path):
    """Bootstrap y dos cambios simultaneos respetan la misma exclusion local."""
    path = root / "knowledge-base" / "state" / "run" / "configuration-change.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = path.open("x", encoding="ascii")
    except FileExistsError as error:
        raise ValueError("Hay un cambio de configuracion en curso o un bloqueo que TI debe revisar.") from error
    try:
        with lock:
            try:
                created = psutil.Process().create_time()
            except psutil.Error:
                created = None  # Sin identidad verificada no se elimina automaticamente un bloqueo viejo.
            json.dump({"pid": os.getpid(), "created_at": created}, lock)
        yield
    finally:
        path.unlink(missing_ok=True)


def _regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_097_152:
        raise ValueError("Se requiere un archivo local regular de tamano limitado.")
    return path.read_bytes()


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".matrix-config-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_plan(root: Path, plan: dict, backup: Path, *, validate=validate_candidate) -> dict:
    with configuration_lock(root):
        return _apply_plan(root, plan, backup, validate=validate)


def _apply_plan(root: Path, plan: dict, backup: Path, *, validate=validate_candidate) -> dict:
    assert_stopped(root)
    env = root / "backend" / "config" / ".env"
    before = _regular(env)
    if plan.get("schema_version") != 1 or _sha(before) != plan.get("before_sha256"):
        raise ValueError(".env cambio desde el plan; genere y revise un plan nuevo.")
    after, settings = _candidate(before, plan.get("changes"))
    if _sha(after) != plan.get("after_sha256"):
        raise ValueError("El plan fue alterado o no corresponde a la configuracion.")
    validation = validate(settings)  # Revalida digest/capacidades, sin confiar en un JSON editable.
    if backup.exists() or backup.is_symlink():
        raise ValueError("El respaldo ya existe; no se sobrescribe.")
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with backup.open("xb") as output:
        os.chmod(backup, 0o600)
        output.write(before)
    if _regular(env) != before:
        raise ValueError(".env cambio durante la validacion; se conserva el archivo vigente.")
    assert_stopped(root)
    _atomic_write(env, after)
    return {"applied": True, "before_sha256": _sha(before), "after_sha256": _sha(after),
            "embedding_changed": False, "reindex_required": False, "validation": validation}


def rollback(root: Path, plan: dict, backup: Path) -> dict:
    with configuration_lock(root):
        return _rollback(root, plan, backup)


def _rollback(root: Path, plan: dict, backup: Path) -> dict:
    assert_stopped(root)
    env = root / "backend" / "config" / ".env"
    current, original = _regular(env), _regular(backup)
    if _sha(current) != plan.get("after_sha256") or _sha(original) != plan.get("before_sha256"):
        raise ValueError("La configuracion o respaldo cambio; no se sobrescribe una edicion posterior.")
    _atomic_write(env, original)
    return {"restored": True, "sha256": _sha(original), "runtime_validation_required": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    sub = parser.add_subparsers(dest="action", required=True)
    plan_command = sub.add_parser("plan")
    plan_command.add_argument("--candidate", type=Path, required=True)
    plan_command.add_argument("--output", type=Path, required=True)
    for action in ("apply", "rollback"):
        command = sub.add_parser(action)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if set(os.environ) & GENERATOR_KEYS:
            raise ValueError("Retire variables de generacion heredadas de esta consola y use un .env explicito.")
        if args.action == "plan":
            report = build_plan(_regular(args.root / "backend" / "config" / ".env"), _regular(args.candidate))
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with args.output.open("x", encoding="utf-8") as output:
                json.dump(report, output, indent=2, ensure_ascii=False)
        else:
            report = json.loads(_regular(args.plan))
            report = (apply_plan(args.root, report, args.backup) if args.action == "apply"
                      else rollback(args.root, report, args.backup))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except Exception as error:
        # No serializar Settings, argumentos, errores HTTP ni valores .env.
        detail = "No se completo el cambio. Revise configuracion, modelo local y plan; .env no se fuerza."
        if isinstance(error, MatrixError):
            detail = error.message
        elif isinstance(error, ValueError) and not isinstance(error, ValidationError):
            detail = str(error)
        print(json.dumps({"ok": False, "error_type": type(error).__name__, "message": detail}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
