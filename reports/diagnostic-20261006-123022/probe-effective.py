"""Observe real payload construction using current config and synthetic HTTP only."""
import dataclasses
import hashlib
import json
import marshal
import os
from pathlib import Path
import socket
import struct
import sys

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
os.chdir(ROOT)

def forbidden(*args, **kwargs):
    raise AssertionError('No network in the diagnostic reproduction')
socket.socket.connect = forbidden

import httpx
from app.config import get_settings
from app.agents import prompts
from app.agents.knowledge_agent import KnowledgeAgent
from app.llm.model_policy import ModelPolicy, ModelChoice
from app.llm.provider import ModelClient
from app.rag.schemas import Evidence
from app.rag.numeric_grounding import numeric_claim_supported
from app.rag.grounding import verify_grounding

s = get_settings()
payloads = []
inventory_calls = []
def transport(request):
    if request.url.path == '/api/tags':
        inventory_calls.append('synthetic inventory; NOT verification of installed model')
        return httpx.Response(200, json={'models': [{'name': s.ollama_fast_model, 'digest': s.llm_fast_digest}]})
    assert request.url.path == '/api/chat'
    payloads.append(json.loads(request.content))
    response = 'Respuesta sintetica sin citas' if len(payloads) == 1 else '### Información documentada\nLa tasa sintética es 70% [[E1]]'
    return httpx.Response(200, json={'model': s.ollama_fast_model,
        'message': {'role': 'assistant', 'content': response}, 'done': True, 'done_reason': 'stop'})

synthetic = Evidence(source_id='synthetic/plan.pdf#0', text='Antigüedad %\n7 – 7.99 70',
    filename='Plan 2041.pdf', category='prestaciones', page_or_sheet='pagina 9', section='Portabilidad',
    document_id='synthetic', chunk_id='synthetic', score=.9)
with httpx.Client(transport=httpx.MockTransport(transport)) as http:
    result = KnowledgeAgent(llm=ModelClient(client=http), policy=ModelPolicy()).synthesize(
        question='Explica la tasa de prueba', evidences=(synthetic,),
        model_name=s.ollama_fast_model, choice=ModelChoice.FAST)
assert len(payloads) == 2 and result.grounding.grounded
assert all(prompts.RESPONSE_STYLE_POLICY in p['messages'][0]['content'] and
           prompts.DOCUMENT_SCOPE_POLICY in p['messages'][0]['content'] for p in payloads)

pdfs = json.loads((OUT/'pdf-comparison.json').read_text(encoding='utf-8'))
pension = next(d for d in pdfs if 'DICIEMBRE 2022' in d['file'])
chunk = next(c for c in pension['related_chunks'] if c['index'] == 8)
experiments = {}
for name, claim in {
    'table_percent_alone': 'La regla establece 70%',
    'dated_source_provenance': 'El documento de diciembre de 2022 establece 70%',
    'conditional_user_tenure': 'Si declara 7 años y 6 meses, la regla establece 70%',
    'incorrect_percentage': 'La regla establece 71%',
}.items():
    experiments[name] = {'claim': claim, 'supported': numeric_claim_supported(claim, (chunk['text'],))}

pyc = ROOT/'backend/app/agents/__pycache__/prompts.cpython-312.pyc'
data = pyc.read_bytes()
flags, timestamp, size = struct.unpack('<III', data[4:16])
source = ROOT/'backend/app/agents/prompts.py'
stat = source.stat()
cached_code = marshal.loads(data[16:])
def strings(code):
    for c in code.co_consts:
        if isinstance(c, str):
            yield c
        elif hasattr(c, 'co_consts'):
            yield from strings(c)
cached_strings = list(strings(cached_code))
evidence = {'runtime_process_verified': False,
    'reason': 'Backend PID 53604 stopped at 18:26:41 UTC; no active Python backend during audit',
    'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
    'pyc_sha256': hashlib.sha256(data).hexdigest(), 'pyc_flags': flags,
    'pyc_matches_source_mtime_size': timestamp == int(stat.st_mtime) and size == stat.st_size,
    'pyc_contains_current_style': prompts.RESPONSE_STYLE_POLICY in cached_strings,
    'pyc_contains_current_scope': prompts.DOCUMENT_SCOPE_POLICY in cached_strings,
    'payloads': payloads, 'synthetic_inventory_calls': inventory_calls,
    'result': dataclasses.asdict(result), 'pdf_numeric_experiments': experiments,
    'limitations': 'Fresh offline process, real builders/agent/provider/client/validators, synthetic transport; no historical prompt reconstruction or real inference'}
(OUT/'effective-payload-offline.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in evidence.items() if k not in ('payloads','result')},ensure_ascii=True,indent=2))
