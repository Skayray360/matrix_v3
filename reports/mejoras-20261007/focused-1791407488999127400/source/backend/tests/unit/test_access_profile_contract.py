# Creado por Aldo Garcia.
"""Contrato funcional de la matriz inicial de perfiles de acceso v1.1."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.load_entra_mapping import load_mapping_file

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAPPING_PATH = PROJECT_ROOT / "config" / "authorization" / "entra-role-mapping.yaml"
CATEGORIES_PATH = PROJECT_ROOT / "config" / "authorization" / "categories.yaml"
SOURCES_PATH = PROJECT_ROOT / "config" / "data_sources" / "sources.yaml"


def test_matriz_hcm_es_autocontenida_y_acumulativa() -> None:
    contract = load_mapping_file(MAPPING_PATH)
    roles = {role.name: role for role in contract.roles}
    mapped_roles = {entry.role for entry in contract.mappings}
    external_keys = {entry.external_key for entry in contract.mappings}

    assert "HCM_EMP_BASICO_MX" in external_keys
    assert roles["hcm_emp_basico"].categories == ["general"]
    assert mapped_roles <= set(roles)
    assert len(contract.mappings) == len(
        {(entry.provider, entry.external_kind, entry.external_key) for entry in contract.mappings}
    )
    assert all(role.categories and "general" in role.categories for role in contract.roles)

    for role in contract.roles:
        if role.name.startswith("hcm_adm_"):
            assert "knowledge.admin" in role.permissions
        if role.name.startswith("hcm_coord_"):
            assert "knowledge.admin" not in role.permissions


def test_nomina_general_y_confidencial_permanecen_segregadas() -> None:
    roles = {role.name: role for role in load_mapping_file(MAPPING_PATH).roles}

    assert "nomina_confidencial" not in roles["hcm_adm_nomina_gral"].categories
    assert "nomina_general" not in roles["hcm_adm_nomina_conf"].categories
    assert "nomina_confidencial" not in roles["hcm_coord_nomina_gral"].categories
    assert "nomina_general" not in roles["hcm_coord_nomina_conf"].categories


def test_politica_de_carpetas_no_concede_acceso_por_descubrimiento() -> None:
    raw = yaml.safe_load(CATEGORIES_PATH.read_text(encoding="utf-8"))
    categories = {item["name"]: item for item in raw["categories"]}

    assert raw["default_wildcard_eligible"] is False
    assert categories["general"]["allowed_groups"] == ["HCM_EMP_BASICO_MX"]
    assert categories["nomina_confidencial"]["wildcard_eligible"] is False
    assert set(categories["nomina_confidencial"]["allowed_groups"]) == {
        "HCM_ADM_NOMINA_CONF_MX",
        "HCM_COORD_NOMINA_CONF_MX",
    }


def test_conectores_solo_se_declaran_para_administradores_con_permiso() -> None:
    contract = load_mapping_file(MAPPING_PATH)
    roles = {role.name: role for role in contract.roles}
    raw = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8"))

    hcm_roles_with_sources = {
        role_name
        for source in raw["sources"]
        for role_name in source["allowed_roles"]
        if role_name.startswith("hcm_")
    }
    assert hcm_roles_with_sources == {"hcm_adm_admpersonal", "hcm_adm_ia_matrix"}
    assert all(
        "structured.query" in roles[role_name].permissions
        for role_name in hcm_roles_with_sources
    )
    assert all(
        "structured.query" not in role.permissions
        for role in contract.roles
        if role.name.startswith("hcm_coord_")
    )
