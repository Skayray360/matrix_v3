# Creado por Aldo Garcia.
"""Purga de contenido de Matrix RH; solo lectura hasta proporcionar --apply.

Ejemplo: python -m scripts.purge --deleted-only --batch-size 100 --apply
Repetir hasta que conversation_candidates sea cero. En modo embedded debe
ejecutarse con el backend detenido para no competir por el lock de Qdrant.
"""

from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Confirmar la purga del lote seleccionado.")
    parser.add_argument("--deleted-only", action="store_true", help="Solo contenido ya eliminado por sus usuarios.")
    parser.add_argument("--batch-size", type=int, default=None, help="Conversaciones por pasada (1..1000).")
    args = parser.parse_args(argv)
    if args.batch_size is not None and not 1 <= args.batch_size <= 1000:
        parser.error("--batch-size debe estar entre 1 y 1000")
    from app.database.engine import session_scope
    from app.jobs.retention import preview_maintenance, run_maintenance

    with session_scope() as db:
        if args.apply:
            stats = run_maintenance(db, batch_size=args.batch_size, deleted_only=args.deleted_only)
            print(json.dumps({"apply": True, **stats.to_dict()}, ensure_ascii=False, indent=2))
            return 1 if stats.pending_cleanup else 0
        print(json.dumps(preview_maintenance(
            db, batch_size=args.batch_size, deleted_only=args.deleted_only
        ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
