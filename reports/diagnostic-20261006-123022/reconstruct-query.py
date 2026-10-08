"""Historical query reconstruction from a bounded, same-owner conversation window.

This is not a capture of the historical retriever call. Uses actual visibility
and classifier functions; only SELECTs, no vector store or generation.
"""
import json
import os
from pathlib import Path
import sys

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT/'backend'))
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from app.config import get_settings
from app.database.models import ChatOperation, ConversationMessage, User
from app.authorization.policy import get_policy_engine
from app.memory.service import (MemoryService, authorization_fingerprint, DEFAULT_RECENT_TURNS,
                                MAX_MESSAGE_CHARS_IN_CONTEXT)
from app.llm.model_policy import ModelPolicy
from app.rag.embedding_prompts import format_query
from app.security.prompt_guard import sanitize_user_message
from scripts.diagnosticar_rag import select_only_guard

requests = json.loads((OUT/'recent-requests.json').read_text(encoding='utf-8'))
engine = create_engine(get_settings().database_url.get_secret_value(), hide_parameters=True)
event.listen(engine, 'before_cursor_execute', select_only_guard)
results = []
with Session(engine, autoflush=False) as db:
    for request in requests:
        if request['public_sources']:
            continue
        op = db.scalar(select(ChatOperation).where(ChatOperation.request_id == request['request_id']))
        user = db.get(User, op.user_id)
        assert user.is_active
        policy = get_policy_engine()
        ctx = policy.build_context(db, user=user, session_id='offline-diagnostic-not-a-session', request_id=op.request_id)
        categories = policy.effective_categories(ctx)
        scope = authorization_fingerprint(db, ctx, categories)
        answer = db.scalar(select(ConversationMessage).where(ConversationMessage.id == request['answer_message_id'],
            ConversationMessage.user_id == op.user_id, ConversationMessage.conversation_id == op.conversation_id))
        question = db.scalar(select(ConversationMessage).where(ConversationMessage.seq < answer.seq,
            ConversationMessage.role == 'user', ConversationMessage.user_id == op.user_id,
            ConversationMessage.conversation_id == op.conversation_id).order_by(ConversationMessage.seq.desc()).limit(1))
        rows = list(db.scalars(select(ConversationMessage).where(ConversationMessage.seq < question.seq,
            ConversationMessage.user_id == op.user_id, ConversationMessage.conversation_id == op.conversation_id
        ).order_by(ConversationMessage.seq.desc()).limit(DEFAULT_RECENT_TURNS * 3)))
        visible = [r for r in reversed(rows) if MemoryService.message_visible(r, categories, scope)][-DEFAULT_RECENT_TURNS:]
        prior = tuple(r.content[:MAX_MESSAGE_CHARS_IN_CONTEXT] for r in visible if r.role == 'user')
        text = sanitize_user_message(question.content).text
        reference = ModelPolicy().contextual_reference(text, prior_questions=prior)
        query = f'{reference}\nSeguimiento: {text}' if reference else text
        results.append({'request_id': op.request_id, 'same_authorization_fingerprint': scope == op.authorization_scope,
            'reference': reference, 'retrieval_query_reconstructed': query, 'embedding_query_reconstructed': format_query(query),
            'intent_recomputed': ModelPolicy().classify_intent(text, previous_question=reference).value,
            'visible_prior_turn_count': len(visible), 'limitation': 'reconstructed; historical payload not recorded'})
engine.dispose()
(OUT/'reconstructed-queries.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=True,indent=2))
