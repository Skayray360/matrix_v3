"""Read all manifest entries and verify preserved baseline; never changes hashes."""
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime,timezone

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]
sys.path.insert(0,str(ROOT/'backend'))
os.chdir(ROOT)
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
baseline=json.loads((OUT/'baseline.json').read_text(encoding='utf-8'))
files=baseline['files']
changed=[name for name,before in files.items() if sha(ROOT/name)!=before['actual']]
discrepancies=[name for name,before in files.items() if sha(ROOT/name)!=before['expected']]
from scripts.preflight import PreflightReport,check_integrity
report=PreflightReport()
check_integrity(report,root=ROOT,require_manifest=True)
from dataclasses import asdict
result={'verified_at':datetime.now(timezone.utc).isoformat(),'changed_from_baseline':changed,
    'manifest_unchanged':sha(ROOT/'SHA256SUMS.txt')==baseline['manifest_sha256'],
    'full_manifest_discrepancies':discrepancies,
    'protected_unchanged':{name:sha(ROOT/name)==before for name,before in baseline['protected'].items()},
    'operational_control_exists':(ROOT/'var/diagnostics/next-answer.json').exists(),
    'mandatory_integrity':asdict(report)}
(OUT/'verification-current.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
