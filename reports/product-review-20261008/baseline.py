"""Preserve the existing delivery before the authorized product review."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
REPORT = Path(__file__).resolve().parent
BEFORE = REPORT / 'before'
assert not (REPORT / 'baseline.json').exists(), 'Baseline already completed; do not overwrite it.'
manifest = ROOT / 'SHA256SUMS.txt'
expected = {line.split('  ', 1)[1]: line.split('  ', 1)[0]
            for line in manifest.read_text(encoding='utf-8').splitlines() if line}
names = {name for name in expected if not name.startswith(('data/', 'var/', 'reports/'))} | {'SHA256SUMS.txt'}
for directory in ('backend', 'frontend/src', 'frontend/tests', 'config', 'docs', 'windows', 'scripts'):
    for path in (ROOT / directory).rglob('*'):
        if path.is_file() and not {'__pycache__', '.pytest_cache', 'node_modules', '.venv'}.intersection(path.parts):
            names.add(path.relative_to(ROOT).as_posix())
files = {}
for name in sorted(names):
    source = ROOT / name
    assert source.resolve().is_relative_to(ROOT) and source.is_file(), name
    assert source.name != '.env' and not name.startswith(('data/', 'var/', 'reports/')), name
    destination = BEFORE / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        assert destination.read_bytes() == source.read_bytes(), 'Source changed during baseline: ' + name
    else:
        shutil.copy2(source, destination)
    files[name] = hashlib.sha256(source.read_bytes()).hexdigest()
differences = [name for name, value in expected.items() if name in files and files[name] != value]
(REPORT / 'baseline.json').write_text(json.dumps({
    'created_utc': datetime.now(timezone.utc).isoformat(),
    'version': '1.3.1', 'git_repository': False,
    'requested_skills_available': [],
    'manifest_mismatches': differences, 'files': files,
    'excluded': ['private .env', 'operational corpus', 'runtime storage', 'previous reports'],
}, indent=2), encoding='utf-8')
print(json.dumps({'copied_files': len(files), 'manifest_mismatches': differences}))
