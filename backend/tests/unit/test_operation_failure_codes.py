# Creado por Aldo Garcia.
"""Diagnosticos cerrados y preflight sin revelar contenido privado."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.common.errors import ConfigurationError
from scripts import diagnosticar_solicitud, preflight

pytestmark = pytest.mark.unit


def write(root, relative, contents):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


@pytest.mark.parametrize(("reason", "code"), [
    ("citas fuera de la evidencia recuperada", "citations_outside_evidence"),
    ("identificador de fuente ambiguo entre evidencias diferentes", "source_identity_ambiguous"),
    ("beneficio y fuente citada no corresponden", "topic_source_mismatch"),
    ("vigencia actual no acreditada", "current_validity_unverified"),
    ("elegibilidad personal no acreditada", "personal_eligibility_unverified"),
    ("afirmacion documental final sin cita", "uncited_final_claim"),
    ("requiere una unidad de evidencia completa; no se acredita una frase parcial", "incomplete_extract"),
    ("ninguna unidad extractiva completa verificable", "no_complete_extract"),
    ("resumen omite documentos recuperados", "summary_document_omitted"),
])
def test_known_rejections_have_specific_non_sensitive_codes(reason, code):
    event = diagnosticar_solicitud._safe_event({"message": "agent.answer_validation_failed", "reason": reason})
    assert event["reason"] == code


def test_completion_diagnostic_distinguishes_safe_causes_and_omits_free_text():
    event = diagnosticar_solicitud._safe_event({
        "message": "chat.queue_job_failed", "error_type": "InferenceFailureError",
        "error_code": "ollama_unavailable", "failure_kind": "context_limit",
        "finish_reason": "length", "generation_completed": True, "response_chars": 17,
        "error": "private-synthetic-provider-text", "selected_model": "private-synthetic-model",
    })
    assert event == {
        "message": "chat.queue_job_failed", "error_type": "InferenceFailureError",
        "error_code": "ollama_unavailable", "failure_kind": "context_limit",
        "finish_reason": "length", "generation_completed": True, "response_chars": 17,
    }
    unsafe = diagnosticar_solicitud._safe_event({
        "message": "chat.queue_job_failed", "error_code": "private-code",
        "failure_kind": "private-kind", "finish_reason": "private-reason",
        "generation_completed": "private-completion", "reason": "private-reason",
    })
    assert unsafe == {"message": "chat.queue_job_failed", "reason": "other_omitted"}


def test_request_diagnostic_reports_summary_and_clarification_recovery_without_content(tmp_path):
    reference = "c" * 64
    records = [
        {"message": "agent.summary_recovered", "request_id": reference,
         "recovery_method": "complete_extracts", "evidence_count": 4, "cited_source_count": 2,
         "map_batches": 3, "answer": "private-synthetic-source-text"},
        {"message": "agent.summary_recovered", "request_id": reference,
         "recovery_method": "validated_sections"},
        {"message": "agent.clarification_recovered", "request_id": reference,
         "recovery_method": "safe_question", "question": "private-synthetic-question"},
        {"message": "agent.summary_recovered", "request_id": reference,
         "recovery_method": "private-synthetic-method", "map_batches": -1},
    ]
    write(tmp_path, "backend/logs/backend-recovery.log", "\n".join(json.dumps(item) for item in records))
    result = diagnosticar_solicitud.diagnose(tmp_path, reference)
    assert result["events"] == [
        {"message": "agent.summary_recovered", "recovery_method": "complete_extracts",
         "evidence_count": 4, "cited_source_count": 2, "map_batches": 3},
        {"message": "agent.summary_recovered", "recovery_method": "validated_sections"},
        {"message": "agent.clarification_recovered", "recovery_method": "safe_question"},
        {"message": "agent.summary_recovered"},
    ]
    assert "private-synthetic" not in json.dumps(result)


def test_preflight_settings_error_does_not_echo_untrusted_detail(monkeypatch):
    def invalid_settings():
        raise ConfigurationError(detail="APP_LOG_LEVEL invalido: private-synthetic-value")

    monkeypatch.setattr("app.config.get_settings", invalid_settings)
    report = preflight.PreflightReport()
    assert preflight.check_settings(report) is None
    output = json.dumps(report.as_dict())
    assert "private-synthetic" not in output
    assert "APP_LOG_LEVEL" in output and "backend/config/env.example" in output


def test_preflight_config_parsers_do_not_echo_private_yaml_input(monkeypatch):
    def invalid_config():
        raise ValueError("private-synthetic-invalid-yaml-input")

    monkeypatch.setattr("app.authorization.categories.load_category_policy_file", invalid_config)
    monkeypatch.setattr("app.structured_data.sources.load_sources", invalid_config)
    report = preflight.PreflightReport()
    preflight.check_config_files(report, SimpleNamespace(matrix_external_connectors_required=False))
    output = json.dumps(report.as_dict())
    assert report.as_dict()["failed"] == ["politica_categorias", "fuentes_estructuradas"]
    assert "private-synthetic" not in output
    assert "categories.yaml" in output and "sources.yaml" in output
