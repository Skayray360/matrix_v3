"""Diagnostic calls to actual pure functions; no mocks and no inference."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sys

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]
sys.path.insert(0,str(ROOT/'backend'))
os.chdir(ROOT)
from app.llm.model_policy import ModelPolicy
from app.rag.numeric_grounding import numeric_claim_supported, _facts, _plain, _table_percentages
from app.rag.grounding import verify_grounding
from app.rag.schemas import Evidence
from app.agents.prompts import answer_system_policy, RESPONSE_STYLE_POLICY, DOCUMENT_SCOPE_POLICY

def save(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
requests=json.loads((OUT/'requests-current.json').read_text(encoding='utf-8'))
question=next(r['question'] for r in requests if r['request_id']=='61a95692-0e10-4b41-8be2-6a388569333c')
p=ModelPolicy()
scenarios=[('fresh',question,()),('explicit_after_benefit',question,('Explícame mis prestaciones.','¿Qué es el Plan Libre?')),
    ('repeat_complete',question,(question,)),('new_benefit','Según el reglamento de becas de 2025, ¿qué cubre el programa académico?',('¿Qué dice el plan de pensiones?',)),
    ('elliptical','¿Y con 8 años?',('¿Qué dice el plan de pensiones?',)),
    ('topic_change','¿Qué es la fotosíntesis?',('Explícame mis prestaciones.',))]
policy=[]
for name,q,prior in scenarios:
    ref=p.contextual_reference(q,prior_questions=prior)
    policy.append({'case':name,'question':q,'prior_questions':prior,'reference':ref,
        'intent':p.classify_intent(q,previous_question=ref).value,'query':f'{ref}\nSeguimiento: {q}' if ref else q})
save('policy-probes.json',policy)
docs=json.loads((OUT/'extraction-current.json').read_text(encoding='utf-8'))
chunks={c['source_id']:{'filename':Path(d['file']).name,**c} for d in docs for c in d['chunks']}
pension=next(c for c in chunks.values() if 'PENSIONES' in c['filename'] and c['index']==8)
text=pension['text']
claims=[('table_percent_only','La tabla muestra 70%.'),
    ('another_row_percent_only','La tabla muestra 80%.'),
    ('wrong_row_relationship','Para el tramo 7 – 7.99, la tabla indica 80%.'),
    ('metadata_year','El documento de diciembre de 2022 indica 70%.'),
    ('declared_tenure','Si los 7 años y 6 meses de antigüedad que declara son correctos, la regla indica 70%.'),
    ('derived_decimal','Para 7.5 años, la regla indica 70%.'),
    ('invented_percent','La tabla indica 71%.'),
    ('invented_with_abstention','No cuento con información suficiente, pero la tabla indica 71%.')]
save('numeric-probes.json',[{'name':name,'claim':claim,'supported':numeric_claim_supported(claim,(text,)),
    'claim_raw':sorted(_facts(_plain(claim)).raw),'claim_quantities':sorted(_facts(_plain(claim)).quantities),
    'table_quantities':sorted(_table_percentages(_plain(text)))} for name,claim in claims])
replays=[]
for request in requests[:2]:
    response=request['response']
    sources=response['sources']
    ev=[]
    for source in sources:
        c=chunks[source['source_id']]
        ev.append(Evidence(source_id=c['source_id'],text=c['text'],score=source['score'],category=source['category'],
            filename=c['filename'],section=c['section'],page_or_sheet=c['page_or_sheet'],document_id='offline-current-corpus',chunk_id='offline-current-corpus'))
    answer=response.get('answer',response.get('content'))
    if not isinstance(answer,str):
        raise ValueError(f'Response content field missing: {list(response)}')
    report=verify_grounding(answer,tuple(ev),mode='cited',allow_general_knowledge=True)
    replays.append({'request_id':request['request_id'],'evidence_basis':'current PDF extraction, publicly cited IDs; NOT original sent payload',
        'report':dataclasses.asdict(report)})
save('published-answer-validator-replays.json',replays)
save('prompt-fingerprints.json',{'style_sha256':hashlib.sha256(RESPONSE_STYLE_POLICY.encode()).hexdigest(),
    'scope_sha256':hashlib.sha256(DOCUMENT_SCOPE_POLICY.encode()).hexdigest(),
    'initial_system_sha256':hashlib.sha256(answer_system_policy().encode()).hexdigest(),
    'documentary_only_system_sha256':hashlib.sha256(answer_system_policy(documentary_only=True).encode()).hexdigest()})
print(json.dumps({'policy_cases':len(policy),'numeric_cases':len(claims),'published_validator_replays':replays},ensure_ascii=False))
