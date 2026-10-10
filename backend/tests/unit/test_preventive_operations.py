# Creado por Aldo Garcia.
"""Comprobaciones operativas sintéticas: no se conecta a servicios instalados."""
import json

import pytest

from app.common.redaction import REDACTED, redact_text, redact_value
from scripts import diagnosticar_solicitud

pytestmark = pytest.mark.unit


def write(root, relative, contents):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


@pytest.mark.parametrize("key", [
    "MATRIX_SEED_PASSWORD", "matrix_seed_password", "LLM_API_KEY", "OIDC_CLIENT_SECRET",
    "password_hash_argon2id", "APP_SECRET_KEY", "QDRANT_API_KEY",
])
def test_configured_secret_names_are_redacted_in_fields_and_assignments(key):
    secret = "synthetic-" + "q" * 25
    assert redact_value({key: secret})[key] == REDACTED
    assert secret not in redact_text(f'{key}="{secret}"')


@pytest.mark.parametrize("assignment", [
    'MATRIX_SEED_PASSWORD="synthetic phrase with spaces"',
    "MATRIX_SEED_PASSWORD='synthetic;phrase,with punctuation'",
    'MATRIX_SEED_PASSWORD="x"',
    'MATRIX_SEED_PASSWORD=x',
    r'MATRIX_SEED_PASSWORD="synthetic\"escaped phrase"',
])
def test_text_assignment_redaction_covers_entire_quoted_or_short_secret(assignment):
    assert redact_text(assignment) == f"MATRIX_SEED_PASSWORD={REDACTED}"


def test_runtime_reference_and_diagnostic_fields_remain_visible():
    value = {"request_id": "a" * 64, "application_missing_fields": ["entry"], "application_count": 0}
    assert redact_value(value) == value


def test_request_diagnostic_only_returns_closed_fields_for_exact_reference(tmp_path):
    reference = "a" * 64
    private = "synthetic-private-draft-secret"
    records = [
        {"message": "agent.answer_validation_failed", "request_id": reference,
         "timestamp": "2026-10-08T10:00:00+00:00", "question": private, "draft": private,
         "selected_model": private, "reason": private,
         "validation_detail": "contrato documental JSON invalido: unsafe_clarification",
         "application_missing_fields": ["entry", private], "evidence_count": 6, "best_score": .6},
        {"message": "agent.regenerating", "request_id": "b" * 64, "question": reference},
        {"message": "unapproved.event", "request_id": reference, "question": private},
        {"message": [], "request_id": reference},
        {"message": "rag.retrieved", "operation_id": reference, "error_type": {},
         "returned": True, "best_score": 10 ** 100, "application_missing_fields": {}},
    ]
    write(tmp_path, "backend/logs/backend-test.log", "\n".join(json.dumps(item) for item in records))
    result = diagnosticar_solicitud.diagnose(tmp_path, reference.upper())
    assert result["found"] and len(result["events"]) == 2
    assert private not in json.dumps(result)
    assert result["events"][0] == {
        "message": "agent.answer_validation_failed", "timestamp": "2026-10-08T10:00:00+00:00",
        "evidence_count": 6, "best_score": .6, "reason": "other_omitted",
        "validation_detail": "unsafe_clarification", "application_missing_fields": ["entry"],
    }
    assert result["events"][1] == {"message": "rag.retrieved"}


def test_request_diagnostic_caps_events_and_skips_oversized_or_invalid_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnosticar_solicitud, "MAX_EVENTS", 2)
    monkeypatch.setattr(diagnosticar_solicitud, "MAX_LINE_BYTES", 512)
    reference = "a" * 64
    records = [{"message": "rag.retrieved", "request_id": reference, "returned": index} for index in range(4)]
    contents = "bad JSON\n" + "x" * 1500 + "\n" + "\n".join(json.dumps(item) for item in records)
    write(tmp_path, "backend/logs/backend-test.log", contents)
    result = diagnosticar_solicitud.diagnose(tmp_path, reference)
    assert result["truncated"]
    assert [event["returned"] for event in result["events"]] == [2, 3]


def test_request_diagnostic_handles_missing_logs_and_rejects_bad_reference(tmp_path):
    assert not diagnosticar_solicitud.diagnose(tmp_path, "b" * 64)["found"]
    with pytest.raises(ValueError):
        diagnosticar_solicitud.diagnose(tmp_path, "not-a-reference")


def test_summary_shape_failure_is_diagnosable_without_private_content(tmp_path):
    reference = "c" * 64
    write(tmp_path, "backend/logs/backend-summary.log", json.dumps({
        "message": "agent.regenerating", "request_id": reference,
        "reason": "resumen no respeta cantidad de puntos",
        "validation_detail": "resumen no respeta cantidad de puntos",
        "question": "private document title", "draft": "private source content",
    }))
    event = diagnosticar_solicitud.diagnose(tmp_path, reference)["events"][0]
    assert event == {
        "message": "agent.regenerating", "reason": "summary_point_count_mismatch",
        "validation_detail": "summary_point_count_mismatch",
    }


def test_request_diagnostic_refuses_redirected_log_directory(tmp_path):
    external = tmp_path / "outside"
    external.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "backend").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="local y regular"):
        diagnosticar_solicitud.diagnose(root, "a" * 64)
