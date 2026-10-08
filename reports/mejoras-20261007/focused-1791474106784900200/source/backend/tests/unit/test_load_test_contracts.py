# Creado por Aldo Garcia.
"""Cada intento debe producir un solo resultado, incluso con un HTTP 200 invalido."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from scripts import load_test

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("body,expected", [
    ("not-json", "transport_or_contract_error"),
    ({"conversation_id": "c"}, "transport_or_contract_error"),
    ({"conversation_id": "c", "message_id": "m", "answer": "   "}, "transport_or_contract_error"),
    ({"conversation_id": "c", "message_id": "m", "answer": "Texto sintetico", "sources": []}, "200"),
])
def test_one_attempt_one_outcome(tmp_path, monkeypatch, body, expected):
    sessions = tmp_path / "sessions.json"
    sessions.write_text(json.dumps([{"session_token": "synthetic", "csrf_token": "synthetic"}]))
    prompts = tmp_path / "prompts.json"
    prompts.write_text(json.dumps(["Pregunta documental sintetica"]))
    clock = [0.0]
    monkeypatch.setattr(load_test.time, "monotonic", lambda: clock[0])
    actual_client = httpx.AsyncClient

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"user_id": "synthetic-user"})
        clock[0] += 2
        return httpx.Response(200, json=body) if isinstance(body, dict) else httpx.Response(200, text=body)

    monkeypatch.setattr(
        load_test.httpx, "AsyncClient",
        lambda **kwargs: actual_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    args = SimpleNamespace(sessions=sessions, prompts=prompts, users=1, base_url="http://127.0.0.1:8000",
                           timeout=3, cookie_name="synthetic", duration=1, slo_p95=3, max_error_pct=0)
    result = asyncio.run(load_test.run(args))
    assert result["requests"] == 1
    assert result["statuses"] == {expected: 1}
    assert result["completed"] == (1 if expected == "200" else 0)
    assert result["latency_error_gate_passed"] is (expected == "200")
    assert result["capacity_certified"] is False


def run_queued(tmp_path, monkeypatch, responder, *, duration=0.1, deadline=6):
    sessions = tmp_path / "sessions.json"
    sessions.write_text(json.dumps([{"session_token": "synthetic", "csrf_token": "synthetic"}]))
    prompts = tmp_path / "prompts.json"
    prompts.write_text(json.dumps(["Pregunta documental sintetica"]))
    clock = [0.0]
    requests = []
    sleeps = []
    monkeypatch.setattr(load_test.time, "monotonic", lambda: clock[0])

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(load_test.asyncio, "sleep", sleep)
    actual_client = httpx.AsyncClient

    def respond(request):
        requests.append((request.method, request.url.path, request.content))
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json={"user_id": "synthetic-user"})
        if request.url.path == "/api/v1/chat/submit":
            clock[0] += 0.2
        return responder(request)

    monkeypatch.setattr(
        load_test.httpx, "AsyncClient",
        lambda **kwargs: actual_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    args = SimpleNamespace(sessions=sessions, prompts=prompts, users=1, base_url="http://127.0.0.1:8000",
                           timeout=3, cookie_name="synthetic", duration=duration, slo_p95=3, max_error_pct=0,
                           queued=True, request_deadline=deadline)
    return asyncio.run(load_test.run(args)), requests, sleeps


def queued_body(status, *, notice=None, answer=None):
    return {
        "status": status, "conversation_id": "c", "response": answer, "notice": notice,
        "capacity": {"active": 45, "limit": 50, "queued": 1, "queue_limit": 50,
                     "utilization_pct": 90, "overloaded": bool(notice)},
        "poll_after_ms": 2000,
    }


ANSWER = {"conversation_id": "c", "message_id": "m", "answer": "Texto sintetico", "sources": []}


def test_queued_completion_counts_once_and_includes_wait(tmp_path, monkeypatch):
    polling = iter([queued_body("running", notice="Alta ocupacion"), queued_body("completed", answer=ANSWER)])

    def respond(request):
        if request.method == "POST":
            return httpx.Response(202, json=queued_body("queued", notice="Alta ocupacion"))
        return httpx.Response(200, json=next(polling))

    result, requests, sleeps = run_queued(tmp_path, monkeypatch, respond)
    assert result["statuses"] == {"completed": 1}
    assert result["requests"] == result["completed"] == 1
    assert result["queue_metrics"]["accepted_202"] == 1
    assert result["queue_metrics"]["notice_events"] == 2
    assert result["queue_metrics"]["requests_with_notice"] == 1
    assert result["success_p95_s"] == pytest.approx(4.2)
    assert result["latency_error_gate_passed"] is False
    assert sleeps == [2, 2]
    assert sum(path == "/api/v1/chat/submit" for _, path, _ in requests) == 1


@pytest.mark.parametrize("state", ["failed", "cancelled", "expired"])
def test_terminal_without_answer_is_not_success(tmp_path, monkeypatch, state):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(202, json=queued_body("queued"))
        return httpx.Response(200, json=queued_body(state))

    result, _, _ = run_queued(tmp_path, monkeypatch, respond)
    assert result["statuses"] == {state: 1}
    assert result["completed"] == 0
    assert result["queue_metrics"]["notice_events"] == 0


def test_running_deadline_cancels_and_stops_user(tmp_path, monkeypatch):
    def respond(request):
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(202 if request.method == "POST" else 200, json=queued_body("running"))

    result, requests, _ = run_queued(tmp_path, monkeypatch, respond, deadline=3, duration=100)
    assert result["statuses"] == {"deadline_exceeded": 1}
    assert result["completed"] == 0
    assert result["queue_metrics"]["users_stopped_uncertain"] == 1
    assert sum(path.endswith("/cancel") for _, path, _ in requests) == 1
    assert sum(path == "/api/v1/chat/submit" for _, path, _ in requests) == 1


@pytest.mark.parametrize("recoverable", [True, False])
def test_lost_acceptance_recovers_same_id_or_stops_without_duplicate(tmp_path, monkeypatch, recoverable):
    submitted = []

    def respond(request):
        if request.method == "POST":
            submitted.append(json.loads(request.content)["client_request_id"])
            raise httpx.ReadTimeout("ACK sintetico perdido", request=request)
        assert request.url.path.endswith(submitted[0])
        return (httpx.Response(200, json=queued_body("completed", answer=ANSWER)) if recoverable
                else httpx.Response(404, json={"code": "not_found"}))

    result, _, _ = run_queued(tmp_path, monkeypatch, respond, duration=0.1 if recoverable else 100)
    assert len(submitted) == 1
    assert result["queue_metrics"]["accepted_202"] == 0
    assert result["queue_metrics"]["recoveries_attempted"] == 1
    assert result["statuses"] == {"completed" if recoverable else "uncertain": 1}
    assert result["completed"] == int(recoverable)


def test_capacity_rejection_is_not_accepted_or_polled(tmp_path, monkeypatch):
    def respond(request):
        assert request.method == "POST"
        return httpx.Response(503, json={"code": "chat_capacity_full"})

    result, requests, _ = run_queued(tmp_path, monkeypatch, respond)
    assert result["statuses"] == {"503": 1}
    assert result["completed"] == 0
    assert result["queue_metrics"]["accepted_202"] == 0
    assert len(requests) == 2  # Identidad + una entrega rechazada.


@pytest.mark.parametrize("body", [queued_body("running", answer=ANSWER), queued_body("completed", answer=None),
                                  queued_body("completed", answer={**ANSWER, "conversation_id": "other"})])
def test_invalid_status_contract_never_counts_an_answer(tmp_path, monkeypatch, body):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(202, json=queued_body("queued"))
        return httpx.Response(200, json=body)

    result, _, _ = run_queued(tmp_path, monkeypatch, respond, duration=100)
    assert result["statuses"] == {"transport_or_contract_error": 1}
    assert result["completed"] == 0
    assert result["queue_metrics"]["users_stopped_uncertain"] == 1
