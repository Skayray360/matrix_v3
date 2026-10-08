# Creado por Aldo Garcia.
"""Smoke test de autorizacion Matrix vs MatrixR1 sin levantar el servidor.

Ejercita la ruta completa orquestador -> RAG -> modelos con los dos usuarios
sinteticos y comprueba la matriz de la seccion 6.1. Es la version rapida del
gate critico; la version formal vive en ``tests/security`` y en Playwright.

Uso:
    python -m scripts.smoke_authorization
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from app.agents.orchestrator import Orchestrator
from app.authorization.policy import get_policy_engine
from app.common.ids import new_id
from app.database.engine import session_scope
from app.database.models import User
from app.memory.service import MemoryService


def _context(db, username: str):  # noqa: ANN001, ANN202
    user = db.execute(select(User).where(User.username == username)).scalar_one()
    return get_policy_engine().build_context(
        db, user=user, session_id=new_id(), request_id=new_id()
    )


PROBES: tuple[tuple[str, str], ...] = (
    ("prestaciones", "Cuantos dias de vacaciones corresponden a 5 anios de antiguedad?"),
    ("nomina", "Que dia se paga la nomina del personal de confianza?"),
    ("reclutamiento", "Cuantas etapas tiene el proceso de reclutamiento?"),
    ("relaciones_laborales", "Cuantos minutos de tolerancia hay antes de un retardo?"),
    ("salud_ambiental", "Con que frecuencia se realizan los simulacros?"),
)


def main() -> int:
    orchestrator = Orchestrator()
    memory = MemoryService()
    failures: list[str] = []

    with session_scope() as db:
        for username, expect_allowed in (("Matrix", "todas"), ("MatrixR1", "solo prestaciones")):
            ctx = _context(db, username)
            categories = get_policy_engine().effective_categories(ctx)
            print(f"\n=== {username} ({expect_allowed}) ===")
            print(f"categorias efectivas: {sorted(categories)}")

            for category, question in PROBES:
                conversation = memory.create_conversation(db, ctx, title=f"probe-{category}")
                outcome = orchestrator.handle_chat(
                    db, ctx=ctx, conversation=conversation, message=question
                )
                leaked = [s for s in outcome.evidences if s.category != category]
                allowed_expected = username == "Matrix" or category == "prestaciones"
                got_evidence = any(e.category == category for e in outcome.evidences)

                mark = "OK "
                if allowed_expected and not got_evidence:
                    mark = "FAIL"
                    failures.append(f"{username}/{category}: sin evidencia esperada")
                if not allowed_expected and got_evidence:
                    mark = "LEAK"
                    failures.append(f"{username}/{category}: FUGA DE ACL")
                categorias = sorted({e.category for e in outcome.evidences})
                print(
                    f"  [{mark}] {category:22s} evidencia={len(outcome.evidences)} "
                    f"modelo={outcome.model or '-'} categorias={categorias}"
                )
                if leaked and not allowed_expected:
                    failures.append(f"{username}/{category}: categorias ajenas {leaked}")

    print("\n--- RESULTADO ---")
    if failures:
        for failure in failures:
            print(f"FALLO: {failure}")
        return 1
    print("Matriz de autorizacion correcta.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
