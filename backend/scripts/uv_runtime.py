# Creado por Aldo Garcia.
"""Comprueba uv antes de sincronizar paquetes; solo usa la biblioteca estandar.

La compatibilidad del lock y los flags se comprobo con uv 0.11.33 y 0.12.18.
Se admiten los parches desde 0.11.33 de esas dos ramas; CI conserva su pin 0.12.8.
No se mezclan paquetes resueltos de nuevo: el instalador mantiene --frozen.
"""

from __future__ import annotations

import argparse
import re

MIN_VERSION = (0, 11, 33)
MAX_VERSION_EXCLUSIVE = (0, 13, 0)
SUPPORTED_RANGE = ">=0.11.33,<0.13.0"

_BANNER = re.compile(
    r"\Auv (?P<version>\d+\.\d+\.\d+)"
    r"(?: \((?:[0-9A-Fa-f]{7,64} +\d{4}-\d{2}-\d{2}"
    r"(?: +(?P<dated_target>[A-Za-z0-9_-]+))?|(?P<target>[A-Za-z0-9_-]+))\))?\Z"
)


def compatible_uv_version(banner: str, *, expected_target: str = "") -> str:
    """Acepta una version estable y metadata oficial; rechaza salidas ambiguas o un target ajeno."""
    match = _BANNER.fullmatch(banner)
    if match is None:
        raise ValueError("Salida de 'uv --version' invalida; se esperaba una sola linea de version estable.")
    version = match.group("version")
    parts = tuple(int(part) for part in version.split("."))
    if not MIN_VERSION <= parts < MAX_VERSION_EXCLUSIVE:
        raise ValueError(f"Se admite uv {SUPPORTED_RANGE}; encontrado {version}.")
    target = match.group("dated_target") or match.group("target")
    if expected_target and target and target != expected_target:
        raise ValueError(f"uv declara el target {target}; se requiere {expected_target}.")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compatibilidad de uv para instalar Matrix RH")
    parser.add_argument("--banner", required=True, help="unica linea devuelta por uv --version")
    parser.add_argument("--target", default="", help="target esperado si uv lo declara")
    args = parser.parse_args(argv)
    try:
        version = compatible_uv_version(args.banner, expected_target=args.target)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[ OK ] uv {version} compatible; la instalacion usara uv.lock con --frozen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
