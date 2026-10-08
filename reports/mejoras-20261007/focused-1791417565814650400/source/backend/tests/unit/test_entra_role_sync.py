# Creado por Aldo Garcia.
"""Regresiones de sincronizacion revocable de perfiles Entra/AD."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from app.api.routes.auth import _sync_entra_roles
from app.auth.provider import NormalizedIdentity


class _ScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[object]:
        return self._values

    def scalar_one_or_none(self) -> object | None:
        if not self._values:
            return None
        assert len(self._values) == 1
        return self._values[0]


class _FakeSession:
    def __init__(self, results: list[list[object]]) -> None:
        self._results = iter(results)
        self.statements: list[object] = []
        self.added: list[object] = []
        self.flushed = False

    def execute(self, statement: object) -> _ScalarResult:
        self.statements.append(statement)
        statement_text = str(statement)
        if statement_text.startswith("DELETE"):
            return _ScalarResult([])
        return _ScalarResult(next(self._results))

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        self.flushed = True


def _identity(*, groups: tuple[str, ...] = (), roles: tuple[str, ...] = ()) -> NormalizedIdentity:
    return NormalizedIdentity(
        subject_id="subject-sintetico",
        username="usuario@ejemplo.invalid",
        display_name="Usuario sintetico",
        email=None,
        auth_source="entra",
        groups=groups,
        roles=roles,
    )


def test_sync_retira_rol_ad_obsoleto_y_conserva_rol_no_gobernado() -> None:
    base_role = "role-base"
    old_role = "role-old"
    new_role = "role-new"
    manual_role = "role-manual"
    session = _FakeSession(
        [
            [base_role, old_role, new_role],
            [SimpleNamespace(role_id=base_role), SimpleNamespace(role_id=new_role)],
            [SimpleNamespace(id=base_role)],
            [base_role, old_role, manual_role],
        ]
    )

    _sync_entra_roles(
        session,  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        _identity(groups=("grupo-base", "grupo-nuevo")),
    )

    delete_statement = next(s for s in session.statements if str(s).startswith("DELETE"))
    params = cast(Any, delete_statement).compile().params
    assert params["user_id_1"] == "user-1"
    assert params["role_id_1"] == [old_role]
    assert [vars(row)["role_id"] for row in session.added] == [new_role]
    assert session.flushed is True


def test_sync_sin_claims_revoca_todos_los_roles_gobernados_por_entra() -> None:
    session = _FakeSession(
        [
            ["role-base", "role-administrado"],
            [SimpleNamespace(id="role-base")],
            ["role-administrado", "role-manual"],
        ]
    )

    _sync_entra_roles(
        session,  # type: ignore[arg-type]
        SimpleNamespace(id="user-2"),  # type: ignore[arg-type]
        _identity(),
    )

    assert not session.added
    delete_statement = next(s for s in session.statements if str(s).startswith("DELETE"))
    assert cast(Any, delete_statement).compile().params["role_id_1"] == ["role-administrado"]


def test_sync_no_mutua_cuando_el_conjunto_efectivo_no_cambia() -> None:
    mapping = SimpleNamespace(role_id="role-vigente")
    session = _FakeSession(
        [
            ["role-vigente"],
            [mapping],
            [SimpleNamespace(id="role-vigente")],
            ["role-vigente"],
        ]
    )

    _sync_entra_roles(
        session,  # type: ignore[arg-type]
        SimpleNamespace(id="user-3"),  # type: ignore[arg-type]
        _identity(roles=("HCM_EMP_BASICO_MX",)),
    )

    assert not session.added
    assert not any(str(s).startswith("DELETE") for s in session.statements)
    assert session.flushed is True


def test_sync_rechaza_perfil_especializado_sin_perfil_base() -> None:
    session = _FakeSession(
        [
            ["role-base", "role-especial"],
            [SimpleNamespace(role_id="role-especial")],
            [SimpleNamespace(id="role-base")],
            ["role-base", "role-especial"],
        ]
    )

    _sync_entra_roles(
        session,  # type: ignore[arg-type]
        SimpleNamespace(id="user-4"),  # type: ignore[arg-type]
        _identity(roles=("HCM_ADM_RECLUTAMIENTO_MX",)),
    )

    assert not session.added
    delete_statement = next(s for s in session.statements if str(s).startswith("DELETE"))
    assert set(cast(Any, delete_statement).compile().params["role_id_1"]) == {
        "role-base",
        "role-especial",
    }


def test_sync_asigna_perfil_base_y_especializado_cuando_ambos_claims_existen() -> None:
    session = _FakeSession(
        [
            ["role-base", "role-especial"],
            [
                SimpleNamespace(role_id="role-base"),
                SimpleNamespace(role_id="role-especial"),
            ],
            [SimpleNamespace(id="role-base")],
            [],
        ]
    )

    _sync_entra_roles(
        session,  # type: ignore[arg-type]
        SimpleNamespace(id="user-5"),  # type: ignore[arg-type]
        _identity(
            roles=("HCM_EMP_BASICO_MX", "HCM_ADM_RECLUTAMIENTO_MX"),
        ),
    )

    assert {vars(row)["role_id"] for row in session.added} == {
        "role-base",
        "role-especial",
    }
    assert not any(str(statement).startswith("DELETE") for statement in session.statements)
