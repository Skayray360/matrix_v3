"""Record validated follow-up changes; update only their delivery hashes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = Path(__file__).resolve().parent
MODIFIED = (
    "backend/app/common/answers.py",
    "backend/app/rag/claim_context.py",
    "backend/app/rag/grounding.py",
    "backend/app/agents/prompts.py",
    "backend/app/agents/knowledge_agent.py",
)
ADDED = (
    "backend/app/rag/calculated_application.py",
    "backend/tests/unit/test_claim_role_scopes.py",
    "backend/tests/unit/test_clarification_language_followup.py",
    "backend/tests/unit/test_topic_transitions.py",
    "backend/tests/unit/test_calculated_application.py",
    "backend/tests/unit/test_calculated_application_agent.py",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit-report", required=True, type=Path)
    parser.add_argument("--chat-report", required=True, action="append", type=Path)
    args = parser.parse_args()
    unit = json.loads(args.unit_report.read_text(encoding="utf-8"))
    assert unit["exit_code"] == 0 and unit["failed"] == unit["errors"] == 0
    assert not unit["isolation_violations"]
    chats = []
    for path in args.chat_report:
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["real_inference"] and report["clean_shutdown"]
        assert report["cases"] and report["passed"] and all(case["passed"] for case in report["cases"])
        for name, expected in report["code_sha256"].items():
            assert digest(ROOT / name) == expected, f"Source changed after chat verification: {name}"
        pension_cases = [case for case in report["cases"] if "pension_acceptance" in case]
        for case in pension_cases:
            direct = case["calculated_application_direct_check"]
            assert direct["returned_answer"] and direct["grounded"] and direct["complete_pension_answer"]
        chats.append({"artifact": path.resolve().relative_to(ROOT).as_posix(),
                      "scenario": report["scenario"], "cases": len(report["cases"]),
                      "accepted": sum(case["passed"] for case in report["cases"]),
                      "complete_pension_answers": len(pension_cases),
                      "calculated_application_used": sum(case["calculated_application"] for case in report["cases"]),
                      "direct_calculation_checks_passed": len(pension_cases),
                      "transport": report["transport"]})

    manifest = ROOT / "SHA256SUMS.txt"
    previous = REPORT / "before/SHA256SUMS.txt"
    assert manifest.read_bytes() == previous.read_bytes(), "Delivery manifest changed during diagnosis"
    old_entries = dict((line.split("  ", 1)[1], line.split("  ", 1)[0])
                       for line in previous.read_text(encoding="utf-8").splitlines() if line)
    updated = dict(old_entries)
    changes = []
    for name in (*MODIFIED, *ADDED):
        before = digest(REPORT / "before" / name) if name in MODIFIED else None
        assert old_entries.get(name) == before, f"Unexpected original manifest entry: {name}"
        after = digest(ROOT / name)
        updated[name] = after
        changes.append({"path": name, "before_sha256": before, "after_sha256": after})
    assert all(updated[name] == value for name, value in old_entries.items() if name not in MODIFIED)
    manifest.write_text("".join(f"{updated[name]}  {name}\n" for name in sorted(updated)), encoding="utf-8")
    summary = {
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "reported_request": "18df73988feaf42311d6ee2c83f777e71cc98aa51c5169e3acfcc8073a996306",
        "changes": changes,
        "fixes": [
            "Recognize bounded clarification prefaces and paired Markdown emphasis without exempting factual claims.",
            "Validate complete document titles, grammatical connectors and page attribution without accepting a different title.",
            "Keep declared tenure, exact documentary intervals and percentages distinct across validated citations in the same conditional section.",
            "End inherited headings at explicit transitions to the full topic of the cited sources.",
            "Distinguish a request for the user's current employment context from an assertion of current documentary validity.",
            "After two failed generations involving numeric validation, answer only a fully recognized calculation request from its unique verified table/rule, and validate the complete rendered answer with the normal verifier; fabricated citations remain rejected.",
            "Require complete interpreted source rules for calculation recovery, including every retrieved fragment of the identified document; reject additional population, age or other uninterpreted requirements.",
            "Continue rejecting changed assumptions, wrong rows/pages/percentages, ambiguous titles and personal entitlement claims.",
        ],
        "unit_test_results": unit,
        "unit_test_artifact": args.unit_report.resolve().relative_to(ROOT).as_posix(),
        "real_chat_probes": chats,
        "limitations": [
            "API exercised through FastAPI TestClient ASGI in-process; not browser/TCP.",
            "Runtime storage is disposable SQLite, with MEDIUMTEXT/sequence adaptations and autoflush disabled to avoid its single-writer lock; operational MySQL is SELECT-only for authorization/history metadata.",
            "Citation and numeric validation is not a universal semantic-accuracy guarantee.",
        ],
        "operational_database_or_index_modified_by_probes": False,
        "authorization_or_validation_disabled": False,
        "manifest_sha256": digest(manifest),
    }
    (REPORT / "validation-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"unit_passed": unit["passed"], "chat_cases": sum(item["cases"] for item in chats),
                      "modified_manifest_entries": len(changes)}, indent=2))


if __name__ == "__main__":
    main()
