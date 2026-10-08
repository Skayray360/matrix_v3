"""Reproduce documentary synthesis locally without DB, vector index or chat writes."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.dont_write_bytecode = True
os.environ["APP_ENV"] = "test"
logging.disable(logging.CRITICAL)

from app.config.settings import Settings
Settings.model_config["env_file"] = None
from app.agents.knowledge_agent import KnowledgeAgent
from app.common import answer_diagnostics
from app.ingestion.loaders import extract_document
from app.llm.model_policy import ModelPolicy
from app.llm.provider import ModelClient
from app.rag.chunking import chunk_blocks
from app.rag.schemas import Evidence

QUESTION = (
    "Según la plática del Plan de Pensiones por Jubilación de diciembre de 2022, "
    "si ingresé en mayo de 2016 y me retiro con 7 años y 6 meses de antigüedad antes de jubilarme, "
    "¿qué porcentaje me corresponde de las aportaciones básica, básica complementaria y adicional complementaria? "
    "Indica documento y página."
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", nargs="+", type=int, default=[9])
    args = parser.parse_args()
    # No operational settings or persistence. Only the installed local model may be called.
    def audit(event, values):
        if event == "socket.connect" and values[1] != ("127.0.0.1", 11434):
            raise RuntimeError("Only local Ollama is permitted")
    sys.addaudithook(audit)
    pdf = next((ROOT / "data/prestaciones").glob("PLATICA*DICIEMBRE 2022.pdf"))
    document = extract_document(pdf.read_bytes(), filename=pdf.name)
    chunks = chunk_blocks(document.blocks, chunk_size_tokens=900, overlap_tokens=120)
    sources = tuple(Evidence(
        source_id=f"local-document#{c.index}", text=c.text, score=1.0,
        category="prestaciones", filename=pdf.name, section=c.section,
        page_or_sheet=c.page_or_sheet, document_id="local-read-only-document", chunk_id=str(c.index),
    ) for c in chunks if c.page_or_sheet in {f"pagina {p}" for p in args.pages})
    def validated(answer, report, *, retry):
        cursor = 0
        failed = ""
        for i, match in enumerate(re.finditer(r"(?:\[\[[^\]]{1,240}\]\][ \t]*)+", answer), 1):
            if i == report.claim_index:
                failed = answer[cursor:match.start()].strip()
            cursor = match.end()
        print(json.dumps({"stage": "validation", "retry": retry, "grounded": report.grounded,
            "reason": report.reason, "detail": report.validation_detail,
            "claim_index": report.claim_index, "failed_claim": failed,
            "tail": answer[cursor:].strip() if not report.grounded and not report.claim_index else "",
        }, ensure_ascii=False), flush=True)
    answer_diagnostics.validated = validated
    policy = ModelPolicy()
    client = ModelClient()
    print(json.dumps({"model": policy.fast_model, "pages": args.pages,
        "source_count": len(sources), "retrieval_tested": False}), flush=True)
    try:
        answer = KnowledgeAgent(llm=client, policy=policy).synthesize(
            question=QUESTION, evidences=sources, model_name=policy.fast_model,
        )
        print(json.dumps({"status": "accepted", "answer": answer.answer,
            "regenerated": answer.regenerated}, ensure_ascii=False), flush=True)
    except Exception as error:
        print(json.dumps({"status": "failed", "type": type(error).__name__}, ensure_ascii=False), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
