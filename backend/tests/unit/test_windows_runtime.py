# Creado por Aldo Garcia.
"""Preflight port behavior, independent of the retired Windows controller.

Native Windows supervision is exercised by test_windows_native_controller.py.
The retained checks validate real socket ownership and sanitized failures.
"""
from __future__ import annotations

import errno
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from scripts import preflight

pytestmark = pytest.mark.unit


@pytest.fixture
def foreign_http():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"app":"Otra aplicacion"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_puerto_ajeno_es_un_fallo_obligatorio_del_preflight(foreign_http):
    report = preflight.PreflightReport()
    preflight.check_ports(report, SimpleNamespace(app_host="127.0.0.1", app_port=foreign_http, app_name="Matrix RH"))
    assert not report.ok
    assert report.as_dict()["failed"] == ["puerto_backend"]


def test_preflight_sonda_escucha_total_en_loopback(foreign_http):
    report = preflight.PreflightReport()
    preflight.check_ports(report, SimpleNamespace(app_host="0.0.0.0", app_port=foreign_http, app_name="Matrix RH"))
    assert report.as_dict()["failed"] == ["puerto_backend"]
    assert report.checks[0].detail == "ocupado por otro proceso"


def test_preflight_reporta_error_de_socket_sin_traceback(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("fixture de resolucion DNS")

    monkeypatch.setattr(preflight.socket, "getaddrinfo", unavailable)
    report = preflight.PreflightReport()
    preflight.check_ports(report, SimpleNamespace(app_host="fixture", app_port=8000, app_name="Matrix RH"))
    assert report.as_dict()["failed"] == ["puerto_backend"]
    assert report.checks[0].detail == "no verificable: OSError"


def test_preflight_puerto_libre_no_depende_de_conectar_y_lo_libera(monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError("conexion saliente lenta del caso Windows")

    monkeypatch.setattr(preflight.socket, "create_connection", timeout)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    report = preflight.PreflightReport()
    preflight.check_ports(report, SimpleNamespace(app_host="127.0.0.1", app_port=port, app_name="Matrix RH"))
    assert report.ok
    assert "libre (bind verificado)" in report.checks[0].detail
    with socket.socket() as reopen:
        reopen.bind(("127.0.0.1", port))
        reopen.listen(1)


def test_preflight_escucha_total_rechaza_colision_en_otra_interfaz():
    with socket.socket() as foreign:
        try:
            foreign.bind(("127.0.0.2", 0))
        except OSError:
            pytest.skip("El sistema no permite la segunda direccion loopback")
        foreign.listen(1)
        report = preflight.PreflightReport()
        preflight.check_ports(
            report, SimpleNamespace(app_host="0.0.0.0", app_port=foreign.getsockname()[1]), require_free=True,
        )
        assert report.as_dict()["failed"] == ["puerto_backend"]


def test_diagnostico_reconoce_matrix_activo_pero_instalacion_exige_puerto_libre(monkeypatch):
    requests = []

    class MatrixHealth(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            requests.append(self.path)
            body = b'{"app":"Matrix RH"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), MatrixHealth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    settings = SimpleNamespace(app_host="127.0.0.1", app_port=server.server_port, app_name="Matrix RH")
    try:
        report = preflight.PreflightReport()
        preflight.check_ports(report, settings)
        assert report.ok
        assert report.checks[0].detail == "ocupado por Matrix RH"
        strict = preflight.PreflightReport()
        preflight.check_ports(strict, settings, require_free=True)
        assert strict.as_dict()["failed"] == ["puerto_backend"]
        assert requests == ["/health"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("code", [errno.EADDRINUSE, 10048, 10013, errno.EADDRNOTAVAIL])
def test_preflight_no_oculta_error_de_bind_ni_filtra_excepcion_privada(monkeypatch, code):
    closed = []

    class RejectedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def setsockopt(self, *args):
            return

        def bind(self, *args):
            raise OSError(code, "MARCADOR_PRIVADO_ERROR_SOCKET")

    monkeypatch.setattr(preflight.socket, "socket", lambda *args: RejectedSocket())
    report = preflight.PreflightReport()
    preflight.check_ports(report, SimpleNamespace(app_host="127.0.0.1", app_port=8000), require_free=True)
    assert report.as_dict()["failed"] == ["puerto_backend"]
    assert f"codigo {code}" in report.checks[0].detail
    assert "MARCADOR_PRIVADO_ERROR_SOCKET" not in preflight.render_text(report)
    assert closed == [True]
