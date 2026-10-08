# Creado por Aldo Garcia.
"""Recuperacion autorizada: filtro ACL -> fetch_k -> dedup -> MMR -> top_k.

Orden obligatorio (seccion 9):

1. resolver identidad y permisos;
2. determinar el alcance/categorias autorizadas;
3. crear la consulta semantica;
4. recuperar ``fetch_k`` en Qdrant **usando el filtro de permisos**;
5. eliminar duplicados o chunks casi equivalentes;
6. aplicar MMR;
7. devolver como maximo ``top_k``;
8. preservar diversidad por documento/categoria en preguntas comparativas;
9. entregar scores y source IDs al Agente de Conocimiento.

Nunca se consulta primero y se filtra despues: los chunks restringidos no entran
siquiera en ``fetch_k`` (requisito 6.2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from qdrant_client import models

from app.authorization.context import UserContext
from app.common.errors import QdrantUnavailableError
from app.common.logging import get_logger
from app.config import get_settings
from app.llm.ollama_client import get_ollama_client
from app.llm.provider import InferenceClient
from app.rag.document_selection import explicit_document_candidates
from app.rag.embedding_prompts import format_query
from app.rag.index_manifest import indexing_fingerprint
from app.rag.schemas import SCOPE_CONVERSATION, Evidence
from app.rag.vector_store import ScoredPayload, VectorStore, get_vector_store, payload_to_evidence

logger = get_logger(__name__)

#: Margen sobre ``RAG_MIN_SIMILARITY`` por debajo del cual se considera que la
#: recuperacion es debil y conviene escalar a la ruta profunda.
LOW_CONFIDENCE_MARGIN = 0.05


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Evidencia autorizada y trazas del proceso de recuperacion."""

    evidences: tuple[Evidence, ...] = field(default_factory=tuple)
    authorized_categories: tuple[str, ...] = field(default_factory=tuple)
    fetched: int = 0
    after_dedup: int = 0
    best_score: float = 0.0
    used_private_scope: bool = False
    truncated: bool = False

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidences)

    @property
    def low_confidence(self) -> bool:
        """Senal para el router: la ruta rapida no basta si la evidencia es floja.

        El margen se calibro contra el corpus sintetico con ``embeddinggemma``:
        una coincidencia buena de seccion queda entre 0.40 y 0.72, mientras que
        una coincidencia debil ronda el umbral minimo (0.35). Por eso basta un
        margen pequeno sobre ``RAG_MIN_SIMILARITY``; un margen grande enviaba
        toda consulta a la ruta profunda y anulaba de hecho la politica de dos
        modelos.
        """
        settings = get_settings()
        return self.best_score < (settings.rag_min_similarity + LOW_CONFIDENCE_MARGIN)

    def source_ids(self) -> tuple[str, ...]:
        return tuple(e.source_id for e in self.evidences)

    def distinct_categories(self) -> tuple[str, ...]:
        return tuple(sorted({e.category for e in self.evidences}))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Similitud coseno entre dos vectores densos."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _normalize_for_dedup(text: str) -> str:
    return " ".join(text.lower().split())


def deduplicate(candidates: list[ScoredPayload], *, preserve_sources: bool = False) -> list[ScoredPayload]:
    """Elimina duplicados exactos y chunks casi equivalentes.

    "Casi equivalente" = mismo documento y texto normalizado identico, o un texto
    contenido integramente en otro ya seleccionado. Es habitual cuando el solape
    entre chunks reproduce un procedimiento corto dos veces.

    En comparativas, textos iguales de documentos/categorias diferentes son
    evidencia de coincidencia: se conservan sus procedencias para poder citarlas.
    """
    seen_exact: set[tuple[tuple[str, ...], str]] = set()
    kept: list[ScoredPayload] = []

    def source_key(candidate: ScoredPayload) -> tuple[str, ...]:
        if not preserve_sources:
            return ()
        payload = candidate.payload
        return (
            str(payload.get("scope", "")),
            str(payload.get("category", "")),
            str(payload.get("document_id") or payload.get("filename", "")),
            str(payload.get("page_or_sheet", "")),
            str(payload.get("section", "")),
        )

    for candidate in sorted(candidates, key=lambda c: c.score, reverse=True):
        text = _normalize_for_dedup(str(candidate.payload.get("text", "")))
        key = source_key(candidate)
        if not text or (key, text) in seen_exact:
            continue
        if any(
            source_key(k) == key and text in _normalize_for_dedup(str(k.payload.get("text", "")))
            for k in kept
        ):
            continue
        seen_exact.add((key, text))
        kept.append(candidate)
    return kept


def maximal_marginal_relevance(
    query_vector: list[float],
    candidates: list[ScoredPayload],
    *,
    top_k: int,
    lambda_mult: float,
    preferred: list[ScoredPayload] | None = None,
) -> list[ScoredPayload]:
    """MMR clasico: relevancia menos redundancia.

    ``lambda_mult`` alto favorece relevancia; bajo favorece diversidad. El valor
    por defecto (0.65) prioriza relevancia manteniendo variedad suficiente para
    preguntas comparativas.
    """
    if not candidates:
        return []
    remaining = list(candidates)
    selected: list[ScoredPayload] = []
    # Una referencia explicita debe sobrevivir a la penalizacion por similitud
    # entre ediciones. Aceptar solo candidatos de la recuperacion autorizada.
    for candidate in preferred or ():
        if candidate in remaining and len(selected) < top_k:
            selected.append(candidate)
            remaining.remove(candidate)

    while remaining and len(selected) < top_k:
        best_item: ScoredPayload | None = None
        best_value = -math.inf
        for candidate in remaining:
            vector = candidate.payload.get("_vector")
            relevance = candidate.score
            if selected and isinstance(vector, list):
                redundancy = max(
                    cosine_similarity(vector, s.payload.get("_vector") or []) for s in selected
                )
            else:
                redundancy = 0.0
            value = lambda_mult * relevance - (1.0 - lambda_mult) * redundancy
            if value > best_value:
                best_value = value
                best_item = candidate
        if best_item is None:
            break
        selected.append(best_item)
        remaining.remove(best_item)

    return selected


def enforce_category_diversity(
    candidates: list[ScoredPayload], *, top_k: int, comparative: bool
) -> list[ScoredPayload]:
    """En preguntas comparativas garantiza representacion de varias categorias.

    Sin esto, una categoria con muchos documentos puede monopolizar ``top_k`` y la
    comparacion queda coja aunque la evidencia de la otra categoria exista y este
    autorizada.
    """
    if not comparative or len(candidates) <= top_k:
        return candidates[:top_k]

    by_category: dict[str, list[ScoredPayload]] = {}
    for candidate in candidates:
        by_category.setdefault(str(candidate.payload.get("category", "")), []).append(candidate)

    result: list[ScoredPayload] = []
    # Primera pasada: un chunk por categoria, en orden de mejor score.
    for group in sorted(by_category.values(), key=lambda g: -g[0].score):
        result.append(group[0])
        if len(result) >= top_k:
            return result
    # Segunda pasada: se completa con el resto por score.
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
        if len(result) >= top_k:
            break
    return result[:top_k]


class Retriever:
    """Tool RAG. Solo devuelve evidencia que el contexto ya tiene autorizada."""

    def __init__(
        self,
        *,
        store: VectorStore | None = None,
        llm: InferenceClient | None = None,
    ) -> None:
        self._store = store or get_vector_store()
        self._llm = llm or get_ollama_client()
        self._settings = get_settings()

    def retrieve(
        self,
        *,
        ctx: UserContext,
        question: str,
        authorized_categories: frozenset[str],
        conversation_id: str | None = None,
        include_private: bool = True,
        comparative: bool = False,
    ) -> RetrievalResult:
        """Recupera evidencia para ``question`` dentro del alcance autorizado."""
        settings = self._settings
        # El prefijo de tarea es obligatorio para embeddinggemma: sin el, la
        # consulta y los documentos no son comparables (ver embedding_prompts).
        query_vector = self._llm.embed_one(format_query(question))

        candidates: list[ScoredPayload] = []
        fingerprint = indexing_fingerprint(self._llm)

        # --- corpus corporativo -------------------------------------------
        corporate_filter = VectorStore.build_corporate_filter(authorized_categories)
        if not isinstance(corporate_filter.must, list):
            raise QdrantUnavailableError("Filtro de autorizacion no valido.")
        corporate_filter.must.append(
            models.FieldCondition(key="index_fingerprint", match=models.MatchValue(value=fingerprint))
        )
        candidates.extend(
            self._store.search(
                collection=self._store.collection_for("corporate"),
                query_vector=query_vector,
                query_filter=corporate_filter,
                limit=settings.rag_fetch_k,
                score_threshold=settings.rag_min_similarity,
            )
        )

        # --- adjuntos privados de la conversacion --------------------------
        used_private = False
        if include_private and conversation_id:
            private_filter = VectorStore.build_private_filter(user_id=ctx.user_id, conversation_id=conversation_id)
            if not isinstance(private_filter.must, list):
                raise QdrantUnavailableError("Filtro de autorizacion no valido.")
            private_filter.must.append(
                models.FieldCondition(key="index_fingerprint", match=models.MatchValue(value=fingerprint))
            )
            private_hits = self._store.search(
                collection=self._store.collection_for(SCOPE_CONVERSATION),
                query_vector=query_vector,
                query_filter=private_filter,
                limit=settings.rag_fetch_k,
                score_threshold=settings.rag_min_similarity,
            )
            used_private = bool(private_hits)
            candidates.extend(private_hits)

        fetched = len(candidates)
        # Incluso una pregunta no comparativa puede nombrar una version o
        # documento concreto. Igual texto con distinta procedencia/condicion no
        # autoriza a descartar esa cita en favor de un documento historico.
        deduped = deduplicate(candidates, preserve_sources=True)
        preferred = explicit_document_candidates(question, deduped)
        ranked = maximal_marginal_relevance(
            query_vector,
            deduped,
            top_k=len(deduped) if comparative else settings.rag_top_k,
            lambda_mult=settings.rag_mmr_lambda,
            preferred=preferred,
        )
        final = enforce_category_diversity(ranked, top_k=settings.rag_top_k, comparative=comparative)
        if preferred:
            # Comparar archivos nombrados prevalece sobre diversidad de
            # categorias arbitrarias; los demas cupos mantienen esa diversidad.
            final = (preferred + [item for item in final if item not in preferred])[:settings.rag_top_k]

        evidences = tuple(payload_to_evidence(item.payload, item.score) for item in final)
        best_score = max((e.score for e in evidences), default=0.0)

        logger.info(
            "rag.retrieved",
            extra={
                "user_opaque_id": ctx.user_id,
                "authorized_category_count": len(authorized_categories),
                "fetched": fetched,
                "after_dedup": len(deduped),
                "returned": len(evidences),
                "explicit_document_matches": len(preferred),
                "best_score": round(best_score, 4),
            },
        )
        return RetrievalResult(
            evidences=evidences,
            authorized_categories=tuple(sorted(authorized_categories)),
            fetched=fetched,
            after_dedup=len(deduped),
            best_score=best_score,
            used_private_scope=used_private,
        )

    def retrieve_attachment_summary(
        self,
        *,
        ctx: UserContext,
        conversation_id: str,
    ) -> RetrievalResult:
        """Recupera todos los chunks privados autorizados para resumir.

        No genera embedding y no aplica umbral semantico. La seguridad no se
        relaja: ``list_private_chunks`` aplica ``scope + user + conversation``
        como filtro MUST dentro de Qdrant. Si el limite operativo se alcanza, la
        marca ``truncated`` obliga al sintetizador a informarlo al usuario.
        """
        settings = self._settings
        candidates, truncated = self._store.list_private_chunks(
            user_id=ctx.user_id,
            conversation_id=conversation_id,
            limit=settings.rag_summary_scan_max_chunks,
            index_fingerprint=indexing_fingerprint(self._llm),
        )
        deduped = deduplicate(candidates, preserve_sources=True)
        evidences = tuple(payload_to_evidence(item.payload, item.score) for item in deduped)
        logger.info(
            "rag.private_summary_retrieved",
            extra={
                "user_opaque_id": ctx.user_id,
                "conversation_id": conversation_id,
                "fetched": len(candidates),
                "after_dedup": len(deduped),
                "truncated": truncated,
            },
        )
        return RetrievalResult(
            evidences=evidences,
            fetched=len(candidates),
            after_dedup=len(deduped),
            best_score=1.0 if evidences else 0.0,
            used_private_scope=bool(evidences),
            truncated=truncated,
        )
