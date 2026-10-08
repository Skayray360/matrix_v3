"""Run selected current suites with real components and isolated storage.

Only configuration sources/storage and transport access are isolated here.
This runner does not replace policy engines, retrievers, fixtures, assertions,
or model/embedding/inference defaults. Suite-level transport and storage doubles are described in INFORME.txt.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import weakref

REPORT = Path(__file__).resolve().parent
ROOT = REPORT.parents[1]
BACKEND = ROOT / "backend"
SUITES = (
    'test_ai_runtime_contracts.py',
    'test_chat_failure_codes_127.py',
    'test_rag_citation_transport.py',
    'test_rag_eval_annotations.py',
    'test_rag_integrity_final.py',
    'test_rag_validation_diagnostics_hotfix.py',
    'test_local_context_budget.py',
    'test_completion_recovery_128.py',
    'test_local_inference_metrics.py',
    'test_local_model_protocols_123.py',
    'test_provider_deadline.py',
    'test_provider_time_limits_127.py',
    'test_process_owner_restart.py',
    'test_access_profile_contract.py',

    "test_review_contract.py",
    "test_conditional_contract.py",
    "test_retrieval_ranking.py",
    "test_grounding.py",
    "test_answer_diagnostics.py",
    "test_llm_client.py",
    "test_grounding_numeric_rag_hotfix.py",
    "test_conversation_policy_stage1.py",
    "test_model_policy.py",
    "test_rh_routing.py",
    "test_routing_timeouts_127.py",
    "test_memory_and_agent.py",
    "test_answer_contract_127.py",
    "test_authorization.py",
    "test_chat_service.py",
    "test_rag_pipeline_isolated.py",
)


def main():
    phase = sys.argv[1]
    if phase not in {"before", "after", "repro"}:
        raise SystemExit("Use before, after or repro")
    destination = REPORT / phase
    if destination.exists():
        destination = REPORT / f"{phase}-rerun-{time.time_ns()}"
    destination.mkdir(exist_ok=False)
    storage = destination / "storage"
    storage.mkdir()
    process_tmp = destination / "process-tmp"
    process_tmp.mkdir()
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    os.environ.update({
        "APP_ENV": "test",
        "APP_SECRET_KEY": "synthetic-stage1-regression-secret-000000000",
        "APP_LOG_LEVEL": "WARNING",
        "DATABASE_URL": "sqlite:///" + (storage / "unused-default.sqlite").as_posix(),
        "QDRANT_MODE": "embedded",
        "QDRANT_PATH": str(storage / "qdrant"),
        "RAG_KNOWLEDGE_ROOT": str(storage / "knowledge"),
        "UPLOAD_STORAGE_ROOT": str(storage / "uploads"),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMP": str(process_tmp),
        "TEMP": str(process_tmp),
    })
    violations = []
    synthetic_listeners = []

    def reject(reason):
        violations.append(reason)
        raise RuntimeError(reason)

    def audit(event, args):
        if event == "socket.bind":
            address = args[1]
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and address[1] == 0:
                synthetic_listeners.append(weakref.ref(args[0]))
        if event == "socket.connect":
            address = args[1]
            allowed = False
            for ref in synthetic_listeners:
                listener = ref()
                try:
                    allowed |= listener is not None and listener.getsockname() == address
                except OSError:
                    pass
            if not allowed:
                reject("Connections allowed only to ephemeral listeners created by these tests")
        if event == "sqlite3.connect" and args[0] != ":memory:":
            database = Path(args[0]).resolve()
            if not database.is_relative_to(REPORT):
                reject("SQLite path outside isolated report storage")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            if os.fsdecode(args[0]).casefold() == os.devnull.casefold():
                return  # Windows NUL device, not a filesystem write.
            path = Path(os.fsdecode(args[0])).resolve()
            synthetic_corpus = path.is_relative_to(ROOT / "data" / "synthetic_test_data" / "knowledge") and path.suffix == ".md"
            if path == ROOT / ".env" or (any(path.is_relative_to(ROOT / name) for name in ("var", "data")) and not synthetic_corpus):
                reject("Operational environment, corpus and runtime storage are prohibited")
            flags = args[2] or 0
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writing and not path.is_relative_to(REPORT):
                reject("File write outside isolated report storage")

    sys.addaudithook(audit)
    from app.config.settings import Settings
    # Process-local setting: never read the operational .env. No source edits.
    Settings.model_config["env_file"] = None
    import pytest

    class Results:
        def __init__(self):
            self.reports = []
            self.collected = 0

        def pytest_collection_modifyitems(self, items):
            self.collected = len(items)
            forbidden = {"db_session", "require_database", "database_available", "require_ollama", "require_qdrant"}
            for item in items:
                if item.get_closest_marker("integration") or forbidden.intersection(item.fixturenames):
                    raise pytest.UsageError("An external integration fixture was selected: " + item.nodeid)

        def pytest_runtest_logreport(self, report):
            self.reports.append({
                "nodeid": report.nodeid, "when": report.when, "outcome": report.outcome,
                "duration": report.duration, "detail": str(report.longrepr) if report.longrepr else "",
            })

    results = Results()
    arguments = sys.argv[2:] or ["tests/unit/" + suite for suite in SUITES]
    if any(argument.split("::", 1)[0] not in {"tests/unit/" + suite for suite in SUITES}
           for argument in arguments):
        raise SystemExit("Only the authorized suite paths and their nodeids are accepted")
    arguments += ["-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", "--tb=short", "-ra", "--color=no",
                  "--log-file=" + str(destination / "pytest-events.log"),
                  "--basetemp=" + str(destination / "tmp"),
                  "--junitxml=" + str(destination / "junit.xml")]
    hashes = {"tests/unit/" + suite: hashlib.sha256((BACKEND / "tests/unit" / suite).read_bytes()).hexdigest()
              for suite in SUITES}
    for name in ("app/agents/prompts.py", "app/agents/orchestrator.py", "app/llm/model_policy.py", "tests/conftest.py"):
        hashes[name] = hashlib.sha256((BACKEND / name).read_bytes()).hexdigest()
    (destination / "command.json").write_text(json.dumps({
        "python": sys.executable, "argv": sys.argv, "pytest_arguments": arguments,
        "cwd": str(BACKEND), "sha256": hashes,
        "isolation": "No operational .env/data/var; only synthetic ephemeral loopback connections; synthetic Markdown corpus read-only; writes limited to this report tree; real components; runner does not replace policy/retriever",
    }, indent=2) + "\n", encoding="utf-8")
    start = time.perf_counter()
    with (destination / "pytest.log").open("w", encoding="utf-8") as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            code = int(pytest.main(arguments, plugins=[results]))
    failures = [r for r in results.reports if r["outcome"] == "failed"]
    calls = {}
    for report in results.reports:
        if report["when"] == "call":
            calls.setdefault(report["nodeid"], set()).add(report["outcome"])
    summary = {
        "exit_code": code, "collected": results.collected,
        "passed": sum(outcomes == {"passed"} for outcomes in calls.values()),
        "failed": sum("failed" in outcomes for outcomes in calls.values()),
        "subtest_reports": sum(r["when"] == "call" for r in results.reports) - len(calls),
        "errors": sum(r["when"] != "call" and r["outcome"] == "failed" for r in results.reports),
        "skipped": sum(r["outcome"] == "skipped" for r in results.reports),
        "duration_seconds": round(time.perf_counter() - start, 3),
        "isolation_violations": violations,
        "failures": failures,
    }
    (destination / "results.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (destination / "test-reports.json").write_text(json.dumps(results.reports, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "failures"}, indent=2))
    print("Artifacts: " + str(destination))
    print((destination / "pytest.log").read_text(encoding="utf-8"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
