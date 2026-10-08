"""Read-only checks of the restarted local service; record minimal metadata."""
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.request import urlopen

REPORT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"


def read(path):
    with urlopen(BASE + path, timeout=20) as response:
        assert response.status == 200
        return response.read(), response.headers.get("Content-Type", "")


health = json.loads(read("/health")[0])
ready = json.loads(read("/ready")[0])
frontend, content_type = read("/")
assert health["status"] == "ok"
assert ready["ready"] and all(component["ok"] for component in ready["components"])
assert "text/html" in content_type and b"<html" in frontend.lower()
assert not (REPORT / "retrieval-index").exists()
runtime = {
    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    "base_url": BASE,
    "health_http_status": 200,
    "health_status": health["status"],
    "ready_http_status": 200,
    "ready": ready["ready"],
    "components": [{"name": item["name"], "ok": item["ok"]} for item in ready["components"]],
    "frontend_http_status": 200,
    "frontend_is_html": True,
}
summary_path = REPORT / "validation-summary.json"
summary = json.loads(summary_path.read_text(encoding="utf-8"))
summary.update({
    "ruff_changed_files": "passed",
    "launcher_integrity_check": "passed",
    "temporary_index_copy_removed": True,
    "running_service": runtime,
})
summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(REPORT / "running-service-validation.json").write_text(json.dumps(runtime, indent=2), encoding="utf-8")
print(json.dumps(runtime, indent=2))
