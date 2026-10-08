"""Probe real retrieval and synthesis without HTTP, chat writes, or the live index.

Uses an explicitly supplied diagnostic index copy and current permissions for the account
that submitted the reported operation. SQL is guarded against non-SELECT work.
The JSON report contains metadata only. Prompts, evidence text, credentials,
identities, and generated answers are not saved. Only a validated answer is
printed to stdout. This is a service integration probe, not an HTTP/UI test;
private attachments, persistence, and queue are excluded. Conversation memory
is reconstructed as it was before the selected failed question, using current
permissions, and kept only in process memory.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT)
logging.disable(logging.CRITICAL)

PENSION_QUESTION = (
    "Según la plática del Plan de Pensiones por Jubilación de diciembre de 2022, "
    "si ingresé en mayo de 2016 y me retiro con 7 años y 6 meses de antigüedad antes de jubilarme, "
    "¿qué porcentaje me corresponde de las aportaciones básica, básica complementaria y adicional complementaria? "
    "Indica documento y página."
)
QUESTIONS = {"pensions": PENSION_QUESTION, "benefits": "explicame mis prestaciónes"}


def select_only(_conn, _cursor, statement, _parameters, _context, _executemany):
    if not statement.lstrip().upper().startswith("SELECT "):
        raise RuntimeError("Diagnostic SQL permits SELECT only")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-copy", type=Path, required=True,
                        help="Existing disposable Qdrant copy inside this diagnostic directory")
    parser.add_argument("--operation-id", required=True,
                        help="Reported diagnostic operation used only to resolve its account")
    parser.add_argument("--question-case", choices=tuple(QUESTIONS), default="pensions")
    parser.add_argument("--memory-mode", choices=("reported", "none"), default="reported")
    parser.add_argument("--retrieval-only", action="store_true")
    parser.add_argument("--show-failed-claim", action="store_true",
                        help="Print only the rejected cited claim for local diagnosis; never save it")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    question = QUESTIONS[args.question_case]
    output = (args.output or HERE / f"{args.question_case}-retrieved-generation-metadata.json").resolve()
    if output.parent != HERE:
        raise ValueError("Metadata output must stay in this diagnostic directory")
    snapshot = args.index_copy.resolve()
    if snapshot.parent != HERE or not (snapshot / "meta.json").is_file():
        raise RuntimeError("Existing diagnostic index copy required")

    # This investigation authorizes only installed, loopback SQL and inference.
    def local_connections(event_name, values):
        if event_name == "socket.connect":
            address = values[1]
            if not isinstance(address, tuple) or address[:2] not in {
                ("127.0.0.1", 3306), ("127.0.0.1", 11434),
                ("::1", 3306), ("::1", 11434),
            }:
                raise RuntimeError("Diagnostic permits local database and Ollama only")
    sys.addaudithook(local_connections)

    from qdrant_client import QdrantClient
    from sqlalchemy import event, select
    from sqlalchemy.orm import Session
    from app.agents.knowledge_agent import KnowledgeAgent
    from app.authorization.policy import get_policy_engine
    from app.common import answer_diagnostics
    from app.config import get_settings
    from app.database.engine import get_engine
    from app.database.models import ChatOperation, ConversationMessage, ConversationSummary, User
    from app.llm.model_policy import ModelPolicy
    from app.llm.provider import ModelClient, inference_deadline
    from app.memory.service import (
        ConversationContext, ConversationTurn, MemoryService, authorization_fingerprint,
    )
    from app.rag.retriever import Retriever
    from app.rag.vector_store import VectorStore

    report = {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "http_path_tested": False,
        "database_writes_allowed": False,
        "operational_index_opened": False,
        "conversation_memory_used": args.memory_mode == "reported",
        "memory_mode": args.memory_mode,
        "private_attachments_used": False,
        "question_case": args.question_case,
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "question_chars": len(question),
        "validation_attempts": [],
    }
    engine = None
    vector_client = None
    original_validated = answer_diagnostics.validated

    def validation_metadata(_answer, result, *, retry):
        entry = {
            "retry": retry, "grounded": result.grounded,
            "reason": result.reason, "detail": result.validation_detail,
            "claim_index": result.claim_index,
            "cited_source_count": len(result.cited_source_ids),
            "invalid_source_count": len(result.invalid_source_ids),
            "declares_insufficiency": result.declares_insufficiency,
        }
        report["validation_attempts"].append(entry)
        print(json.dumps({"stage": "validation", **entry}, ensure_ascii=False), flush=True)
        if args.show_failed_claim and not result.grounded:
            cursor = 0
            for index, match in enumerate(re.finditer(r"(?:\[\[[^\]]{1,240}\]\][ \t]*)+", _answer), 1):
                if index == result.claim_index:
                    print(json.dumps({"failed_claim": _answer[cursor:match.start()].strip(),
                                      "failed_citations": match.group()},
                                     ensure_ascii=False), flush=True)
                    break
                cursor = match.end()
            if result.claim_index is None:
                print(json.dumps({"failed_tail": _answer[cursor:].strip()}, ensure_ascii=False), flush=True)

    try:
        engine = get_engine()
        event.listen(engine, "before_cursor_execute", select_only)
        with Session(engine, autoflush=False, expire_on_commit=False) as db:
            operation = db.get(ChatOperation, args.operation_id)
            if operation is None:
                raise RuntimeError("Reported operation unavailable")
            user = db.get(User, operation.user_id)
            if user is None or not user.is_active:
                raise RuntimeError("Original account is unavailable or inactive")
            policies = get_policy_engine()
            context = policies.build_context(
                db, user=user, session_id=str(uuid4()), request_id=str(uuid4()),
            )
            context.require_valid(get_settings().app_secret_key.get_secret_value())
            categories = policies.effective_categories(context)
            rows = list(db.scalars(select(ConversationMessage).where(
                ConversationMessage.conversation_id == operation.conversation_id,
                ConversationMessage.created_at >= operation.created_at,
                ConversationMessage.role == "user",
            ).order_by(ConversationMessage.seq).limit(1)))
            if not rows:
                raise RuntimeError("Reported operation question unavailable")
            reported = rows[0]
            if args.question_case == "benefits":
                reported = db.scalars(select(ConversationMessage).where(
                    ConversationMessage.conversation_id == operation.conversation_id,
                    ConversationMessage.seq < reported.seq,
                    ConversationMessage.role == "user",
                ).order_by(ConversationMessage.seq.desc()).limit(1)).one()
            question = reported.content
            report["question_sha256"] = hashlib.sha256(question.encode("utf-8")).hexdigest()
            report["question_chars"] = len(question)
            scope = authorization_fingerprint(db, context, categories)
            prior = list(reversed(list(db.scalars(select(ConversationMessage).where(
                ConversationMessage.conversation_id == operation.conversation_id,
                ConversationMessage.seq < reported.seq,
            ).order_by(ConversationMessage.seq.desc()).limit(18)))))
            visible = [row for row in prior if MemoryService.message_visible(row, categories, scope)]
            summary_row = db.scalars(select(ConversationSummary).where(
                ConversationSummary.conversation_id == operation.conversation_id,
                ConversationSummary.created_at < reported.created_at,
            ).order_by(ConversationSummary.created_at.desc()).limit(1)).one_or_none()
            memory = ConversationContext(
                conversation_id=operation.conversation_id,
                summary=summary_row.summary if summary_row and summary_row.authorization_scope == scope else "",
                turns=tuple(ConversationTurn(role=row.role, content=row.content[:1200],
                    documented=row.role == "assistant" and bool(row.source_ids)
                        and row.answer_basis in {"documented", "mixed"}) for row in visible[-6:]),
                dropped_turns=len(prior) - len(visible),
            ) if args.memory_mode == "reported" else None
            report["memory"] = {"turn_count": len(memory.turns) if memory else 0,
                "summary_present": bool(memory.summary) if memory else False,
                "roles": [turn.role for turn in memory.turns] if memory else []}
        report["signed_context_verified"] = True
        report["authorized_category_count"] = len(categories)

        vector_client = QdrantClient(path=str(snapshot))
        model_client = ModelClient()
        with inference_deadline():
            retrieval = Retriever(store=VectorStore(client=vector_client), llm=model_client).retrieve(
                ctx=context, question=question, authorized_categories=categories,
                include_private=False, comparative=False,
            )
            report["retrieval"] = {
                "fetched": retrieval.fetched, "after_dedup": retrieval.after_dedup,
                "returned": len(retrieval.evidences), "best_score": retrieval.best_score,
                "truncated": retrieval.truncated,
                "sources": [{
                    "filename": item.filename, "page": item.page_or_sheet,
                    "category": item.category, "scope": item.scope, "score": item.score,
                } for item in retrieval.evidences],
            }
            print(json.dumps({"stage": "retrieval", **report["retrieval"]}, ensure_ascii=False), flush=True)
            if not retrieval.has_evidence:
                raise RuntimeError("No authorized evidence recovered")
            if args.retrieval_only:
                report["status"] = "retrieved"
                return 0

            model_policy = ModelPolicy()
            intent = model_policy.classify_intent(question)
            route = model_policy.route(
                question, intent=intent, categories_in_scope=len(retrieval.distinct_categories()),
                low_retrieval_confidence=retrieval.low_confidence,
                evidence_count=len(retrieval.evidences),
                evidence_chars=sum(len(item.text) for item in retrieval.evidences),
            )
            report["route"] = route.as_audit_dict()
            answer_diagnostics.validated = validation_metadata
            result = KnowledgeAgent(llm=model_client, policy=model_policy).synthesize(
                question=question, evidences=retrieval.evidences,
                memory=memory,
                model_name=route.model_name, choice=route.choice,
                deep_model_name=model_policy.deep_model, intent=intent,
                scope_note=", ".join(sorted(categories)),
                evidence_truncated=retrieval.truncated,
            )
        report.update({
            "status": "accepted", "regenerated": result.regenerated,
            "model": result.model, "grounded": result.grounding.grounded,
            "answer_basis": result.answer_basis, "latency_ms": result.latency_ms,
        })
        print(json.dumps({"status": "accepted", "answer": result.answer,
                          "regenerated": result.regenerated}, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        # Exception messages can contain payloads or credentials; publish class only.
        report.update({"status": "failed", "error_type": type(error).__name__})
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), flush=True)
        return 1
    finally:
        answer_diagnostics.validated = original_validated
        if vector_client is not None:
            vector_client.close()
        if engine is not None:
            event.remove(engine, "before_cursor_execute", select_only)
            engine.dispose()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
