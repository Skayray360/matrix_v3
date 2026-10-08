# Creado por Aldo Garcia.
"""Regresiones del alcance SQL por rol con filas SQLite sinteticas reales."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from hashlib import sha256

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.authorization.context import StructuredSourceGrant
from app.authorization.policy import PolicyEngine
from app.common.errors import ForbiddenError, StructuredQueryRejectedError
from app.config import get_settings
from app.database.models import (
    Base,
    Conversation,
    ConversationMessage,
    ConversationSummary,
    Permission,
    Role,
    RolePermission,
    StructuredSourcePermission,
    User,
    UserRole,
)
from app.memory.service import MemoryService, authorization_fingerprint
from app.structured_data.compiler import compile_plan
from app.structured_data.schemas import StructuredQueryPlan
from app.structured_data.sources import EntityConfig, SourceCatalog, SourceConfig
from app.structured_data.tool import StructuredDataTool
from app.structured_data.validator import QueryPolicyValidator
from tests.conftest import make_context

pytestmark = [pytest.mark.unit, pytest.mark.security]


def grant(entities=None, filters=None, *, source="rh_demo"):
    return StructuredSourceGrant(
        source_name=source,
        allowed_entities_json=None if entities is None else json.dumps(entities),
        row_filter=None if filters is None else json.dumps({"filters": filters}),
    )


def eq(field, value):
    return {"field": field, "operator": "eq", "value": value}


def context(*grants, **overrides):
    return replace(make_context(), structured_source_grants=tuple(grants), **overrides).sign(
        get_settings().app_secret_key.get_secret_value()
    )


def query(entity="plantilla", **overrides):
    return StructuredQueryPlan.model_validate({
        "source": "rh_demo", "entity": entity, "fields": ["empleado_id"], **overrides,
    })


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    database = tmp_path / "scope.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE plantilla (empleado_id TEXT, pais TEXT, centro TEXT, estatus TEXT, owner_email TEXT)")
        db.executemany("INSERT INTO plantilla VALUES (?, ?, ?, ?, ?)", [
            ("mx_a", "MX", "A", "activo", "alice@example.invalid"),
            ("mx_b", "MX", "B", "activo", "bob@example.invalid"),
            ("co_a", "CO", "A", "activo", "alice@example.invalid"),
            ("co_b", "CO", "B", "activo", "bob@example.invalid"),
            ("mx_inactivo", "MX", "A", "baja", "alice@example.invalid"),
        ])
    monkeypatch.setenv("MATRIX_SCOPE_SQLITE_DSN", f"sqlite:///{database.as_posix()}")
    source = SourceConfig(
        name="rh_demo", engine="sqlite", enabled=True, secret_ref="MATRIX_SCOPE_SQLITE_DSN",
        row_scope="role_view", approved_security_views=["plantilla"],
        entities=[
            EntityConfig(name=name, table="plantilla", allowed_columns=["empleado_id", "pais", "centro", "estatus", "owner_email"],
                         required_filters={"estatus": "activo"})
            for name in ("plantilla", "nomina")
        ],
    )
    return SourceCatalog(sources={source.name: source})


def ids(tool, ctx, **overrides):
    return {row[0] for row in tool.run(query(**overrides), ctx=ctx).rows}


def test_entidad_restringida_no_llega_al_plan_ni_a_la_consulta(catalog):
    tool = StructuredDataTool(catalog=catalog)
    ctx = context(grant(["plantilla"]))
    assert [entry["name"] for entry in tool.available_entities(ctx)[0]["entities"]] == ["plantilla"]
    with pytest.raises(ForbiddenError):
        tool.run(query("nomina"), ctx=ctx)
    assert tool._adapters == {}


def test_filtro_de_rol_y_catalogo_se_aplican_juntos_en_sql_real(catalog):
    tool = StructuredDataTool(catalog=catalog)
    ctx = context(grant(["plantilla"], [eq("pais", "MX")]))
    assert ids(tool, ctx) == {"mx_a", "mx_b"}
    assert ids(tool, ctx, filters=[eq("pais", "CO")]) == set()
    assert ids(tool, ctx, filters=[eq("estatus", "baja")]) == set()


def test_roles_se_unen_sin_mezclar_campos_de_concesiones(catalog):
    ctx = context(
        grant(["plantilla"], [eq("pais", "MX"), eq("centro", "A")]),
        grant(["plantilla"], [eq("pais", "CO"), eq("centro", "B")]),
    )
    assert ids(StructuredDataTool(catalog=catalog), ctx) == {"mx_a", "co_b"}


def test_grant_amplio_de_otra_entidad_no_elimina_filtro(catalog):
    ctx = context(grant(["plantilla"], [eq("pais", "MX")]), grant(["nomina"]))
    assert ids(StructuredDataTool(catalog=catalog), ctx) == {"mx_a", "mx_b"}


@pytest.mark.parametrize("entities", [None, ["*"]])
def test_wildcard_de_entidad_conserva_filtros_de_fila(catalog, entities):
    tool = StructuredDataTool(catalog=catalog)
    ctx = context(grant(entities, [eq("pais", "MX")]))
    assert len(tool.available_entities(ctx)[0]["entities"]) == 2
    assert ids(tool, ctx) == {"mx_a", "mx_b"}


@pytest.mark.parametrize("grants", [(), (grant([]),), (grant(source="otra_fuente"),)])
def test_sin_filas_de_concesion_o_lista_vacia_no_hay_acceso(catalog, grants):
    ctx = context(*grants)
    tool = StructuredDataTool(catalog=catalog)
    assert tool.available_entities(ctx) == []
    with pytest.raises(ForbiddenError):
        tool.run(query(), ctx=ctx)


def test_wildcard_documental_no_concede_fuente_sql(catalog):
    ctx = context(grant(["*"]), allowed_sources=frozenset(), category_wildcard=True)
    tool = StructuredDataTool(catalog=catalog)
    assert tool.available_entities(ctx) == []
    with pytest.raises(ForbiddenError):
        tool.run(query(), ctx=ctx)


def test_fuente_sin_permiso_structured_query_no_es_visible(catalog):
    ctx = context(grant(), permissions=frozenset())
    tool = StructuredDataTool(catalog=catalog)
    assert tool.available_entities(ctx) == []
    with pytest.raises(ForbiddenError):
        tool.run(query(), ctx=ctx)


@pytest.mark.parametrize("invalid", [
    "pais = 'MX'", "", "null", "{}", '{"filters": []}',
    '{"filters": [{"field": "pais", "operator": "eq", "value": "MX"}], "sql": "SELECT 1"}',
    '{"filters": [{"field": "pais", "operator": "eq", "value": "MX"}], "filters": []}',
    '{"filters": [{"field": "pais", "operator": "eq", "value": ["MX"]}]}',
    '{"filters": [{"field": "pais", "operator": "in", "value": "MX"}]}',
    '{"filters": [{"field": "pais", "operator": "eq", "value": NaN}]}',
])
def test_filtro_corrupto_cierra_fuente_incluso_con_otro_rol_amplio(catalog, invalid):
    ctx = context(replace(grant(), row_filter=invalid), grant())
    tool = StructuredDataTool(catalog=catalog)
    assert tool.available_entities(ctx) == []
    with pytest.raises(StructuredQueryRejectedError):
        tool.run(query(), ctx=ctx)
    assert tool._adapters == {}


@pytest.mark.parametrize("invalid", ["*", {"plantilla": True}, ["*", "plantilla"], [12], ["x;DROP TABLE"]])
def test_entidades_corruptas_no_se_convierten_en_wildcard(catalog, invalid):
    tool = StructuredDataTool(catalog=catalog)
    ctx = context(grant(invalid))
    assert tool.available_entities(ctx) == []
    with pytest.raises(StructuredQueryRejectedError):
        tool.run(query(), ctx=ctx)


def test_columna_del_filtro_ausente_no_se_ignora(catalog):
    ctx = context(grant(["plantilla"], [eq("columna_secreta", "MX")]))
    tool = StructuredDataTool(catalog=catalog)
    assert tool.available_entities(ctx) == []
    with pytest.raises(StructuredQueryRejectedError):
        tool.run(query(), ctx=ctx)


def test_valor_de_filtro_inyeccion_permanece_parametro(catalog):
    malicious = "MX' OR 1=1 --"
    ctx = context(grant(["plantilla"], [eq("pais", malicious)]))
    compiled = compile_plan(QueryPolicyValidator(catalog=catalog).validate(query(), ctx=ctx))
    assert malicious not in compiled.sql
    assert malicious in compiled.parameters.values()
    assert ids(StructuredDataTool(catalog=catalog), ctx) == set()


def test_restricciones_y_su_ausencia_forman_parte_de_la_firma(catalog):
    secret = get_settings().app_secret_key.get_secret_value()
    ctx = context(grant(["plantilla"], [eq("pais", "MX")]))
    assert ctx.verify(secret)
    for changed in (None, (), (grant(["*"]),)):
        forged = replace(ctx, structured_source_grants=changed)
        assert not forged.verify(secret)
        with pytest.raises(ForbiddenError):
            StructuredDataTool(catalog=catalog).run(query(), ctx=forged)


def test_contexto_real_materializa_filas_y_revocacion_cambia_huella(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.sqlite3'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            user = User(id="scope-user", username="scope-user", display_name="Scope", auth_source="local_test")
            role = Role(id="scope-role", name="scope-role", description="Sintetico")
            permission = Permission(id="scope-permission", name="structured.query", description="Sintetico")
            db.add_all([user, role, permission])
            db.flush()
            row = StructuredSourcePermission(
                role_id=role.id, source_name="rh_demo", allowed_entities=["plantilla"],
                row_filter=json.dumps({"filters": [eq("pais", "MX")]}),
            )
            db.add_all([UserRole(user_id=user.id, role_id=role.id),
                        RolePermission(role_id=role.id, permission_id=permission.id), row])
            db.flush()
            policy = PolicyEngine()
            first = policy.build_context(db, user=user, session_id="test-session", request_id="test-request")
            assert first.structured_source_grants == (grant(["plantilla"], [eq("pais", "MX")]),)
            fingerprint = authorization_fingerprint(db, first, frozenset())
            row.allowed_entities = []
            db.flush()
            second = policy.build_context(db, user=user, session_id="test-session", request_id="test-request")
            assert first.signature != second.signature
            assert fingerprint != authorization_fingerprint(db, second, frozenset())
            db.delete(row)
            db.flush()
            revoked = policy.build_context(db, user=user, session_id="test-session", request_id="test-request")
            assert revoked.structured_source_grants == ()
            assert revoked.allowed_sources == frozenset()
    finally:
        engine.dispose()


@pytest.mark.parametrize("with_source", [False, True])
def test_historia_sql_anterior_no_se_reutiliza_sin_borrar_registros(tmp_path, with_source):
    from app.api.routes.conversations import get_conversation

    engine = create_engine(f"sqlite:///{tmp_path / 'history.sqlite3'}")
    Base.metadata.create_all(engine)
    ctx = context(grant(), category_wildcard=False,
                  allowed_sources=frozenset({"rh_demo"}) if with_source else frozenset())
    # Formato persistido por v1.2.3, sin version del control SQL. No existen
    # filas DB en este caso sintetico; el usuario conserva exactamente su rol.
    legacy_payload = [[], sorted(ctx.roles), sorted(ctx.permissions), sorted(ctx.allowed_sources), []]
    legacy_scope = sha256(json.dumps(legacy_payload, sort_keys=True).encode()).hexdigest()
    try:
        with Session(engine) as db:
            conversation = Conversation(id="old-chat", user_id=ctx.user_id, title="Historial")
            message = ConversationMessage(
                id="old-answer", seq=1, conversation_id=conversation.id, user_id=ctx.user_id,
                role="assistant", content="Contenido anterior", authorization_scope=legacy_scope,
                source_ids=["db:rh_demo/plantilla"] if with_source else None,
            )
            summary = ConversationSummary(
                id="old-summary", conversation_id=conversation.id, summary="Resumen anterior",
                authorization_scope=legacy_scope, message_count=1,
            )
            db.add_all([conversation, message, summary])
            db.flush()
            current_scope = authorization_fingerprint(db, ctx, frozenset())
            assert (current_scope != legacy_scope) is with_source
            memory = MemoryService()
            rendered = memory.build_context(db, ctx, conversation, authorized_categories=frozenset())
            detail = get_conversation(conversation.id, db=db, ctx=ctx, before_seq=None)
            if with_source:
                assert rendered.summary == ""
                assert rendered.turns == ()
                assert detail.messages == []
                db.info["authorization_scope"] = current_scope
                memory.store_summary(db, conversation.id, summary="Resumen con permiso vigente", message_count=1)
                rebuilt = memory.build_context(db, ctx, conversation, authorized_categories=frozenset())
                assert rebuilt.summary == "Resumen con permiso vigente"
            else:
                assert rendered.summary == "Resumen anterior"
                assert rendered.turns[0].content == "Contenido anterior"
                assert detail.messages[0].content == "Contenido anterior"
            # Se retiran de contexto/API, conservando las filas para auditoria.
            assert db.get(ConversationMessage, "old-answer").content == "Contenido anterior"
            assert db.get(ConversationSummary, "old-summary").summary == "Resumen anterior"
    finally:
        engine.dispose()


@pytest.fixture()
def user_catalog(catalog):
    source = catalog.get("rh_demo").model_copy(deep=True)
    source.row_scope = "user"
    for entity in source.entities:
        entity.required_user_filters = {"owner_email": "user.email"}
    return SourceCatalog(sources={source.name: source})


def test_dos_identidades_con_mismo_rol_reciben_solo_sus_filas(user_catalog):
    tool = StructuredDataTool(catalog=user_catalog)
    alice = context(grant(["*"]), user_id="alice", email="alice@example.invalid")
    bob = context(grant(["*"]), user_id="bob", email="bob@example.invalid")
    assert alice.roles == bob.roles
    assert ids(tool, alice) == {"mx_a", "co_a"}
    assert ids(tool, bob) == {"mx_b", "co_b"}
    assert len(tool.available_entities(alice)[0]["entities"]) == 2


@pytest.mark.parametrize("value", [None, "", " ", " alice@example.invalid", 12, True, ["alice@example.invalid"], {"email": "alice"}, "x" * 257])
def test_atributo_firmado_ausente_o_tipo_invalido_niega_antes_del_adapter(user_catalog, value):
    tool = StructuredDataTool(catalog=user_catalog)
    ctx = context(grant(), email=value)
    assert tool.available_entities(ctx) == []
    with pytest.raises(ForbiddenError):
        tool.run(query(), ctx=ctx)
    assert tool._adapters == {}


def test_modificar_atributo_de_identidad_sin_refirmar_se_rechaza(user_catalog):
    alice = context(grant(), email="alice@example.invalid")
    forged = replace(alice, email="bob@example.invalid")
    assert not forged.verify(get_settings().app_secret_key.get_secret_value())
    with pytest.raises(ForbiddenError):
        StructuredDataTool(catalog=user_catalog).run(query(), ctx=forged)


@pytest.mark.parametrize("condition", [
    eq("owner_email", "bob@example.invalid"),
    {"field": "owner_email", "operator": "neq", "value": "alice@example.invalid"},
    {"field": "owner_email", "operator": "like", "value": "%bob%"},
])
def test_filtro_del_plan_no_reemplaza_alcance_de_identidad(user_catalog, condition):
    ctx = context(grant(), email="alice@example.invalid")
    assert ids(StructuredDataTool(catalog=user_catalog), ctx, filters=[condition]) == set()


def test_lista_del_plan_y_rol_amplio_no_amplian_filas_del_usuario(user_catalog):
    ctx = context(grant(), grant(["*"]), email="alice@example.invalid")
    condition = {"field": "owner_email", "operator": "in", "value": ["alice@example.invalid", "bob@example.invalid"]}
    assert ids(StructuredDataTool(catalog=user_catalog), ctx, filters=[condition]) == {"mx_a", "co_a"}


def test_valor_de_identidad_hostil_es_parametro_verificado_por_ast(user_catalog):
    from app.structured_data.compiler import verify_sql_is_readonly

    malicious = "alice@example.invalid' OR 1=1 --"
    ctx = context(grant(), email=malicious)
    compiled = compile_plan(QueryPolicyValidator(catalog=user_catalog).validate(query(), ctx=ctx))
    assert malicious not in compiled.sql
    assert malicious in compiled.parameters.values()
    verify_sql_is_readonly(compiled.sql, dialect="sqlite", allowed_table="plantilla")
    assert ids(StructuredDataTool(catalog=user_catalog), ctx) == set()


@pytest.mark.parametrize("attribute,value", [("user.user_id", "mx_a"), ("user.username", "mx_b")])
def test_ids_confiables_existentes_se_resuelven_con_tipo_texto(user_catalog, attribute, value):
    source = user_catalog.get("rh_demo")
    source.entity("plantilla").required_user_filters = {"empleado_id": attribute}
    ctx = context(grant(), user_id=value, username=value)
    assert ids(StructuredDataTool(catalog=user_catalog), ctx) == {value}


@pytest.mark.parametrize("reference", ["user.centro_trabajo", "user.roles", "request.user_id", "__import__('os')", "user.email.upper()"])
def test_referencias_no_declaradas_nunca_se_evalúan(reference):
    with pytest.raises(ValidationError):
        EntityConfig(name="e", table="t", allowed_columns=["owner_email"], required_user_filters={"owner_email": reference})


def test_fuente_habilitada_sin_filtros_del_usuario_falla_cerrado(catalog):
    source = catalog.get("rh_demo")
    source.row_scope = "user"
    tool = StructuredDataTool(catalog=catalog)
    ctx = context(grant(["*"]))
    assert tool.available_entities(ctx) == []
    with pytest.raises(StructuredQueryRejectedError, match="alcance por usuario"):
        tool.run(query(), ctx=ctx)
    assert tool._adapters == {}


def test_vista_por_rol_exige_aprobacion_nominal_de_tabla(catalog):
    catalog.get("rh_demo").approved_security_views = ["otra_vista"]
    ctx = context(grant())
    tool = StructuredDataTool(catalog=catalog)
    assert tool.available_entities(ctx) == []
    with pytest.raises(StructuredQueryRejectedError, match="seguridad aprobada"):
        tool.run(query(), ctx=ctx)


def test_user_filter_sobre_columna_no_permitida_niega_catalogo_y_sql(user_catalog):
    user_catalog.get("rh_demo").entity("plantilla").required_user_filters = {"correo_oculto": "user.email"}
    tool = StructuredDataTool(catalog=user_catalog)
    ctx = context(grant(["plantilla"]), email="alice@example.invalid")
    assert tool.available_entities(ctx) == []
    with pytest.raises(StructuredQueryRejectedError, match="columna no permitida"):
        tool.run(query(), ctx=ctx)


def test_payload_del_plan_no_puede_proveer_atributos_confiables():
    with pytest.raises(ValidationError):
        StructuredQueryPlan.model_validate({"source": "rh_demo", "entity": "plantilla", "fields": ["empleado_id"],
                                            "user": {"email": "bob@example.invalid"}})


def test_contexto_backend_firma_correo_de_identidad_persistida_y_su_revocacion(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'row-identity.sqlite3'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            user = User(id="alice", username="alice", display_name="Alice", email="alice@example.invalid", auth_source="oidc")
            db.add(user)
            db.flush()
            policy = PolicyEngine()
            first = policy.build_context(db, user=user, session_id="session", request_id="request")
            assert first.email == "alice@example.invalid"
            assert first.verify(get_settings().app_secret_key.get_secret_value())
            user.email = None
            db.flush()
            second = policy.build_context(db, user=user, session_id="session", request_id="request")
            assert second.email is None
            assert second.signature != first.signature
    finally:
        engine.dispose()


def test_correo_federado_cambia_desde_identidad_validada_al_reautenticar(tmp_path, monkeypatch):
    from app.api.routes import auth
    from app.auth.provider import NormalizedIdentity
    from app.database.models import IdentityLink

    engine = create_engine(f"sqlite:///{tmp_path / 'idp-row-identity.sqlite3'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(auth, "_sync_entra_roles", lambda _db, _user, _identity: None)
    try:
        with Session(engine) as db:
            user = User(id="alice", username="alice", display_name="Alice", email="old@example.invalid", auth_source="oidc")
            db.add(user)
            db.flush()
            db.add(IdentityLink(user_id=user.id, provider="oidc", subject_id="signed-subject"))
            db.flush()
            identity = NormalizedIdentity(subject_id="signed-subject", username="alice", display_name="Alice", email="alice@example.invalid", auth_source="oidc")
            assert auth._upsert_user_from_identity(db, identity).email == "alice@example.invalid"
            assert auth._upsert_user_from_identity(db, replace(identity, email=None)).email is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("source_name", ["sap_hcm", "oracle_hcm", "postgres_analytics"])
def test_plantilla_real_enabled_sin_mapeo_sigue_denegada(source_name):
    from app.structured_data.sources import load_sources

    source = load_sources().get(source_name).model_copy(update={"enabled": True}, deep=True)
    assert source.row_scope == "user"
    assert source.approved_security_views == []
    tool = StructuredDataTool(catalog=SourceCatalog(sources={source.name: source}))
    ctx = context(grant(source=source_name), allowed_sources=frozenset({source_name}))
    assert tool.available_entities(ctx) == []
    plan = StructuredQueryPlan(source=source_name, entity=source.entities[0].name, fields=source.entities[0].allowed_columns[:1])
    with pytest.raises(StructuredQueryRejectedError, match="alcance por usuario"):
        tool.run(plan, ctx=ctx)
    assert tool._adapters == {}


def test_adapter_reporta_mapeo_ausente_al_habilitar_fuente(catalog):
    from app.structured_data.adapters import ReadOnlySourceAdapter

    source = catalog.get("rh_demo")
    source.row_scope = "user"
    assert any("filtros por usuario" in problem for problem in ReadOnlySourceAdapter(source).validate_configuration())


def test_tool_conservado_recarga_filtros_y_adapter_antes_de_consultar(user_catalog, monkeypatch):
    from app.structured_data import sources

    selected = [user_catalog]
    with monkeypatch.context() as patch:
        patch.setattr(sources, "load_sources", lambda: selected[0])
        sources.refresh_source_catalog()
        tool = StructuredDataTool()
        ctx = context(grant(["*"]), user_id="mx_a", email="alice@example.invalid")
        try:
            assert ids(tool, ctx) == {"mx_a", "co_a"}
            previous = tool._adapters["rh_demo"]
            first_fingerprint = sources.authorization_catalog_fingerprint(ctx.allowed_sources)
            new_source = user_catalog.get("rh_demo").model_copy(deep=True)
            for entity in new_source.entities:
                entity.required_user_filters = {"empleado_id": "user.user_id"}
            selected[0] = SourceCatalog(sources={new_source.name: new_source})
            sources.refresh_source_catalog()
            assert sources.authorization_catalog_fingerprint(ctx.allowed_sources) != first_fingerprint
            assert ids(tool, ctx) == {"mx_a"}
            assert tool._adapters["rh_demo"] is not previous
            assert previous._engine is None
            selected[0] = SourceCatalog(sources={new_source.name: new_source.model_copy(update={"enabled": False})})
            sources.refresh_source_catalog()
            assert tool.available_entities(ctx) == []
            with pytest.raises(StructuredQueryRejectedError, match="habilitada"):
                tool.run(query(), ctx=ctx)
        finally:
            for adapter in tool._adapters.values():
                adapter.dispose()
    sources.refresh_source_catalog()


def test_recarga_entre_validar_y_compilar_no_ejecuta_plan_anterior(user_catalog, monkeypatch):
    from app.structured_data import sources
    from app.structured_data import tool as tool_module

    selected = [user_catalog]
    original_compile = tool_module.compile_plan
    with monkeypatch.context() as patch:
        patch.setattr(sources, "load_sources", lambda: selected[0])
        sources.refresh_source_catalog()
        tool = StructuredDataTool()
        ctx = context(grant(), email="alice@example.invalid")

        def revoke_when_compiling(validated):
            new_source = user_catalog.get("rh_demo").model_copy(update={"enabled": False}, deep=True)
            selected[0] = SourceCatalog(sources={new_source.name: new_source})
            sources.refresh_source_catalog()
            return original_compile(validated)

        patch.setattr(tool_module, "compile_plan", revoke_when_compiling)
        with pytest.raises(StructuredQueryRejectedError, match="politica.*cambio"):
            tool.run(query(), ctx=ctx)
        assert tool._adapters == {}
    sources.refresh_source_catalog()


@pytest.mark.parametrize("attribute,value", [("email", "bob@example.invalid"), ("username", "new-username"), ("user_id", "another-user")])
def test_cambio_de_atributo_firmado_invalida_huella_sql(tmp_path, attribute, value):
    engine = create_engine(f"sqlite:///{tmp_path / 'fingerprint-identity.sqlite3'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            first = context(grant(), email="alice@example.invalid")
            changed = replace(first, **{attribute: value}).sign(get_settings().app_secret_key.get_secret_value())
            assert authorization_fingerprint(db, first, frozenset()) != authorization_fingerprint(db, changed, frozenset())
    finally:
        engine.dispose()


@pytest.mark.parametrize("change", ["email", "mapping", "disable", "role_view"])
def test_memoria_sql_no_reutiliza_resultados_tras_cambio_identidad_o_catalogo(tmp_path, user_catalog, monkeypatch, change):
    from app.api.routes.conversations import get_conversation
    from app.structured_data import sources

    selected = [user_catalog]
    engine = create_engine(f"sqlite:///{tmp_path / 'fingerprint-history.sqlite3'}")
    Base.metadata.create_all(engine)
    with monkeypatch.context() as patch:
        patch.setattr(sources, "load_sources", lambda: selected[0])
        sources.refresh_source_catalog()
        try:
            with Session(engine) as db:
                first = context(grant(), user_id="alice", email="alice@example.invalid", category_wildcard=False)
                initial_scope = authorization_fingerprint(db, first, frozenset())
                conversation = Conversation(id="scoped-chat", user_id=first.user_id, title="Synthetic")
                message = ConversationMessage(id="scoped-message", seq=1, conversation_id=conversation.id, user_id=first.user_id,
                                              role="assistant", content="Solo filas Alice", authorization_scope=initial_scope,
                                              source_ids=["db:rh_demo/plantilla"])
                summary = ConversationSummary(id="scoped-summary", conversation_id=conversation.id, summary="Resumen Alice",
                                              authorization_scope=initial_scope, message_count=1)
                db.add_all([conversation, message, summary])
                db.flush()
                memory = MemoryService()
                rendered = memory.build_context(db, first, conversation, authorized_categories=frozenset())
                assert rendered.summary == "Resumen Alice"
                assert rendered.turns[0].content == "Solo filas Alice"
                current = first
                if change == "email":
                    current = replace(first, email="bob@example.invalid").sign(get_settings().app_secret_key.get_secret_value())
                else:
                    new_source = user_catalog.get("rh_demo").model_copy(deep=True)
                    if change == "mapping":
                        for entity in new_source.entities:
                            entity.required_user_filters = {"empleado_id": "user.user_id"}
                    elif change == "disable":
                        new_source.enabled = False
                    else:
                        new_source.row_scope = "role_view"
                        new_source.approved_security_views = ["plantilla"]
                    selected[0] = SourceCatalog(sources={new_source.name: new_source})
                    sources.refresh_source_catalog()
                assert authorization_fingerprint(db, current, frozenset()) != initial_scope
                rendered = memory.build_context(db, current, conversation, authorized_categories=frozenset())
                assert rendered.summary == ""
                assert rendered.turns == ()
                assert get_conversation(conversation.id, db=db, ctx=current, before_seq=None).messages == []
                assert db.get(ConversationMessage, message.id).content == "Solo filas Alice"
                assert db.get(ConversationSummary, summary.id).summary == "Resumen Alice"
        finally:
            engine.dispose()
    sources.refresh_source_catalog()
