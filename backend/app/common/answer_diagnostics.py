# Creado por Aldo Garcia.
"""Traza opt-in de UN turno autorizado; nunca activa recuperacion o inferencia.

Control local: knowledge-base/state/diagnostics/next-answer.json. Requiere usuario, conversacion,
SHA256 de pregunta exacta y caducidad <=15 minutos. Un solo uso por proceso.
No registra memoria, SQL, adjuntos privados ni sistemas completos. Solo permite
extractos de source_ids corporativos enumerados expresamente en el control.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from functools import wraps

from app.common.logging import get_logger
from app.common.redaction import redact_value
from app.config.settings import PROJECT_ROOT

logger = get_logger(__name__)
CONTROL = PROJECT_ROOT / "knowledge-base" / "state" / "diagnostics" / "next-answer.json"
_trace: contextvars.ContextVar[dict | None] = contextvars.ContextVar("answer_diagnostic", default=None)
_used: set[str] = set()
_lock = threading.Lock()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:  # noqa: BLE001 - observar nunca cambia la respuesta
            _trace.set(None)
            return None
    return wrapped


def isolated_turn(function):
    """Reinicia siempre el ContextVar, incluso ante errores o cambio de usuario."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _trace.set(None)
        try:
            return function(*args, **kwargs)
        finally:
            _trace.reset(token)
    return wrapped


@_optional
def begin(*, ctx, conversation_id: str, question: str, retrieval_question: str, intent: str) -> None:
    """Llamar solo DESPUES de autorizar memoria y revalidar categorias."""
    try:
        if not CONTROL.is_file():
            return
        with CONTROL.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return
        control = json.loads(raw)
        remaining = datetime.fromisoformat(control["expires_at"]) - datetime.now(UTC)
        if not 0 < remaining.total_seconds() <= 900:
            return
        if (control["user_id"] != ctx.user_id or control["conversation_id"] != conversation_id
                or control["question_sha256"] != digest(question)):
            return
        allowed = control.get("source_ids", [])
        if (not isinstance(allowed, list) or len(allowed) > 8
                or any(not isinstance(sid, str) or len(sid) > 240 or sid.startswith("__private__/")
                       for sid in allowed)):
            return
        key = hashlib.sha256(raw).hexdigest()
        with _lock:
            if key in _used or len(_used) >= 16:
                return
            _used.add(key)
        _trace.set({"key": key, "expires_at": control["expires_at"], "allowed": frozenset(allowed),
                    "events": 0, "corporate": {}, "request_id": ctx.request_id})
        emit("classified", {"intent": intent, "query": _fingerprint(retrieval_question),
                            "question_sha256": digest(question)})
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Diagnosticar nunca puede impedir una respuesta ni cambiar su ruta.
        return


def _bounded(value: str, limit: int) -> dict:
    return {"sha256": digest(value), "chars": len(value), "excerpt": value[:limit],
            "truncated": len(value) > limit}


def _fingerprint(value: str) -> dict:
    """La consulta reconstruida y el borrador pueden incorporar memoria privada."""
    return {"sha256": digest(value), "chars": len(value)}


@_optional
def emit(stage: str, fields: dict) -> None:
    state = _trace.get()
    if state is None or state["events"] >= 16:
        return
    if datetime.fromisoformat(state["expires_at"]) <= datetime.now(UTC):
        return
    state["events"] += 1
    fields = dict(fields)
    if isinstance(fields.get("retry_note"), str):
        fields["retry_note"] = _fingerprint(fields["retry_note"])
    logger.info("diagnostic.answer_trace", extra={"request_id": state["request_id"],
        "diagnostic_stage": stage, "trace_key": state["key"], "diagnostic": redact_value(fields)})


def evidence_metadata(evidences) -> list[dict]:
    allowed = (_trace.get() or {}).get("allowed", frozenset())
    metadata = []
    for evidence in evidences[:8]:
        item = {"source_sha256": digest(evidence.source_id), "score": evidence.score,
                "scope": evidence.scope, **_fingerprint(evidence.text)}
        # El identificador privado incluye el nombre visible del archivo. Tampoco
        # se vuelcan localizadores de fuentes corporativas no autorizadas para
        # esta traza, aunque el usuario pueda consultarlas en la aplicacion.
        if evidence.scope == "corporate" and evidence.source_id in allowed:
            item.update(source_id=evidence.source_id, page=evidence.page_or_sheet,
                        chunk_id=evidence.chunk_id)
        metadata.append(item)
    return metadata


@_optional
def retrieved(evidences) -> None:
    state = _trace.get()
    if state is None:
        return
    state["corporate"] = {e.source_id: e for e in evidences
                          if e.scope == "corporate" and e.source_id in state["allowed"]}
    emit("retrieved", {"count": len(evidences), "sources": evidence_metadata(evidences),
                       "metadata_truncated": len(evidences) > 8})


@_optional
def packed(evidences, *, limited: bool, memory_present: bool, retry: bool) -> None:
    if _trace.get() is not None:
        emit("packed", {"sources": evidence_metadata(evidences), "count": len(evidences),
                        "context_limited": limited, "memory_present": memory_present, "retry": retry})


@_optional
def payload_ready(payload: dict) -> None:
    """Observa el payload Ollama final, despues del prefijo/opciones/reintentos.

    No copia el mensaje user: aisla solo bloques de evidencia de fuentes
    corporativas ya autorizadas y enumeradas, conservando el texto saneado
    realmente enviado. La huella completa detecta recortes/cambios.
    """
    state = _trace.get()
    if state is None:
        return
    from app.agents.prompts import DOCUMENT_SCOPE_POLICY, RESPONSE_STYLE_POLICY
    from app.security.prompt_guard import sanitize_untrusted_text

    messages = payload["messages"]
    systems = [m["content"] for m in messages if m["role"] == "system"]
    sources = []
    for message in messages:
        for block in re.findall(r"<<<EVIDENCIA_DOCUMENTAL>>>\n(.*?)<<</EVIDENCIA_DOCUMENTAL>>>",
                                message["content"], re.DOTALL):
            for unit in re.split(r"(?=\[cita: \[\[)", block):
                match = re.search(r"\[source_id: ([^\]\n]+)\]", unit)
                if match is None:
                    continue
                sid = match.group(1)
                # Nunca aceptar solo el source_id impreso: comprobar tambien
                # que lo devolvio el recuperador autorizado en ESTE turno.
                if sid not in state["corporate"] or sid not in state["allowed"]:
                    continue
                # Un documento/adjunto podria imitar el encabezado de otro.
                # Registrar solo el texto exacto de la fuente autorizada, nunca
                # la cola arbitraria encontrada por el parser diagnostico.
                expected = sanitize_untrusted_text(state["corporate"][sid].text).text
                _, separator, body = unit.partition("\n")
                if not separator or body.rstrip() != expected.rstrip():
                    continue
                if len(sources) < 8:
                    sources.append({"source_id": sid, "sent_text": _bounded(body.rstrip(), 1800)})
    emit("ollama_payload", {"model": payload["model"], "options": payload["options"],
        "think": payload.get("think"), "keep_alive": payload.get("keep_alive"),
        "stream": payload.get("stream"), "format_present": "format" in payload,
        "messages": [{"role": m["role"], "sha256": digest(m["content"]), "chars": len(m["content"])}
                     for m in messages],
        "style_present": any(RESPONSE_STYLE_POLICY in s for s in systems),
        "scope_present": any(DOCUMENT_SCOPE_POLICY in s for s in systems),
        "style_sha256": digest(RESPONSE_STYLE_POLICY), "scope_sha256": digest(DOCUMENT_SCOPE_POLICY),
        "authorized_excerpts": sources})


@_optional
def validated(answer: str, report, *, retry: bool) -> None:
    if _trace.get() is not None:
        # Ni el borrador, ni las citas inventadas por el modelo, ni el detalle
        # libre del validador estan cubiertos por la allowlist de documentos.
        # Mantener solo estados, recuentos y huellas; nunca serializar asdict.
        summary = {key: bool(getattr(report, key, False)) for key in (
            "grounded", "declares_insufficiency", "citations_valid",
            "factual_verified", "extractive_verified",
        )}
        summary.update(
            cited_source_count=len(report.cited_source_ids),
            invalid_source_count=len(report.invalid_source_ids),
            reason=_fingerprint(report.reason),
            validation_detail=_fingerprint(report.validation_detail),
            claim_index=report.claim_index,
        )
        emit("validated", {"retry": retry, "answer": _fingerprint(answer), "report": summary})
