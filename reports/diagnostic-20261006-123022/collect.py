"""Read-only, bounded inspection. Never instantiates a vector store or LLM client."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
os.chdir(ROOT)

def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

if not (OUT / 'baseline.json').exists():
    manifest = {}
    for line in (ROOT / 'SHA256SUMS.txt').read_text().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            name = name.lstrip('*')
            manifest[name] = {'expected': digest, 'actual': sha(ROOT / name) if (ROOT / name).is_file() else None}
    save('baseline.json', manifest)
    for name in ('backend/app/agents/orchestrator.py', 'backend/app/agents/knowledge_agent.py',
                 'backend/app/llm/ollama_client.py', 'SHA256SUMS.txt'):
        dest = OUT / 'backup' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest)

from app.config import get_settings
s = get_settings()
names = ('answer_evidence_mode', 'answer_allow_general_knowledge', 'llm_provider', 'llm_deep_provider',
         'ollama_fast_model', 'ollama_deep_model', 'llm_fast_thinking', 'llm_fast_top_k',
         'ollama_fast_num_ctx', 'ollama_fast_max_tokens', 'llm_top_p', 'llm_temperature',
         'llm_retry_temperature', 'ollama_keep_alive', 'rag_top_k', 'rag_fetch_k',
         'rag_min_similarity', 'rag_mmr_lambda', 'rag_chunk_size_tokens', 'rag_chunk_overlap_tokens')
config = {name: getattr(s, name, 'NOT_A_FIELD') for name in names}
config['system_prefix_chars'] = len(s.llm_system_prefix)
config['system_prefix_sha256'] = hashlib.sha256(s.llm_system_prefix.encode()).hexdigest()
save('current-config.json', config)

# Scope: only latest backend log; exclude HTTP polling/login and opaque user identifiers.
events = []
log = ROOT / 'var/logs/backend-20261006-122112.log'
for n, line in enumerate(log.read_text(encoding='utf-8').splitlines(), 1):
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if row.get('message') in {'rag.retrieved', 'llm.chat', 'agent.regenerating', 'agent.answer_validation_failed',
                              'audit.chat.answer', 'app.started', 'app.stopped'} or row.get('logger') == 'uvicorn.error':
        events.append({'line': n, **{k: v for k, v in row.items() if k not in ('user_opaque_id', 'role_set_hash', 'color_message')}})
save('recent-events.json', events)

# Real authorization policy, SQL SELECT only, no session creation/commit or migrations.
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from app.database.models import ChatOperation, ConversationMessage, Document, User
from app.authorization.policy import get_policy_engine
from scripts.diagnosticar_rag import select_only_guard
engine = create_engine(s.database_url.get_secret_value(), hide_parameters=True)
event.listen(engine, 'before_cursor_execute', select_only_guard)
audit_ids = [e['request_id'] for e in events if e.get('message') == 'audit.chat.answer']
try:
    with Session(engine, autoflush=False) as db:
        operations = db.scalars(select(ChatOperation).where(ChatOperation.request_id.in_(audit_ids))).all()
        results = []
        for op in operations:
            user = db.get(User, op.user_id)
            if user is None or not user.is_active:
                continue
            policy = get_policy_engine()
            ctx = policy.build_context(db, user=user, session_id='offline-diagnostic-not-a-session', request_id=op.request_id)
            categories = policy.effective_categories(ctx)
            # Matching request response message id ties the prior user message to the exact operation.
            response = op.response or {}
            answer_id = response.get('message_id')
            answer = db.scalar(select(ConversationMessage).where(ConversationMessage.id == answer_id,
                ConversationMessage.user_id == op.user_id, ConversationMessage.conversation_id == op.conversation_id))
            question = None
            if answer:
                question = db.scalar(select(ConversationMessage).where(
                    ConversationMessage.user_id == op.user_id, ConversationMessage.conversation_id == op.conversation_id,
                    ConversationMessage.role == 'user', ConversationMessage.seq < answer.seq
                ).order_by(ConversationMessage.seq.desc()).limit(1))
            if question and not set(question.authorized_categories or []).issubset(categories):
                question = None
            results.append({'request_id': op.request_id, 'created_at': op.created_at,
                'response_keys': sorted(response), 'question': question.content if question else None,
                'answer_message_id': answer_id, 'intent': answer.intent if answer else None,
                'final_answer': answer.content if answer and set(answer.authorized_categories or []).issubset(categories) else None,
                'public_sources': response.get('sources'), 'status': op.status,
                'matching': 'response.message_id plus immediately preceding same-owner/same-conversation user seq'})
        save('recent-requests.json', results)
        # Only current corporate PDFs in the relevant authorized category.
        if operations:
            docs = db.scalars(select(Document).where(Document.scope == 'corporate', Document.category == 'prestaciones',
                Document.category.in_(categories), Document.deleted_at.is_(None))).all()
            save('document-metadata.json', [{k: getattr(d, k) for k in (
                'filename', 'relative_path', 'sha256', 'status', 'chunk_count', 'active_generation', 'updated_at')} for d in docs])
except Exception as exc:
    save('sql-error.json', {'type': type(exc).__name__})
    print('SQL_ERROR', type(exc).__name__)
finally:
    engine.dispose()

# Current bytes through actual PDF loader/chunker, with no embeddings or index access.
from app.ingestion.loaders import extract_pdf
from app.rag.chunking import chunk_blocks
from pypdf import PdfReader
pdfs = []
for path in (ROOT / 'data/prestaciones').glob('*.pdf'):
    reader = PdfReader(path)
    pages = []
    for page_no, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ''
        if any(term in text.casefold() for term in ('casamiento', 'matrimonio', 'ingresaron', 'ingreso', 'antigüedad', 'antiguedad')):
            pages.append({'page': page_no, 'text': text, 'layout_text': page.extract_text(extraction_mode='layout')})
    if not pages:
        continue
    doc = extract_pdf(path.read_bytes())
    chunks = chunk_blocks(doc.blocks, chunk_size_tokens=s.rag_chunk_size_tokens, overlap_tokens=s.rag_chunk_overlap_tokens)
    related = [dataclasses.asdict(c) for c in chunks if any(t in c.text.casefold() for t in
               ('casamiento', 'matrimonio', 'ingresaron', 'ingreso', 'antigüedad', 'antiguedad'))]
    pdfs.append({'file': path.relative_to(ROOT).as_posix(), 'sha256': sha(path), 'page_count': len(reader.pages),
                 'pages': pages, 'blocks': [dataclasses.asdict(b) for b in doc.blocks if b.page_or_sheet in
                     {f"pagina {p['page']}" for p in pages}], 'related_chunks': related, 'warnings': doc.warnings})
save('pdf-comparison.json', pdfs)
print(json.dumps({'report': str(OUT), 'events': len(events), 'pdfs': len(pdfs), 'config': config}, ensure_ascii=False))
