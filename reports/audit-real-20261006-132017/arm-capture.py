"""Arm existing one-turn instrumentation, scoped and authorized. No service or chat calls."""
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone, timedelta

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]
sys.path.insert(0,str(ROOT/'backend'))
os.chdir(ROOT)
from app.config import get_settings
from app.security.prompt_guard import sanitize_user_message
from app.authorization.policy import get_policy_engine
from app.database.models import Conversation,ConversationMessage,User,Document
from sqlalchemy import create_engine,event,select,func
from sqlalchemy.orm import Session
from scripts.diagnosticar_rag import select_only_guard

mode=sys.argv[1]
assert mode in ('new','history')
control=ROOT/'var/diagnostics/next-answer.json'
assert not control.exists(), 'Existing control: inspect before replacing'
owner=json.loads((OUT/'conversations.json').read_text(encoding='utf-8'))['user_id']
records=json.loads((OUT/'requests-current.json').read_text(encoding='utf-8'))
question=next(r['question'] for r in records if r['request_id']=='61a95692-0e10-4b41-8be2-6a388569333c')
assert len(question)==278, len(question)
s=get_settings()
engine=create_engine(s.database_url.get_secret_value(),hide_parameters=True)
event.listen(engine,'before_cursor_execute',select_only_guard)
with Session(engine,autoflush=False) as db:
    user=db.get(User,owner)
    assert user and user.is_active
    policy=get_policy_engine()
    ctx=policy.build_context(db,user=user,session_id='read-only-audit',request_id='read-only-audit')
    categories=policy.effective_categories(ctx)
    count=select(func.count()).where(ConversationMessage.conversation_id==Conversation.id).correlate(Conversation).scalar_subquery()
    if mode=='new':
        matches=db.scalars(select(Conversation).where(Conversation.user_id==owner,Conversation.deleted_at.is_(None),
            Conversation.created_at>=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(minutes=45),count==0)).all()
        assert len(matches)==1, f'Expected one recent empty conversation; found {len(matches)}'
        conv=matches[0]
    else:
        conv=db.get(Conversation,'6cd8b210-5f5f-4ed4-91ca-329aad41f44e')
        assert conv and conv.user_id==owner and conv.deleted_at is None
    docs=db.scalars(select(Document).where(Document.scope=='corporate',Document.category.in_(categories),
        Document.category=='prestaciones',Document.deleted_at.is_(None),Document.status=='indexed')).all()
    pension=next(d for d in docs if 'PENSIONES' in d.filename)
    benefits=next(d for d in docs if d.filename=='prestaciones.pdf')
    sources=[pension.relative_path+'#'+str(i) for i in (8,4,5,6,9,1,2)]+[benefits.relative_path+'#4']
    now=datetime.now(timezone.utc)
    payload={'user_id':owner,'conversation_id':conv.id,
        'question_sha256':hashlib.sha256(sanitize_user_message(question).text.encode()).hexdigest(),
        'expires_at':(now+timedelta(minutes=14)).isoformat(),'source_ids':sources}
    raw=json.dumps(payload,ensure_ascii=False,indent=2).encode()
    control.parent.mkdir(parents=True,exist_ok=True)
    with control.open('xb') as stream:
        stream.write(raw)
    (OUT/f'control-{mode}.json').write_bytes(raw)
    (OUT/f'capture-{mode}-scope.json').write_text(json.dumps({'armed_at':now.isoformat(),
        'conversation_title':conv.title,'conversation_id':conv.id,'empty':mode=='new',
        'question':question,'trace_key':hashlib.sha256(raw).hexdigest()},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'armed':True,'mode':mode,'conversation_id':conv.id,'question':question,'expires_at':payload['expires_at']},ensure_ascii=False))
engine.dispose()
