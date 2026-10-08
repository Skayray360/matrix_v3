"""Read-only verification of changed files, links and operational manifest."""
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote

report = Path(__file__).resolve().parent
root = report.parents[1]
sys.path.insert(0, str(root / "backend"))
from scripts.preflight import PreflightReport, check_integrity  # noqa: E402

changes = json.loads((report / "changes.json").read_text(encoding="utf-8"))["files"]
issues = []
links_checked = 0
for change in changes:
    path = root / change["path"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != change["after_sha256"]:
        issues.append({"file": change["path"], "issue": "changed_since_inventory"})
    if path.suffix != ".md":
        continue
    content = re.sub(r"```[\s\S]*?```", "", path.read_text(encoding="utf-8-sig"))
    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", content):
        if re.match(r"https?://|mailto:|#", target):
            continue
        clean = unquote(target.split("#", 1)[0])
        links_checked += 1
        if not (path.parent / clean).exists():
            issues.append({"file": change["path"], "missing_link": target})
integrity = PreflightReport()
check_integrity(integrity, root=root, require_manifest=True)
result = {"checked_changed_files": len(changes), "local_links_checked": links_checked,
          "changed_file_issues": issues, "operational_integrity_ok": integrity.ok,
          "scope": "Changed Markdown links and hashes; existing unrelated documentation is not certified."}
(report / "delivery-check.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2))
raise SystemExit(int(bool(issues) or not integrity.ok))
