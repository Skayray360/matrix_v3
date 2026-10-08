"""Update ONLY declared intentional hashes; verify every other baseline entry."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
EXISTING = (
    "backend/app/memory/service.py",
    "backend/app/agents/orchestrator.py", "backend/app/agents/knowledge_agent.py",
    "backend/app/agents/prompts.py", "backend/app/llm/model_policy.py",
    "backend/app/common/answers.py", "backend/app/common/errors.py", "backend/app/common/chat_failures.py",
    "backend/app/rag/grounding.py", "backend/app/rag/claim_context.py", "backend/scripts/rag_eval.py",
    "frontend/src/security/Markdown.tsx", "frontend/src/styles.css", "frontend/src/pages/ChatPage.tsx",
    "frontend/tests/Markdown.test.tsx", "frontend/tests/ChatPage.test.tsx",
    "backend/tests/unit/test_ai_runtime_contracts.py", "backend/tests/unit/test_chat_failure_codes_127.py",
    "backend/tests/unit/test_conditional_contract.py", "backend/tests/unit/test_conversation_policy_stage1.py",
    "backend/tests/unit/test_memory_and_agent.py", "backend/tests/unit/test_rag_citation_transport.py",
    "backend/tests/unit/test_rag_eval_annotations.py", "backend/tests/unit/test_rag_integrity_final.py",
    "backend/tests/unit/test_rag_validation_diagnostics_hotfix.py", "backend/tests/unit/test_rh_routing.py",
    "backend/tests/unit/test_local_context_budget.py", "frontend/dist/index.html",
)
NEW = (
    "backend/app/common/timing.py", "backend/tests/unit/test_review_contract.py", "backend/tests/response_samples.py",
    "frontend/dist/assets/index-DSQAAgjg.js", "frontend/dist/assets/index-RXxwWWzy.css",
)
ALLOWED = frozenset(EXISTING + NEW)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def dump(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def entries(raw):
    result = {}
    for line in raw.decode("utf-8-sig").splitlines():
        digest, name = line.split("  ", 1)
        assert name not in result
        result[name] = digest
    return result


baseline = json.loads((OUT / "baseline.json").read_text(encoding="utf-8-sig"))
before = (OUT / "backup/SHA256SUMS.txt").read_bytes()
assert hashlib.sha256(before).hexdigest() == baseline["manifest_sha256"]
old_entries = entries(before)
assert set(baseline["files"]) == set(old_entries)
changed = sorted(name for name, data in baseline["files"].items() if sha(ROOT / name) != data["actual"])
assert set(changed) == set(EXISTING), f"Unexpected baseline changes: {set(changed) ^ set(EXISTING)}"
for name in EXISTING:
    assert sha(OUT / "backup" / name) == baseline["files"][name]["actual"], name
for name in NEW:
    assert name not in old_entries and not (OUT / "backup" / name).exists()
    assert (ROOT / name).is_file()
protected = {name: sha(ROOT / name) == value for name, value in baseline["protected"].items()}
assert all(protected.values()), protected

lines = []
for line in before.splitlines(keepends=True):
    name = line.rstrip(b"\r\n").split(b"  ", 1)[1].decode("utf-8")
    lines.append(sha(ROOT / name).encode("ascii") + line[64:] if name in ALLOWED else line)
newline = b"\r\n" if b"\r\n" in before else b"\n"
assert before.endswith(newline)
for name in NEW:
    lines.append(sha(ROOT / name).encode("ascii") + b"  " + name.encode("utf-8") + newline)
expected_manifest = b"".join(lines)
manifest = ROOT / "SHA256SUMS.txt"
if sys.argv[1:] == ["--apply"]:
    assert manifest.read_bytes() == before, "Manifest changed since baseline; do not overwrite"
    manifest.write_bytes(expected_manifest)
elif sys.argv[1:] == ["--refresh"]:
    previous_check = json.loads((OUT / "verification.json").read_text(encoding="utf-8"))
    assert sha(manifest) == previous_check["manifest_sha256"], "Manifest changed since last verification"
    # Same fixed allowlist and baseline checks as above, after an intentional fix.
    manifest.write_bytes(expected_manifest)
else:
    assert sys.argv[1:] == ["--verify"]
assert manifest.read_bytes() == expected_manifest

current_entries = entries(expected_manifest)
discrepancies = {name: {"expected": digest, "actual": sha(ROOT / name)}
                 for name, digest in current_entries.items() if sha(ROOT / name) != digest}
preexisting = {name: data for name, data in baseline["files"].items() if data["actual"] != data["expected"]}
assert discrepancies == preexisting, "An unrelated discrepancy changed"
assert all(data["actual"] is not None for data in discrepancies.values())

os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "backend"))
from scripts.preflight import PreflightReport, check_integrity

report = PreflightReport()
check_integrity(report, root=ROOT, require_manifest=True)
assert report.ok, report
file_list = []
patches = []
for name in (*EXISTING, *NEW, "SHA256SUMS.txt"):
    old = (OUT / "backup" / name).read_bytes() if name not in NEW else b""
    current = (ROOT / name).read_bytes()
    file_list.append({"path": name, "created": name in NEW,
                      "before_sha256": hashlib.sha256(old).hexdigest() if name not in NEW else None,
                      "after_sha256": hashlib.sha256(current).hexdigest(),
                      "backup": "backup/" + name if name not in NEW else None})
    patches.append("".join(difflib.unified_diff(
        old.decode("utf-8-sig").splitlines(True), current.decode("utf-8-sig").splitlines(True),
        fromfile="a/" + name if name not in NEW else "/dev/null", tofile="b/" + name,
    )))
(OUT / "changes.patch").write_text("".join(patches), encoding="utf-8")
dump("changed-files.json", file_list)
inventory = json.loads((OUT / "backup.json").read_text(encoding="utf-8-sig"))
for name in EXISTING:
    if not any(item["path"] == name for item in inventory):
        inventory.append({"path": name, "sha256": baseline["files"][name]["actual"],
                          "backup_sha256": sha(OUT / "backup" / name)})
dump("backup.json", inventory)
result = {"verified_at": datetime.now(timezone.utc).isoformat(),
          "intentional_changes": [item["path"] for item in file_list],
          "manifest_sha256": sha(manifest),
          "untouched_manifest_lines_preserved_byte_for_byte": True,
          "unrelated_files_changed": [], "full_manifest_discrepancies": discrepancies,
          "missing_files": [], "protected_unchanged": protected,
          "mandatory_integrity": asdict(report), "mandatory_integrity_ok": report.ok,
          "operational_control_exists": (ROOT / "var/diagnostics/next-answer.json").exists()}
dump("verification.json", result)
print(json.dumps(result, ensure_ascii=False))
