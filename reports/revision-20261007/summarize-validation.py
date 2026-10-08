"""Compare completed isolated runs without hiding failures or skipped tests."""
from collections import Counter
import json
from pathlib import Path
import xml.etree.ElementTree as ET

REPORT = Path(__file__).resolve().parent


def latest(pattern, result_name):
    for directory in sorted(REPORT.glob(pattern), reverse=True):
        path = directory / result_name
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if result_name != "results.json" or data.get("collected", 0):
                return directory, data
    raise RuntimeError("No completed run: " + pattern)


def classify(failure):
    node, detail = failure["nodeid"], failure["detail"]
    if "WinError 1314" in detail:
        return "Windows symbolic-link privilege unavailable"
    if "WinError 1920" in detail:
        return "Bash WindowsApps executable inaccessible"
    if "test_clean_legacy_layout.py" in node:
        return "Existing Windows backup rename failure (WinError 5)"
    if "case_sensitive_host" in node:
        return "Case-sensitive filesystem contract on case-insensitive NTFS"
    if "test_identity_remains_visible_without_accessing_sources" in node:
        return "Existing identity authorization contract mismatch"
    if "test_execution_profiles.py" in node:
        return "Existing execution-profile fixture/behavior contract mismatch"
    if "test_numeric_provenance_revision.py" in node:
        return "Numeric provenance regression reproduced before correction"
    if "test_security_boundaries_20261007.py" in node:
        return "Security boundary regression reproduced before correction"
    if "integridad_codigo" in detail or "test_serve_proxy_confia_solo_ips_validadas_y_no_omite_cifrado" in node:
        return "Intermediate snapshot contained app edits awaiting SHA256 manifest synchronization"
    return "Requires individual investigation"


summary = {
    "backend": {}, "frontend": {},
    "preparation_and_superseded_runs": {
        "baseline-1791394215257589900": "Harness rejected pytest's Windows NUL log target before collection; corrected by redirecting logs to the isolated run.",
        "baseline-1791394391163862400": "First full run retained: 57 failures, including three UTF-8/DSN harness artifacts and missing generated frontend build; one transient backup rename WinError 5.",
        "focused-1791394558412231200": "Pytest usage error: requested nonexistent test_grounding_properties.py; no tests collected. Corrected to discovered filenames.",
        "frontend-baseline-1791394555038": "Node preload path used Windows separators incorrectly; four commands failed before executing checks.",
        "frontend-baseline-1791394681879": "Interrupted preparation attempt with over-restrictive network guard; guard subsequently allowed Vitest local IPC/loopback only.",
        "after-1791395060457118300": "Intermediate app snapshot, before final whitespace fix; 26 failures including missing build in isolated source.",
        "after-1791395257562033600": "Latest code but SHA256 manifest not yet synchronized; 34 failures, including nine integrity preflight failures. Final run follows manifest synchronization.",
        "offline-dependencies-supply-chain.json": "Historical dependency directory lacked frontend .npmrc; current frontend copy plus same inspected dependency tree passes frontend-after-supply-chain.json.",
    },
}
for phase in ("baseline", "after"):
    directory, data = latest(phase + "-*", "results.json")
    counts = Counter(classify(failure) for failure in data["failures"])
    summary["backend"][phase] = {
        "artifacts": directory.relative_to(REPORT).as_posix(),
        **{key: value for key, value in data.items() if key != "failures"},
        "failure_categories": dict(counts),
        "failures": [{"nodeid": f["nodeid"], "category": classify(f)} for f in data["failures"]],
        "skip_reasons": dict(Counter(item.attrib.get("message", "") for item in ET.parse(directory / "junit.xml").iter("skipped"))),
    }
    directory, data = latest("frontend-" + phase + "-*", "validation-results.json")
    tests = json.loads((directory / "test-results.json").read_text(encoding="utf-8-sig"))
    summary["frontend"][phase] = {
        "artifacts": directory.relative_to(REPORT).as_posix(),
        "passed": tests["numPassedTests"], "failed": tests["numFailedTests"],
        "pending": tests["numPendingTests"], "success": tests["success"],
        "commands": data["results"], "node": data["node"], "network": data["network"],
    }
(REPORT / "validation-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"backend": {phase: {key: value for key, value in entry.items() if key not in ("failures",)} for phase, entry in summary["backend"].items()}, "frontend": summary["frontend"]}, indent=2))
