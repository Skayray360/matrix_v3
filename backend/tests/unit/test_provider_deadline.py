# Creado por Aldo Garcia.
"""Un cuerpo HTTP a goteo no debe retener el cupo hasta leer toda la respuesta."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.common.errors import OllamaUnavailableError
from app.llm.provider import ModelClient

pytestmark = pytest.mark.unit


def test_deadline_interrumpe_cuerpo_a_goteo_y_libera_admision(monkeypatch):
    body = json.dumps({"result": "respuesta sintetica " * 120}).encode()
    chunks = [body[start:start + 64] for start in range(0, len(body), 64)]
    sent = []
    finish = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for index, chunk in enumerate(chunks):
                if finish.wait(0.03):
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    sent.append(index)
                except (BrokenPipeError, ConnectionResetError):
                    break

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ModelClient()
    monkeypatch.setattr(client.settings, "llm_request_deadline_seconds", 0.13)
    try:
        with pytest.raises(OllamaUnavailableError, match="tiempo limite"):
            client._post(f"http://127.0.0.1:{server.server_port}/api/chat", {"model": "sintetico"})
        assert 0 < len(sent) < len(chunks)
    finally:
        finish.set()
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
