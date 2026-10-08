"""Narrow manifest update, diff and full read-only integrity comparison."""
from pathlib import Path
import difflib
import hashlib
import json
import subprocess

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
EXISTING = ['backend/app/agents/orchestrator.py', 'backend/app/agents/knowledge_agent.py',
            'backend/app/llm/ollama_client.py']
NEW = ['backend/app/common/answer_diagnostics.py', 'backend/tests/unit/test_answer_diagnostics.py']
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

manifest = ROOT/'SHA256SUMS.txt'
old = (OUT/'backup/SHA256SUMS.txt').read_bytes()
lines = old.splitlines(keepends=True)
changed = []
for i, line in enumerate(lines):
    digest, sep, name = line.rstrip(b'\r\n').partition(b'  ')
    if name.decode('utf-8') in EXISTING:
        lines[i] = sha(ROOT/name.decode('utf-8')).encode() + line[64:]
        changed.append(name.decode('utf-8'))
assert sorted(changed) == sorted(EXISTING)
newline = b'\r\n' if lines[0].endswith(b'\r\n') else b'\n'
for name in NEW:
    assert not any(line.rstrip(b'\r\n').endswith(b'  '+name.encode()) for line in lines)
    lines.append(sha(ROOT/name).encode() + b'  ' + name.encode() + newline)
updated = b''.join(lines)
assert manifest.read_bytes() in (old, updated), 'Manifest changed unexpectedly; investigate instead of overwriting'
manifest.write_bytes(updated)

patch = []
for name in [*EXISTING, *NEW, 'SHA256SUMS.txt']:
    before = (OUT/'backup'/name).read_text(encoding='utf-8').splitlines(keepends=True) if name not in NEW else []
    after = (ROOT/name).read_text(encoding='utf-8').splitlines(keepends=True)
    patch.extend(difflib.unified_diff(before, after, fromfile='a/'+name if before else '/dev/null', tofile='b/'+name))
(OUT/'changes.patch').write_text(''.join(patch),encoding='utf-8')

baseline = json.loads((OUT/'baseline.json').read_text(encoding='utf-8'))
unexpected = []
for name, hashes in baseline.items():
    if name not in EXISTING and (sha(ROOT/name) if (ROOT/name).is_file() else None) != hashes['actual']:
        unexpected.append(name)
discrepancies = []
missing = []
for line in manifest.read_text(encoding='utf-8').splitlines():
    digest, name = line.split('  ',1)
    if not (ROOT/name).is_file():
        missing.append(name)
    elif sha(ROOT/name) != digest:
        discrepancies.append(name)
preexisting = [name for name,value in baseline.items() if value['expected'] != value['actual']]
prior = json.loads((ROOT/'reports/stage1-regression-20261006-113952/baseline.json').read_text(encoding='utf-8'))
protected = {name:sha(ROOT/name) == expected for name,expected in prior['protected_hashes'].items()}
preflight = subprocess.run([str(ROOT/'.venv/Scripts/python.exe'), '-B','-S','-m','scripts.preflight',
                           '--integrity-only','--require-manifest'],cwd=ROOT/'backend',text=True,capture_output=True)
ruff = subprocess.run([str(ROOT/'reports/stage1-regression-20261006-113952/test-env/Scripts/ruff.exe'),
                       'check','--no-cache',*EXISTING,*NEW],cwd=ROOT,text=True,capture_output=True)
results = {'intentional_existing_changes': EXISTING,'new_files': NEW, 'manifest_modified_entries':changed,
    'manifest_added_entries':NEW, 'preexisting_discrepancies':preexisting,'current_discrepancies':discrepancies,
    'missing':missing,'unexpected_file_changes':unexpected,'protected_unchanged':protected,
    'preflight':{'exit_code':preflight.returncode,'stdout':preflight.stdout,'stderr':preflight.stderr},
    'ruff':{'exit_code':ruff.returncode,'stdout':ruff.stdout,'stderr':ruff.stderr},
    'diagnostics_control_created':(ROOT/'var/diagnostics/next-answer.json').exists()}
(OUT/'final-verification.json').write_text(json.dumps(results,indent=2,ensure_ascii=False),encoding='utf-8')
assert not unexpected and not missing and discrepancies == preexisting
assert all(protected.values()) and preflight.returncode == ruff.returncode == 0
assert not results['diagnostics_control_created']
print(json.dumps(results,ensure_ascii=True,indent=2))
