"""Compare isolated validations; preserve failures, skips and preparation artifacts."""
from collections import Counter
import json
from pathlib import Path
import xml.etree.ElementTree as ET

REPORT = Path(__file__).resolve().parent
PREVIOUS = json.loads((REPORT.parent / "revision-20261007" / "validation-summary.json").read_text(encoding="utf-8"))
KNOWN = {item["nodeid"]: item["category"] for item in PREVIOUS["backend"]["after"]["failures"]}


def latest(pattern, filename):
    for directory in sorted(REPORT.glob(pattern), reverse=True):
        path = directory / filename
        if path.is_file():
            return directory, json.loads(path.read_text(encoding="utf-8-sig"))
    return None, None


def classify(failure):
    if failure["nodeid"] == "tests/unit/test_local_model_diagnostics.py::test_total_deadline_cancels_trickling_metadata":
        return "Existing unchanged diagnostic test exceeds one-second fixture deadline in this environment"
    if "WinError 1314" in failure["detail"]:
        return "Windows symbolic-link privilege unavailable"
    if "WinError 1920" in failure["detail"]:
        return "Bash WindowsApps executable inaccessible"
    return KNOWN.get(failure["nodeid"], "Requires individual investigation")


summary = {"backend": {}, "frontend": {}, "comparison": {}, "preparation_and_superseded_runs": {
    "frontend-baseline-1791402780941": "94/94 tests passed, then Windows PowerShell 5 treated worker shutdown stderr as terminating error. Complete rerun under pwsh; harness now records native exit codes without aborting on stderr.",
    "after-1791407643833998000": "Intermediate full run: 1906 passed, 25 failed, 46 skipped; preceded durable vector cleanup and category-scoped reconciliation follow-up. Scheduler lifecycle fixture subsequently mocks initial sync to prevent real I/O; no assertions weakened.",
}}
for phase in ("baseline", "after"):
    directory, data = latest(phase + "-*", "results.json")
    if data:
        summary["backend"][phase] = {
            "artifacts": directory.name,
            **{key: value for key, value in data.items() if key != "failures"},
            "failure_categories": dict(Counter(classify(f) for f in data["failures"])),
            "failures": [{"nodeid": f["nodeid"], "category": classify(f)} for f in data["failures"]],
            "skip_reasons": dict(Counter(item.attrib.get("message", "")
                                         for item in ET.parse(directory / "junit.xml").iter("skipped"))),
        }
    directory, data = latest("frontend-" + phase + "-*", "validation-results.json")
    if data:
        tests = json.loads((directory / "test-results.json").read_text(encoding="utf-8-sig"))
        summary["frontend"][phase] = {
            "artifacts": directory.name, "passed": tests["numPassedTests"], "failed": tests["numFailedTests"],
            "pending": tests["numPendingTests"], "success": tests["success"],
            "commands": data["results"], "node": data["node"], "network": data["network"],
        }
if all(phase in summary["backend"] for phase in ("baseline", "after")):
    failures = {phase: {item["nodeid"] for item in summary["backend"][phase]["failures"]}
                for phase in ("baseline", "after")}
    summary["comparison"] = {"new_failure_nodeids": sorted(failures["after"] - failures["baseline"]),
                             "resolved_failure_nodeids": sorted(failures["baseline"] - failures["after"]),
                             "unchanged_failures": len(failures["after"] & failures["baseline"])}
    directory, _ = latest("after-*", "results.json")
    command = json.loads((directory / "command.json").read_text(encoding="utf-8"))
    if command.get("frontend_reused_after_source_hash_comparison"):
        summary["frontend"]["after"] = {
            "validation": "Reused passed baseline checks and build after exact frontend source hash comparison.",
            "build": command["frontend_build"],
            "same_source_as_baseline": True,
        }
(REPORT / "validation-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"backend": {phase: {key: value for key, value in item.items() if key != "failures"}
                             for phase, item in summary["backend"].items()},
                  "frontend": summary["frontend"], "comparison": summary["comparison"]}, indent=2))
