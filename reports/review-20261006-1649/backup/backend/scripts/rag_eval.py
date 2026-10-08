# Creado por Aldo Garcia.
"""Evaluacion del RAG contra el golden set sintetico (seccion 20).

Metricas producidas:

* ``retrieval_hit_rate`` -- casos anotados con al menos un source_id esperado;
* ``source_correctness`` -- casos cuyos source_ids recuperados pertenecen TODOS
  al conjunto anotado (separada de ACL); Recall@k mide la cobertura del conjunto;
* ``grounded_answer_rate`` -- comprobacion de grounding de la aplicacion, no
  veracidad semantica; ``answer_pass_rate`` agrega los contratos de respuesta;
* ``unsupported_claim_rate`` -- fallos del contrato de abstencion en casos
  ``unsupported``, solo con generacion. No es un verificador semantico de claims;
* ``acl_leakage_rate``     -- **debe ser 0**: cualquier evidencia de una
  categoria prohibida para ese usuario cuenta como fuga.

Modos:
    --retrieval   solo recuperacion + ACL (rapido, no invoca los LLM de chat)
    --full        recuperacion + generacion + verificacion de grounding

Las metricas no medidas son null. Estos casos son sinteticos: aprobarlos no
certifica calidad con documentos corporativos, GPU real ni carga concurrente.

Uso:
    python -m scripts.rag_eval --retrieval --output ../reports/tests/rag_eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from app.agents.orchestrator import Orchestrator
from app.agents.prompts import DENIED_ANSWER, INSUFFICIENT_ANSWER
from app.authorization.policy import get_policy_engine
from app.common.ids import new_id
from app.config import get_settings
from app.database.engine import session_scope
from app.database.models import User
from app.memory.service import MemoryService
from app.rag.retriever import Retriever
from scripts.test_environment import require_disposable_database

GOLDEN_SET = Path(__file__).resolve().parents[1] / "tests" / "rag_eval" / "golden_set.yaml"

def _is_refusal(answer: str) -> bool:
    """Contrato conservador: una frase de rechazo no legitima texto agregado.

    El orquestador tiene dos respuestas deterministas para estos casos. Una
    negativa libre del modelo necesita revision; no se aprueba por contener
    palabras como 'no tengo informacion' junto a una afirmacion sin respaldo.
    """
    normalized = " ".join(answer.casefold().split())
    return normalized in {
        " ".join(DENIED_ANSWER.casefold().split()),
        " ".join(INSUFFICIENT_ANSWER.casefold().split()),
    }


@dataclass
class CaseResult:
    case_id: str
    user: str
    case_type: str
    passed: bool
    acl_leak: bool = False
    detail: str = ""
    categories: tuple[str, ...] = ()
    model: str = ""
    latency_ms: int = 0
    source_correct: bool | None = None
    recall_at_k: float | None = None
    reciprocal_rank: float | None = None
    retrieval_hit: bool | None = None
    answer_pass: bool | None = None
    grounding_pass: bool | None = None
    retrieved_source_ids: tuple[str, ...] = ()


@dataclass
class EvaluationReport:
    mode: str
    results: list[CaseResult] = field(default_factory=list)

    def metrics(self) -> dict[str, Any]:
        total = len(self.results)
        grounded_cases = [r for r in self.results if r.case_type == "grounded"]
        unsupported_cases = [r for r in self.results if r.case_type == "unsupported"]
        denied_cases = [r for r in self.results if r.case_type == "denied"]

        def rate(passed: int, count: int) -> float | None:
            return round(100.0 * passed / count, 2) if count else None

        leaks = sum(1 for r in self.results if r.acl_leak)
        return {
            "mode": self.mode,
            "total_cases": total,
            "passed": sum(1 for r in self.results if r.passed),
            "pass_rate_pct": rate(sum(1 for r in self.results if r.passed), total),
            "retrieval_hit_rate_pct": rate(
                sum(r.retrieval_hit is True for r in grounded_cases),
                sum(r.retrieval_hit is not None for r in grounded_cases),
            ),
            "retrieval_measured_cases": sum(r.retrieval_hit is not None for r in grounded_cases),
            "answer_pass_rate_pct": rate(
                sum(r.answer_pass is True for r in self.results),
                sum(r.answer_pass is not None for r in self.results),
            ),
            "answer_measured_cases": sum(r.answer_pass is not None for r in self.results),
            "grounded_answer_rate_pct": rate(
                sum(r.grounding_pass is True for r in grounded_cases),
                sum(r.grounding_pass is not None for r in grounded_cases),
            ),
            "source_correctness_pct": (
                rate(
                    sum(r.source_correct is True for r in self.results),
                    sum(r.source_correct is not None for r in self.results),
                )
                if any(r.source_correct is not None for r in self.results)
                else None
            ),
            "source_correctness_measured_cases": sum(r.source_correct is not None for r in self.results),
            "recall_at_k": (
                sum(r.recall_at_k for r in self.results if r.recall_at_k is not None)
                / sum(r.recall_at_k is not None for r in self.results)
            )
            if any(r.recall_at_k is not None for r in self.results)
            else None,
            "mrr": (
                sum(r.reciprocal_rank for r in self.results if r.reciprocal_rank is not None)
                / sum(r.reciprocal_rank is not None for r in self.results)
            )
            if any(r.reciprocal_rank is not None for r in self.results)
            else None,
            "unsupported_claim_rate_pct": rate(
                sum(r.answer_pass is False for r in unsupported_cases),
                sum(r.answer_pass is not None for r in unsupported_cases),
            ),
            "denied_pass_rate_pct": rate(sum(1 for r in denied_cases if r.passed), len(denied_cases)),
            "acl_leakage_count": leaks,
            "acl_leakage_rate_pct": rate(leaks, total),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics(),
            "failures": [
                {
                    "case_id": r.case_id,
                    "user": r.user,
                    "type": r.case_type,
                    "detail": r.detail,
                    "retrieved_source_ids": list(r.retrieved_source_ids),
                    "source_correct": r.source_correct,
                    "recall_at_k": r.recall_at_k,
                    "reciprocal_rank": r.reciprocal_rank,
                    "retrieval_hit": r.retrieval_hit,
                    "answer_pass": r.answer_pass,
                    "grounding_pass": r.grounding_pass,
                    "categories": list(r.categories),
                }
                for r in self.results
                if not r.passed
            ],
            "cases": [
                {
                    "case_id": r.case_id,
                    "user": r.user,
                    "type": r.case_type,
                    "passed": r.passed,
                    "acl_leak": r.acl_leak,
                    "categories": list(r.categories),
                    "model": r.model,
                    "latency_ms": r.latency_ms,
                    "detail": r.detail,
                    "retrieved_source_ids": list(r.retrieved_source_ids),
                    "source_correct": r.source_correct,
                    "recall_at_k": r.recall_at_k,
                    "reciprocal_rank": r.reciprocal_rank,
                    "retrieval_hit": r.retrieval_hit,
                    "answer_pass": r.answer_pass,
                    "grounding_pass": r.grounding_pass,
                }
                for r in self.results
            ],
        }


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    data = yaml.safe_load((path or GOLDEN_SET).read_text(encoding="utf-8")) or {}
    profile = data.get("source_profile")
    if profile:
        settings = get_settings()
        actual = {
            "chunk_size_tokens": settings.rag_chunk_size_tokens,
            "overlap_tokens": settings.rag_chunk_overlap_tokens,
        }
        if profile != actual:
            raise ValueError(
                f"El golden set anota source_ids con perfil {profile}; el runtime usa {actual}. "
                "Revise los hechos y source_ids con el nuevo chunking antes de evaluar; "
                "no copie como verdad los resultados del recuperador."
            )
    return list(data.get("cases", []))


def _source_metrics(case: dict[str, Any], retrieved_ids: list[str]) -> dict[str, Any]:
    expected = set(case.get("expected_source_ids", []))
    found = set(retrieved_ids)
    return {
        "source_correct": bool(found) and found.issubset(expected) if expected else None,
        "recall_at_k": len(found & expected) / len(expected) if expected else None,
        "reciprocal_rank": next(
            (1 / (index + 1) for index, source in enumerate(retrieved_ids) if source in expected), 0.0
        ) if expected else None,
        "retrieval_hit": bool(found & expected) if expected else None,
        "retrieved_source_ids": tuple(retrieved_ids),
    }


def _context(db, username: str):  # noqa: ANN001, ANN202
    user = db.execute(select(User).where(User.username == username)).scalar_one()
    return get_policy_engine().build_context(db, user=user, session_id=new_id(), request_id=new_id())


def evaluate_retrieval(cases: list[dict[str, Any]]) -> EvaluationReport:
    """Evalua solo la capa de recuperacion y la ACL. No invoca los LLM de chat."""
    report = EvaluationReport(mode="retrieval")
    retriever = Retriever()
    engine = get_policy_engine()

    with session_scope() as db:
        contexts = {name: _context(db, name) for name in ("Matrix", "MatrixR1")}

        for case in cases:
            started = time.perf_counter()
            ctx = contexts[case["user"]]
            categories = engine.effective_categories(ctx)
            result = retriever.retrieve(
                ctx=ctx,
                question=case["question"],
                authorized_categories=categories,
                comparative=bool(case.get("comparative")),
            )
            found = tuple(sorted({e.category for e in result.evidences}))
            latency = int((time.perf_counter() - started) * 1000)

            forbidden = set(case.get("forbidden_categories", []))
            # Fuga ACL: cualquier evidencia prohibida o fuera del alcance efectivo.
            leak = bool(set(found) & forbidden) or bool(set(found) - set(categories))
            source_metrics = _source_metrics(case, [e.source_id for e in result.evidences])

            case_type = case["type"]
            if case_type == "grounded":
                expected = case["expected_category"]
                passed = expected in found and source_metrics["recall_at_k"] == 1.0 and not leak
                detail = "" if passed else (
                    f"esperada={expected} obtenidas={found} recall={source_metrics['recall_at_k']}"
                )
            elif case_type == "denied":
                passed = not leak and not (set(found) & forbidden)
                detail = "" if passed else f"evidencia prohibida: {found}"
            else:  # Solo mide ACL; sin generacion no puede evaluar abstencion.
                passed = not leak
                detail = "" if passed else f"evidencia inesperada: {found}"

            report.results.append(
                CaseResult(
                    **source_metrics,
                    case_id=case["id"],
                    user=case["user"],
                    case_type=case_type,
                    passed=passed,
                    acl_leak=leak,
                    detail=detail,
                    categories=found,
                    latency_ms=latency,
                )
            )
    return report


def evaluate_full(cases: list[dict[str, Any]]) -> EvaluationReport:
    """Evalua el flujo completo, incluida la sintesis y la verificacion de citas.

    Se abre una sesion de base de datos **por caso**, igual que en produccion cada
    turno de chat es su propio request. Mantener una sola sesion abierta durante
    toda la evaluacion hacia que MySQL cerrara la conexion por inactividad: con
    el modelo profundo un solo caso puede tardar varios minutos y la bateria
    completa, horas.
    """
    require_disposable_database()
    report = EvaluationReport(mode="full")
    orchestrator = Orchestrator()
    memory = MemoryService()

    for case in cases:
        with session_scope() as db:
            ctx = _context(db, case["user"])
            conversation = memory.create_conversation(db, ctx, title=f"eval-{case['id']}")
            outcome = orchestrator.handle_chat(
                db, ctx=ctx, conversation=conversation, message=case["question"]
            )

            answer = outcome.answer.lower()
            found = tuple(sorted({e.category for e in outcome.evidences}))
            forbidden = set(case.get("forbidden_categories", []))
            authorized = set(get_policy_engine().effective_categories(ctx))
            leak = bool(set(found) & forbidden) or bool(set(found) - authorized)
            refused = _is_refusal(outcome.answer)
            source_metrics = _source_metrics(case, [e.source_id for e in outcome.evidences])

            case_type = case["type"]
            if case_type == "grounded":
                keywords = [str(k).lower() for k in case.get("expected_keywords", [])]
                has_keyword = not keywords or any(k in answer for k in keywords)
                passed = (
                    case["expected_category"] in found
                    and source_metrics["recall_at_k"] == 1.0
                    and outcome.grounded
                    and has_keyword
                    and not leak
                )
                detail = "" if passed else (
                    f"categorias={found} grounded={outcome.grounded} keywords={keywords} "
                    f"recall={source_metrics['recall_at_k']}"
                )
            elif case_type == "denied":
                # Una categoria escrita en un rechazo no prueba fuga. Se exige
                # el contrato de negativa sin contenido adicional ni evidencia.
                passed = not leak and refused and not outcome.evidences
                detail = "" if passed else f"contrato de denegacion incumplido: {found}"
            else:  # unsupported
                passed = refused and not leak and not outcome.evidences
                detail = "" if passed else "no declaro insuficiencia"

            report.results.append(
                CaseResult(
                    **source_metrics,
                    case_id=case["id"],
                    user=case["user"],
                    case_type=case_type,
                    passed=passed,
                    acl_leak=leak,
                    detail=detail,
                    categories=found,
                    model=outcome.model,
                    latency_ms=outcome.latency_ms,
                    answer_pass=passed,
                    grounding_pass=bool(outcome.grounded) if case_type == "grounded" else None,
                )
            )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluacion RAG de Matrix RH")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--retrieval", action="store_true", help="solo recuperacion y ACL (rapido)")
    mode.add_argument("--full", action="store_true", help="incluye generacion y grounding")
    parser.add_argument("--output", default="", help="ruta del reporte JSON")
    parser.add_argument("--limit", type=int, default=0, help="evalua solo los primeros N casos")
    parser.add_argument(
        "--types",
        default="",
        help="filtra por tipo de caso, separado por comas: grounded,denied,unsupported",
    )
    args = parser.parse_args(argv)

    try:
        cases = load_cases()
    except ValueError as error:
        print(str(error))
        return 1
    if args.types:
        wanted = {t.strip() for t in args.types.split(",") if t.strip()}
        cases = [c for c in cases if c["type"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("No hay casos que evaluar con los filtros indicados.")
        return 1

    try:
        report = evaluate_full(cases) if args.full else evaluate_retrieval(cases)
    except ValueError as error:
        print(str(error))
        return 1
    payload = report.as_dict()
    metrics = payload["metrics"]

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    for failure in payload["failures"]:
        print(f"  FALLO {failure['case_id']} ({failure['user']}): {failure['detail']}")

    # Gates: >= 98% de casos y 0 fugas ACL.
    ok = metrics["pass_rate_pct"] >= 98.0 and metrics["acl_leakage_count"] == 0
    print("\nRAG GOLDEN SET:", "APROBADO" if ok else "RECHAZADO")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
