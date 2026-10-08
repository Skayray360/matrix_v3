"""Compare real builders before/after using synthetic evidence; no LLM quality claim."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

REPORT = Path(__file__).resolve().parent
ROOT = REPORT.parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["APP_ENV"] = "test"
os.environ["APP_SECRET_KEY"] = "synthetic-prompt-inspection-secret-00000000"
from app.config import Settings, get_settings

Settings.model_config["env_file"] = None
from app.agents import prompts as current
from app.memory.service import ConversationContext, ConversationTurn
from app.rag.schemas import Evidence

spec = importlib.util.spec_from_file_location("prior_prompts", REPORT / "backup/backend/app/agents/prompts.py")
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)
settings = get_settings()
evidence = tuple(Evidence(
    source_id=f"synthetic/source-{year}.txt#0", text=f"Documento sintetico {year}. Poblacion {population}; requiere autorizacion.",
    score=0.9, category="synthetic", filename=f"source-{year}.txt", section="Condiciones",
    page_or_sheet="pagina 1", document_id=f"synthetic-{year}", chunk_id=f"synthetic-{year}-0",
) for year, population in ((2020, "A"), (2024, "B")))
memory = ConversationContext(conversation_id="synthetic", turns=(
    ConversationTurn(role="user", content="Declaro que mi contratacion es eventual."),
    ConversationTurn(role="assistant", content="Una respuesta anterior no es evidencia de una prestacion."),
))
snapshots = {}
checks = []


def compare(label, function, **kwargs):
    previous = getattr(prior, function)(**kwargs)
    messages = getattr(current, function)(**kwargs)
    assert previous[1:] == messages[1:], label + ": changed user/evidence/memory/citation payload"
    assert messages[0]["content"].count(current.RESPONSE_STYLE_POLICY) == 1, label
    if function != "build_general_messages":
        assert messages[0]["content"].count(current.DOCUMENT_SCOPE_POLICY) == 1, label
    snapshots[label] = messages
    checks.append({"case": label, "result": "passed", "non_system_messages_unchanged": True,
                   "system_chars_before": len(previous[0]["content"]), "system_chars_after": len(messages[0]["content"])})


for mode in ("cited", "extractive"):
    settings.answer_evidence_mode = mode
    for general in (False, True):
        settings.answer_allow_general_knowledge = general
        for summary in (False, True):
            for documentary_only in (False, True):
                label = f"{mode}-general-{general}-summary-{summary}-documentary-{documentary_only}"
                compare(label, "build_answer_messages", question="Mis prestaciones", evidences=evidence,
                        memory=memory, document_summary=summary, documentary_only=documentary_only)
        assert current.capabilities_answer() == prior.capabilities_answer()
compare("general", "build_general_messages", question="Que es la fotosintesis", memory=memory)
compare("reduce", "build_summary_reduce_messages", question="Resume las condiciones",
        partial_summaries=("Parte sintetica [[synthetic/source-2020.txt#0]]",))
assert current.build_summary_messages("Dialogo sintetico") == prior.build_summary_messages("Dialogo sintetico")
(REPORT / "prompt-snapshots.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(REPORT / "prompt-inspection.json").write_text(json.dumps({
    "exit_code": 0, "builder_cases_passed": len(checks), "cases": checks,
    "capabilities_unchanged": True, "dialogue_summary_unchanged": True,
    "limits": "Only real prompt construction and payload preservation are checked. No inference, response quality, or pension answer is evaluated.",
}, indent=2) + "\n", encoding="utf-8")
print(f"{len(checks)} builder cases passed; evidence, memory, citations and user messages unchanged.")
