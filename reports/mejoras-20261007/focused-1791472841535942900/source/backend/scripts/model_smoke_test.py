# Creado por Aldo Garcia.
"""Aceptacion sintetica y explicita del adapter/modelo local configurado en .env.

No descarga modelos, no abre SQL/Qdrant y no lee documentos corporativos. Sin
--run-inference no carga configuracion ni abre conexiones. No certifica calidad
documental, multimodalidad, rendimiento sostenido ni capacidad concurrente.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Literal

from app.common.inference_errors import InferenceFailureError
from app.config import get_settings
from app.llm.provider import ModelClient
from app.structured_data.schemas import ComparisonOperator, QueryFilter, StructuredQueryPlan

Profile = Literal["fast", "deep", "all"]


def _failure_fields(error: Exception) -> dict[str, int | str | bool | None]:
    if isinstance(error, InferenceFailureError):
        return {"failure_kind": error.failure_kind.value,
                **(error.completion.log_fields() if error.completion is not None else {})}
    return {}


def _add(
    report: dict[str, Any],
    name: str,
    passed: bool,
    code: str,
    *,
    started: float | None = None,
    **metrics: int | str | None,
) -> None:
    check = {"name": name, "passed": passed, "code": code, **metrics}
    if started is not None:
        check["latency_ms"] = int((time.perf_counter() - started) * 1000)
    report["checks"].append(check)
    report["passed"] = all(item["passed"] for item in report["checks"])


def run_smoke_test(profile: Profile = "fast") -> dict[str, Any]:
    """Solo debe invocarse despues del consentimiento explicito para inferencia.

    Los detalles de excepciones pueden contener URLs/credenciales/respuestas;
    por ello el informe emite exclusivamente codigos fijos y medidas numericas.
    """
    report: dict[str, Any] = {
        "format_version": 1,
        "scope": "synthetic_local_adapter_smoke",
        "profile": profile,
        "status": "blocked",
        "passed": False,
        "checks": [],
        "corporate_rag_validated": False,
        "concurrency_validated": False,
        "multimodal_validated": False,
    }
    if profile not in ("fast", "deep", "all"):
        _add(report, "configuration", False, "invalid_profile")
        return report
    try:
        settings = get_settings()
    except Exception:
        _add(report, "configuration", False, "configuration_invalid")
        return report
    providers = (settings.llm_provider, settings.llm_deep_provider, settings.llm_embedding_provider)
    if not settings.llm_local_only or "vertex" in providers:
        _add(report, "configuration", False, "local_only_required")
        return report
    client = None
    try:
        # El constructor valida allowlist, URLs y modelos cloud antes del HTTP.
        try:
            client = ModelClient()
        except Exception:
            _add(report, "configuration", False, "local_adapter_rejected")
            return report
        started = time.perf_counter()
        try:
            inventory = client.list_models()
            for role, model in (
                ("fast", settings.ollama_fast_model),
                ("deep", settings.ollama_deep_model),
                ("embedding", settings.ollama_embedding_model),
            ):
                present = inventory.has(model)
                _add(report, "inventory", present, "ok" if present else "configured_model_missing", profile=role)
        except Exception:
            _add(report, "inventory", False, "inventory_unavailable", started=started)
        if not report["passed"]:
            return report

        started = time.perf_counter()
        try:
            # El RAG necesita una revision identificable para invalidar indices;
            # nombre y dimension correctos no satisfacen este contrato.
            revision_available = bool(client.embedding_revision())
            _add(
                report,
                "embedding_revision",
                revision_available,
                "ok" if revision_available else "embedding_revision_unavailable",
                started=started,
            )
        except Exception:
            _add(report, "embedding_revision", False, "embedding_revision_unavailable", started=started)
        if not report["passed"]:
            return report

        started = time.perf_counter()
        try:
            vectors = client.embed(
                [
                    settings.llm_query_template.format(text="Cuantas unidades quedan en Alfa?"),
                    settings.llm_document_template.format(
                        title="Fixture sintetico", text="Contenedor Alfa: 17 unidades; se retiran 5 unidades."
                    ),
                ],
                model=settings.ollama_embedding_model,
            )
            valid = (
                len(vectors) == 2
                and all(len(vector) == settings.ollama_embedding_dimension for vector in vectors)
                and all(any(value != 0 for value in vector) for vector in vectors)
            )
            _add(
                report,
                "embeddings",
                valid,
                "ok" if valid else "embedding_shape_or_zero_vector",
                started=started,
                vector_count=len(vectors),
                expected_dimension=settings.ollama_embedding_dimension,
            )
        except Exception:
            _add(report, "embeddings", False, "embedding_contract_failed", started=started)

        expected_plan = StructuredQueryPlan(
            source="smoke_demo",
            entity="contenedores",
            fields=["nombre", "unidades"],
            filters=[QueryFilter(field="nombre", operator=ComparisonOperator.EQ, value="Alfa")],
            limit=1,
        )
        for selected in (("fast", "deep") if profile == "all" else (profile,)):
            model = settings.ollama_deep_model if selected == "deep" else settings.ollama_fast_model
            context = settings.ollama_deep_num_ctx if selected == "deep" else settings.ollama_fast_num_ctx
            output_tokens = settings.ollama_deep_max_tokens if selected == "deep" else settings.ollama_fast_max_tokens
            started = time.perf_counter()
            try:
                result = client.chat(
                    model=model,
                    execution_profile=selected,
                    messages=[
                        {
                            "role": "system",
                            "content": "Usa solo la evidencia sintetica. Responde solo un numero entero.",
                        },
                        {
                            "role": "user",
                            "content": (
                                "EVIDENCIA: El contenedor Alfa tiene 17 unidades. Se retiran 5 unidades de Alfa. "
                                "PREGUNTA: Cuantas unidades quedan en Alfa?"
                            ),
                        },
                    ],
                    temperature=settings.llm_temperature,
                    num_ctx=context,
                    max_tokens=output_tokens,
                )
                valid = result.content.strip() == str(17 - 5)
                _add(
                    report,
                    "text_generation",
                    valid,
                    "ok" if valid else "synthetic_numeric_answer_mismatch",
                    started=started,
                    profile=selected,
                    prompt_eval_count=result.prompt_eval_count,
                    eval_count=result.eval_count,
                    context_budget=context,
                    output_budget=output_tokens,
                    generation_attempts=result.generation_attempts,
                )
            except Exception as error:
                _add(report, "text_generation", False, "chat_contract_failed", started=started,
                     profile=selected, **_failure_fields(error))
            started = time.perf_counter()
            try:
                result = client.chat(
                    model=model,
                    execution_profile=selected,
                    messages=[
                        {"role": "system", "content": "Devuelve solo el plan JSON solicitado; nunca SQL."},
                        {
                            "role": "user",
                            "content": (
                                "Fuente sintetica permitida: smoke_demo. Entidad permitida: contenedores. "
                                "Genera un plan para seleccionar nombre y unidades, en ese orden, "
                                "con filtro nombre igual a Alfa y limite 1. No incluyas agregaciones, "
                                "agrupaciones ni ordenamientos. No existen otras fuentes ni entidades."
                            ),
                        },
                    ],
                    temperature=settings.llm_planner_temperature,
                    num_ctx=context,
                    max_tokens=settings.llm_planner_max_tokens,
                    response_schema=StructuredQueryPlan.model_json_schema(),
                )
                plan = StructuredQueryPlan.model_validate_json(result.content)
                valid = plan.model_dump(mode="json") == expected_plan.model_dump(mode="json")
                _add(
                    report,
                    "structured_generation",
                    valid,
                    "ok" if valid else "synthetic_plan_mismatch",
                    started=started,
                    profile=selected,
                    prompt_eval_count=result.prompt_eval_count,
                    eval_count=result.eval_count,
                    output_budget=settings.llm_planner_max_tokens,
                    generation_attempts=result.generation_attempts,
                )
            except Exception as error:
                _add(
                    report,
                    "structured_generation",
                    False,
                    "structured_contract_failed",
                    started=started,
                    profile=selected,
                    **_failure_fields(error),
                )
        report["status"] = "completed"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                _add(report, "cleanup", False, "client_close_failed")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("fast", "deep", "all"), default="fast")
    parser.add_argument("--output", type=Path, help="JSON nuevo; no sobrescribe un archivo existente.")
    parser.add_argument("--run-inference", action="store_true", help="Autoriza sondas sinteticas en runtimes locales.")
    args = parser.parse_args(argv)
    if not args.run_inference:
        parser.print_help()
        print("INCOMPLETE: se requiere --run-inference; no se realizaron conexiones ni pruebas de modelos.")
        return 2
    report = run_smoke_test(args.profile)
    payload = json.dumps(report, ensure_ascii=True, indent=2)
    if args.output is not None:
        try:
            # No permite que un error de ruta sobrescriba .env o documentos.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(payload + "\n")
        except OSError:
            print(json.dumps({"passed": False, "code": "report_write_failed"}))
            return 1
    print(payload)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
