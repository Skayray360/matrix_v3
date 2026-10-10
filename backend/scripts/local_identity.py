# Creado por Aldo Garcia.
"""Bootstrap y mantenimiento local de identidad; nunca imprime contrasenas."""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.auth.local_accounts import (
    LOCAL_ADMIN_ROLE,
    LOCAL_READER_ROLE,
    bootstrap_administrator,
    create_local_account,
    replace_local_password,
    validate_password,
)
from app.common.errors import MatrixError, ValidationFailedError
from app.config import PROJECT_ROOT
from app.database.engine import session_scope
from app.database.models import User


def read_password(path: Path | None, *, root: Path = PROJECT_ROOT) -> str:
    if path is None:
        password = getpass.getpass("Nueva contrasena: ")
        confirmation = getpass.getpass("Repita la contrasena: ")
        if password != confirmation:
            raise ValidationFailedError("Las contrasenas no coinciden.")
    else:
        secret_root = (root / "backend/config/secrets").resolve()
        target = path if path.is_absolute() else root / path
        if (target.is_symlink() or target.absolute() != target.resolve()
                or not target.resolve().is_relative_to(secret_root) or not target.is_file()
                or target.stat().st_size > 1024):
            raise ValidationFailedError("Use un archivo regular dentro de backend/config/secrets.")
        password = target.read_text(encoding="utf-8-sig").rstrip("\r\n")
    validate_password(password)
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("bootstrap", "create-user", "change-password"):
        command = commands.add_parser(name)
        command.add_argument("--username", required=True)
        command.add_argument("--password-file", type=Path, required=name == "bootstrap")
        if name == "create-user":
            command.add_argument("--display-name", default="")
            command.add_argument("--role", choices=(LOCAL_ADMIN_ROLE, LOCAL_READER_ROLE), default=LOCAL_READER_ROLE)
    args = parser.parse_args(argv)
    try:
        password = read_password(args.password_file)
        with session_scope() as db:
            if args.action == "bootstrap":
                _, created = bootstrap_administrator(db, username=args.username, password=password)
                result = {"ok": True, "action": args.action, "created": created, "credentials_reset": False}
            elif args.action == "create-user":
                create_local_account(db, username=args.username, display_name=args.display_name or args.username,
                                     password=password, role_name=args.role)
                result = {"ok": True, "action": args.action, "created": True}
            else:
                user = db.execute(select(User).where(User.username == args.username)
                                  .with_for_update()).scalar_one_or_none()
                if user is None:
                    raise ValidationFailedError("No existe la cuenta local solicitada.")
                count = replace_local_password(db, user=user, password=password)
                result = {"ok": True, "action": args.action, "revoked_sessions": count}
        print(json.dumps(result))
        return 0
    except (MatrixError, SQLAlchemyError, OSError, UnicodeError, ValueError):
        print(json.dumps({"ok": False, "action": args.action,
                          "message": "Revise el proveedor local, la cuenta, la base y el archivo privado; "
                                     "no se modificaron credenciales existentes por bootstrap."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
