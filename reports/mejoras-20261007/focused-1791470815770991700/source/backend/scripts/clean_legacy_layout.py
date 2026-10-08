# Creado por Aldo Garcia.
"""Retira restos conocidos al instalar sobre la misma carpeta.

Solo los tres lanzadores retirados se archivan aunque esten personalizados.
Documentacion y asset requieren sus hashes exactos de la entrega conocida.
Todos los archivos se respaldan antes del primer borrado; no se leen .env,
corpus, indices, conversaciones ni configuracion. No depende de terceros.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import html
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.parse import unquote
from uuid import uuid4

# Allowlist de la consolidacion 1.3.1. No se carga de un archivo editable.
LEGACY_DOCUMENT_HASHES = {
    "backend/app/README.md": "7291302e2223ab3a40d31ee919ac48162a22dd53a2b77077356d79b242d44458",
    "backend/app/agents/README.md": "bf864c8fedd8ef23ff9001e312f21242b65d2e70e71a57de19a727da85e3e067",
    "backend/app/api/README.md": "c1a020780cbef9a9c5e673fb7012781dc2ecbafa387f8c6ab118aa9a317f489c",
    "backend/app/api/routes/README.md": "271169e98bea2c97b881eabed6a4c2282dfaced9e4c6ae32615bfd0075a08916",
    "backend/app/audit/README.md": "d47513a58709f5306ce9c1d73e3fc4bf1a51ae6b55bfd17c7b250f99d60f67b1",
    "backend/app/auth/README.md": "564cfd98b555bb0808de958f3c87025178298bedc43fbe7696375ace2c6369db",
    "backend/app/authorization/README.md": "924c13a445750821382c2b813dabc501d95f7f748386a997025e5b73d862938d",
    "backend/app/common/README.md": "d8240be41d919cfba8d931284a7341cc1b93f97639234a25bc2401833e137166",
    "backend/app/config/README.md": "cceac1f7441cc2c643d168e3898bac2354b7102cbdd95387f399b6ae8bebee2f",
    "backend/app/database/README.md": "dd5d5b5bd28be6777ea7359ce23deb2f0d3d4b46ff013b9cd0767dad5fc9a096",
    "backend/app/ingestion/README.md": "e1d603ca4d0c2f2b2692d1f7c978c1173327e4c856d3c524eadda42cca53d39f",
    "backend/app/jobs/README.md": "5a59984f4962b5d6d514e075cc9272fe9f23ae9d5bdc6922001662ef75b32779",
    "backend/app/llm/README.md": "b24b0f5d60aaaa91822f1f5efde7db1d16d1d32d723fc96bb569224259ab1d05",
    "backend/app/memory/README.md": "0c78e3afcbee0a396177860de19ca5ab9dc6b7c71ee299546d245067554ad463",
    "backend/app/rag/README.md": "773cca10c658228e124d3549fed16a942e46872895f5545cc10f25c81aa7543b",
    "backend/app/security/README.md": "8f4728d47f944c1df17a139d3597fa2bf7d69a7fa479b7e8c7f74de34bd99004",
    "backend/app/structured_data/README.md": "13bb699753a16cb23ba175c31d83ad293aacd8d72948cb35a5d59944553723ce",
    "backend/migrations/README.md": "b318b83354138a4e346e90088dccb30129314ae2637c500248ec716548a9453c",
    "backend/seeds/README.md": "8a24a2b9fb73dde4f98ed5c148517024be481e6d7fc6051e7fc98f4a3371946f",
    "backend/tests/README.md": "e63756ca0045a08f7dc3e27b6be792affac44169f12617ea67bdb9d57ffe53da",
    "backend/tests/integration/README.md": "f538124799fd01020ccb7ef199501c2a9b482904dc1b937facb64aeae911a6a6",
    "backend/tests/rag_eval/README.md": "ced14302c38d1440cd9f62667e38f7f282f298f44ef496c756930dd1def8c9ca",
    "backend/tests/security/README.md": "793748fc62538b4e4ad5b3a164279588e9552cebf82ca6b2ddb2b3d6fed541af",
    "backend/tests/unit/README.md": "d738e6c2e85951d34d1f4e94b44057442698934870aeb6ecd4b6dcd6bbbf7854",
    "config/authorization/README.md": "8a5e14ff20457f75723d3cb02462289ab2edbc86c343e2804089610ae412eaf6",
    "config/data_sources/README.md": "1d75d84f1af69b676148c6134ed6be1e3de93ab405931e8932f0dd1bd597c0e4",
    "docs/ACCESS_PROFILES.md": "359576671d3a432e375b38a1d7402633761eb31c9c70d7011cbc19f77ea7b842",
    "docs/AI_DESIGN.md": "f951b3f0b7cc3efba04d3fd357f1bedce3f0215ded47867bd98c06fa4117e322",
    "docs/DATA_MODEL.md": "9fb5276a9201a38d7cfe0752528f807858fd6a134c617008237586a642aa370b",
    "docs/DEPLOYMENT.md": "a049e6b926650fd2ad6566d38523306da9fd90b2b132e60f325c5e28dda41508",
    "docs/DEPLOYMENT_V2.md": "aaa3362a40dbb51e140a7f8c2c76eaf9a2eb847da99e72936bcf9119bd461a8e",
    "docs/DOCUMENTATION_GOVERNANCE.md": "7996a37b578cd395d26496cbb916735bc262f59c96db1a31e95b6650b23eb6ea",
    "docs/E2E.md": "e1e17fa3bf0e012d0c76f1356108dadbad517491c24b8db715c73683d3001a0c",
    "docs/EMBEDDING_COMPARISON.md": "8f6a8cc951b37b0345bd7756a606088fa5e88c4d7b16de21fab5bbe685730cb1",
    "docs/EXTERNAL_DEPENDENCIES_STATUS.md": "6eeb02dc08725b01c239ef6fc6413922a7d173f74bb25a4f29492d5a0467b9f0",
    "docs/FINAL_AUDIT.md": "25eb5aac21101c329dea724347c5c4d176080b7ee78340843513ba52903a3a88",
    "docs/FINAL_CONSOLIDATION.md": "12998467f1562d43c9d59ad634fccc89c627fb5053e96d68addc5e90a38331e0",
    "docs/INSTALACION_LIMPIA_1.3.0.md": "f25993f6b753326b1f30727ce2d83c24a541b2aa0038cb8ad9c55d2f9aede307",
    "docs/LOCAL_FINETUNING.md": "5de1ba25c0d2ddc7538ed413a44b2a17ec6ebaacb05a5f970b720043dab2f209",
    "docs/LOCAL_MODEL_OPTIMIZATION.md": "0bbddf00f6a0037266eb8dd80deebecdd76c2eb05f56ba68dd1021861d6cd4e2",
    "docs/LOCAL_MODEL_READINESS.md": "7cf9dcf5869c52bb9ce10aa041ce5d65ef303368c4728f20c7cb341dd1ea4266",
    "docs/MODEL_PROVIDERS.md": "45afbb9b6da7eebcda478930db158af6cbf3e7650bb5c39ea394632727cdaafe",
    "docs/MODEL_SMOKE_TEST.md": "9f7d6f73435cc2c96c7d661dc675bec6c1d0b21d2e937895e52f4502f9588f2f",
    "docs/OPERATOR_QUICKSTART.md": "dfcb66860a2dbf85a8ba25b59bf1c7dd52cbb61f5d1189d72cc4b6a3a92f2724",
    "docs/PROJECT_INTENT.md": "da8ef0a03ae2968705e9a541e328e201adcc2ea5d1dd2ea06c07e825f9404bad",
    "docs/REQUIREMENTS_TRACEABILITY.md": "6ec0f52f1e6fe132cef0dbb9a058868d07b928aad3cb70cf8cad086ed4d3d5e3",
    "docs/THREAT_MODEL.md": "ff316db75f85ed8257e688bccce2ccb2126c932ffa2640d12d8919b18a8bf020",
    "docs/integrations/IDENTITY_PROVIDER_DESIGN.md": "04263573110104a335c5ebdf92b79cd86d8a716d84c3e98736f19b684a568d6c",
    "docs/integrations/MIGRATE_LOCAL_TEST_TO_ENTRA.md": (
        "f34bde0f68e7d805b822778117d198dc6cd3a9661ad0c911db6c6298696ff32f"
    ),
    "frontend/src/README.md": "ba8f3dc043a928f4a7e2b21aa3cecc245bf08b0112bbd66ef3d4033ba3a2c369",
    "frontend/src/assets/README.md": "5c9ae05b8332bebcca35e1474ed479344e3ad60676e1cfab3f3e6b157d8d996e",
    "frontend/src/components/README.md": "774845e081ba87a8cc7917f632fb125b13b334f91f24b37e286205f818f40c47",
    "frontend/src/pages/README.md": "3d3a8e37d6cd785e8f5e505e0ab383220e41375fb3fcd5edf55ceb962feb146d",
    "frontend/src/security/README.md": "75ba1e9f8f2e9ef469adfcf7895867503c0d0bd2215537c3f806bce8aaeafc8e",
    "frontend/src/services/README.md": "fe7a3ddf01f58cf810ae03f2949e5f52e12dbfdc3defc8ad4acca160d85f0c37",
    "frontend/tests/README.md": "6717f80f0f0718b87f964952f9f4c57e82f29b74245716a9f1459fe58d2084e7",
    "frontend/tests/e2e/README.md": "ac9b3d5cc5f189acf189528a783bbd1de2e89eb723a90fbb76acea932c29577a",
    "frontend/tests/visual/README.md": "75bee2d4efb9c6493f320711c95e5d99baa6ab392af9c18dc88c32ae0c49e31d",
    "infrastructure/database/README.md": "c215512cb10214142ecd9a2fcaa0aba4c28ff30a0af527b8e653c9f01c9cf643",
    "infrastructure/database/init/README.md": "36947721a74ab381f6c87513d5e69ad53a6b6b4eabd3fe9a896fd2ce5e94efeb",
    "infrastructure/nginx/README.md": "7d489deff3b6604cbfb6424d388c4d915a2710239498d7ca0ba686cbce3af887",
    "reports/COMPARACION_MATRIX_V2.md": "01d0aadd75a211ea5f8a2b91d288b9d27303f498fa48d9376da84ba355361477",
    "reports/README.md": "da3770b1ae8a881bb2c1d7b12925d8fa2f7dddd69536eea48db2834ff6cbcc57",
    "reports/VALIDACION_1.3.0.md": "b9481d3320c6b07a8ce4263c1b2c2f3969727354bc171fcb90737bcca079ecda",
}

LEGACY_LAUNCHERS = (
    "ACTUALIZAR_MATRIX_RH.bat",
    "PROBAR_MODELOS_MATRIX_RH.bat",
    "install.bat",
)
LEGACY_ASSET = "frontend/dist/assets/index-B6hzhSCJ.js"
LEGACY_ASSET_SHA256 = "87e7a0c62c0d3569470f5557a8cd57225a722679eaefb4c4d6e2f918cec16d77"
FRONTEND_INDEX = "frontend/dist/index.html"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class UnsafeLayoutError(ValueError):
    """Una ruta no cumple las condiciones de mantenimiento local."""

    def __init__(self, message: str, code: str = "unsafe_layout") -> None:
        super().__init__(message)
        self.code = code


@contextmanager
def _operation(phase: str, relative: str) -> Iterator[None]:
    """Anota contexto local sin volcar mensajes/rutas arbitrarios del sistema."""
    try:
        yield
    except (OSError, UnsafeLayoutError) as error:
        if not hasattr(error, "cleanup_phase"):
            error.__dict__.update(cleanup_phase=phase, cleanup_relative=relative)
        raise


class CleanupResult(TypedDict):
    ok: bool
    retired: int
    preserved_modified: int
    preserved_referenced: int
    preserved_unchecked_asset: int
    absent: int
    backup: str | None
    retired_paths: list[str]
    preserved_paths: list[str]


@dataclass(frozen=True)
class _Candidate:
    relative: str
    digest: str
    identity: tuple[int, ...]
    content: bytes


def _redirected(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _guard_path(path: Path) -> None:
    """Inspecciona cada componente sin resolver enlaces ni junctions."""
    for current in reversed((path, *path.parents)):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if _redirected(info):
            raise UnsafeLayoutError("No se permiten enlaces ni junctions en las rutas de limpieza.", "redirected_path")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise UnsafeLayoutError("Un componente de la ruta no es un directorio.")


def _path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if (
        parts.is_absolute() or not parts.parts
        or any(piece in (".", "..") or ":" in piece or "\\" in piece for piece in parts.parts)
    ):
        raise UnsafeLayoutError("Ruta relativa de limpieza no valida.")
    path = root.joinpath(*parts.parts)
    _guard_path(path)
    return path


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _opened_identity(info: os.stat_result) -> tuple[int, ...]:
    """Campos comparables entre lstat(path) y fstat(fd), tambien en Windows.

    lstat puede agregar bits de ejecucion por extension (.bat), y su ctime
    puede representar nacimiento mientras fstat devuelve cambio de metadata.
    No comparar esos campos entre APIs. Sus cambios SI se comprueban con
    _identity contra la lectura anterior de la MISMA API.
    """
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns)


def _read(root: Path, relative: str) -> _Candidate | None:
    with _operation("read", relative):
        return _read_checked(root, relative)


def _read_checked(root: Path, relative: str) -> _Candidate | None:
    path = _path(root, relative)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise UnsafeLayoutError("Un archivo de limpieza no es un archivo regular.", "not_regular_file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        opened = os.fstat(handle.fileno())
        if _opened_identity(opened) != _opened_identity(before) or _redirected(opened):
            raise UnsafeLayoutError("El archivo cambio durante la revision.", "file_changed_during_open")
        content = handle.read()
        if _identity(os.fstat(handle.fileno())) != _identity(opened):
            raise UnsafeLayoutError("El archivo cambio durante la lectura.", "file_changed_during_read")
    _guard_path(path)
    if _identity(path.lstat()) != _identity(before):
        raise UnsafeLayoutError("El archivo fue reemplazado durante la revision.", "file_replaced")
    return _Candidate(relative, hashlib.sha256(content).hexdigest(), _identity(before), content)


def _unchanged(root: Path, original: _Candidate) -> None:
    current = _read(root, original.relative)
    if current is None or (current.identity, current.digest) != (original.identity, original.digest):
        raise UnsafeLayoutError("Un archivo cambio; se conserva y se cancela la limpieza.", "file_changed")


def _plan(root: Path) -> tuple[list[_Candidate], CleanupResult, _Candidate | None]:
    candidates = []
    result: CleanupResult = {
        "ok": True,
        "retired": 0,
        "preserved_modified": 0,
        "preserved_referenced": 0,
        "preserved_unchecked_asset": 0,
        "absent": 0,
        "backup": None,
        "retired_paths": [],
        "preserved_paths": [],
    }
    for relative in LEGACY_LAUNCHERS:
        candidate = _read(root, relative)
        if candidate is None:
            result["absent"] += 1
        else:
            candidates.append(candidate)
    for relative, digest in LEGACY_DOCUMENT_HASHES.items():
        candidate = _read(root, relative)
        if candidate is None:
            result["absent"] += 1
        elif candidate.digest == digest:
            candidates.append(candidate)
        else:
            result["preserved_modified"] += 1
            result["preserved_paths"].append(relative)
    asset = _read(root, LEGACY_ASSET)
    index = None
    if asset is None:
        result["absent"] += 1
    elif asset.digest != LEGACY_ASSET_SHA256:
        result["preserved_modified"] += 1
        result["preserved_paths"].append(LEGACY_ASSET)
    else:
        index = _read(root, FRONTEND_INDEX)
        if index is None:
            result["preserved_unchecked_asset"] += 1
            result["preserved_paths"].append(LEGACY_ASSET)
        elif Path(LEGACY_ASSET).name.casefold() in html.unescape(
            unquote(index.content.decode("utf-8-sig", errors="replace"))
        ).casefold():
            result["preserved_referenced"] += 1
            result["preserved_paths"].append(LEGACY_ASSET)
        else:
            candidates.append(asset)
    # Validar tambien los destinos aunque no haya archivos para retirar.
    backup_parent = _path(root, "var/backups")
    if backup_parent.exists() and not backup_parent.is_dir():
        raise UnsafeLayoutError("La ruta de respaldos no es un directorio.", "backup_not_directory")
    return candidates, result, index


def _mkdir_checked(root: Path, path: Path) -> None:
    _guard_path(path)
    path.mkdir(parents=True, exist_ok=True)
    _guard_path(path)
    if not path.is_dir() or path.stat().st_dev != root.stat().st_dev:
        raise UnsafeLayoutError(
            "El respaldo debe permanecer en el mismo sistema de archivos.", "backup_other_filesystem",
        )


def _write_backup(root: Path, staging: Path, candidate: _Candidate) -> None:
    target = staging / (candidate.relative + ".retired")
    _mkdir_checked(root, target.parent)
    _guard_path(target)
    with target.open("xb") as handle:
        handle.write(candidate.content)
        handle.flush()
        os.fsync(handle.fileno())
    saved = _read(root, target.relative_to(root).as_posix())
    if saved is None or saved.digest != candidate.digest:
        raise UnsafeLayoutError("El respaldo no coincide con el archivo original.", "backup_mismatch")


def _publish_backup(root: Path, candidates: list[_Candidate]) -> Path:
    parent = _path(root, "var/backups")
    _mkdir_checked(root, parent)
    staging = Path(tempfile.mkdtemp(prefix=".layout-staging-", dir=parent))
    _guard_path(staging)
    for candidate in candidates:
        _write_backup(root, staging, candidate)
    destination = parent / ("layout-" + uuid4().hex)
    _guard_path(destination)
    if destination.exists():
        raise UnsafeLayoutError("El destino del respaldo ya existe.", "backup_exists")
    _guard_path(staging)
    staging.rename(destination)
    _guard_path(destination)
    return destination


def clean(root: Path | str) -> CleanupResult:
    """Devuelve conteos y rutas relativas; jamas devuelve contenidos."""
    # abspath normaliza '.' sin seguir enlaces. No usar resolve() aqui.
    root = Path(os.path.abspath(os.fspath(root)))
    with _operation("inspect", "."):
        _guard_path(root)
        if not root.is_dir():
            raise UnsafeLayoutError("La raiz del proyecto no es un directorio.", "invalid_root")
        candidates, result, index = _plan(root)
    if not candidates:
        return result
    # Revalidar el plan completo antes de crear archivos.
    for candidate in candidates:
        with _operation("revalidate", candidate.relative):
            _unchanged(root, candidate)
    if index is not None:
        with _operation("revalidate", index.relative):
            _unchanged(root, index)
    with _operation("backup", "var/backups"):
        backup = _publish_backup(root, candidates)
    result["backup"] = backup.relative_to(root).as_posix()
    # Todos los respaldos ya existen antes de retirar el primer archivo.
    for candidate in candidates:
        with _operation("retire", candidate.relative):
            _unchanged(root, candidate)
            if candidate.relative == LEGACY_ASSET and index is not None:
                _unchanged(root, index)
            saved = _read(root, (backup / (candidate.relative + ".retired")).relative_to(root).as_posix())
            if saved is None or saved.digest != candidate.digest:
                raise UnsafeLayoutError("El respaldo cambio; no se retira el archivo.", "backup_changed")
            source = _path(root, candidate.relative)
            # Ultima comprobacion inmediatamente antes de unlink.
            if _identity(source.lstat()) != candidate.identity:
                raise UnsafeLayoutError("El archivo cambio antes de retirarlo.", "file_changed")
            source.unlink()
        result["retired"] += 1
        result["retired_paths"].append(candidate.relative)
    return result


def _failure_details(error: OSError | UnsafeLayoutError) -> dict:
    """Diagnostico accionable sin contenido de archivos ni excepciones crudas."""
    details = {
        "unsafe_layout": "La ruta no cumple las condiciones de limpieza segura.",
        "redirected_path": "La ruta contiene un enlace o junction; no se sigue ni se elimina.",
        "not_regular_file": "La entrada esperada como archivo no es un archivo regular.",
        "file_changed_during_open": "La identidad, tipo, tamano o fecha del archivo cambio al abrirlo.",
        "file_changed_during_read": "El archivo abierto cambio durante la lectura.",
        "file_replaced": "La ruta fue reemplazada durante la revision.",
        "file_changed": "El archivo cambio despues de planificar su retirada; se conserva.",
        "backup_not_directory": "var/backups existe pero no es un directorio.",
        "backup_other_filesystem": "El respaldo debe quedar en el mismo sistema de archivos.",
        "backup_mismatch": "El hash del respaldo no coincide con el original.",
        "backup_exists": "El destino del respaldo ya existe; no se sobrescribe.",
        "backup_changed": "El respaldo cambio; no se elimina el original.",
        "invalid_root": "La raiz indicada no es un directorio existente.",
        "permission_denied": "Acceso denegado; revisar permisos o atributo de solo lectura de la ruta indicada.",
        "file_in_use": "Otro proceso tiene abierto el archivo; cerrarlo y volver a ejecutar Instalar.",
        "disk_full": "No hay espacio disponible para completar el respaldo.",
        "read_only_filesystem": "El sistema de archivos no permite escritura.",
        "filesystem_error": "El sistema no pudo completar la operacion; revisar errno/winerror y ruta.",
    }
    number = error.errno if isinstance(error, OSError) else None
    winerror = getattr(error, "winerror", None)
    if isinstance(error, UnsafeLayoutError):
        code = error.code if error.code in details else "unsafe_layout"
    elif winerror in (32, 33):
        code = "file_in_use"
    else:
        code = {
            errno.EACCES: "permission_denied", errno.EPERM: "permission_denied",
            errno.ENOSPC: "disk_full", errno.EROFS: "read_only_filesystem",
        }.get(number, "filesystem_error") if number is not None else "filesystem_error"
    relative = getattr(error, "cleanup_relative", None)
    if relative is not None and (
        not isinstance(relative, str) or not relative.isprintable()
        or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
        or ":" in relative or "\\" in relative
    ):
        relative = None
    phase = getattr(error, "cleanup_phase", None)
    if phase not in {"inspect", "read", "revalidate", "backup", "retire"}:
        phase = None
    return {"code": code, "detail": details[code], "phase": phase, "path": relative,
            "errno": number, "winerror": winerror}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archiva y retira restos conocidos de Matrix RH.")
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[2])
    args = parser.parse_args(argv)
    try:
        result = clean(args.root)
    except (OSError, UnsafeLayoutError) as error:
        # No imprimir excepciones con rutas externas o datos del sistema.
        print(json.dumps({
            "ok": False,
            "error": "Limpieza cancelada: ruta insegura, archivo modificado o respaldo no disponible.",
            **_failure_details(error),
            "recovery": "Los archivos retirados permanecen respaldados en var/backups/layout-*.",
        }, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
