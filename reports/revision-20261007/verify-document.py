"""Lectura local del PDF autorizado; NO prueba embeddings, recuperación ni Ollama.

Verifica extracción/fragmentación/aplicación/citas con generación simulada.
La expectativa 70 procede de la lectura manual de página 9, no del código app.
No guarda texto extraído, prompts, conversaciones ni credenciales.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["APP_ENV"] = "test"
logging.disable(logging.CRITICAL)

from app.config.settings import Settings
Settings.model_config["env_file"] = None
from app.agents.knowledge_agent import KnowledgeAgent
from app.common.errors import AnswerValidationError
from app.ingestion.loaders import extract_document
from app.llm.model_policy import ModelPolicy
from app.llm.ollama_client import ChatResult
from app.rag.chunking import chunk_blocks
from app.rag.claim_context import claim_context
from app.rag.schemas import Evidence

QUESTION = (
    "Según la plática del Plan de Pensiones por Jubilación de diciembre de 2022, "
    "si ingresé en mayo de 2016 y me retiro con 7 años y 6 meses de antigüedad antes de jubilarme, "
    "¿qué porcentaje me corresponde de las aportaciones básica, básica complementaria y adicional complementaria? "
    "Indica documento y página."
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    args = parser.parse_args()

    def audit(event, _args):
        if event == "socket.connect":
            raise RuntimeError("Esta comprobación no admite conexiones de red.")

    sys.addaudithook(audit)
    document = extract_document(args.pdf.read_bytes(), filename=args.pdf.name)
    chunks = chunk_blocks(document.blocks, chunk_size_tokens=900, overlap_tokens=120)
    # La fuente y su localizador vienen del extractor real, no de metadata inventada.
    selected = [chunk for chunk in chunks if chunk.page_or_sheet == "pagina 9"]
    assert len(selected) == 1, "La página esperada no se conserva como unidad completa"
    chunk = selected[0]
    source = Evidence(
        source_id="regression-document#" + str(chunk.index), text=chunk.text, score=1.0,
        category="prestaciones", filename=args.pdf.name, section=chunk.section,
        page_or_sheet=chunk.page_or_sheet, document_id="read-only-real-document", chunk_id=str(chunk.index),
    )
    context = claim_context(QUESTION, (source,))
    assert len(context.applications) == 1, context.limitations
    application = context.applications[0]
    assert application.percent == 70
    assert set(application.rule.table_subjects) == {
        "aportacion basica", "aportacion basica complementaria", "adicional complementaria",
    }
    assert not application.rule.fixed

    class SimulatedGeneration:
        def __init__(self, percentage):
            self.percentage = percentage

        def chat(self, **kwargs):
            text = (
                "Si esos datos declarados son correctos, la Aportación Básica, la Aportación Básica Complementaria "
                f"y Adicional Complementaria se calculan al {self.percentage}%. [[E1]]\n"
                f'Documento: "{source.filename}", página 9. [[E1]]'
            )
            return ChatResult(content=text, model=kwargs["model"], latency_ms=0)

    policy = ModelPolicy()
    answer = KnowledgeAgent(llm=SimulatedGeneration(70), policy=policy).synthesize(
        question=QUESTION, evidences=(source,), model_name=policy.fast_model,
    )
    assert answer.grounding.grounded
    assert answer.cited_source_ids == (source.source_id,)
    rejected = False
    try:
        KnowledgeAgent(llm=SimulatedGeneration(100), policy=policy).synthesize(
            question=QUESTION, evidences=(source,), model_name=policy.fast_model,
        )
    except AnswerValidationError:
        rejected = True
    assert rejected, "Una cifra de otro contexto no debe aceptarse para esta fila"
    print(json.dumps({
        "document": source.filename, "page": source.page_or_sheet,
        "real_extraction_and_chunking": True, "percentages": [70, 70, 70],
        "declared_tenure": "15/2 years", "correct_answer_and_citation_accepted": True,
        "wrong_100_percent_rejected": rejected, "generation": "simulated",
        "semantic_retrieval_tested": False, "mysql_tested": False, "qdrant_opened": False,
        "ollama_tested": False, "writes_to_real_data": False,
    }, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
