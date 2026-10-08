"""Audit current files and bounded authorized SQL metadata; never opens Qdrant."""
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
sys.path.insert(0, str(ROOT/'backend'))
os.chdir(ROOT)
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def save(name, value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
if not (OUT/'baseline.json').exists():
    files={}
    for line in (ROOT/'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        expected,name=line.split('  ',1)
        files[name]={'expected':expected,'actual':sha(ROOT/name) if (ROOT/name).is_file() else None}
    save('baseline.json',{'checked_at':datetime.now(timezone.utc), 'files':files,
        'manifest_sha256':sha(ROOT/'SHA256SUMS.txt'),
        'protected':{n:sha(ROOT/n) for n in ('.env','backend/pyproject.toml','backend/uv.lock')},
        'control_existed':(ROOT/'var/diagnostics/next-answer.json').exists()})

from app.config import get_settings
s=get_settings()
names=('answer_evidence_mode','answer_allow_general_knowledge','llm_provider','llm_deep_provider',
    'ollama_fast_model','ollama_deep_model','llm_fast_thinking','llm_fast_top_k','ollama_fast_num_ctx',
    'ollama_fast_max_tokens','llm_top_p','llm_temperature','llm_retry_temperature','ollama_keep_alive',
    'rag_top_k','rag_fetch_k','rag_min_similarity','rag_mmr_lambda','rag_chunk_size_tokens',
    'rag_chunk_overlap_tokens','ollama_embedding_model','llm_embedding_provider','llm_embedding_revision',
    'llm_embedding_digest','rag_embedding_dimension','qdrant_mode','rag_collection_corporate',
    'llm_query_template','llm_document_template','app_log_level')
save('declared-config.json',{**{k:getattr(s,k,None) for k in names},
    'system_prefix_chars':len(s.llm_system_prefix),
    'system_prefix_sha256':hashlib.sha256(s.llm_system_prefix.encode()).hexdigest()})

log=ROOT/'var/logs/backend-20261006-131037.log'
events=[]
for number,line in enumerate(log.read_text(encoding='utf-8').splitlines(),1):
    try: row=json.loads(line)
    except ValueError: continue
    if row.get('message') in {'audit.chat.answer','rag.retrieved','llm.chat','agent.regenerating',
        'agent.answer_validation_failed','diagnostic.answer_trace','app.starting','app.stopped'}:
        events.append({'line':number,**row})
save('events-current.json',events)

from sqlalchemy import create_engine,event,select,func
from sqlalchemy.orm import Session
from app.database.models import User,Conversation,ConversationMessage,ChatOperation,Document,DocumentVersion,IngestionJob
from app.authorization.policy import get_policy_engine
from app.memory.service import MemoryService,authorization_fingerprint
from scripts.diagnosticar_rag import select_only_guard
engine=create_engine(s.database_url.get_secret_value(),hide_parameters=True)
event.listen(engine,'before_cursor_execute',select_only_guard)
audits=[r for r in events if r.get('message')=='audit.chat.answer']
requests=[]
with Session(engine,autoflush=False) as db:
    target_user=audits[-1]['user_opaque_id']
    user=db.get(User,target_user)
    assert user and user.is_active
    policy=get_policy_engine()
    ctx=policy.build_context(db,user=user,session_id='read-only-audit-no-session',request_id='read-only-audit')
    categories=policy.effective_categories(ctx)
    scope=authorization_fingerprint(db,ctx,categories)
    conversations=db.scalars(select(Conversation).where(Conversation.user_id==user.id,
        Conversation.deleted_at.is_(None)).order_by(Conversation.updated_at.desc()).limit(6)).all()
    save('conversations.json',{'user_id':user.id,'username':user.username,
        'effective_categories':sorted(categories),'permissions':sorted(ctx.permissions),
        'conversations':[{'id':c.id,'title':c.title,'updated_at':c.updated_at} for c in conversations]})
    for a in audits[-8:]:
        if a['user_opaque_id'] != user.id: continue
        op=db.scalar(select(ChatOperation).where(ChatOperation.request_id==a['request_id'],ChatOperation.user_id==user.id))
        if op is None or not op.response: continue
        response=op.response
        answer=db.scalar(select(ConversationMessage).where(ConversationMessage.id==response.get('message_id'),
            ConversationMessage.user_id==user.id,ConversationMessage.conversation_id==op.conversation_id))
        if answer is None or not MemoryService.message_visible(answer,categories,scope): continue
        question=db.scalar(select(ConversationMessage).where(ConversationMessage.user_id==user.id,
            ConversationMessage.conversation_id==op.conversation_id,ConversationMessage.role=='user',
            ConversationMessage.seq<answer.seq).order_by(ConversationMessage.seq.desc()).limit(1))
        if not question or not MemoryService.message_visible(question,categories,scope): continue
        requests.append({'request_id':a['request_id'],'conversation_id':op.conversation_id,'question':question.content,
            'question_seq':question.seq,'answer_seq':answer.seq,'response':response,'created_at':op.created_at,
            'same_authorization_scope':scope==op.authorization_scope})
    save('requests-current.json',requests)
    documents=db.scalars(select(Document).where(Document.scope=='corporate',Document.category.in_(categories),
        Document.category=='prestaciones',Document.deleted_at.is_(None))).all()
    fields=('id','filename','relative_path','sha256','status','chunk_count','active_generation','index_fingerprint',
            'ingestion_version','index_cleanup_pending','updated_at','category','scope')
    save('documents-current.json',[{k:getattr(d,k) for k in fields} for d in documents])
    duplicates=db.execute(select(Document.relative_path,func.count(Document.id)).where(Document.scope=='corporate',
        Document.category.in_(categories),Document.category=='prestaciones',Document.deleted_at.is_(None))
        .group_by(Document.relative_path).having(func.count(Document.id)>1)).all()
    versions=db.execute(select(DocumentVersion.document_id,DocumentVersion.sha256,DocumentVersion.ingested_at)
        .where(DocumentVersion.document_id.in_([d.id for d in documents]))).all()
    save('ingestion-metadata.json',{'duplicate_active_document_paths':[list(r) for r in duplicates],
        'document_versions':[list(r) for r in versions]})
engine.dispose()
print(json.dumps({'requests':len(requests),'documents':len(documents),'report':str(OUT)},ensure_ascii=True))
