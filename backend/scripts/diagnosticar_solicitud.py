# Creado por Aldo Garcia.
"""Diagnóstico local por referencia: solo logs, campos cerrados y recuentos.

No carga configuración ni abre SQL, Qdrant o inferencia. No imprime preguntas,
borradores, títulos, nombres de personas, credenciales ni excepciones libres.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import deque
from datetime import datetime
from pathlib import Path

EVENTS = frozenset({
    "rag.retrieved", "agent.regenerating", "agent.answer_validation_failed",
    "rag.document_summary_retrieved", "rag.private_summary_retrieved",
    "agent.rejected_fabricated_citations", "agent.calculated_application", "chat.queue_job_failed",
    "agent.summary_recovered", "agent.clarification_recovered",
    "llm.completion_regenerating", "llm.completion_recovered",
})
REASONS = {
    "citas fuera de la evidencia recuperada": "citations_outside_evidence",
    "identificador de fuente ambiguo entre evidencias diferentes": "source_identity_ambiguous",
    "respuesta afirmativa sin evidencia disponible": "answer_without_evidence",
    "afirmacion numerica sin respaldo": "numeric_not_grounded",
    "afirmacion numerica sin respaldo en sus fuentes citadas": "numeric_not_grounded",
    "requiere una unidad de evidencia completa; no se acredita una frase parcial": "incomplete_extract",
    "ninguna unidad extractiva completa verificable": "no_complete_extract",
    "afirmacion final sin cita": "uncited_final_claim",
    "afirmacion documental final sin cita": "uncited_final_claim",
    "aplicacion documental en orientacion general": "application_in_general_section",
    "orientacion general atribuida a fuentes documentales": "general_section_with_citations",
    "capacidad general deshabilitada": "general_knowledge_disabled",
    "secciones de procedencia mezcladas": "mixed_provenance_sections",
    "cita sin afirmacion documental": "citation_without_claim",
    "beneficio y fuente citada no corresponden": "topic_source_mismatch",
    "vigencia actual no acreditada": "current_validity_unverified",
    "elegibilidad personal no acreditada": "personal_eligibility_unverified",
    "abstencion mezclada con afirmaciones documentales": "abstention_mixed_with_claims",
    "citas inventadas en una consulta general": "general_answer_with_citations",
    "solicitud de precision contiene afirmaciones no acreditadas": "unsafe_clarification",
    "declara insuficiencia de evidencia": "evidence_insufficient",
    "abstencion documental": "evidence_insufficient",
    "resumen omite documentos recuperados": "summary_document_omitted",
    "resumen no respeta cantidad de puntos": "summary_point_count_mismatch",
    "tabla o regla incompleta, ambigua o fuera de intervalo": "rule_or_table_unresolved",
    "porcentaje no corresponde a la fila y conceptos aplicables": "wrong_row_or_concept",
    "datos declarados sin aplicacion condicional": "missing_condition",
    "evidencia contradictoria para los mismos conceptos y supuestos": "conflicting_rules",
    "respuesta documental sin fuentes citadas": "missing_citations",
    "respuesta documental sin ninguna cita": "missing_citations",
    "contrato documental JSON invalido": "documentary_contract",
    "antiguedad atribuida al caso distinta de la declarada": "case_tenure_mismatch",
    "intervalo no corresponde a la antiguedad declarada": "table_interval_mismatch",
    "conversion de antiguedad no corresponde a las entradas declaradas": "tenure_conversion_mismatch",
    "fecha de ingreso atribuida al caso distinta de la declarada": "case_entry_mismatch",
    "representacion numerica no reconocida": "numeric_representation_unknown",
    "identidad documental ambigua entre las fuentes citadas": "document_identity_ambiguous",
    "atribucion documento y pagina ambigua; cite cada localizador por separado": "document_locator_ambiguous",
    "pagina no corresponde a la metadata de sus fuentes citadas": "page_metadata_mismatch",
}
CONTRACT_CODES = frozenset({
    "json_parse", "duplicate_property", "schema_violation", "unsafe_clarification",
    "insufficient_with_claims", "empty_answer", "invalid_claim_format", "unknown_alias",
})
COUNTS = frozenset({
    "claim_index", "evidence_count", "structured_count", "cited_source_count", "invalid_source_count",
    "fetched", "returned", "application_count", "application_limitation_count", "application_conflict_count",
    "generation_attempts", "output_tokens", "output_token_limit",
    "response_chars", "thinking_chars", "invalid_count", "allowlist_size", "map_batches",
    "selected_documents", "after_dedup", "authorized_category_count",
})
# Mantener solo valores del contrato; no importar la app ni cargar Settings.
CLOSED_FIELDS = {
    "recovery_method": frozenset({"complete_extracts", "validated_sections", "safe_question"}),
    "failure_kind": frozenset({
        "cancelled", "timeout", "deadline", "http", "busy", "transport", "format", "context_limit",
        "incomplete", "empty", "reasoning", "schema", "response_size",
    }),
    "finish_reason": frozenset({"stop", "length", "not_done", "unknown"}),
    "error_code": frozenset({
        "unauthorized", "forbidden", "unsupported_file", "file_too_large", "extraction_failed",
        "ingestion_failed", "conversation_cleanup_pending", "ollama_unavailable", "chat_capacity_full",
        "embedding_dimension_mismatch", "qdrant_unavailable", "database_unavailable", "structured_query_rejected",
        "insufficient_evidence", "answer_unverified", "out_of_scope", "policy_missing", "not_found",
        "validation_error", "rate_limited", "configuration_error", "internal_error",
    }),
}
MAX_FILES, MAX_FILE_BYTES, MAX_LINE_BYTES, MAX_EVENTS = 32, 8_388_608, 131_072, 100


def _reason(value) -> str:
    if not isinstance(value, str):
        return ""
    if value in REASONS:
        return REASONS[value]
    prefix = "contrato documental JSON invalido: "
    if value.startswith(prefix) and value[len(prefix):] in CONTRACT_CODES:
        return value[len(prefix):]
    return "other_omitted" if value else ""


def _safe_event(record: dict) -> dict:
    event = {"message": record["message"]}
    try:
        timestamp = record.get("timestamp", "")
        if isinstance(timestamp, str) and len(timestamp) <= 60:
            event["timestamp"] = datetime.fromisoformat(timestamp).isoformat()
    except ValueError:
        pass
    for key in COUNTS:
        value = record.get(key)
        if type(value) is int and 0 <= value <= 1_000_000_000:
            event[key] = value
    score = record.get("best_score")
    if type(score) in (int, float) and -1 <= score <= 1 and math.isfinite(score):
        event["best_score"] = score
    for key in ("reason", "validation_detail"):
        if code := _reason(record.get(key)):
            event[key] = code
    for key, allowed in CLOSED_FIELDS.items():
        value = record.get(key)
        if isinstance(value, str) and value in allowed:
            event[key] = value
    for key in ("application_requested", "generation_completed", "truncated", "section_requested"):
        if type(record.get(key)) is bool:
            event[key] = record[key]
    missing = record.get("application_missing_fields")
    if isinstance(missing, list):
        event["application_missing_fields"] = [name for name in ("entry", "tenure") if name in missing]
    error_type = record.get("error_type")
    if isinstance(error_type, str) and error_type in {"AnswerValidationError", "InferenceFailureError"}:
        event["error_type"] = record["error_type"]
    return event


def diagnose(root: Path, reference: str) -> dict:
    if not re.fullmatch(r"[a-fA-F0-9]{64}", reference):
        raise ValueError("Use la referencia hexadecimal completa de 64 caracteres.")
    reference = reference.lower()
    logs = root.resolve() / "backend" / "logs"
    if any(path.is_symlink() or path.is_junction() for path in (logs.parent, logs)):
        raise ValueError("La carpeta de logs debe ser local y regular.")
    files = sorted((path for path in logs.glob("backend-*.log*")
                    if path.is_file() and not path.is_symlink() and not path.is_junction()),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    truncated = len(files) > MAX_FILES
    selected = files[:MAX_FILES]
    events = deque(maxlen=MAX_EVENTS)
    matches = 0
    for path in reversed(selected):
        with path.open("rb") as stream:
            size = path.stat().st_size
            offset = max(0, size - MAX_FILE_BYTES)
            stream.seek(offset)
            remaining = size - offset
            discarding = offset > 0
            truncated |= offset > 0
            while remaining > 0:
                line = stream.readline(min(MAX_LINE_BYTES + 1, remaining))
                if not line:
                    break
                remaining -= len(line)
                if discarding or len(line) > MAX_LINE_BYTES:
                    discarding = not line.endswith(b"\n")
                    truncated = True
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if (not isinstance(record, dict) or not isinstance(record.get("message"), str)
                        or record["message"] not in EVENTS):
                    continue
                if not any(record.get(key) == reference for key in ("request_id", "operation_id", "request_reference")):
                    continue
                matches += 1
                events.append(_safe_event(record))
    return {"read_only": True, "reference": reference, "found": bool(matches), "scanned_files": len(selected),
            "truncated": truncated or matches > MAX_EVENTS, "events": list(events)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--reference", required=True)
    args = parser.parse_args()
    try:
        report = diagnose(args.root, args.reference)
    except (OSError, ValueError):
        print("No se pudo leer el diagnóstico. Revise la referencia y el acceso a backend/logs.")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
