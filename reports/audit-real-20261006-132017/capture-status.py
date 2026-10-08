"""Read only the scoped request logs/SQL result; remove own consumed diagnostic control."""
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]
sys.path.insert(0,str(ROOT/'backend'))
os.chdir(ROOT)
mode=sys.argv[1]
assert mode in ('new','history')
control_raw=(OUT/f'control-{mode}.json').read_bytes()
control=json.loads(control_raw)
key=hashlib.sha256(control_raw).hexdigest()
all_events=[]
for path in sorted((ROOT/'var/logs').glob('backend-20261006-*.log')):
    if path.name<'backend-20261006-131037.log' or path.name.endswith('.err.log'):
        continue
    for line_number,line in enumerate(path.read_text(encoding='utf-8').splitlines(),1):
        try: row=json.loads(line)
        except ValueError: continue
        if row.get('trace_key')==key or row.get('message') in {
            'audit.chat.answer','rag.retrieved','llm.chat','agent.regenerating','agent.answer_validation_failed',
            'llm.completion_regenerating','llm.completion_recovered','llm.failed','llm.model_fallback'}:
            all_events.append({'log':path.name,'line':line_number,**row})
ids={e['request_id'] for e in all_events if e.get('trace_key')==key}
assert len(ids)<=1, 'Unexpected multiple scoped requests'
events=[e for e in all_events if e.get('request_id') in ids]
if not events:
    print(json.dumps({'mode':mode,'captured':False,'expires_at':control['expires_at']}))
    sys.exit(0)
(OUT/f'capture-{mode}-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8')
completed=any(e.get('message')=='audit.chat.answer' for e in events)
if completed:
    from app.config import get_settings
    from app.database.models import User,Conversation,ConversationMessage,ChatOperation
    from app.authorization.policy import get_policy_engine
    from app.memory.service import MemoryService,authorization_fingerprint
    from sqlalchemy import create_engine,event,select
    from sqlalchemy.orm import Session
    from scripts.diagnosticar_rag import select_only_guard
    engine=create_engine(get_settings().database_url.get_secret_value(),hide_parameters=True)
    event.listen(engine,'before_cursor_execute',select_only_guard)
    with Session(engine,autoflush=False) as db:
        user=db.get(User,control['user_id'])
        conv=db.get(Conversation,control['conversation_id'])
        assert user and user.is_active and conv and conv.user_id==user.id and not conv.deleted_at
        policy=get_policy_engine()
        ctx=policy.build_context(db,user=user,session_id='read-only-audit',request_id='read-only-audit')
        categories=policy.effective_categories(ctx)
        scope=authorization_fingerprint(db,ctx,categories)
        op=db.scalar(select(ChatOperation).where(ChatOperation.request_id==next(iter(ids)),
            ChatOperation.user_id==user.id,ChatOperation.conversation_id==conv.id))
        assert op and op.response and scope==op.authorization_scope
        answer=db.get(ConversationMessage,op.response['message_id'])
        assert answer and answer.user_id==user.id and answer.conversation_id==conv.id
        assert MemoryService.message_visible(answer,categories,scope)
        result={'request_id':op.request_id,'conversation_id':conv.id,'same_authorization_scope':True,
            'response':op.response,'answer_seq':answer.seq,'status':op.status}
        (OUT/f'capture-{mode}-published.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    engine.dispose()
    operational=ROOT/'var/diagnostics/next-answer.json'
    if operational.is_file() and hashlib.sha256(operational.read_bytes()).hexdigest()==key:
        operational.unlink()
        (OUT/f'capture-{mode}-closed.json').write_text(json.dumps({'closed_at':datetime.now(timezone.utc).isoformat(),
            'removed_own_control':True,'trace_key':key},indent=2),encoding='utf-8')
print(json.dumps({'mode':mode,'request_ids':list(ids),'completed':completed,
    'stages':[e.get('diagnostic_stage') for e in events if e.get('trace_key')==key],
    'validation':[{'retry':e['diagnostic']['retry'],'report':e['diagnostic']['report']} for e in events if e.get('diagnostic_stage')=='validated']},ensure_ascii=False))
