# Creado por Aldo Garcia.
"""Carga local con sesiones de prueba distintas; no crea usuarios ni guarda tokens."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx


def percentile(values: list[float], q: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)] if values else None


def validate_answer(data: object) -> dict:
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), str) or not data[key].strip()
        for key in ("conversation_id", "message_id", "answer")
    ):
        raise ValueError("Respuesta de chat incompleta")
    return data


@dataclass
class QueuedOutcome:
    status: str
    response: dict | None = None
    accepted_202: bool = False
    recovered: bool = False
    notice_events: int = 0
    stop_user: bool = False


async def queued_request(client: httpx.AsyncClient, payload: dict, deadline: float) -> QueuedOutcome:
    """Una sola entrega; recuperar su ID nunca vuelve a enviar el trabajo."""
    result = QueuedOutcome("uncertain", stop_user=True)
    request_path = f"/api/v1/chat/requests/{payload['client_request_id']}"
    expires = time.monotonic() + deadline

    def inspect(data: object) -> bool:
        if not isinstance(data, dict) or data.get("status") not in {
            "queued", "running", "completed", "failed", "cancelled", "expired",
        }:
            raise ValueError("Estado de solicitud invalido")
        if not isinstance(data.get("conversation_id"), str) or not data["conversation_id"]:
            raise ValueError("Estado sin conversacion")
        result.notice_events += bool(data.get("notice"))
        if data["status"] in ("queued", "running"):
            if data.get("response") is not None:
                raise ValueError("Una solicitud pendiente no contiene respuesta final")
            return False
        answer = None
        if data["status"] == "completed":
            answer = validate_answer(data.get("response"))
            if answer["conversation_id"] != data["conversation_id"]:
                raise ValueError("La respuesta pertenece a otra conversacion")
        result.status = data["status"]
        result.response = answer
        result.stop_user = False
        return True

    async def cancel() -> None:
        # Cancelar no prueba que el runtime haya interrumpido la GPU.
        with suppress(httpx.HTTPError, TimeoutError):
            async with asyncio.timeout(2):
                await client.post(f"{request_path}/cancel", timeout=2)

    try:
        async with asyncio.timeout(deadline):
            try:
                response = await client.post("/api/v1/chat/submit", json=payload)
                if response.status_code == 202:
                    result.accepted_202 = True
                    if inspect(response.json()):
                        return result
                elif 400 <= response.status_code < 500:
                    result.status = str(response.status_code)
                    result.stop_user = response.status_code in (401, 403)
                    return result
                else:
                    error = response.json() if response.status_code == 503 else None
                    if isinstance(error, dict) and error.get("code") == "chat_capacity_full":
                        result.status = "503"
                        result.stop_user = False
                        return result
                    # Incluso un 5xx puede seguir a una aceptacion cuyo ACK se perdio.
                    result.recovered = True
            except (httpx.HTTPError, ValueError, KeyError):
                result.recovered = True

            while time.monotonic() < expires:
                if not result.recovered:
                    await asyncio.sleep(min(2, max(0, expires - time.monotonic())))
                if time.monotonic() >= expires:
                    break
                try:
                    response = await client.get(request_path)
                    if response.status_code in (401, 403, 404):
                        # No conocer el estado no autoriza a duplicar el trabajo.
                        result.status = "uncertain"
                        return result
                    response.raise_for_status()
                    if inspect(response.json()):
                        return result
                except httpx.HTTPError:
                    pass
                except (ValueError, KeyError):
                    result.status = "transport_or_contract_error"
                    return result
                if result.recovered:
                    await asyncio.sleep(min(2, max(0, expires - time.monotonic())))
    except TimeoutError:
        pass
    result.status = "deadline_exceeded"
    await cancel()
    return result


async def run(args) -> dict:
    import uuid

    sessions = json.loads(args.sessions.read_text(encoding="utf-8"))
    prompts = json.loads(args.prompts.read_text(encoding="utf-8"))
    if len(sessions) < args.users or not prompts or not all(isinstance(p, str) and p.strip() for p in prompts):
        raise ValueError("Se requieren sesiones suficientes y prompts documentales no vacios.")
    clients = [
        httpx.AsyncClient(
            base_url=args.base_url,
            trust_env=False,
            timeout=args.timeout,
            cookies={args.cookie_name: entry["session_token"]},
            headers={"X-CSRF-Token": entry["csrf_token"]},
        )
        for entry in sessions[: args.users]
    ]
    latencies: list[float] = []
    all_latencies: list[float] = []
    statuses: Counter[str] = Counter()
    grounded, citations, completed = 0, 0, 0
    queue_metrics: Counter[str] = Counter()
    queued = getattr(args, "queued", False)
    try:
        # Preparacion fuera del reloj de carga: comprobar usuarios distintos.
        identities = set()
        for client in clients:
            profile = await client.get("/api/v1/me")
            profile.raise_for_status()
            identities.add(profile.json()["user_id"])
        if len(identities) != args.users:
            raise ValueError("Cada usuario concurrente debe tener una identidad de prueba distinta.")
        started = time.monotonic()

        async def worker(index: int):
            nonlocal grounded, citations, completed
            conversation = None
            turn = 0
            while time.monotonic() - started < args.duration:
                before = time.monotonic()
                stop_user = False
                try:
                    payload = {
                        "conversation_id": conversation,
                        "message": prompts[(index + turn) % len(prompts)],
                        "client_request_id": str(uuid.uuid4()),
                    }
                    if queued:
                        outcome = await queued_request(clients[index], payload, args.request_deadline)
                        status = outcome.status
                        data = outcome.response
                        stop_user = outcome.stop_user
                        queue_metrics["accepted_202"] += outcome.accepted_202
                        queue_metrics["recoveries_attempted"] += outcome.recovered
                        queue_metrics["notice_events"] += outcome.notice_events
                        queue_metrics["requests_with_notice"] += bool(outcome.notice_events)
                        queue_metrics["users_stopped_uncertain"] += stop_user and status not in ("401", "403")
                    else:
                        response = await clients[index].post("/api/v1/chat", json=payload)
                        status = str(response.status_code)
                        data = validate_answer(response.json()) if response.is_success else None
                    elapsed = time.monotonic() - before
                    if data is not None:
                        conversation = data["conversation_id"]
                        latencies.append(elapsed)
                        grounded += bool(data.get("grounded"))
                        citations += bool(data.get("sources"))
                        completed += 1
                    statuses[status] += 1
                    if status in ("429", "503"):
                        # Un encabezado incorrecto no convierte un rechazo en
                        # dos intentos ni permite una espera indefinida.
                        try:
                            delay = 2 if queued else float(response.headers.get("Retry-After", "1"))
                            if not math.isfinite(delay):
                                delay = 1
                        except ValueError:
                            delay = 1
                        await asyncio.sleep(max(0, min(delay, 5)))
                except (httpx.HTTPError, ValueError, KeyError):
                    elapsed = time.monotonic() - before
                    statuses["transport_or_contract_error"] += 1
                all_latencies.append(elapsed)
                turn += 1
                if stop_user:
                    break

        await asyncio.gather(*(worker(i) for i in range(args.users)))
        total = sum(statuses.values())
        error_pct = 100 * (total - completed) / total if total else 100
        p95 = percentile(latencies, 0.95)
        # Sin umbrales proporcionados por TI no se inventa una aprobacion.
        accepted = None
        if args.slo_p95 is not None and args.max_error_pct is not None:
            accepted = bool(p95 is not None and p95 <= args.slo_p95 and error_pct <= args.max_error_pct)
        return {
            "users": args.users,
            "mode": "queued" if queued else "legacy",
            "requested_duration_s": args.duration,
            "actual_duration_s": round(time.monotonic() - started, 3),
            "requests": total,
            "completed": completed,
            "statuses": dict(statuses),
            "error_pct": error_pct,
            "success_p50_s": percentile(latencies, 0.50),
            "success_p95_s": p95,
            "success_p99_s": percentile(latencies, 0.99),
            "all_p95_s": percentile(all_latencies, 0.95),
            "grounded_responses": grounded,
            "responses_with_citations": citations,
            "slo_p95_s": args.slo_p95,
            "max_error_pct": args.max_error_pct,
            "latency_error_gate_passed": accepted,
            "capacity_certified": False,
            "queue_metrics": dict(queue_metrics) if queued else None,
            "request_deadline_s": args.request_deadline if queued else None,
            "note": "No mide TTFT ni verifica contenido. Requiere hardware, calidad y recuperacion correlacionados.",
        }
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--users", type=int, choices=range(1, 251), required=True)
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--timeout", type=float, default=150)
    parser.add_argument("--queued", action="store_true", help="submit 202 + consulta hasta estado terminal")
    parser.add_argument("--request-deadline", type=float, default=360,
                        help="limite total por solicitud en modo queued, incluida la espera (segundos)")
    parser.add_argument("--cookie-name", default="matrixrh_session")
    parser.add_argument("--slo-p95", type=float)
    parser.add_argument("--max-error-pct", type=float)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if any(not math.isfinite(value) or value <= 0 for value in (args.duration, args.timeout, args.request_deadline)):
        parser.error("duration, timeout y request-deadline deben ser finitos y positivos")
    result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 1 if result["latency_error_gate_passed"] is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
