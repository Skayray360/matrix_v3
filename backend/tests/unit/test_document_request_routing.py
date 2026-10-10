# Creado por Aldo Garcia.
"""Solicitudes de documentos y seguimientos, sin servicios ni modelos reales."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.agents.contextual_query import contextualize_question
from app.agents.orchestrator import Orchestrator, _summary_source_scope
from app.common.errors import ForbiddenError
from app.llm.model_policy import Intent, ModelPolicy
from app.memory.service import ConversationContext, ConversationTurn
from app.rag.retriever import RetrievalResult
from app.rag.schemas import Evidence

pytestmark = pytest.mark.unit

TITLE = "Guia_configuracion_SSO_DIRECTORIO_LAB"
REFERENCE = f"Explícame sobre este documento {TITLE}."


@pytest.mark.parametrize("question", [
    REFERENCE,
    "explicame sobre este documento Guia_configuracion_Kerberos_AD_PENOLEST_MX.",
    "Explica el documento Procedimiento de altas.pdf",
    "¿Puedes explicarme este archivo?",
    "¿De qué trata este documento?",
    "Descríbeme el contenido del archivo adjunto",
    "Hola, explícame el documento Procedimiento de altas",
])
def test_document_request_routing_overview_uses_complete_document_intent(question):
    assert ModelPolicy().classify_intent(question) is Intent.DOCUMENT_SUMMARY


@pytest.mark.parametrize("question, expected", [
    ("Explícame la sección de requisitos del documento adjunto", Intent.DOCUMENTAL),
    ("Explica el párrafo de renovación del archivo", Intent.DOCUMENTAL),
    ("Explícame cómo reiniciar el servicio según este documento", Intent.DOCUMENTAL),
    ("Explica qué es la fotosíntesis", Intent.GENERAL),
    ("Explica nuestras prestaciones", Intent.DOCUMENTAL),
])
def test_document_request_routing_focused_requests_keep_their_intent(question, expected):
    assert ModelPolicy().classify_intent(question) is expected


@pytest.mark.parametrize("question", [
    "dame un resumen", "Hazme un resumen breve", "Resúmelo", "Dame los puntos clave",
    "Explícamelo", "Por favor, sintetízalo",
])
def test_document_request_routing_generic_followup_retains_authorized_source(question):
    policy = ModelPolicy()
    reference = policy.contextual_reference(question, prior_questions=(REFERENCE,))
    assert reference == REFERENCE
    query = contextualize_question(question, reference)
    assert TITLE in query and question in query
    assert policy.classify_intent(question, previous_question=reference) is Intent.DOCUMENT_SUMMARY


@pytest.mark.parametrize("question", [
    "Explícame sobre este documento Procedimiento_ALTAS_NUEVO.",
    "Resume ese archivo Instructivo_Bajas_Operativas.docx",
    "Dame un resumen sobre la fotosíntesis",
    "Cambiando de tema, dame un resumen",
])
def test_document_request_routing_new_named_source_does_not_inherit_old_file(question):
    assert ModelPolicy().contextual_reference(question, prior_questions=(REFERENCE,)) == ""


def test_document_request_routing_summary_chain_preserves_title_without_inventing_history():
    policy = ModelPolicy()
    reference = policy.contextual_reference(
        "Dame los puntos clave", prior_questions=(REFERENCE, "Dame un resumen"),
    )
    assert TITLE in reference
    assert policy.contextual_reference("Dame un resumen", prior_questions=()) == ""
    assert policy.contextual_reference(
        "Dame un resumen", prior_questions=(REFERENCE, "Qué es la fotosíntesis"),
    ) == ""


def _source() -> Evidence:
    return Evidence(
        source_id="private/synthetic#0", text="Guía de configuración para acceso de prueba.",
        score=1.0, category="privados", filename=TITLE + ".docx", section="Introducción",
        page_or_sheet="", document_id="own-document", chunk_id="chunk-0", scope="conversation",
    )


def _gather(question, *, private_result=None):
    calls = []

    class Retriever:
        def retrieve_attachment_summary(self, **kwargs):
            calls.append(("private", kwargs))
            return private_result if private_result is not None else RetrievalResult(evidences=(_source(),))

        def retrieve(self, **kwargs):
            calls.append(("corporate", kwargs))
            return RetrievalResult()

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._retriever = Retriever()
    result, structured, used = orchestrator._gather_evidence(
        ctx=SimpleNamespace(user_id="owner"), conversation=SimpleNamespace(id="own-conversation"),
        question=question, intent=Intent.DOCUMENT_SUMMARY,
        authorized_categories=frozenset({"authorized"}), comparative=False,
    )
    assert not structured
    return result, used, calls


def test_document_request_routing_private_summary_receives_current_named_target():
    _, _, calls = _gather(REFERENCE)
    assert len(calls) == 1
    assert calls[0][1]["question"] == REFERENCE
    assert calls[0][1]["ctx"].user_id == "owner"
    assert calls[0][1]["conversation_id"] == "own-conversation"


def test_document_request_routing_corporate_summary_requests_complete_document_scope():
    _, _, calls = _gather("Resume el reglamento corporativo de seguridad")
    assert len(calls) == 1
    assert calls[0][0] == "corporate"
    assert calls[0][1]["summary"] is True
    assert calls[0][1]["include_private"] is False
    assert calls[0][1]["authorized_categories"] == frozenset({"authorized"})


@pytest.mark.parametrize("question", [
    "Explica el documento Manual de acceso.pdf",
    "Resume el archivo Política de contraseñas.docx",
    "Explica el documento Reglamento de usuarios",
])
def test_document_request_routing_policy_word_in_title_does_not_override_attachment_lookup(question):
    assert _summary_source_scope(question) == "auto"


@pytest.mark.parametrize("title", ["Guia_ACCESO.docx", "Guía de Altas y Bajas.docx"])
def test_document_request_routing_mixed_summary_resolves_names_inside_each_scope(title):
    _, _, calls = _gather(f"Resume el archivo adjunto {title} y la política corporativa de vacaciones")
    assert [kind for kind, _ in calls] == ["private", "corporate"]
    private_question, corporate_question = (kwargs["question"] for _, kwargs in calls)
    assert title in private_question and "política corporativa" not in private_question
    assert "política corporativa de vacaciones" in corporate_question and title not in corporate_question


def test_document_request_routing_ambiguous_private_scope_never_falls_back_to_corporate():
    private = SimpleNamespace(
        has_evidence=False, evidences=(), clarification="¿Qué archivo desea resumir?",
    )
    result, _, calls = _gather("dame un resumen", private_result=private)
    assert result is private
    assert [kind for kind, _ in calls] == ["private"]


def _orchestrator_turn(monkeypatch, *, turns=(), retrieval=None, permissions_change=False, attachment_count=0):
    events, received = [], {}
    ctx = SimpleNamespace(user_id="owner", request_id="request", role_set_hash="roles")
    conversation = SimpleNamespace(id="own-conversation", user_id="owner")
    db = SimpleNamespace(
        info={}, commit=lambda: events.append("commit"),
        execute=MagicMock(return_value=SimpleNamespace(scalar_one=lambda: attachment_count)),
    )
    monkeypatch.setattr("app.agents.orchestrator.authorization_fingerprint", lambda *_: "authorized-scope")
    authorized = frozenset({"authorized"})

    def permissions(_ctx):
        events.append("authorize")
        return frozenset() if permissions_change and events.count("authorize") > 1 else authorized

    def context(*_args, **kwargs):
        events.append("authorized-memory")
        assert kwargs["authorized_categories"] == authorized
        return ConversationContext(conversation_id=conversation.id, turns=turns)

    def attachment(**kwargs):
        events.append("attachment")
        received["retrieval"] = kwargs
        return retrieval

    def synthesize(**kwargs):
        events.append("synthesis")
        received["synthesis"] = kwargs
        return SimpleNamespace()

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._model_policy = ModelPolicy()
    orchestrator._policies = SimpleNamespace(effective_categories=permissions)
    orchestrator._memory = SimpleNamespace(
        build_context=context, rename_if_untitled=MagicMock(),
        append_message=MagicMock(return_value=SimpleNamespace(id="saved-message")),
    )
    orchestrator._retriever = SimpleNamespace(
        retrieve_attachment_summary=attachment,
        retrieve=MagicMock(side_effect=AssertionError("Unexpected corporate retrieval")),
    )
    orchestrator._agent = SimpleNamespace(
        synthesize=synthesize, ask_clarification=MagicMock(side_effect=AssertionError("Unexpected LLM call")),
    )
    orchestrator._audit = SimpleNamespace(record=MagicMock())
    orchestrator._finish_answer = lambda *_args, **kwargs: received.update(finished=kwargs) or "complete"
    return orchestrator, db, ctx, conversation, events, received


@pytest.mark.parametrize("message, prior", [
    (REFERENCE, ()),
    ("dame un resumen", ()),
    ("dame un resumen", (ConversationTurn(role="user", content=REFERENCE),)),
])
def test_document_request_routing_handle_chat_authorizes_then_synthesizes_all_chunks(monkeypatch, message, prior):
    chunks = tuple(replace(_source(), source_id=f"private/synthetic#{i}", chunk_id=f"chunk-{i}") for i in range(85))
    retrieval = RetrievalResult(evidences=chunks, best_score=1.0, used_private_scope=True)
    orchestrator, db, ctx, conversation, events, received = _orchestrator_turn(
        monkeypatch, turns=prior, retrieval=retrieval,
    )
    assert orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message=message) == "complete"
    assert events[:3] == ["authorize", "authorized-memory", "authorize"]
    assert events.index("attachment") > events.index("commit")
    assert received["synthesis"]["intent"] is Intent.DOCUMENT_SUMMARY
    assert received["synthesis"]["evidences"] == chunks
    assert received["synthesis"]["evidence_truncated"] is False
    assert received["retrieval"]["ctx"] is ctx
    assert received["retrieval"]["conversation_id"] == conversation.id
    if prior:
        assert TITLE in received["retrieval"]["question"]
    assert received["finished"]["routing"].intent is Intent.DOCUMENT_SUMMARY


def test_document_request_routing_permission_change_stops_before_attachment_retrieval(monkeypatch):
    orchestrator, db, ctx, conversation, events, _ = _orchestrator_turn(monkeypatch, permissions_change=True)
    with pytest.raises(ForbiddenError):
        orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message=REFERENCE)
    assert events == ["authorize", "authorized-memory", "authorize"]
    orchestrator._memory.append_message.assert_not_called()


def test_document_request_routing_ambiguous_selection_is_saved_without_inference(monkeypatch):
    clarification = "Hay varios adjuntos disponibles. Indique cuál desea resumir."
    retrieval = RetrievalResult(clarification=clarification)
    orchestrator, db, ctx, conversation, events, _ = _orchestrator_turn(monkeypatch, retrieval=retrieval)
    result = orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message="dame un resumen")
    assert result.answer == clarification and result.answer_basis == "insufficient"
    assert result.model == "" and result.grounded is False and result.public_sources() == []
    assert "synthesis" not in events
    stored = orchestrator._memory.append_message.call_args.kwargs
    assert stored["content"] == clarification and stored["answer_basis"] == "insufficient"
    assert orchestrator._audit.record.call_args.args[1].event_type == "chat.insufficient_evidence"


@pytest.mark.parametrize("message", ["dame un resumen", "Hazme un resumen breve", "Resume"])
def test_document_request_routing_generic_followup_with_multiple_attachments_clarifies_before_old_title(
    monkeypatch, message,
):
    old = RetrievalResult(evidences=(_source(),), best_score=1.0, used_private_scope=True)
    orchestrator, db, ctx, conversation, events, _ = _orchestrator_turn(
        monkeypatch, turns=(ConversationTurn(role="user", content=REFERENCE),),
        retrieval=old, attachment_count=2,
    )
    result = orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message=message)
    assert result.answer_basis == "insufficient" and not result.grounded
    assert result.model == "" and not result.public_sources()
    assert "attachment" not in events and "synthesis" not in events
    stored_user = orchestrator._memory.append_message.call_args_list[0].kwargs
    assert stored_user["context_query"] == message
    statement = db.execute.call_args.args[0]
    assert {"owner", "own-conversation", "conversation"}.issubset(set(statement.compile().params.values()))
    assert "documents.deleted_at IS NULL" in str(statement)


@pytest.mark.parametrize("message", ["Resúmelo", "Dame un resumen de ese documento"])
def test_document_request_routing_explicit_anaphora_keeps_selected_source_among_multiple_files(monkeypatch, message):
    old = RetrievalResult(evidences=(_source(),), best_score=1.0, used_private_scope=True)
    orchestrator, db, ctx, conversation, _, received = _orchestrator_turn(
        monkeypatch, turns=(ConversationTurn(role="user", content=REFERENCE),),
        retrieval=old, attachment_count=2,
    )
    assert orchestrator.handle_chat(db, ctx=ctx, conversation=conversation, message=message) == "complete"
    assert TITLE in received["retrieval"]["question"]
    db.execute.assert_not_called()
