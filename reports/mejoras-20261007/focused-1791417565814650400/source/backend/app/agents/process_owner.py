# Creado por Aldo Garcia.
"""Identidad local del dispatcher; nunca adivina el dueño de un UUID antiguo."""

from __future__ import annotations

import hashlib
import os
import platform
import sys
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import getnode, uuid4

import psutil

from app.config.settings import PROJECT_ROOT

OwnerState = Literal["alive", "dead", "unknown"]


@lru_cache(maxsize=1)
def local_scope() -> str:
    """Huella del equipo y carpeta, sin hostname/rutas legibles en SQL."""
    machine = ""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                machine = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            pass
    else:
        with suppress(OSError, UnicodeError):
            machine = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    # Si la identidad cambia, el lease previo queda desconocido: nunca se roba.
    machine = machine or f"{platform.node()}:{getnode()}"
    root = os.path.normcase(str(PROJECT_ROOT.resolve()))
    return hashlib.sha256(f"{machine}\0{root}".encode()).hexdigest()[:32]


def new_owner() -> str:
    """PID + instante de creación evita confundir un PID reutilizado."""
    try:
        process = psutil.Process(os.getpid())
        created = round(process.create_time() * 1_000_000)
        return f"local2:{local_scope()}:{process.pid}:{created}:{uuid4().hex}"
    except (psutil.Error, OSError):
        # Compatible con el lease temporal cuando no se permite inspeccionar procesos.
        return str(uuid4())


def owner_state(owner: str) -> OwnerState:
    """Solo declara huérfano al proceso de ESTA carpeta y ESTE equipo."""
    parts = owner.split(":")
    if len(parts) != 5 or parts[0] != "local2" or parts[1] != local_scope():
        return "unknown"
    try:
        process_id, created = int(parts[2]), int(parts[3])
        if process_id <= 0 or created <= 0 or len(parts[4]) != 32:
            return "unknown"
        int(parts[4], 16)
        process = psutil.Process(process_id)
        return "alive" if round(process.create_time() * 1_000_000) == created else "dead"
    except psutil.NoSuchProcess:
        return "dead"
    except (ValueError, psutil.Error, OSError):
        return "unknown"
