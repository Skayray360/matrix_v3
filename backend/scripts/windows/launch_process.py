# Creado por Aldo Garcia.
"""Start one owned native runtime with isolated environment and durable log handles.

This helper never adopts a process, opens a network socket or accepts shell syntax.
The PowerShell supervisor records and verifies the resulting process identity.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def launch(request: Path) -> int:
    root = Path(__file__).resolve().parents[3]
    run = root / "knowledge-base" / "state" / "run"
    if not request.resolve().is_relative_to(run.resolve()) or request.stat().st_size > 65536:
        raise ValueError("invalid launch request")
    settings = json.loads(request.read_text(encoding="utf-8-sig"))
    executable = Path(settings["executable"]).resolve(strict=True)
    runtime = (root / "backend" / "runtime").resolve()
    if not executable.is_relative_to(runtime) or executable.name != "mysqld.exe":
        raise ValueError("runtime executable outside this installation")
    arguments = settings["arguments"]
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        raise ValueError("invalid argument list")
    config = root / "backend" / "config"
    if executable.name == "mysqld.exe":
        permitted = [f"--defaults-file={config / 'mysql.ini'}"]
        with_initialization = [*permitted, f"--init-file={config / 'secrets' / 'mysql-init.sql'}"]
        if arguments not in (permitted, with_initialization):
            raise ValueError("MySQL options must reference this installation")
    environment = os.environ.copy()
    overrides = settings.get("environment", {})
    if not isinstance(overrides, dict) or bool(overrides):
        raise ValueError("invalid environment overrides")
    environment.update({key: str(value) for key, value in overrides.items()})
    logs = (root / "backend" / "logs").resolve()
    stdout = Path(settings["stdout"]).resolve()
    stderr = Path(settings["stderr"]).resolve()
    if not stdout.is_relative_to(logs) or not stderr.is_relative_to(logs):
        raise ValueError("logs outside this installation")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with stdout.open("ab", buffering=0) as out, stderr.open("ab", buffering=0) as err:
        # Executable and complete argument list are allowlisted above; no shell.
        process = subprocess.Popen([str(executable), *arguments], cwd=root, env=environment,  # noqa: S603
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   close_fds=True, creationflags=flags)
    return process.pid


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("This launcher requires native Windows.")
    try:
        print(launch(Path(sys.argv[1])), flush=True)
    except (OSError, ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError) as error:
        # Do not echo command lines or environment values.
        raise SystemExit(f"Native runtime could not start ({type(error).__name__}).") from None
