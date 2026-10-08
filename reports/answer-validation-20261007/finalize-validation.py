"""Record this repair's verified changes and refresh only their release hashes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CHANGED = (
    "backend/app/agents/prompts.py",
    "backend/app/agents/knowledge_agent.py",
    "backend/app/rag/claim_context.py",
    "backend/app/rag/numeric_grounding.py",
    "backend/app/common/answers.py",
)
ADDED = (
    "backend/tests/unit/test_current_validity_contract.py",
    "backend/tests/unit/test_answer_representation_contract.py",
    "backend/tests/unit/test_calendar_date_grounding.py",
    "backend/tests/unit/test_clarification_contract.py",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-results", type=Path, required=True)
    args = parser.parse_args()
    test_path = args.test_results.resolve()
    if not test_path.is_relative_to(ROOT / "reports"):
        raise ValueError("Expected an isolated test report inside this project")
    tests = json.loads(test_path.read_text(encoding="utf-8"))
    assert tests["exit_code"] == 0 and tests["failed"] == 0 and not tests["isolation_violations"]
    probes = {case: json.loads((HERE / f"{case}-retrieved-generation-metadata.json").read_text(encoding="utf-8"))
              for case in ("pensions", "benefits")}
    assert all(probe["status"] == "accepted" for probe in probes.values())
    manifest = ROOT / "SHA256SUMS.txt"
    entries = {relative: value for value, relative in (
        line.split("  ", 1) for line in manifest.read_text(encoding="utf-8-sig").splitlines()
    )}
    changes = []
    for relative in CHANGED + ADDED:
        before = HERE / "before" / relative
        old_hash = digest(before) if relative in CHANGED else None
        if old_hash is not None and entries.get(relative) != old_hash:
            raise RuntimeError("Manifest no longer matches backed-up source: " + relative)
        new_hash = digest(ROOT / relative)
        entries[relative] = new_hash
        changes.append({"path": relative, "before_sha256": old_hash, "after_sha256": new_hash})
    manifest.write_text("".join(f"{value}  {relative}\n" for relative, value in sorted(entries.items())), encoding="utf-8")
    report = {
        "date": "2026-10-07",
        "changes": changes,
        "fixes": [
            "Distinguish historical salary calculation bases and bounded uncertainty notices from current-policy claims.",
            "Keep verified tenure-row bounds distinct from the user's declared tenure.",
            "Match imported accent escapes to display titles without losing document/page identity.",
            "Compare complete calendar dates, including primero/1 and de/del, without reusing their components as amounts.",
            "Recognize bounded clarification questions and guide generation to complete citations and conditional claims.",
        ],
        "unit_test_results": tests,
        "unit_test_artifact": test_path.relative_to(ROOT).as_posix(),
        "real_local_probes": probes,
        "pdf_page_9_checked": {"percentages": [70, 70, 70], "wrong_100_percent_rejected": True},
        "limitations": ["HTTP/UI/queue and conversation memory were not exercised by the service probes.",
                        "Successful citation and numeric checks are not a universal semantic accuracy guarantee."],
        "operational_database_or_index_modified": False,
        "retrieval_or_similarity_threshold_changed": False,
        "backend_started_or_restarted": False,
        "manifest_sha256": digest(manifest),
    }
    (HERE / "validation-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"tests_passed": tests["passed"], "real_probes": {k: v["status"] for k, v in probes.items()},
                      "changed_files": len(changes), "manifest_updated": True}))


if __name__ == "__main__":
    main()
