# Creado por Aldo Garcia.
"""Prepara una copia Linux separada; no accede a Docker ni a los datos de Windows.

Ejecutar con la imagen backend y el UID/GID del operador. Solo crea directorios,
secretos persistentes y TLS local autofirmado. No borra ni restablece datos.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

LINUX_FILESYSTEMS = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "overlay", "overlayfs"})
STATE_DIRECTORIES = ("mysql", "qdrant", "qdrant-snapshots", "ollama", "uploads", "run", "tls")
SECRET_FILES = ("mysql-root-password.txt", "bootstrap-admin-password.txt")
PIN_KEYS = ("LLM_FAST_DIGEST", "LLM_DEEP_DIGEST", "LLM_EMBEDDING_DIGEST")


def _checked_path(root: Path, relative: str) -> Path:
    target = root / relative
    if target.absolute() != target.resolve() or not target.resolve().is_relative_to(root):
        raise ValueError("No se admiten enlaces ni rutas fuera de esta copia Linux.")
    return target


def filesystem_type(path: Path, *, mountinfo: Path = Path("/proc/self/mountinfo")) -> str:
    """La coincidencia mas larga incluye el bind real, no solo la raiz del contenedor."""
    selected: tuple[int, str] | None = None
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        left, separator, right = line.partition(" - ")
        fields, details = left.split(), right.split()
        if not separator or len(fields) < 5 or not details:
            continue
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
        if path == mount or path.is_relative_to(mount):
            match = (len(str(mount)), details[0])
            if selected is None or match[0] > selected[0]:
                selected = match
    if selected is None:
        raise ValueError("No se pudo comprobar el filesystem Linux del proyecto.")
    return selected[1]


def read_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line.strip())
        if match:
            values[match[1]] = match[2]
        elif line.strip() and not line.lstrip().startswith("#"):
            raise ValueError("El archivo de configuracion contiene una linea no reconocida.")
    return values


def _private_file(path: Path, value: str | None = None) -> str:
    if path.exists():
        if not path.is_file() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("Un archivo privado no es regular o permite acceso a otros usuarios.")
        return path.read_text(encoding="utf-8").strip()
    if value is None:
        raise ValueError("Falta un secreto existente. Restaure su respaldo; no se regenera contra una base activa.")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(value + "\n")
        output.flush()
        os.fsync(output.fileno())
    return value


def prepare_certificates(directory: Path) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        raise ValueError("La imagen de preparacion necesita OpenSSL.")
    certificate, key = directory / "matrixrh.crt", directory / "matrixrh.key"
    if certificate.exists() != key.exists():
        raise ValueError("TLS incompleto: restaure el par existente antes de continuar.")
    if not certificate.exists():
        subprocess.run([  # noqa: S603 - ejecutable fijo y argumentos separados, sin shell.
            openssl, "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes", "-days", "365",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout", str(key), "-out", str(certificate),
        ], check=True, capture_output=True, timeout=60)
        key.chmod(0o600)
        certificate.chmod(0o644)
    _private_file(key)
    for target in (certificate, key):
        if target.is_symlink() or not target.is_file():
            raise ValueError("TLS debe consistir en archivos regulares dentro del proyecto.")
    subprocess.run([openssl, "x509", "-in", str(certificate), "-checkend", "86400", "-noout"],  # noqa: S603
                   check=True, capture_output=True, timeout=10)
    public_certificate = subprocess.run(  # noqa: S603
        [openssl, "x509", "-in", str(certificate), "-pubkey", "-noout"],
        check=True, capture_output=True, timeout=10,
    ).stdout
    public_key = subprocess.run([openssl, "pkey", "-in", str(key), "-pubout"],  # noqa: S603
                                check=True, capture_output=True, timeout=10).stdout
    if public_certificate != public_key:
        raise ValueError("El certificado TLS no corresponde a su clave privada.")


def prepare(root: Path, *, uid: int | None = None, gid: int | None = None) -> dict[str, object]:
    if sys.platform != "linux":
        raise ValueError("Esta alternativa requiere Linux; Windows utiliza instalar.bat.")
    root = root.absolute()
    if root != root.resolve() or not root.is_dir():
        raise ValueError("Use una carpeta Linux real, sin enlaces.")
    owner_uid = os.getuid() if uid is None else uid
    owner_gid = os.getgid() if gid is None else gid
    if owner_uid <= 0 or owner_gid <= 0:
        raise ValueError("Ejecute la preparacion con --user UID:GID de un operador Linux sin privilegios.")
    for relative in ("backend/config/env.example", "frontend/package.json", "docker-compose.yml"):
        if not _checked_path(root, relative).is_file():
            raise ValueError("Extraiga el proyecto completo en una copia Linux separada.")
    if _checked_path(root, "backend/config/.env").exists():
        raise ValueError("Esta copia contiene configuracion nativa. Extraiga otra copia para Docker.")
    state = _checked_path(root, "knowledge-base/state")
    if state.exists() and any(item.name not in {"docker", ".gitkeep"}
                              and (item.is_file() or (item.is_dir() and any(item.iterdir())))
                              for item in state.iterdir()):
        raise ValueError("Existe estado nativo. Docker requiere otra copia; no comparte datos vivos con WAMP.")
    filesystem = filesystem_type(root)
    for target in (root, state, *(root / f"knowledge-base/state/docker/{name}" for name in STATE_DIRECTORIES)):
        if target.exists() and filesystem_type(target) not in LINUX_FILESYSTEMS:
            raise ValueError("Qdrant requiere filesystem Linux local; no se admiten NTFS, DrvFS, 9p ni red.")
    env_file = _checked_path(root, "backend/config/docker.env")
    existing = env_file.exists()
    if not existing:
        for name in ("mysql", "qdrant", "ollama", "uploads"):
            target = _checked_path(root, f"knowledge-base/state/docker/{name}")
            if target.exists() and any(target.iterdir()):
                raise ValueError("Hay datos Docker sin su configuracion. Restaure docker.env y sus secretos juntos.")
    for relative in ("knowledge-base/documents", "backend/config/secrets/docker",
                     *(f"knowledge-base/state/docker/{name}" for name in STATE_DIRECTORIES)):
        _checked_path(root, relative).mkdir(parents=True, exist_ok=True, mode=0o700)
    secret_root = root / "backend/config/secrets/docker"
    for name in SECRET_FILES:
        secret = _private_file(_checked_path(root, f"backend/config/secrets/docker/{name}"),
                               None if existing else secrets.token_urlsafe(32))
        if not 32 <= len(secret) <= 128 or any(character.isspace() for character in secret):
            raise ValueError("El archivo secreto no cumple el formato de la instalacion Docker.")
    prepare_certificates(root / "knowledge-base/state/docker/tls")
    if existing:
        _private_file(env_file)
        values = read_values(env_file)
        if (values.get("MATRIX_DOCKER_UID") != str(owner_uid)
                or values.get("MATRIX_DOCKER_GID") != str(owner_gid)
                or values.get("MATRIX_INSTALL_MODE") != "docker_local"
                or not re.fullmatch(r"[0-9a-f]{16}", values.get("MATRIX_DOCKER_PROJECT_ID", ""))):
            raise ValueError("La configuracion existente pertenece a otro operador o modo; no se modifica.")
    else:
        values = read_values(root / "backend/config/env.example")
        mysql_password = secrets.token_hex(32)
        values.update({
            "APP_ENV": "development", "APP_HOST": "0.0.0.0", "APP_PORT": "8000",  # noqa: S104 - red interna.
            "APP_BASE_URL": "https://127.0.0.1:8443", "APP_SECRET_KEY": secrets.token_hex(32),
            "AUTH_PROVIDER": "local", "LOCAL_TEST_AUTH_ENABLED": "false",
            "LOCAL_TEST_SEED_USERS_ENABLED": "false", "MATRIX_SEED_PASSWORD": "",
            "SESSION_COOKIE_SECURE": "true", "MATRIX_INSTALL_MODE": "docker_local",
            # secrets-scan: allow (DSN construido con secreto aleatorio en runtime, sin credencial fija)
            "DATABASE_URL": f"mysql+pymysql://matrixrh:{mysql_password}@mysql:3306/matrix_rh?charset=utf8mb4",
            "DATABASE_DATADIR_PATH": "", "MATRIX_DOCKER_MYSQL_PASSWORD": mysql_password,
            "MATRIX_ADOPT_EXISTING_DATABASE": "true", "QDRANT_MODE": "server",
            "QDRANT_URL": "http://qdrant:6333", "QDRANT_API_KEY": secrets.token_hex(32),
            "OLLAMA_BASE_URL": "http://ollama:11434", "LLM_LOCAL_ONLY": "true",
            "LLM_LOCAL_HOSTS": "127.0.0.1,localhost,::1,ollama",
            "MATRIX_DOCKER_UID": str(owner_uid), "MATRIX_DOCKER_GID": str(owner_gid),
            "MATRIX_DOCKER_PROJECT_ID": secrets.token_hex(8),
            "MATRIX_DOCKER_ADMIN": "Matrix", "MATRIX_PROXY_SUBNET": "172.31.240.0/28",
            "MATRIX_NGINX_IP": "172.31.240.2", "FORWARDED_ALLOW_IPS": "172.31.240.2",
            "OLLAMA_MEMORY_LIMIT": "24g", "OLLAMA_CPUS": "4",
        })
        values.update(dict.fromkeys(PIN_KEYS, ""))
        _private_file(env_file, "# Privado. Perfil Docker local Linux; conservar con el respaldo.\n"
                      + "\n".join(f"{key}={value}" for key, value in values.items()))
    # No imprime valores, claves, contrasenas ni el contenido de documentos.
    return {"ok": True, "mode": "docker_local", "configuration_created": not existing,
            "filesystem": filesystem, "admin_username": values.get("MATRIX_DOCKER_ADMIN", "Matrix"),
            "credential_file": (secret_root / "bootstrap-admin-password.txt").relative_to(root).as_posix(),
            "tls": "self_signed_local_not_trusted", "data_deleted": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        print(json.dumps(prepare(args.root), ensure_ascii=False))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({"ok": False, "message": detail}, ensure_ascii=False))
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
