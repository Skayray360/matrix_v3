# Creado por Aldo Garcia.
"""Contexto de usuario firmado y motor de politicas."""

from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from app.authorization.categories import CategoryPolicy, CategoryRegistry, validate_category_name
from app.authorization.context import UserContext
from app.authorization.policy import Effect, PolicyEngine
from app.common.errors import ConfigurationError, ForbiddenError
from tests.conftest import make_context

pytestmark = pytest.mark.unit


def build_registry(**overrides: bool) -> CategoryRegistry:
    names = (
        "prestaciones",
        "nomina",
        "reclutamiento",
        "relaciones_laborales",
        "salud_ambiental",
        "investigaciones_internas",
    )
    policies = {
        name: CategoryPolicy(
            name=name,
            wildcard_eligible=overrides.get(name, name != "investigaciones_internas"),
        )
        for name in names
    }
    return CategoryRegistry(
        policies=policies, default_wildcard_eligible=True, default_sensitivity="internal"
    )


@pytest.fixture()
def engine() -> PolicyEngine:
    return PolicyEngine(registry=build_registry())


class TestFirmaDelContexto:
    def test_un_contexto_firmado_se_verifica(self, secret: str):
        ctx = make_context()
        assert ctx.verify(secret) is True

    def test_un_contexto_sin_firma_no_se_verifica(self, secret: str):
        ctx = dataclasses.replace(make_context(), signature="")
        assert ctx.verify(secret) is False
        with pytest.raises(ForbiddenError):
            ctx.require_valid(secret)

    def test_manipular_los_roles_invalida_la_firma(self, secret: str):
        ctx = make_context(roles=frozenset({"prestaciones_reader_test"}))
        forjado = dataclasses.replace(ctx, roles=frozenset({"matrix_admin_test"}))
        assert forjado.verify(secret) is False

    def test_manipular_las_categorias_invalida_la_firma(self, secret: str):
        ctx = make_context(categories=frozenset({"prestaciones"}), wildcard=False)
        forjado = dataclasses.replace(ctx, allowed_categories=frozenset({"prestaciones", "nomina"}))
        assert forjado.verify(secret) is False

    def test_una_clave_distinta_no_valida(self):
        assert make_context().verify("otra-clave-cualquiera") is False

    def test_el_contexto_es_inmutable(self):
        ctx = make_context()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.category_wildcard = False  # type: ignore[misc]

    def test_el_perfil_publico_no_expone_la_firma(self):
        profile = make_context().public_profile()
        assert "signature" not in profile
        assert "session_id" not in profile


class TestCategoriasEfectivas:
    def test_el_wildcard_enumera_categorias_conocidas(self, engine: PolicyEngine, admin_context):
        efectivas = engine.effective_categories(admin_context)
        assert "prestaciones" in efectivas
        assert "nomina" in efectivas
        assert len(efectivas) == 5

    def test_el_wildcard_no_alcanza_categorias_no_elegibles(self, engine: PolicyEngine, admin_context):
        assert "investigaciones_internas" not in engine.effective_categories(admin_context)

    def test_el_usuario_restringido_solo_ve_su_categoria(
        self, engine: PolicyEngine, restricted_context
    ):
        assert engine.effective_categories(restricted_context) == frozenset({"prestaciones"})

    def test_una_concesion_a_categoria_inexistente_no_abre_comodin(self, engine: PolicyEngine):
        ctx = make_context(categories=frozenset({"categoria_fantasma"}), wildcard=False)
        assert engine.effective_categories(ctx) == frozenset()


class TestMatrizDeAutorizacion:
    """Matriz de la seccion 6.1 evaluada en la capa de politicas."""

    @pytest.mark.parametrize(
        "category",
        ["prestaciones", "nomina", "reclutamiento", "relaciones_laborales", "salud_ambiental"],
    )
    def test_matrix_tiene_acceso_a_todas(self, engine: PolicyEngine, admin_context, category: str):
        assert engine.can_read_category(admin_context, category).effect is Effect.ALLOW

    def test_matrixr1_accede_a_prestaciones(self, engine: PolicyEngine, restricted_context):
        assert engine.can_read_category(restricted_context, "prestaciones").allowed is True

    @pytest.mark.parametrize(
        "category", ["nomina", "reclutamiento", "relaciones_laborales", "salud_ambiental"]
    )
    def test_matrixr1_es_denegado_en_el_resto(
        self, engine: PolicyEngine, restricted_context, category: str
    ):
        decision = engine.can_read_category(restricted_context, category)
        assert decision.effect is Effect.DENY
        assert decision.reason == "deny_by_default"

    def test_categoria_nueva_denegada_para_restringido(self, engine: PolicyEngine, restricted_context):
        assert engine.can_read_category(restricted_context, "categoria_nueva").allowed is False

    def test_administracion_denegada_para_restringido(self, engine: PolicyEngine, restricted_context):
        assert engine.can_administer_knowledge(restricted_context).allowed is False
        assert engine.can_read_diagnostics(restricted_context).allowed is False

    def test_administracion_permitida_para_matrix(self, engine: PolicyEngine, admin_context):
        assert engine.can_administer_knowledge(admin_context).allowed is True

    def test_fuentes_estructuradas_denegadas_por_defecto(
        self, engine: PolicyEngine, restricted_context
    ):
        assert engine.can_query_source(restricted_context, "rh_demo").allowed is False

    def test_fuente_concedida_a_matrix(self, engine: PolicyEngine, admin_context):
        assert engine.can_query_source(admin_context, "rh_demo").allowed is True

    def test_fuente_no_concedida_se_deniega_aunque_haya_permiso(
        self, engine: PolicyEngine, admin_context
    ):
        assert engine.can_query_source(admin_context, "sap_hcm").allowed is False


class TestOwnershipDeConversaciones:
    def test_el_dueno_accede(self, engine: PolicyEngine, admin_context):
        assert engine.can_access_conversation(admin_context, admin_context.user_id).allowed is True

    def test_ni_siquiera_el_administrador_lee_conversaciones_ajenas(
        self, engine: PolicyEngine, admin_context
    ):
        assert engine.can_access_conversation(admin_context, "otro-usuario").allowed is False


class TestNombresDeCategoria:
    @pytest.mark.parametrize("name", ["prestaciones", "salud_ambiental", "area-01"])
    def test_nombres_validos(self, name: str):
        assert validate_category_name(name) == name

    @pytest.mark.parametrize(
        "name", ["../etc", "C:\\windows", "nomina/../secreto", "Nomina Confidencial", "", "a" * 100]
    )
    def test_nombres_invalidos_se_rechazan(self, name: str):
        with pytest.raises(ConfigurationError):
            validate_category_name(name)

    def test_se_normaliza_a_minusculas(self):
        assert validate_category_name("  PRESTACIONES  ") == "prestaciones"


class TestHashDeRoles:
    def test_es_estable_e_independiente_del_orden(self):
        a = make_context(roles=frozenset({"rol_a", "rol_b"}))
        b = make_context(roles=frozenset({"rol_b", "rol_a"}))
        assert a.role_set_hash == b.role_set_hash

    def test_roles_distintos_producen_hash_distinto(self):
        a = make_context(roles=frozenset({"rol_a"}))
        b = make_context(roles=frozenset({"rol_b"}))
        assert a.role_set_hash != b.role_set_hash

    def test_no_revela_los_nombres_de_rol(self):
        ctx: UserContext = make_context(roles=frozenset({"matrix_admin_test"}))
        assert "matrix_admin_test" not in ctx.role_set_hash


@pytest.fixture()
def live_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Una politica declarativa real que se restaura al terminar la prueba."""
    from app.authorization import categories

    policy_file = tmp_path / "categories.yaml"
    policy_file.write_text(
        "categories:\n  - name: nomina_confidencial\n    wildcard_eligible: true\n",
        encoding="utf-8",
    )
    with monkeypatch.context() as patch:
        patch.setattr(categories, "get_settings", lambda: SimpleNamespace(
            authorization_policy_path=policy_file, knowledge_root_path=tmp_path / "knowledge",
        ))
        categories.refresh_registry()
        try:
            yield policy_file
        finally:
            categories._cached_registry.cache_clear()
    categories.refresh_registry()


def test_recarga_revoca_wildcard_en_motor_ya_conservado_por_consumidor(live_registry):
    from app.authorization.categories import get_registry, refresh_registry
    from app.authorization.policy import get_policy_engine

    engine = get_policy_engine()
    held_engine = engine
    ctx = make_context()
    assert engine.can_read_category(ctx, "nomina_confidencial").allowed
    assert "nomina_confidencial" in engine.effective_categories(ctx)
    old_snapshot = get_registry()
    live_registry.write_text(
        "categories:\n  - name: nomina_confidencial\n    wildcard_eligible: false\n    sensitivity: restricted\n",
        encoding="utf-8",
    )
    refresh_registry()
    assert get_policy_engine() is held_engine
    assert old_snapshot is not get_registry()
    assert not held_engine.can_read_category(ctx, "nomina_confidencial").allowed
    assert "nomina_confidencial" not in held_engine.effective_categories(ctx)


def test_recarga_descubre_categoria_y_conserva_denegacion_wildcard(live_registry):
    from app.authorization.categories import refresh_registry

    engine = PolicyEngine()
    admin = make_context()
    granted = make_context(categories=frozenset({"dominio_nuevo"}), wildcard=False)
    assert engine.effective_categories(granted) == frozenset()
    live_registry.write_text("categories:\n  - name: dominio_nuevo\n    wildcard_eligible: false\n", encoding="utf-8")
    refresh_registry()
    assert not engine.can_read_category(admin, "dominio_nuevo").allowed
    assert engine.effective_categories(granted) == frozenset({"dominio_nuevo"})


def test_recarga_invalida_no_reutiliza_politica_permisiva(live_registry):
    from app.authorization.categories import refresh_registry

    engine = PolicyEngine()
    assert engine.can_read_category(make_context(), "nomina_confidencial").allowed
    live_registry.write_text("categories:\n  - name: ../escape\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        refresh_registry()
    with pytest.raises(ConfigurationError):
        engine.effective_categories(make_context())


def test_consulta_concurrente_a_recarga_no_observa_version_permisiva(live_registry, monkeypatch):
    from app.authorization import categories

    engine = PolicyEngine()
    ctx = make_context()
    live_registry.write_text(
        "categories:\n  - name: nomina_confidencial\n    wildcard_eligible: false\n",
        encoding="utf-8",
    )
    builder = categories.build_registry
    started, release, reading = Event(), Event(), Event()

    def delayed_build():
        started.set()
        if not release.wait(5):
            raise AssertionError("la prueba no libero la recarga")
        return builder()

    def read_decision():
        reading.set()
        return engine.can_read_category(ctx, "nomina_confidencial")

    with monkeypatch.context() as patch:
        patch.setattr(categories, "build_registry", delayed_build)
        with ThreadPoolExecutor(max_workers=2) as workers:
            refresh = workers.submit(categories.refresh_registry)
            assert started.wait(5)
            decision = workers.submit(read_decision)
            assert reading.wait(5)
            try:
                assert not decision.done()
            finally:
                release.set()
            refresh.result(timeout=5)
            assert not decision.result(timeout=5).allowed


def test_categoria_desconocida_con_grant_no_se_autoriza(engine):
    ctx = make_context(categories=frozenset({"no_existe"}), wildcard=False)
    assert not engine.can_read_category(ctx, "no_existe").allowed
    assert engine.effective_categories(ctx) == frozenset()
