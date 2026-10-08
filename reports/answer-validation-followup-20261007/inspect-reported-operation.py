"""Inspect only reported operation context; read-only SQL, metadata output."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT)
logging.disable(logging.CRITICAL)

from sqlalchemy import event, select
from sqlalchemy.orm import Session
from app.authorization.policy import get_policy_engine
from app.database.engine import get_engine
from app.database.models import ChatOperation, Conversation, ConversationMessage, ConversationSummary, Document, User
from app.memory.service import MemoryService, authorization_fingerprint
from app.llm.model_policy import ModelPolicy

OPERATION = "18df73988feaf42311d6ee2c83f777e71cc98aa51c5169e3acfcc8073a996306"

def select_only(_conn, _cursor, statement, _parameters, _context, _executemany):
    if not statement.lstrip().upper().startswith("SELECT "):
        raise RuntimeError("Diagnostic SQL permits SELECT only")

engine = get_engine()
event.listen(engine, "before_cursor_execute", select_only)
try:
    with Session(engine, autoflush=False, expire_on_commit=False) as db:
        op = db.get(ChatOperation, OPERATION)
        assert op is not None
        user = db.get(User, op.user_id)
        policies = get_policy_engine()
        ctx = policies.build_context(db, user=user, session_id=str(uuid4()), request_id=str(uuid4()))
        cats = policies.effective_categories(ctx)
        scope = authorization_fingerprint(db, ctx, cats)
        conversation = db.get(Conversation, op.conversation_id)
        assert conversation is not None
        messages = list(db.scalars(select(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation.id
        ).order_by(ConversationMessage.seq)))
        operations = list(db.scalars(select(ChatOperation).where(
            ChatOperation.conversation_id == conversation.id
        ).order_by(ChatOperation.created_at)))
        summaries = list(db.scalars(select(ConversationSummary).where(
            ConversationSummary.conversation_id == conversation.id
        )))
        attachments = list(db.scalars(select(Document).where(Document.conversation_id == conversation.id)))
        print(json.dumps({"operation_status": op.status, "error": op.error_code,
            "operation_created_at": str(op.created_at), "operation_started_at": str(op.started_at),
            "pending_message_present": bool(op.message), "response_present": bool(op.response),
            "conversation_deleted": conversation.deleted_at is not None,
            "total_messages": len(messages), "summary_count": len(summaries),
            "attachment_count": len(attachments), "authorized_category_count": len(cats),
            "operations": [{"at": str(x.created_at), "status": x.status, "error": x.error_code} for x in operations],
            "messages": [{"seq": x.seq, "role": x.role, "at": str(x.created_at), "chars": len(x.content),
                "intent": x.intent, "answer_basis": x.answer_basis, "has_sources": bool(x.source_ids),
                "visible": MemoryService.message_visible(x, cats, scope)} for x in messages]}, ensure_ascii=False))
        # Only exact reported topic questions, never earlier unrelated content.
        selected = [x for x in messages if x.role == "user" and
            any(term in x.content.lower() for term in ("prestacion", "prestación", "pensiones"))][-2:]
        for row in selected:
            prior = [x for x in messages if x.seq < row.seq][-18:]
            visible = [x for x in prior if MemoryService.message_visible(x, cats, scope)][-6:]
            questions = []
            documented = set()
            for x in visible:
                if x.role == "user":
                    questions.append(x.content[:1200])
                elif x.source_ids and x.answer_basis in {"documented", "mixed"} and questions:
                    documented.add(len(questions)-1)
            policy = ModelPolicy()
            ref = policy.contextual_reference(row.content, prior_questions=tuple(questions), documented_indices=frozenset(documented))
            intent = policy.classify_intent(row.content, previous_question=ref)
            print(json.dumps({"question": row.content, "prior_memory_turns": len(visible),
                "prior_memory_roles": [x.role for x in visible],
                "prior_memory_chars": [len(x.content[:1200]) for x in visible],
                "contextual_reference_present": bool(ref), "intent": intent.value,
                "summary_used": bool(summaries)}, ensure_ascii=False))
finally:
    event.remove(engine, "before_cursor_execute", select_only)
    engine.dispose()
