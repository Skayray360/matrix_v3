# Creado por Aldo Garcia.
"""Vector store Qdrant con filtros de autorizacion en la propia consulta.

Por que Qdrant (seccion 4):

* persistencia real en disco;
* busqueda vectorial por coseno;
* **filtros de metadata evaluados dentro del motor**, lo que permite aplicar la
  ACL antes de que ningun chunk llegue al LLM;
* separacion por colecciones/namespaces (corporativo vs adjunto privado);
* operacion local y contenerizable.

Modos de operacion:

* ``embedded`` -- almacenamiento local persistente gestionado por el cliente
  oficial de Qdrant. Mismo API y mismos filtros; es el modo por defecto en
  equipos Windows sin Docker.
* ``server`` -- instancia Qdrant dedicada accesible por HTTP.

**No existe fallback silencioso.** Si el modo configurado no esta operativo,
``health()`` falla, ``/ready`` devuelve no-listo y las consultas RAG devuelven un
error controlado.
"""

from __future__ import annotations

import math
import threading
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient, models

from app.common.errors import QdrantUnavailableError
from app.common.logging import get_logger
from app.config import QdrantMode, get_settings
from app.rag.schemas import SCOPE_CONVERSATION, SCOPE_CORPORATE, Chunk, Evidence, document_source_id

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScoredPayload:
    """Resultado crudo de Qdrant antes de MMR y deduplicacion."""

    score: float
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AuthorizedDocument:
    """Identidad documental obtenida tras aplicar la ACL SQL, nunca evidencia."""

    document_id: str
    filename: str
    generation: str
    chunk_count: int
    available: bool = True


class VectorStore:
    """Envoltorio delgado sobre ``QdrantClient`` con la ACL incorporada."""

    def __init__(self, *, client: QdrantClient | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._dimension = settings.rag_embedding_dimension
        self._corporate = settings.rag_collection_corporate
        self._private = settings.rag_collection_private
        self._lock = threading.Lock()
        self._client = client or self._build_client()
        self._ensured: set[str] = set()

    def _build_client(self) -> QdrantClient:
        settings = self._settings
        try:
            if settings.qdrant_mode is QdrantMode.SERVER:
                api_key = settings.qdrant_api_key.get_secret_value() or None
                return QdrantClient(url=settings.qdrant_url, api_key=api_key, timeout=30)
            path = settings.qdrant_storage_path
            path.mkdir(parents=True, exist_ok=True)
            return QdrantClient(path=str(path))
        except Exception as exc:  # noqa: BLE001
            # En modo embedded el almacenamiento local queda bloqueado por el
            # proceso que lo abre. Es la limitacion conocida de este modo y la
            # causa mas frecuente de fallo al lanzar un script mientras el
            # backend esta arriba: conviene decirlo explicitamente en lugar de
            # devolver un error generico.
            hint = ""
            if settings.qdrant_mode is QdrantMode.EMBEDDED and "already accessed" in str(exc):
                hint = (
                    " El almacen embebido admite un unico proceso: detenga el backend "
                    "(detener.bat) antes de ejecutar scripts que accedan "
                    "directamente al indice, o use QDRANT_MODE=server."
                )
            raise QdrantUnavailableError(
                "No fue posible inicializar el almacen vectorial." + hint, detail=str(exc)
            ) from exc

    # ------------------------------------------------------------ colecciones
    def ensure_collection(self, name: str) -> None:
        """Crea la coleccion si falta, con coseno y la dimension configurada."""
        if name in self._ensured:
            return
        with self._lock:
            try:
                if not self._client.collection_exists(name):
                    self._client.create_collection(
                        collection_name=name,
                        vectors_config=models.VectorParams(size=self._dimension, distance=models.Distance.COSINE),
                    )
                    logger.info("rag.collection_created", extra={"collection": name})
                info = self._client.get_collection(name)
                vectors = info.config.params.vectors
                if getattr(vectors, "size", None) != self._dimension:
                    raise ValueError("La dimension de la coleccion no coincide; cree una coleccion nueva.")
                if self._settings.qdrant_mode is QdrantMode.SERVER:
                    for field in (
                        "scope",
                        "category",
                        "owner_user_id",
                        "conversation_id",
                        "document_id",
                        "generation",
                        "index_fingerprint",
                    ):
                        if field not in (info.payload_schema or {}):
                            self._client.create_payload_index(
                                collection_name=name,
                                field_name=field,
                                field_schema=models.PayloadSchemaType.KEYWORD,
                                wait=True,
                            )
                self._ensured.add(name)
            except Exception as exc:  # noqa: BLE001
                raise QdrantUnavailableError(
                    "No fue posible preparar la coleccion vectorial.", detail=str(exc)
                ) from exc

    def collection_for(self, scope: str) -> str:
        return self._private if scope == SCOPE_CONVERSATION else self._corporate

    def has_complete_generation(
        self,
        *,
        document_id: str,
        generation: str,
        scope: str,
        index_fingerprint: str,
        expected_chunks: int,
        document_sha256: str,
        category: str | None = None,
        owner_user_id: str | None = None,
        conversation_id: str | None = None,
        allowed_groups: tuple[str, ...] = (),
        sensitivity: str = "internal",
    ) -> bool:
        """Comprueba la generacion real antes de declarar un documento sin cambios.

        SQL no demuestra que los vectores sigan presentes tras copiar el
        proyecto o restaurar solo la base. Esta lectura no crea colecciones y
        distingue ausencia/incompletitud de un fallo de acceso a Qdrant. Solo
        lee contenido con la ACL del documento; otras generaciones no cuentan.
        """
        if not document_id or not generation or not index_fingerprint or expected_chunks < 1:
            return False
        if scope == SCOPE_CORPORATE and category:
            acl = self.build_corporate_filter(frozenset({category}))
            expected_category = category
        elif scope == SCOPE_CONVERSATION and owner_user_id and conversation_id:
            acl = self.build_private_filter(user_id=owner_user_id, conversation_id=conversation_id)
            expected_category = "__private__"
        else:
            return False
        collection = self.collection_for(scope)
        identity = models.Filter(must=[
            models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)),
            models.FieldCondition(key="generation", match=models.MatchValue(value=generation)),
        ])
        query_filter = models.Filter(must=[
            identity, acl,
            models.FieldCondition(key="index_fingerprint", match=models.MatchValue(value=index_fingerprint)),
        ])
        expected_metadata = {
            "document_id": document_id, "generation": generation, "scope": scope,
            "index_fingerprint": index_fingerprint, "document_sha256": document_sha256,
            "category": expected_category,
            "owner_user_id": owner_user_id if scope == SCOPE_CONVERSATION else None,
            "conversation_id": conversation_id if scope == SCOPE_CONVERSATION else None,
            "embedding_dimension": self._dimension, "embedding_model": self._settings.ollama_embedding_model,
            "sensitivity": sensitivity,
        }
        fields = [*expected_metadata, "chunk_id", "chunk_index", "allowed_roles", "allowed_groups",
                  "filename", "relative_path", "source_id", "text"]
        seen: set[int] = set()
        offset: Any | None = None
        try:
            if not self._client.collection_exists(collection):
                self._ensured.discard(collection)
                return False
            vectors = self._client.get_collection(collection).config.params.vectors
            if getattr(vectors, "size", None) != self._dimension:
                raise QdrantUnavailableError("La dimension del indice no coincide con la configuracion.")
            if getattr(vectors, "distance", None) != models.Distance.COSINE:
                raise QdrantUnavailableError("La metrica del indice no coincide con la configuracion.")
            # El conteo sin contenido detecta puntos sobrantes de esa misma
            # identidad, incluidos los que tengan una ACL/huella inconsistente.
            count = self._client.count(collection_name=collection, count_filter=identity, exact=True).count
            if count != expected_chunks:
                return False
            while len(seen) < expected_chunks:
                points, next_offset = self._client.scroll(
                    collection_name=collection, scroll_filter=query_filter,
                    limit=min(128, expected_chunks - len(seen)), offset=offset,
                    with_payload=models.PayloadSelectorInclude(include=fields), with_vectors=True,
                )
                for point in points:
                    payload = point.payload or {}
                    index = payload.get("chunk_index")
                    if (type(index) is not int or not 0 <= index < expected_chunks or index in seen
                            or any(payload.get(key) != value for key, value in expected_metadata.items())
                            or payload.get("chunk_id") != str(point.id)
                            or payload.get("allowed_roles") != []
                            or not isinstance(payload.get("allowed_groups"), list)
                            or not all(isinstance(group, str) for group in payload["allowed_groups"])
                            or sorted(payload["allowed_groups"]) != sorted(allowed_groups)):
                        return False
                    vector = point.vector
                    if (not isinstance(vector, list) or len(vector) != self._dimension
                            or not all(type(value) in (int, float) and math.isfinite(value) for value in vector)
                            or not any(vector)):
                        return False
                    text = payload.get("text")
                    filename = payload.get("filename")
                    if not isinstance(text, str) or not text.strip() or not isinstance(filename, str) or not filename:
                        return False
                    if payload.get("source_id") != document_source_id(
                        category=expected_category, filename=filename, chunk_index=index,
                        scope=scope, document_id=document_id, relative_path=str(payload.get("relative_path") or ""),
                    ):
                        return False
                    seen.add(index)
                if next_offset is None:
                    return len(seen) == expected_chunks
                if not points or next_offset == offset:
                    return False
                offset = next_offset
        except QdrantUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - un fallo de lectura no significa indice vacio
            raise QdrantUnavailableError(
                "No se pudo verificar la integridad de la generacion vectorial.", detail=type(exc).__name__,
            ) from exc
        return len(seen) == expected_chunks

    # ----------------------------------------------------------------- upsert
    def upsert_chunks(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        """Indexa chunks. Verifica la dimension de cada vector antes de escribir."""
        if not chunks:
            return 0
        if len(chunks) != len(vectors):
            raise QdrantUnavailableError(detail="chunks y vectors tienen distinta longitud")

        by_collection: dict[str, list[models.PointStruct]] = {}
        for chunk, vector in zip(chunks, vectors, strict=True):
            if len(vector) != self._dimension:
                raise QdrantUnavailableError(
                    detail=f"vector de dimension {len(vector)}, esperada {self._dimension}"
                )
            collection = self.collection_for(chunk.metadata.scope)
            by_collection.setdefault(collection, []).append(
                models.PointStruct(
                    id=chunk.metadata.chunk_id,
                    vector=vector,
                    payload=chunk.metadata.to_payload(chunk.text),
                )
            )

        total = 0
        for collection, points in by_collection.items():
            self.ensure_collection(collection)
            try:
                self._client.upsert(collection_name=collection, points=points, wait=True)
            except Exception as exc:  # noqa: BLE001
                raise QdrantUnavailableError(
                    "Fallo la escritura en el almacen vectorial.", detail=str(exc)
                ) from exc
            total += len(points)
        return total

    # ----------------------------------------------------------------- delete
    def delete_generation(self, document_id: str, *, generation: str, scope: str) -> None:
        """Compensa solo la generacion candidata; preserva todas las anteriores."""
        if not document_id or not generation or scope not in {SCOPE_CORPORATE, SCOPE_CONVERSATION}:
            raise ValueError("La compensacion requiere documento, generacion y scope validos.")
        collection = self.collection_for(scope)
        self.ensure_collection(collection)
        try:
            self._client.delete(
                collection_name=collection, wait=True,
                points_selector=models.FilterSelector(filter=models.Filter(must=[
                    models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)),
                    models.FieldCondition(key="generation", match=models.MatchValue(value=generation)),
                    models.FieldCondition(key="scope", match=models.MatchValue(value=scope)),
                ])),
            )
        except Exception as exc:  # noqa: BLE001
            raise QdrantUnavailableError("Fallo la limpieza de una generacion no publicada.") from exc

    def delete_document(self, document_id: str, *, scope: str = SCOPE_CORPORATE) -> None:
        """Elimina todos los chunks de un documento (reindexado y borrado)."""
        collection = self.collection_for(scope)
        self.ensure_collection(collection)
        try:
            self._client.delete(
                collection_name=collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="document_id", match=models.MatchValue(value=document_id)
                            )
                        ]
                    )
                ),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise QdrantUnavailableError(
                "Fallo el borrado de vectores.", detail=str(exc)
            ) from exc

    def cleanup_generations(self) -> int:
        from sqlalchemy import select

        from app.database.engine import session_scope
        from app.database.models import Document

        cleaned = 0
        with session_scope() as db:
            documents = (
                db.execute(select(Document).where(Document.index_cleanup_pending.is_(True)).with_for_update())
                .scalars()
                .all()
            )
            for document in documents:
                collection = self.collection_for(document.scope)
                try:
                    # Nada obsoleto puede borrarse de una coleccion inexistente.
                    # No crearla ni confundir esto con una generacion integra:
                    # la ingesta comprobara los vectores antes de omitirla.
                    if self._client.collection_exists(collection):
                        active = ([models.FieldCondition(
                            key="generation", match=models.MatchValue(value=document.active_generation),
                        )] if document.active_generation else None)
                        self._client.delete(
                            collection_name=collection, wait=True,
                            points_selector=models.FilterSelector(filter=models.Filter(
                                must=[
                                    models.FieldCondition(
                                        key="document_id", match=models.MatchValue(value=document.id),
                                    ),
                                    models.FieldCondition(key="scope", match=models.MatchValue(value=document.scope)),
                                ],
                                must_not=active,
                            )),
                        )
                    else:
                        self._ensured.discard(collection)
                except Exception as exc:  # noqa: BLE001 - conservar el pendiente si no se pudo comprobar/borrar
                    raise QdrantUnavailableError(
                        "No se pudo completar la limpieza de generaciones vectoriales.", detail=type(exc).__name__,
                    ) from exc
                document.index_cleanup_pending = False
                cleaned += 1
        return cleaned

    def delete_conversation(self, conversation_id: str) -> None:
        """Al borrar una conversacion se borran sus vectores temporales."""
        self.ensure_collection(self._private)
        try:
            self._client.delete(
                collection_name=self._private,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="conversation_id",
                                match=models.MatchValue(value=conversation_id),
                            )
                        ]
                    )
                ),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise QdrantUnavailableError(detail=str(exc)) from exc

    # ------------------------------------------------------------------ ACL
    @staticmethod
    def build_corporate_filter(
        allowed_categories: frozenset[str] | set[str],
    ) -> models.Filter:
        """Filtro de autorizacion del corpus corporativo.

        Se construye SIEMPRE con una lista **enumerada** de categorias. Incluso el
        rol administrativo con wildcard llega aqui con su lista ya resuelta por el
        motor de politicas: nunca se consulta sin filtro (requisito 6.1).
        """
        categories = sorted(allowed_categories)
        if not categories:
            # Un usuario sin ninguna categoria autorizada no debe poder recuperar
            # nada. Se usa una condicion imposible en lugar de omitir el filtro.
            return models.Filter(
                must=[
                    models.FieldCondition(key="scope", match=models.MatchValue(value=SCOPE_CORPORATE)),
                    models.FieldCondition(
                        key="category", match=models.MatchValue(value="__none__")
                    ),
                ]
            )
        return models.Filter(
            must=[
                models.FieldCondition(key="scope", match=models.MatchValue(value=SCOPE_CORPORATE)),
                models.FieldCondition(key="category", match=models.MatchAny(any=categories)),
            ]
        )

    @staticmethod
    def build_private_filter(*, user_id: str, conversation_id: str) -> models.Filter:
        """Adjuntos privados: aislados por usuario **y** conversacion."""
        return models.Filter(
            must=[
                models.FieldCondition(key="scope", match=models.MatchValue(value=SCOPE_CONVERSATION)),
                models.FieldCondition(key="owner_user_id", match=models.MatchValue(value=user_id)),
                models.FieldCondition(
                    key="conversation_id", match=models.MatchValue(value=conversation_id)
                ),
            ]
        )

    # -------------------------------------------------------------- busqueda
    def search(
        self,
        *,
        collection: str,
        query_vector: list[float],
        query_filter: models.Filter,
        limit: int,
        score_threshold: float | None = None,
    ) -> list[ScoredPayload]:
        """Consulta vectorial con filtro de metadata aplicado en el motor."""
        self.ensure_collection(collection)
        from app.rag.index_manifest import active_filter

        query_filter = active_filter(query_filter)
        try:
            response = self._client.query_points(
                collection_name=collection,
                query=query_vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                with_vectors=True,
                score_threshold=score_threshold,
            )
        except Exception as exc:  # noqa: BLE001
            raise QdrantUnavailableError("Fallo la busqueda vectorial.", detail=str(exc)) from exc

        results: list[ScoredPayload] = []
        for point in response.points:
            payload = dict(point.payload or {})
            if point.vector is not None and isinstance(point.vector, list):
                payload["_vector"] = point.vector
            results.append(ScoredPayload(score=float(point.score), payload=payload))
        from app.rag.index_manifest import visible_candidates

        return visible_candidates(results)

    def list_private_chunks(
        self,
        *,
        user_id: str,
        conversation_id: str,
        limit: int,
        index_fingerprint: str | None = None,
    ) -> tuple[list[ScoredPayload], bool]:
        """Lista chunks de adjuntos con ACL aplicada dentro de Qdrant.

        Un resumen de archivo no es una busqueda semantica: frases como
        ``resume esto`` no comparten necesariamente vocabulario con el adjunto.
        Por eso se hace ``scroll`` por metadata, manteniendo como condiciones
        obligatorias el dueno y la conversacion. Se pagina hasta ``limit`` y se
        devuelve una marca explicita si queda material fuera del limite.
        """
        if limit < 1:
            return [], False
        self.ensure_collection(self._private)
        query_filter = self.build_private_filter(user_id=user_id, conversation_id=conversation_id)
        from app.rag.index_manifest import active_filter, indexing_fingerprint

        if not isinstance(query_filter.must, list):
            raise QdrantUnavailableError("Filtro de autorizacion no valido.")
        query_filter.must.append(
            models.FieldCondition(
                key="index_fingerprint", match=models.MatchValue(value=index_fingerprint or indexing_fingerprint())
            )
        )
        query_filter = active_filter(query_filter)
        collected: list[ScoredPayload] = []
        offset: Any | None = None
        target = limit + 1
        try:
            while len(collected) < target:
                page_size = min(128, target - len(collected))
                points, next_offset = self._client.scroll(
                    collection_name=self._private,
                    scroll_filter=query_filter,
                    limit=page_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                collected.extend(ScoredPayload(score=1.0, payload=dict(point.payload or {})) for point in points)
                if next_offset is None:
                    break
                offset = next_offset
        except Exception as exc:  # noqa: BLE001
            raise QdrantUnavailableError("Fallo la recuperacion de adjuntos privados.", detail=str(exc)) from exc

        from app.rag.index_manifest import visible_candidates

        collected = visible_candidates(collected)
        collected.sort(
            key=lambda item: (
                str(item.payload.get("filename", "")).casefold(),
                str(item.payload.get("document_id", "")),
                int(item.payload.get("chunk_index", 0)),
            )
        )
        return collected[:limit], len(collected) > limit

    def list_authorized_documents(
        self,
        *,
        scope: str,
        index_fingerprint: str,
        authorized_categories: frozenset[str] = frozenset(),
        user_id: str = "",
        conversation_id: str = "",
        include_unavailable: bool = False,
    ) -> list[AuthorizedDocument]:
        """Resuelve identidades documentales dentro del alcance permitido.

        La consulta no lee texto, rutas de almacenamiento ni otros propietarios.
        Un nombre completo puede identificar el documento incluso cuando una
        pregunta como ``explicame este archivo`` tiene similitud baja.
        ``include_unavailable`` permite distinguir un adjunto pendiente de uno
        inexistente; esos metadatos no habilitan recuperacion de contenido.
        """
        from sqlalchemy import select

        from app.database.models import Document
        from app.rag.index_manifest import session_scope

        query = select(
            Document.id, Document.filename, Document.active_generation, Document.chunk_count,
            Document.status, Document.index_fingerprint,
        ).where(
            Document.scope == scope,
            Document.deleted_at.is_(None),
        )
        if not include_unavailable:
            query = query.where(
                Document.status == "indexed", Document.active_generation.is_not(None),
                Document.index_fingerprint == index_fingerprint,
            )
        if scope == SCOPE_CONVERSATION and user_id and conversation_id:
            query = query.where(Document.owner_user_id == user_id, Document.conversation_id == conversation_id)
        elif scope == SCOPE_CORPORATE and authorized_categories:
            query = query.where(Document.category.in_(authorized_categories))
        else:
            return []
        with session_scope() as db:
            rows = db.execute(query.order_by(Document.filename, Document.id)).all()
        return [AuthorizedDocument(
            row.id, row.filename, row.active_generation or "", row.chunk_count,
            row.status == "indexed" and bool(row.active_generation) and row.index_fingerprint == index_fingerprint,
        ) for row in rows]

    def list_document_chunks(
        self,
        *,
        documents: list[AuthorizedDocument],
        scope: str,
        limit: int,
        index_fingerprint: str,
        authorized_categories: frozenset[str] = frozenset(),
        user_id: str = "",
        conversation_id: str = "",
    ) -> tuple[list[ScoredPayload], bool]:
        """Lee documentos elegidos con ACL, huella y generacion dentro de Qdrant.

        Se conserva el orden documental. Cuando el limite es menor que el
        documento, la seleccion cubre inicio, centro y final por ``chunk_index``
        en vez de depender del orden aleatorio de UUID del scroll. La marca de
        truncamiento acompana tanto muestreo como unidades activas ausentes.
        """
        if not documents or limit < 1:
            return [], bool(documents)
        if scope == SCOPE_CONVERSATION and user_id and conversation_id:
            query_filter = self.build_private_filter(user_id=user_id, conversation_id=conversation_id)
        elif scope == SCOPE_CORPORATE and authorized_categories:
            query_filter = self.build_corporate_filter(authorized_categories)
        else:
            return [], False
        from app.rag.index_manifest import active_filter, visible_candidates

        if not isinstance(query_filter.must, list):
            raise QdrantUnavailableError("Filtro de autorizacion no valido.")
        query_filter.must.extend([
            models.FieldCondition(key="index_fingerprint", match=models.MatchValue(value=index_fingerprint)),
            models.FieldCondition(
                key="document_id", match=models.MatchAny(any=[doc.document_id for doc in documents]),
            ),
        ])
        query_filter = active_filter(query_filter)
        collection = self.collection_for(scope)
        self.ensure_collection(collection)
        collected: list[ScoredPayload] = []
        truncated = False
        remaining = limit
        capacities = [max(1, document.chunk_count or limit) for document in documents]
        for position, document in enumerate(documents):
            if remaining < 1:
                truncated = True
                break
            # Distribuir el presupuesto segun el tamano publicado; si todos
            # caben, una diferencia de tamanos no trunca al primer documento.
            quota = min(capacities[position], max(1, remaining * capacities[position] // sum(capacities[position:])))
            conditions: list[Any] = [
                query_filter,
                models.FieldCondition(key="document_id", match=models.MatchValue(value=document.document_id)),
                models.FieldCondition(key="generation", match=models.MatchValue(value=document.generation)),
            ]
            if document.chunk_count > quota:
                indices = (
                    [0] if quota == 1 else
                    [index * (document.chunk_count - 1) // (quota - 1) for index in range(quota)]
                )
                conditions.append(models.FieldCondition(key="chunk_index", match=models.MatchAny(any=indices)))
                truncated = True
            document_filter = models.Filter(must=conditions)
            page_items: list[ScoredPayload] = []
            offset: Any | None = None
            try:
                while len(page_items) < quota + 1:
                    points, next_offset = self._client.scroll(
                        collection_name=collection,
                        scroll_filter=document_filter,
                        limit=min(128, quota + 1 - len(page_items)),
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                    page_items.extend(
                        ScoredPayload(score=1.0, payload=dict(point.payload or {})) for point in points
                    )
                    if next_offset is None:
                        break
                    offset = next_offset
            except Exception as exc:  # noqa: BLE001
                raise QdrantUnavailableError("Fallo la recuperacion documental autorizada.", detail=str(exc)) from exc
            page_items.sort(key=lambda item: int(item.payload.get("chunk_index", 0)))
            visible = visible_candidates(page_items[:quota])
            truncated = truncated or len(page_items) > quota or len(visible) < min(document.chunk_count, quota)
            collected.extend(visible)
            remaining -= len(visible)
        return collected, truncated

    # ------------------------------------------------------------- utilidades
    def count(self, collection: str) -> int:
        self.ensure_collection(collection)
        try:
            return int(self._client.count(collection_name=collection, exact=True).count)
        except Exception as exc:  # noqa: BLE001
            raise QdrantUnavailableError(detail=str(exc)) from exc

    def health(self) -> tuple[bool, str]:
        """Estado real del almacen. Lo consume ``/ready`` y el diagnostico."""
        try:
            self._client.get_collections()
            mode = self._settings.qdrant_mode
            location = (
                str(self._settings.qdrant_storage_path)
                if mode is QdrantMode.EMBEDDED
                else self._settings.qdrant_url
            )
            return True, f"{mode}:{location}"
        except Exception as exc:  # noqa: BLE001
            return False, type(exc).__name__

    def close(self) -> None:
        # Cierre best-effort: si el cliente ya esta cerrado no hay nada que hacer
        # y el apagado del proceso no debe fallar por eso.
        with suppress(Exception):
            self._client.close()


_store: VectorStore | None = None
_store_lock = threading.Lock()


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = VectorStore()
    return _store


def reset_vector_store() -> None:
    """Cierra el store (necesario en modo embedded: bloquea el directorio)."""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None


def payload_to_evidence(payload: dict[str, Any], score: float) -> Evidence:
    """Convierte un payload de Qdrant en evidencia citable."""
    category = str(payload.get("category", ""))
    filename = str(payload.get("filename", ""))
    chunk_index = int(payload.get("chunk_index", 0))
    scope = str(payload.get("scope", SCOPE_CORPORATE))
    return Evidence(
        source_id=str(payload.get("source_id") or document_source_id(
            category=category, filename=filename, chunk_index=chunk_index, scope=scope,
            relative_path=str(payload.get("relative_path") or ""),
            document_id=str(payload.get("document_id") or ""),
        )),
        text=str(payload.get("text", "")),
        score=score,
        category=category,
        filename=filename,
        section=str(payload.get("section", "")),
        page_or_sheet=str(payload.get("page_or_sheet", "")),
        document_id=str(payload.get("document_id", "")),
        chunk_id=str(payload.get("chunk_id", "")),
        scope=scope,
    )
