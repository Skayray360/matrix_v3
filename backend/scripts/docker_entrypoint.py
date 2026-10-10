# Creado por Aldo Garcia.
"""Preparacion y arranque del perfil Docker Linux local, sin secretos en salida."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values

from scripts.docker_prepare import PIN_KEYS

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def operation_lock(directory: Path) -> Iterator[int]:
    """El lock del kernel separa preparacion y servidor aun entre contenedores."""
    import fcntl  # Solo se ejecuta en el perfil Linux.

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "docker-operation.lock"
    if path.is_symlink():
        raise ValueError("El bloqueo Docker no puede ser un enlace.")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Docker ya esta activo. Ejecute compose down antes de preparar.") from exc
        yield descriptor
    finally:
        os.close(descriptor)


def _run(module: str, *arguments: str, environment: dict[str, str]) -> None:
    subprocess.run([sys.executable, "-m", module, *arguments],  # noqa: S603
                   check=True, env=environment, cwd=PROJECT_ROOT / "backend")


def wait_for_services(environment: dict[str, str], *, seconds: float = 180) -> None:
    import httpx

    deadline = time.monotonic() + seconds
    with httpx.Client(trust_env=False, timeout=3) as client:
        while True:
            try:
                client.get("http://ollama:11434/api/tags").raise_for_status()
                client.get("http://qdrant:6333/collections",
                           headers={"api-key": environment["QDRANT_API_KEY"]}).raise_for_status()
                return
            except (httpx.HTTPError, KeyError):
                if time.monotonic() >= deadline:
                    raise ValueError("Qdrant u Ollama no estan listos; revise compose ps y logs.") from None
                time.sleep(2)


def runtime_configuration(environment: dict[str, str]) -> None:
    required = {
        "APP_ENV": "development", "AUTH_PROVIDER": "local", "APP_HOST": "0.0.0.0",  # noqa: S104 - red interna.
        "APP_BASE_URL": "https://127.0.0.1:8443", "APP_PORT": "8000",
        "SESSION_COOKIE_SECURE": "true", "LOCAL_TEST_AUTH_ENABLED": "false",
        "LOCAL_TEST_SEED_USERS_ENABLED": "false", "LLM_LOCAL_ONLY": "true",
        "OLLAMA_BASE_URL": "http://ollama:11434", "QDRANT_MODE": "server",
        "QDRANT_URL": "http://qdrant:6333", "MATRIX_INSTALL_MODE": "docker_local",
        "LLM_PROVIDER": "ollama", "LLM_DEEP_PROVIDER": "ollama", "LLM_EMBEDDING_PROVIDER": "ollama",
    }
    if any(environment.get(key, "").lower() != value.lower() for key, value in required.items()):
        raise ValueError("El perfil Docker debe conservar identidad real local, HTTPS e inferencia local.")
    if any(not re.fullmatch(r"[0-9a-f]{64}", environment.get(key, "")) for key in PIN_KEYS):
        raise ValueError("Faltan los digests de modelos. Complete compose run --rm prepare antes de iniciar.")
    from app.config import Settings

    # Validacion explicita independiente de cualquier .env nativo.
    Settings(_env_file=None, **{key.lower(): value for key, value in environment.items()})


def _marker(environment: dict[str, str]) -> dict[str, object]:
    from app import __version__

    return {"version": __version__, "mode": "docker_local",
            "digests": {key: environment[key] for key in PIN_KEYS}}


def prepare_application(root: Path = PROJECT_ROOT) -> None:
    env_file = root / "backend/config/docker.env"
    if (env_file.is_symlink() or env_file.absolute() != env_file.resolve()
            or not env_file.is_file() or (root / "backend/config/.env").exists()):
        raise ValueError("Falta docker.env privado o se mezclo una configuracion nativa.")
    marker = root / "knowledge-base/state/run/docker-setup.json"
    if marker.is_symlink():
        raise ValueError("El estado de preparacion no puede ser un enlace.")
    # Una preparacion fallida nunca conserva un marcador de exito anterior.
    marker.unlink(missing_ok=True)
    environment = dict(os.environ)
    environment.update({key: value for key, value in dotenv_values(env_file).items() if value is not None})
    # pin-models distingue un pin vacio heredado de un campo vacio aun no fijado.
    for key in PIN_KEYS:
        if not environment.get(key):
            environment.pop(key, None)
    wait_for_services(environment)
    _run("scripts.installation_state", "--root", str(root), "--env-file", str(env_file),
         "--guard-install", "--verify-server-collections", environment=environment)
    _run("scripts.bootstrap", "pin-models", "--env-file", str(env_file), environment=environment)
    environment.update({key: value for key, value in dotenv_values(env_file).items() if value is not None})
    runtime_configuration(environment)
    _run("scripts.preflight", "--llm-only", environment=environment)
    _run("scripts.bootstrap", "migrate", environment=environment)
    _run("scripts.local_identity", "bootstrap", "--username", environment.get("MATRIX_DOCKER_ADMIN", "Matrix"),
         "--password-file", "backend/config/secrets/docker/bootstrap-admin-password.txt", environment=environment)
    _run("scripts.bootstrap", "ingest", environment=environment)
    directory = root / "knowledge-base/state/run"
    descriptor, temporary_name = tempfile.mkstemp(prefix="docker-setup-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(_marker(environment), output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / "docker-setup.json")
    finally:
        temporary.unlink(missing_ok=True)
    print("[OK] Docker preparado: migraciones, identidad local, modelos fijados e ingesta verificados.")


def serve(*, lock_descriptor: int, root: Path = PROJECT_ROOT) -> None:
    env_file = root / "backend/config/docker.env"
    if (env_file.is_symlink() or env_file.absolute() != env_file.resolve() or not env_file.is_file()):
        raise ValueError("Falta el archivo Docker privado de esta instancia.")
    environment = dict(os.environ)
    # Archivo y guard leen exactamente la misma configuracion, incluso tras un
    # restart de un contenedor cuyo entorno fue creado antes de editar docker.env.
    environment.update({key: value for key, value in dotenv_values(env_file).items() if value is not None})
    runtime_configuration(environment)
    marker = root / "knowledge-base/state/run/docker-setup.json"
    if (marker.is_symlink() or not marker.is_file()
            or json.loads(marker.read_text(encoding="utf-8")) != _marker(environment)):
        raise ValueError("Esta version/configuracion aun no termino prepare; no se inicia contra estado incompleto.")
    # status/get_vector_store pueden abrir colecciones: comprobar el estado
    # antes impide materializar un indice vacio sobre SQL publicado.
    _run("scripts.installation_state", "--root", str(root), "--env-file", str(env_file),
         "--guard-install", "--verify-server-collections", environment=environment)
    # Comprueba digests reales y conectividad; nunca sustituye pins.
    _run("scripts.bootstrap", "status", environment=environment)
    # exec conserva SIGTERM y el lock; no queda un shell padre que impida la parada.
    os.set_inheritable(lock_descriptor, True)
    os.execve(  # noqa: S606 - interprete actual y modulo fijo, sin shell ni argumentos del usuario.
        sys.executable, [sys.executable, "-m", "scripts.bootstrap", "serve", "--skip-preflight"], environment,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "serve"), default="serve", nargs="?")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if sys.platform != "linux":
            raise ValueError("Docker local requiere Linux; use los BAT para Windows.")
        with operation_lock(PROJECT_ROOT / "knowledge-base/state/run") as descriptor:
            if args.action == "prepare":
                prepare_application()
                return 0
            serve(lock_descriptor=descriptor)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # ValidationError representa valores privados; no se imprime su texto.
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(f"[FALLA] {detail}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
