"""Isolated real submit/poll reproduction; never modifies the operational DB.

Uses FastAPI TestClient ASGI transport in-process, not browser/TCP. Source SQL
is SELECT-only. Only RBAC, authorized corporate document metadata and the
reported conversation prefix are read. No other users/chats/credentials are
copied. Runtime SQL is disposable SQLite (removed on clean shutdown); report
JSON contains metadata only. Full failing claims are printed only to stdout.

Default is preparation/auth verification only. --run explicitly enables real
Ollama inference. The Qdrant snapshot must be closed in all other processes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from itertools import count
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import unicodedata
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
REPORT_ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.authorization.categories import get_registry
from app.authorization.policy import get_policy_engine
from app.config import get_settings, QdrantMode
from app.database import engine as database
from app.database.models import (
    AuthorizationPolicy, Base, CategoryPermission, ChatOperation,
    ConversationMessage, Document, DocumentAccessPolicy, Permission, Role,
    RolePermission, StructuredSourcePermission, User, UserRole,
)

OPERATION = "18df73988feaf42311d6ee2c83f777e71cc98aa51c5169e3acfcc8073a996306"
BENEFITS = "Explicame mis prestaciones."
REPORTED_BENEFITS = "explicame sobre mis prestaciones"
PENSION = (
    "Según la plática del Plan de Pensiones por Jubilación de diciembre de 2022, "
    "si ingresé en mayo de 2016 y me retiro con 7 años y 6 meses de antigüedad "
    "antes de jubilarme, ¿qué porcentaje me corresponde de las aportaciones "
    "básica, básica complementaria y adicional complementaria? Indica documento y página."
)


@compiles(MEDIUMTEXT, "sqlite")
def sqlite_mediumtext(_type, _compiler, **_kwargs):
    return "TEXT"


def emit(event_name, **fields):
    print(json.dumps({"probe": event_name, **fields}, ensure_ascii=False), flush=True)


def select_only(_conn, _cursor, statement, _parameters, _context, _executemany):
    if not statement.lstrip().upper().startswith("SELECT "):
        raise RuntimeError("Operational SQL permits SELECT only")


def row_dict(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def normalized(text):
    return "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(char))


def pension_acceptance(answer, sources):
    """Inspect the complete generated answer in RAM; return booleans only.

    Conservative units keep separate table rows/bullets/sentences apart, so a
    nearby percentage for another benefit cannot satisfy a required concept.
    One shared statement explicitly listing all three at 70% is supported.
    """
    from app.rag.grounding import CITATION_RE, extract_citations
    text = normalized(CITATION_RE.sub("", answer))
    text = text.replace("**", "").replace("__", "")
    units = [unit.strip() for unit in re.split(r"\n|;|(?<=[.!?])\s+", text) if unit.strip()]
    concept_patterns = {
        "basic_70_percent": r"\bbasicas?\b(?!\s+complementarias?\b)",
        "basic_complementary_70_percent": r"\bbasicas?\s+complementarias?\b",
        "additional_complementary_70_percent": r"\badicional(?:es)?\s+complementarias?\b",
    }
    result = {}
    for name, concept in concept_patterns.items():
        result[name] = any(
            re.search(concept, unit) is not None
            and re.findall(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:%|por ciento)", unit)
            and all(float(value.replace(",", ".")) == 70 for value in
                    re.findall(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:%|por ciento)", unit))
            for unit in units
        )
    cited = set(extract_citations(answer))
    expected_name = normalized("PLATICA DE PLAN DE PENSIONES POR JUBILACI#U00d3N DICIEMBRE 2022.pdf")
    correct_document = [source for source in sources
                        if normalized(str(source.get("filename", ""))) == expected_name
                        and source.get("source_id") in cited]
    result["correct_document_cited"] = bool(correct_document)
    result["correct_document_page_9_cited"] = any(
        re.fullmatch(r"(?:pagina|pag\.?|p\.?)\s*9|9", normalized(str(source.get("page_or_sheet", ""))).strip())
        is not None for source in correct_document
    )
    result["complete_pension_answer"] = all(result.values())
    return result


def read_source():
    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value(), hide_parameters=True)
    event.listen(engine, "before_cursor_execute", select_only)
    try:
        with Session(engine, autoflush=False, expire_on_commit=False) as db:
            operation = db.get(ChatOperation, OPERATION)
            if operation is None:
                raise RuntimeError("Reported operation was not found")
            user = db.get(User, operation.user_id)
            if user is None or not user.is_active:
                raise RuntimeError("Reported user is unavailable")
            policy = get_policy_engine()
            ctx = policy.build_context(db, user=user, session_id=str(uuid4()), request_id=str(uuid4()))
            categories = policy.effective_categories(ctx)
            role_ids = list(db.scalars(select(UserRole.role_id).where(UserRole.user_id == user.id)))
            permission_ids = list(db.scalars(select(RolePermission.permission_id).where(RolePermission.role_id.in_(role_ids))))
            source_rows = {}
            for model, condition in (
                (Role, Role.id.in_(role_ids)),
                (Permission, Permission.id.in_(permission_ids)),
                (RolePermission, RolePermission.role_id.in_(role_ids)),
                (CategoryPermission, CategoryPermission.role_id.in_(role_ids)),
                (StructuredSourcePermission, StructuredSourcePermission.role_id.in_(role_ids)),
                (Document, (Document.scope == "corporate") & Document.category.in_(categories)),
            ):
                source_rows[model] = [row_dict(row) for row in db.scalars(select(model).where(condition))]
            document_ids = [row["id"] for row in source_rows[Document]]
            source_rows[DocumentAccessPolicy] = [row_dict(row) for row in db.scalars(
                select(DocumentAccessPolicy).where(DocumentAccessPolicy.document_id.in_(document_ids))
            )]
            policies = db.scalars(select(AuthorizationPolicy).where(
                ((AuthorizationPolicy.subject_kind == "user") & (AuthorizationPolicy.subject_key == user.id))
                | ((AuthorizationPolicy.subject_kind == "role") & AuthorizationPolicy.subject_key.in_(ctx.roles))
            ))
            source_rows[AuthorizationPolicy] = [row_dict(row) for row in policies]
            # The source prefix is used only in reported-history and never printed.
            original_messages = list(db.scalars(select(ConversationMessage).where(
                ConversationMessage.conversation_id == operation.conversation_id,
                ConversationMessage.user_id == user.id,
            ).order_by(ConversationMessage.seq)))
            prefix = []
            failed_benefits_turn = None
            from app.memory.service import MemoryService, authorization_fingerprint
            scope = authorization_fingerprint(db, ctx, categories)
            for message in original_messages:
                if message.role == "user" and "prestacion" in message.content.lower():
                    failed_benefits_turn = {name: getattr(message, name) for name in (
                        "role", "content", "model", "intent", "answer_basis", "source_ids",
                    )}
                    break
                if MemoryService.message_visible(message, categories, scope):
                    prefix.append({name: getattr(message, name) for name in (
                        "role", "content", "model", "intent", "answer_basis", "source_ids",
                    )})
            # Real category policies are cached before runtime paths are redirected.
            registry = get_registry()
            return {
                "rows": source_rows, "roles": role_ids, "source_user_id": user.id,
                "expected": (ctx.roles, ctx.permissions, categories, ctx.category_wildcard,
                             ctx.allowed_sources, ctx.structured_source_grants),
                "registry_categories": registry.known(), "prefix": prefix,
                "failed_benefits_turn": failed_benefits_turn,
                "metadata": {"authorized_categories": len(categories), "roles": len(role_ids),
                             "corporate_documents": len(document_ids), "history_prefix_turns": len(prefix)},
            }
    finally:
        event.remove(engine, "before_cursor_execute", select_only)
        engine.dispose()


class Observation:
    """Passive Python return observer: does not alter arguments/results/control flow."""
    def __init__(self):
        self.current = "setup"
        self.validations = []
        self.calculated_cases = set()
        self.pension_evidence = {}
        self.diagnostics = threading.local()
        self.last_inner_report = threading.local()

    def profile(self, frame, event_name, result):
        if getattr(self.diagnostics, "direct_check", False):
            return
        if event_name != "return" or frame.f_code.co_name not in (
            "verify_grounding", "_verify_cited_answer", "calculated_application_answer"
        ):
            return
        if (frame.f_code.co_name == "calculated_application_answer"
                and frame.f_code.co_filename.replace("\\", "/").endswith("/app/rag/calculated_application.py")):
            if isinstance(result, tuple) and len(result) == 2 and getattr(result[1], "grounded", False):
                self.calculated_cases.add(self.current)
                emit("calculated_application", case=self.current, calculated_application=True)
            return
        if not frame.f_code.co_filename.replace("\\", "/").endswith("/app/rag/grounding.py"):
            return
        if result is None or not hasattr(result, "grounded"):
            return
        if frame.f_locals.get("question") == PENSION and frame.f_locals.get("evidences"):
            self.pension_evidence[self.current] = tuple(frame.f_locals["evidences"])
        # Inner cited validation and its outer return share one result object.
        if frame.f_code.co_name == "verify_grounding" and getattr(self.last_inner_report, "value", None) is result:
            self.last_inner_report.value = None
            return
        if frame.f_code.co_name == "_verify_cited_answer":
            self.last_inner_report.value = result
        item = {"case": self.current, "grounded": result.grounded,
                "reason": result.reason, "validation_detail": result.validation_detail,
                "claim_index": result.claim_index, "cited_sources": len(result.cited_source_ids)}
        self.validations.append(item)
        fields = dict(item)
        if not result.grounded:
            fields["failed_claim"] = frame.f_locals.get(
                "tail" if "final sin cita" in result.reason else "claim", ""
            )
            fields["heading"] = frame.f_locals.get("heading", "")
        emit("validation", **fields)

    def check_calculated_application(self, case):
        """Separate read-only helper check over actual generation evidence in RAM.

        This is never substituted for a published answer and does not mark a
        case as having used calculated recovery in the real chat execution.
        """
        from app.rag.calculated_application import calculated_application_answer
        evidence = self.pension_evidence.get(case, ())
        self.diagnostics.direct_check = True
        try:
            calculated = calculated_application_answer(PENSION, evidence) if evidence else None
            acceptance = pension_acceptance(calculated[0], [item.to_public_dict() for item in evidence]) if calculated else {}
            result = {"separate_from_published_answer": True, "real_generation_evidence_captured": bool(evidence),
                      "returned_answer": calculated is not None,
                      "grounded": bool(calculated and calculated[1].grounded),
                      "complete_pension_answer": acceptance.get("complete_pension_answer", False)}
            emit("calculated_application_direct_check", case=case, **result)
            return result
        finally:
            self.diagnostics.direct_check = False
            self.pension_evidence.pop(case, None)


def verify_persistence(factory, operation_id, response):
    with factory() as db:
        op = db.get(ChatOperation, operation_id)
        if op is None:
            raise AssertionError("Accepted operation was not persisted")
        result = {"persisted_status": op.status, "pending_message_cleared": op.message is None,
                  "pending_session_cleared": op.session_id is None}
        messages = list(db.scalars(select(ConversationMessage).where(
            ConversationMessage.conversation_id == op.conversation_id).order_by(ConversationMessage.seq)))
        result["thread_turns"] = len(messages)
        result["thread_assistant_turns"] = sum(message.role == "assistant" for message in messages)
        if response.get("status") == "completed":
            public = response["response"]
            stored = db.get(ConversationMessage, public["message_id"])
            if stored is None or stored.role != "assistant" or stored.content != public["answer"]:
                raise AssertionError("Completed HTTP response does not match published assistant message")
            if op.response != public:
                raise AssertionError("Published operation differs from HTTP response")
            result.update({"assistant_published": True, "answer_chars": len(stored.content),
                           "answer_basis": public["answer_basis"], "grounded": public["grounded"],
                           "intent": public["intent"], "source_count": len(public["sources"]),
                           "latency_ms": public["latency_ms"]})
            from app.rag.grounding import extract_citations
            cited = set(extract_citations(stored.content))
            result["stored_citations_match"] = cited == set(stored.source_ids or [])
            if not result["stored_citations_match"]:
                raise AssertionError("Published citations differ from stored authorization references")
            result["sources"] = [{key: source.get(key) for key in (
                "filename", "page", "page_start", "page_end", "page_or_sheet", "category", "source_id"
            ) if key in source} for source in public["sources"]]
        else:
            result["assistant_published"] = False
            result["error_code"] = op.error_code
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Enable real inference (otherwise auth preparation only)")
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--scenario", choices=("all", "isolated", "threaded", "reported-history", "exact-reported"), default="all")
    parser.add_argument("--question", choices=("both", "benefits", "pension"), default="both")
    parser.add_argument("--snapshot", type=Path, default=REPORT_ROOT / "retrieval-index")
    parser.add_argument("--label", default="full-chat")
    args = parser.parse_args()
    if not args.label.replace("-", "").replace("_", "").isalnum():
        parser.error("label must be alphanumeric, hyphens or underscores")
    if not args.snapshot.resolve().is_relative_to(REPORT_ROOT.resolve()):
        parser.error("snapshot must be within this diagnostic report directory")
    if args.run and not (args.snapshot / "meta.json").is_file():
        parser.error("prepared Qdrant snapshot is missing")
    logging.disable(logging.CRITICAL)
    source = read_source()
    settings = get_settings()
    if (settings.llm_provider, settings.llm_deep_provider, settings.llm_embedding_provider) != ("ollama",) * 3:
        raise RuntimeError("This probe requires the real configured Ollama providers")
    run_id = uuid4().hex[:12]
    runtime = REPORT_ROOT / f"runtime-{args.label}-{run_id}"
    runtime.mkdir()
    sqlite_file = runtime / "chat.sqlite"
    report_path = REPORT_ROOT / f"{args.label}-{run_id}.json"
    settings.database_url = SecretStr(f"sqlite:///{sqlite_file.as_posix()}?timeout=30")
    settings.database_echo = False
    settings.qdrant_mode = QdrantMode.EMBEDDED
    settings.qdrant_path = args.snapshot.resolve()
    settings.scheduler_enabled = False
    settings.retention_enabled = False
    settings.upload_storage_root = runtime / "uploads"
    settings.rag_knowledge_root = runtime / "knowledge"
    settings.app_secret_key = SecretStr(secrets.token_urlsafe(48))
    for category in source["registry_categories"]:
        (settings.rag_knowledge_root / category).mkdir(parents=True, exist_ok=True)
    database.dispose_engine()
    engine = database.get_engine()
    if engine.url.get_backend_name() != "sqlite":
        raise AssertionError("Refusing a non-isolated runtime engine")
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        connection.commit()
    sequence = count(1)
    def assign_sequence(_mapper, _connection, message):
        if message.seq is None:
            message.seq = next(sequence)
    event.listen(ConversationMessage, "before_insert", assign_sequence)
    factory = database.get_sessionmaker()
    # MySQL locks rows, while SQLite permits only one writer. The publishing
    # session has already explicitly flushed its assistant message when the
    # independent reauthorization session marks last_seen_at dirty. Deferring
    # implicit autoflush prevents that disposable read-check session from
    # attempting a second global SQLite write. Explicit flush/commit, cookie
    # validation, queue, model, ACL recheck and publication remain unchanged.
    factory.configure(autoflush=False)
    observer = Observation()
    tested_paths = (
        "backend/app/common/answers.py", "backend/app/rag/claim_context.py",
        "backend/app/rag/grounding.py", "backend/app/agents/prompts.py",
        "backend/app/agents/knowledge_agent.py", "backend/app/rag/calculated_application.py",
    )
    report = {"transport": "FastAPI TestClient ASGI in-process; no browser or TCP",
              "run_id": run_id, "real_inference": args.run, "scenario": args.scenario,
              "repetitions": args.repetitions, "started_utc": datetime.now(timezone.utc).isoformat(),
              "source": source["metadata"], "cases": [], "validations": observer.validations,
              "code_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in tested_paths},
              "runtime_sqlite_retained": False,
              "sqlite_adaptations": {"mediumtext_compiles_as_text": True,
                                     "message_sequence_assigned_like_existing_tests": True,
                                     "autoflush": False, "explicit_flush_and_commit_unchanged": True,
                                     "reason": "SQLite global writer lock differs from MySQL row locks during nested reauthorization"}}
    try:
        from app.auth.sessions import create_session
        from app.memory.service import MemoryService, authorization_fingerprint
        with factory() as db:
            user = User(id=str(uuid4()), username=f"diagnostic-{run_id}", display_name="Usuario sintético diagnóstico",
                        auth_source="local_test", is_active=True, is_synthetic_test=True)
            db.add(user)
            for model, rows in source["rows"].items():
                for row in rows:
                    if model is AuthorizationPolicy and row["subject_kind"] == "user":
                        row = {**row, "subject_key": user.id}
                    db.add(model(**row))
            for role_id in source["roles"]:
                db.add(UserRole(user_id=user.id, role_id=role_id))
            db.flush()
            issued = create_session(db, user=user, auth_source="local_test")
            ctx = get_policy_engine().build_context(db, user=user, session_id=issued.session_id, request_id=run_id)
            categories = get_policy_engine().effective_categories(ctx)
            observed = (ctx.roles, ctx.permissions, categories, ctx.category_wildcard,
                        ctx.allowed_sources, ctx.structured_source_grants)
            if observed != source["expected"]:
                raise AssertionError("Synthetic authorization differs from the reported user's current grants")
            db.commit()
        from app.main import create_app
        from app.agents.chat_queue import get_chat_queue
        app = create_app()
        threading.setprofile(observer.profile)
        sys.setprofile(observer.profile)
        with TestClient(app, base_url="https://diagnostic.test") as client:
            client.cookies.set(settings.session_cookie_name, issued.session_token)
            # Genuine cookie resolution, CSRF dependency and HTTP error handling.
            check = client.post("/api/v1/chat/submit", json={"message": "auth check", "client_request_id": str(uuid4())})
            if check.status_code != 403:
                raise AssertionError("Missing CSRF was not rejected")
            client.headers["X-CSRF-Token"] = issued.csrf_token
            report["auth_checks"] = {"cookie_resolved": True, "missing_csrf_rejected": True,
                                     "authorization_equivalent": True, "dependency_overrides": len(app.dependency_overrides)}
            emit("prepared", **source["metadata"], real_inference=args.run)

            transport_state = {}

            def transport_metadata(stage, response):
                transport_state.update({"stage": stage, "http_status": response.status_code})
                if response.status_code >= 400:
                    try:
                        code = response.json().get("code", "")
                    except (ValueError, AttributeError):
                        code = ""
                    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", code):
                        transport_state["api_error_code"] = code

            def execute_turn(case, question, conversation_id=None):
                observer.current = case
                request_id = str(uuid4())
                payload = {"message": question, "client_request_id": request_id}
                if conversation_id:
                    payload["conversation_id"] = conversation_id
                started = time.monotonic()
                transport_state.clear()
                transport_state["stage"] = "submit"
                accepted = client.post("/api/v1/chat/submit", json=payload)
                transport_metadata("submit", accepted)
                if accepted.status_code != 202:
                    raise AssertionError(f"Submit rejected: HTTP {accepted.status_code}")
                data = accepted.json()
                statuses = [data["status"]]
                while data["status"] in ("queued", "running"):
                    if time.monotonic() - started > settings.llm_request_deadline_seconds + 90:
                        client.post(f"/api/v1/chat/requests/{request_id}/cancel")
                        raise TimeoutError("Isolated HTTP operation exceeded request deadline")
                    time.sleep(0.5)
                    response = client.get(f"/api/v1/chat/requests/{request_id}")
                    transport_metadata("poll", response)
                    if response.status_code != 200:
                        raise AssertionError(f"Poll rejected: HTTP {response.status_code}")
                    data = response.json()
                    if statuses[-1] != data["status"]:
                        statuses.append(data["status"])
                transport_state["stage"] = "verify_persistence"
                result = {"case": case, "http_submit": 202, "http_poll": 200,
                          "status": data["status"], "status_transitions": statuses,
                          "calculated_application": case in observer.calculated_cases,
                          "elapsed_seconds": round(time.monotonic() - started, 2),
                          **verify_persistence(factory, data["operation_id"], data)}
                if question == PENSION:
                    public = data.get("response") or {}
                    result["pension_acceptance"] = pension_acceptance(
                        public.get("answer", ""), public.get("sources", [])
                    )
                    result["calculated_application_direct_check"] = observer.check_calculated_application(case)
                result["passed"] = bool(
                    result["status"] == "completed" and result["assistant_published"]
                    and result.get("stored_citations_match") and result.get("grounded")
                    and result.get("source_count", 0) > 0
                    and (question != PENSION or result["pension_acceptance"]["complete_pension_answer"])
                )
                report["cases"].append(result)
                emit("case", **result)
                return data["conversation_id"]

            def turn(case, question, conversation_id=None):
                try:
                    return execute_turn(case, question, conversation_id)
                except Exception as error:
                    # Collect every scenario before returning nonzero. Never
                    # serialize exception messages that could contain content.
                    result = {"case": case, "passed": False, "error_type": type(error).__name__,
                              "calculated_application": case in observer.calculated_cases,
                              "transport_failure": dict(transport_state)}
                    report["cases"].append(result)
                    emit("case", **result)
                    return conversation_id

            def seed_reported_history(*, with_failed_benefits=False):
                with factory() as db:
                    memory = MemoryService()
                    db.info["authorization_scope"] = authorization_fingerprint(db, ctx, categories)
                    conversation = memory.create_conversation(db, ctx)
                    messages = list(source["prefix"])
                    if with_failed_benefits:
                        if source["failed_benefits_turn"] is None:
                            raise AssertionError("Reported benefits turn was not found")
                        messages.append(source["failed_benefits_turn"])
                    for message in messages:
                        memory.append_message(db, conversation, **{**message,
                            "source_ids": tuple(message["source_ids"] or ()),
                            "authorized_categories": tuple(sorted(categories))})
                    db.commit()
                    return conversation.id

            if args.run:
                scenarios = ("isolated", "threaded", "reported-history", "exact-reported") if args.scenario == "all" else (args.scenario,)
                for repetition in range(1, args.repetitions + 1):
                    for scenario in scenarios:
                        conversation_id = None
                        if scenario == "reported-history":
                            conversation_id = seed_reported_history()
                        questions = (("benefits", BENEFITS if scenario == "isolated" else REPORTED_BENEFITS),
                                     ("pension", PENSION))
                        for question_id, question in questions:
                            if args.question != "both" and args.question != question_id:
                                continue
                            if scenario == "exact-reported":
                                # Independent conversations reproduce original
                                # 4-turn benefits / 5-turn pension memory, even
                                # if the new benefits generation now succeeds.
                                conversation_id = seed_reported_history(with_failed_benefits=question_id == "pension")
                            conversation_id = turn(f"{scenario}-{repetition}-{question_id}", question,
                                                   None if scenario == "isolated" else conversation_id)
            report["workers_before_shutdown"] = len(get_chat_queue()._workers)
        report["clean_shutdown"] = True
    finally:
        threading.setprofile(None)
        sys.setprofile(None)
        from app.agents.chat_queue import stop_chat_queue
        from app.rag.vector_store import reset_vector_store
        stop_chat_queue()
        reset_vector_store()
        database.dispose_engine()
        event.remove(ConversationMessage, "before_insert", assign_sequence)
        # Runtime DB includes only synthetic sessions and temporary generation;
        # remove all SQLite/WAL bytes rather than retaining full prompt/answers.
        for name in ("chat.sqlite", "chat.sqlite-wal", "chat.sqlite-shm", "chat.sqlite-journal"):
            path = runtime / name
            if path.exists():
                path.unlink()
        for directory in sorted(runtime.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if directory.is_dir():
                directory.rmdir()
        runtime.rmdir()
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        report["passed"] = bool(report.get("clean_shutdown") and
                                (not args.run or (report["cases"] and
                                 all(case.get("passed", False) for case in report["cases"]))))
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        emit("report", path=str(report_path.relative_to(ROOT)), cases=len(report["cases"]),
             clean_shutdown=report.get("clean_shutdown", False), passed=report["passed"])
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Never print provider errors/SQL parameters/source records.
        emit("failed", error_type=type(error).__name__)
        raise SystemExit(1) from None
