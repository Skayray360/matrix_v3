"""Run current or baseline backend tests with isolated synthetic storage.

No dependency downloads, operational .env, private corpus, or live services.
Tests and implementation remain unchanged. Source snapshots are immutable.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import weakref

REPORT = Path(__file__).resolve().parent
ROOT = REPORT.parents[1]
EXCLUDED = {"__pycache__", ".ruff_cache", "node_modules", "dist", "build", ".pytest_cache"}


def snapshot(destination: Path) -> None:
    directories = ("backend", "frontend", "config", "scripts", "windows", "docs", "infrastructure", ".github")
    candidates = [p for p in ROOT.iterdir() if p.is_file() and p.name != ".env"]
    for directory in directories:
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and not any(p in EXCLUDED or p.endswith(".egg-info") for p in path.parts):
                candidates.append(path)
    for path in (ROOT / "data").rglob("README.md"):
        candidates.append(path)
    for path in (ROOT / "data" / "synthetic_test_data" / "knowledge").rglob("*.md"):
        candidates.append(path)
    for source in candidates:
        target = destination / source.relative_to(ROOT)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def main() -> int:
    # PowerShell fixtures emit UTF-8. Make subprocess text decoding explicit,
    # without editing the fixtures or depending on the workstation code page.
    if not sys.flags.utf8_mode:
        raise SystemExit("Run Python with -X utf8 so PowerShell fixture output is decoded correctly")
    phase = sys.argv[1]
    if phase not in {"baseline", "after", "focused"}:
        raise SystemExit("Use baseline, after or focused followed by optional pytest paths")
    destination = REPORT / (phase + "-" + str(time.time_ns()))
    destination.mkdir()
    source = destination / "source"
    if phase == "baseline":
        shutil.copytree(REPORT / "before", source)
    else:
        snapshot(source)
    # The source-only before snapshot deliberately excludes generated files.
    # Supply the build produced from the same phase when it is available.
    frontend_build = None
    for candidate in sorted(REPORT.glob("frontend-" + ("baseline" if phase == "baseline" else "after") + "-*"), reverse=True):
        summary_path = candidate / "validation-results.json"
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
            if any(result["name"] == "build" and result["exit_code"] == 0 for result in summary["results"]):
                frontend_build = candidate / "dist"
                shutil.copytree(frontend_build, source / "frontend" / "dist")
                break
    backend = source / "backend"
    storage = destination / "storage"
    process_tmp = destination / "process-tmp"
    storage.mkdir()
    process_tmp.mkdir()
    os.chdir(backend)
    sys.path.insert(0, str(backend))
    os.environ.update({
        "APP_ENV": "test", "APP_SECRET_KEY": "synthetic-revision-validation-key-000000000",
        "APP_LOG_LEVEL": "WARNING", "DATABASE_URL": "mysql+pymysql://root:replace_me@127.0.0.1:1/matrix_rh_test",
        "QDRANT_MODE": "embedded", "QDRANT_PATH": str(storage / "qdrant"),
        "RAG_KNOWLEDGE_ROOT": str(storage / "knowledge"), "UPLOAD_STORAGE_ROOT": str(storage / "uploads"),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "TMP": str(process_tmp), "TEMP": str(process_tmp),
    })
    sys.dont_write_bytecode = True
    violations = []
    listeners = []

    def reject(reason):
        violations.append(reason)
        raise RuntimeError(reason)

    def audit(event, args):
        if event == "socket.bind":
            address = args[1]
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and address[1] == 0:
                listeners.append(weakref.ref(args[0]))
        if event == "socket.connect":
            allowed = False
            for ref in listeners:
                listener = ref()
                try:
                    allowed |= listener is not None and listener.getsockname() == args[1]
                except OSError:
                    pass
            if not allowed:
                reject("Connection blocked: only synthetic ephemeral listeners are permitted")
        if event == "sqlite3.connect" and args[0] != ":memory:":
            if not Path(args[0]).resolve().is_relative_to(destination):
                reject("SQLite outside synthetic storage blocked")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            if os.fsdecode(args[0]).casefold() == os.devnull.casefold():
                return
            path = Path(os.fsdecode(args[0])).resolve()
            if path == ROOT / ".env" or any(path.is_relative_to(ROOT / name) for name in ("var", "data")):
                reject("Operational environment, corpus or storage access blocked")
            flags = args[2] or 0
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                if not path.is_relative_to(destination):
                    reject("Write outside isolated test destination blocked: " + str(path))

    sys.addaudithook(audit)
    from app.config.settings import Settings
    Settings.model_config["env_file"] = None
    import pytest

    class Results:
        collected = 0
        reports = []

        def pytest_collection_modifyitems(self, items):
            self.collected = len(items)

        def pytest_runtest_logreport(self, report):
            self.reports.append({
                "nodeid": report.nodeid, "when": report.when, "outcome": report.outcome,
                "duration": report.duration, "detail": str(report.longrepr) if report.longrepr else "",
            })

    arguments = sys.argv[2:] or ["tests/unit", "tests/security", "tests/test_chat_queue.py"]
    if any(not arg.startswith(("tests/unit", "tests/security", "tests/test_chat_queue.py")) for arg in arguments):
        raise SystemExit("Only unit, security and queue tests may be selected; no live integration")
    arguments += ["-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", "--tb=short", "-ra", "--color=no",
                  "--log-file=" + str(destination / "pytest-events.log"),
                  "--basetemp=" + str(destination / "tmp"), "--junitxml=" + str(destination / "junit.xml")]
    hashes = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in backend.rglob("*.py")}
    (destination / "command.json").write_text(json.dumps({
        "python": sys.executable, "argv": sys.argv, "pytest_arguments": arguments,
        "cwd": str(backend), "source_sha256": hashes,
        "frontend_build": str(frontend_build) if frontend_build else None,
        "utf8_mode": sys.flags.utf8_mode,
        "isolation": "Source snapshot; no operational .env/data/var; only synthetic ephemeral sockets; writes limited to this test destination. Native test subprocesses use reviewed synthetic harnesses.",
    }, indent=2) + "\n", encoding="utf-8")
    results = Results()
    start = time.perf_counter()
    with (destination / "pytest.log").open("w", encoding="utf-8") as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            code = int(pytest.main(arguments, plugins=[results]))
    calls = {}
    for report in results.reports:
        if report["when"] == "call":
            calls.setdefault(report["nodeid"], set()).add(report["outcome"])
    summary = {
        "exit_code": code, "collected": results.collected,
        "passed": sum(v == {"passed"} for v in calls.values()),
        "failed": sum("failed" in v for v in calls.values()),
        "errors": sum(r["when"] != "call" and r["outcome"] == "failed" for r in results.reports),
        "skipped": sum(r["outcome"] == "skipped" for r in results.reports),
        "duration_seconds": round(time.perf_counter() - start, 3), "isolation_violations": violations,
        "failures": [r for r in results.reports if r["outcome"] == "failed"],
    }
    (destination / "results.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "failures"}, indent=2))
    print("Artifacts: " + str(destination))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
