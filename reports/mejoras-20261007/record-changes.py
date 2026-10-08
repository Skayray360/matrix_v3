"""Inventory only this stage's source changes and refresh their manifest entries."""
import hashlib
import json
from pathlib import Path

report = Path(__file__).resolve().parent
root = report.parents[1]
before = json.loads((report / "baseline-hashes.json").read_text(encoding="utf-8"))
allowed = ("backend/app/", "backend/scripts/", "backend/tests/unit/", "docs/")
singles = {".env.example", "README.md", "SHA256SUMS.txt"}
paths = set(before)
for prefix in allowed:
    paths.update(p.relative_to(root).as_posix() for p in (root / prefix).rglob("*")
                 if p.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in p.parts))


def changes():
    result = []
    for relative in sorted(paths):
        path = root / relative
        if not path.is_file():
            raise ValueError("Unexpected deletion: " + relative)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest == before.get(relative):
            continue
        if not (relative.startswith(allowed) or relative in singles):
            raise ValueError("Change outside stage scope: " + relative)
        result.append({"path": relative, "kind": "modified" if relative in before else "new",
                       "before_sha256": before.get(relative), "after_sha256": digest})
    return result


entries = {}
for line in (root / "SHA256SUMS.txt").read_text(encoding="utf-8-sig").splitlines():
    digest, relative = line.split("  ", 1)
    entries[relative] = digest
for change in changes():
    if change["path"] != "SHA256SUMS.txt":
        entries[change["path"]] = change["after_sha256"]
(root / "SHA256SUMS.txt").write_text(
    "".join(f"{entries[p]}  {p}\n" for p in sorted(entries)), encoding="utf-8",
)
inventory = {"files": changes(), "preserved": [".env", "data", "var", "databases", "indexes"]}
(report / "changes.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"changed_files": len(inventory["files"]), "files": [c["path"] for c in inventory["files"]]}, indent=2))
