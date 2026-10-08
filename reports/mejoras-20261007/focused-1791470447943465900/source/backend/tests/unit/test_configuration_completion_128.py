# Creado por Aldo Garcia.
"""Migracion de limites/thinking con preservacion exacta de credenciales y pins."""

from __future__ import annotations

import pytest

from scripts.configuration_upgrade import ADDITIONS, upgrade_configuration

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_upgrade_127_preserves_private_lines_encoding_comments_and_backup(tmp_path, newline, bom):
    lines = [
        "# Fixture sintetico; nunca una configuracion real.",
        "APP_SECRET_KEY=synthetic-only-key",
        "MATRIX_SEED_PASSWORD='synthetic only password' # no cambiar",
        "LLM_FAST_DIGEST=" + "a" * 64,
        "LLM_DEEP_DIGEST=" + "b" * 64,
        "LLM_EMBEDDING_DIGEST=" + "c" * 64,
        "OLLAMA_FAST_MODEL=gemma4:latest", "OLLAMA_DEEP_MODEL=qwen3.6:latest",
        "export LLM_FAST_THINKING = 'default' # anterior", "LLM_DEEP_THINKING=default",
        "LLM_STRUCTURED_THINKING=default", "OLLAMA_FAST_MAX_TOKENS=768",
        "OLLAMA_DEEP_MAX_TOKENS=2048", "LLM_REQUEST_DEADLINE_SECONDS=600",
        "LLM_FAST_TIMEOUT_SECONDS=180", "LLM_DEEP_TIMEOUT_SECONDS=180",
        "ANSWER_ALLOW_GENERAL_KNOWLEDGE=true", "ANSWER_EVIDENCE_MODE=cited",
    ]
    original = bom + (newline.join(lines) + newline).encode()
    target = tmp_path / ".env"
    target.write_bytes(original)
    result = upgrade_configuration(tmp_path)
    assert set(result["changed_keys"]) == {"LLM_FAST_THINKING", "LLM_DEEP_THINKING", "LLM_STRUCTURED_THINKING",
                                           "OLLAMA_FAST_MAX_TOKENS", "OLLAMA_DEEP_MAX_TOKENS", "LLM_COMPLETION_RETRIES"}
    expected = (newline.join(lines) + newline).replace("'default'", "auto").replace("=default", "=auto")
    expected = expected.replace("=768", "=1536").replace("=2048", "=3072")
    expected += "LLM_COMPLETION_RETRIES=1" + newline
    assert target.read_bytes() == bom + expected.encode()
    backups = list((tmp_path / "var/backups/configuration").glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert upgrade_configuration(tmp_path) == {"changed_keys": [], "backup_created": False}


def test_explicit_thinking_and_custom_limits_are_not_overwritten(tmp_path):
    values = {**ADDITIONS, "LLM_FAST_THINKING": "enabled", "LLM_DEEP_THINKING": "disabled",
              "OLLAMA_FAST_MAX_TOKENS": "900", "OLLAMA_DEEP_MAX_TOKENS": "3000", "LLM_COMPLETION_RETRIES": "0",
              "LLM_REQUEST_DEADLINE_SECONDS": "300"}
    original = "\n".join(f"{key}={value}" for key, value in values.items()).encode()
    target = tmp_path / ".env"
    target.write_bytes(original)
    assert upgrade_configuration(tmp_path) == {"changed_keys": [], "backup_created": False}
    assert target.read_bytes() == original


@pytest.mark.parametrize("explicit_limit", [True, False])
def test_small_custom_deep_context_keeps_room_for_document_evidence(tmp_path, explicit_limit):
    original = "OLLAMA_DEEP_NUM_CTX=4096\n"
    if explicit_limit:
        original += "OLLAMA_DEEP_MAX_TOKENS=2048\n"
    target = tmp_path / ".env"
    target.write_text(original)
    upgrade_configuration(tmp_path)
    updated = dict(line.split("=", 1) for line in target.read_text().splitlines() if "=" in line)
    assert updated["OLLAMA_DEEP_MAX_TOKENS"] == "2048"
    assert int(updated["OLLAMA_DEEP_NUM_CTX"]) - int(updated["OLLAMA_DEEP_MAX_TOKENS"]) - 1400 > 0
