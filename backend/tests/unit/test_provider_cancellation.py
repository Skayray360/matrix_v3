# Creado por Aldo Garcia.
"""Sockets loopback reales con contenido sintetico; no usan Ollama ni GPU."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.llm.provider import ModelClient, inference_deadline
from app.llm.request_control import check_inference_control
from app.security.admission import snapshot

pytestmark = pytest.mark.unit


@pytest.fixture
def blocking_server():
    received, disconnected = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/ok":
                content = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            if self.path == "/body":
                self.send_response(200)
                self.send_header("Content-Length", "1024")
                self.end_headers()
                self.wfile.flush()
            received.set()
            self.connection.settimeout(2)
            try:
                if self.connection.recv(1) == b"":
                    disconnected.set()
            except (OSError, ConnectionError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received, disconnected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("path", ["/headers", "/body"])
def test_cancel_closes_own_socket_and_other_request_still_succeeds(blocking_server, path):
    base, received, disconnected = blocking_server
    cancel = threading.Event()
    failures = []
    client = ModelClient(base_url=base)

    def blocked_turn():
        try:
            with inference_deadline(cancel_event=cancel):
                client._post(path, {"model": "synthetic"})
        except InferenceFailureError as error:
            failures.append(error.failure_kind)

    thread = threading.Thread(target=blocked_turn)
    thread.start()
    try:
        assert received.wait(2)
        # Otro turno usa el mismo singleton y no comparte el evento/socket.
        assert client._post("/ok", {}) == {"ok": True}
        started = time.monotonic()
        cancel.set()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert time.monotonic() - started < 1
        assert failures == [InferenceFailureKind.CANCELLED]
        assert disconnected.wait(1), "El socket sigue abierto tras cancelar"
        assert snapshot().get("inference", 0) == 0
    finally:
        cancel.set()
        thread.join(timeout=3)
        client.close()


@pytest.mark.parametrize("path", ["/headers", "/body"])
def test_absolute_deadline_closes_silent_socket(blocking_server, path):
    base, _received, disconnected = blocking_server
    client = ModelClient(base_url=base)
    started = time.monotonic()
    try:
        with (pytest.raises(InferenceFailureError) as failure,
              inference_deadline(absolute_deadline=started + 0.25)):
            client._post(path, {})
        assert failure.value.failure_kind is InferenceFailureKind.DEADLINE
        assert time.monotonic() - started < 1
        assert disconnected.wait(1)
        assert snapshot().get("inference", 0) == 0
    finally:
        client.close()


def test_nested_context_cannot_hide_parent_cancellation():
    outer, inner = threading.Event(), threading.Event()
    with (pytest.raises(InferenceFailureError) as failure,
          inference_deadline(cancel_event=outer), inference_deadline(cancel_event=inner)):
        outer.set()
        check_inference_control()
    assert failure.value.failure_kind is InferenceFailureKind.CANCELLED
    check_inference_control()  # Los tokens no contaminan el siguiente turno.
