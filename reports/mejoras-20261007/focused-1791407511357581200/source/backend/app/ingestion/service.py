# Creado por Aldo Garcia.
"""Servicio de ingesta: extraer -> chunkear -> embeber -> upsert -> manifest.

Pipeline reproducible (seccion 9):
``discover -> authorize policy metadata -> extract -> normalize -> structural
split -> chunk -> embed -> upsert -> manifest -> verify``

El *manifest* es la propia base interna: ``documents`` guarda el estado actual
(SHA-256, numero de chunks, estado) y ``document_versions`` el historico. Se
eligio la base de datos en lugar de un JSON en disco porque la reconciliacion
necesita consultas por SHA y por ruta, y porque un archivo suelto se desincroniza
en cuanto dos procesos escriben a la vez.

Idempotencia: se comparan SHA-256 y huella de configuracion. Se escribe una nueva
generacion antes de activarla en SQL; la limpieza posterior es reintentable.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.authorization.categories import CategoryPolicy, get_registry, validate_category_name
from app.common.errors import ConversationCleanupPendingError, IngestionFailedError, ValidationFailedError
from app.common.ids import new_id, sha256_bytes, utcnow_naive
from app.common.logging import get_logger
from app.config import get_settings
from app.database.models import Document, DocumentAccessPolicy, DocumentVersion, User
from app.ingestion.isolated_extraction import extract_document
from app.ingestion.loaders import mime_for_extension
from app.llm.ollama_client import get_ollama_client
from app.llm.provider import InferenceClient
from app.memory.service import MemoryService
from app.rag.chunking import ChunkDraft, chunk_blocks
from app.rag.embedding_prompts import format_document
from app.rag.index_manifest import PIPELINE_VERSION, indexing_fingerprint
from app.rag.schemas import SCOPE_CONVERSATION, SCOPE_CORPORATE, Chunk, ChunkMetadata
from app.rag.vector_store import VectorStore, get_vector_store
from app.security.upload_guard import assert_safe_relative_path, read_bounded

logger = get_logger(__name__)

#: Version del pipeline. Cambiarla obliga a reindexar (se compara en el manifest).
#: v2 introduce los prefijos de tarea de embeddinggemma en los documentos.
#: v3 hace de los encabezados una frontera estructural del chunking.
#: v6 rechaza el truncamiento silencioso al calcular embeddings.
#: v7 corrige el presupuesto del solape y la procedencia por pagina/hoja.
INGESTION_VERSION = str(PIPELINE_VERSION)

#: Lote de textos por llamada a /api/embed. Equilibra memoria y numero de viajes.
EMBED_BATCH_SIZE = 16


class SourceChangedError(IngestionFailedError):
    """El productor aun cambia el archivo; se reintentara en otra pasada."""


def file_signature(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """Resultado de ingerir un documento."""

    document_id: str
    chunk_count: int
    status: str
    skipped: bool = False
    warnings: tuple[str, ...] = ()


class IngestionService:
    """Ingesta de documentos corporativos y de adjuntos privados."""

    def __init__(
        self,
        *,
        store: VectorStore | None = None,
        llm: InferenceClient | None = None,
    ) -> None:
        self._store = store or get_vector_store()
        self._llm = llm or get_ollama_client()
        self._settings = get_settings()

    # ------------------------------------------------------------------------
    # Corporativo
    # ------------------------------------------------------------------------
    def ingest_corporate_file(
        self,
        db: Session,
        *,
        absolute_path: Path,
        relative_path: str,
        category: str,
        force: bool = False,
        legacy_relative_paths: tuple[str, ...] = (),
    ) -> IngestionOutcome:
        """Ingiere un archivo del knowledge root."""
        safe_relative = assert_safe_relative_path(relative_path)
        safe_category = validate_category_name(category)
        if any(
            candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction())
            for candidate in (absolute_path, *absolute_path.parents)
        ):
            raise IngestionFailedError(
                "No se ingieren documentos corporativos mediante enlaces de archivos o carpetas."
            )
        initial_signature = file_signature(absolute_path)
        with absolute_path.open("rb") as stream:
            data = read_bounded(stream, max_bytes=self._settings.upload_max_bytes)
        if file_signature(absolute_path) != initial_signature or len(data) != initial_signature[2]:
            raise SourceChangedError("El archivo cambio durante su lectura. Se reintentara su sincronizacion.")
        digest = sha256_bytes(data)

        document = db.execute(
            select(Document).where(
                Document.scope == SCOPE_CORPORATE,
                Document.relative_path == safe_relative,
            )
        ).scalar_one_or_none()
        if document is None:
            # Una carpeta que antes se recorria desde RAG_KNOWLEDGE_ROOT puede
            # convertirse en un alias de data. La ruta fisica identifica el
            # mismo documento: no se crea otra version solo por ese cambio.
            candidates = db.execute(
                select(Document).where(
                    Document.scope == SCOPE_CORPORATE,
                    Document.storage_path == str(absolute_path),
                )
            ).scalars().all()
            if not candidates and legacy_relative_paths:
                legacy_candidates = db.execute(
                    select(Document).where(
                        Document.scope == SCOPE_CORPORATE,
                        Document.relative_path.in_(legacy_relative_paths),
                    )
                ).scalars().all()
                candidates = [
                    candidate for candidate in legacy_candidates
                    if candidate.storage_path and any(
                        str(candidate.storage_path).replace("\\", "/").casefold().endswith(
                            "/data/" + old_relative.casefold()
                        ) for old_relative in legacy_relative_paths
                    )
                ]
            if len(candidates) > 1:
                raise IngestionFailedError("Hay manifests corporativos duplicados para una misma ruta.")
            if candidates:
                document = candidates[0]
                document.relative_path = safe_relative

        if (
            document is not None
            and not force
            and document.sha256 == digest
            and document.status == "indexed"
            and document.deleted_at is None
            and document.ingestion_version == INGESTION_VERSION
            and document.index_fingerprint == indexing_fingerprint(self._llm)
            and document.category == safe_category
        ):
            # Conservar el ID, los chunks y la version al mover todo el proyecto.
            # Solo se actualiza la ubicacion operativa de sus mismos bytes.
            document.storage_path = str(absolute_path)
            document.filename = absolute_path.name
            return IngestionOutcome(
                document_id=document.id, chunk_count=document.chunk_count, status="unchanged", skipped=True
            )

        now = utcnow_naive()
        if document is None:
            document = Document(
                id=new_id(),
                scope=SCOPE_CORPORATE,
                category=safe_category,
                filename=absolute_path.name,
                relative_path=safe_relative,
                storage_path=str(absolute_path),
                mime_type=mime_for_extension(absolute_path.suffix),
                size_bytes=len(data),
                sha256=digest,
                status="pending",
                ingestion_version=INGESTION_VERSION,
                created_at=now,
                updated_at=now,
            )
            db.add(document)
            db.flush()
        else:
            document.category = safe_category
            document.filename = absolute_path.name
            document.storage_path = str(absolute_path)
            document.mime_type = mime_for_extension(absolute_path.suffix)
            document.size_bytes = len(data)
            document.sha256 = digest
            document.status = "pending"
            document.deleted_at = None
            document.ingestion_version = INGESTION_VERSION
            document.updated_at = now
            db.flush()

        self._ensure_access_policy(db, document, category=safe_category)
        outcome = self._index_document(db, document=document, data=data, category=safe_category)
        # La extraccion/embedding puede tardar. Solo publicar los bytes leidos
        # si el productor no reemplazo ni siguio escribiendo la fuente.
        if file_signature(absolute_path) != initial_signature:
            raise SourceChangedError("El archivo cambio durante su indexacion. Se reintentara su sincronizacion.")
        return outcome

    # ------------------------------------------------------------------------
    # Adjunto privado de conversacion
    # ------------------------------------------------------------------------
    def ingest_conversation_attachment(
        self,
        db: Session,
        *,
        data: bytes,
        display_name: str,
        internal_filename: str,
        mime_type: str,
        owner_user_id: str,
        conversation_id: str,
    ) -> IngestionOutcome:
        """Ingiere un adjunto en el namespace privado ``user + conversation``.

        Este documento **no** pertenece a ninguna categoria corporativa y no puede
        recuperarse desde otra conversacion ni por otro usuario: su ACL es la
        pareja (owner_user_id, conversation_id), aplicada como filtro en Qdrant.
        """
        storage_path, portable_path = self._attachment_storage_path(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            internal_filename=internal_filename,
        )
        conversation = MemoryService._lock_conversation(db, conversation_id)
        if conversation.user_id != owner_user_id:
            raise IngestionFailedError("El adjunto no pertenece a esta conversacion.")
        self._reserve_upload_quota(db, owner_user_id=owner_user_id, incoming_bytes=len(data))
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_bytes(data)

        now = utcnow_naive()
        document = Document(
            id=new_id(),
            scope=SCOPE_CONVERSATION,
            category=None,
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            filename=display_name,
            relative_path=None,
            storage_path=portable_path,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256=sha256_bytes(data),
            status="pending",
            ingestion_version=INGESTION_VERSION,
            created_at=now,
            updated_at=now,
        )
        db.add(document)
        db.flush()
        return self._index_document(db, document=document, data=data, category=None)

    def _attachment_storage_path(
        self, *, owner_user_id: str, conversation_id: str, internal_filename: str
    ) -> tuple[Path, str]:
        """Reconstruye la ruta dentro del namespace privado del servidor actual.

        Los nombres internos son UUID generados por el servidor. Ningun segmento
        puede convertir el namespace en una ruta absoluta ni redirigirlo mediante
        un enlace simbolico hacia archivos de otra conversacion.
        """
        parts = (owner_user_id, conversation_id, internal_filename)
        if any(
            not part
            or part in (".", "..")
            or any(character in part for character in '/\\:\x00')
            or PureWindowsPath(part).name != part
            for part in parts
        ):
            raise IngestionFailedError("El adjunto tiene una ruta interna no valida.")
        root = self._settings.upload_storage_path.resolve()
        namespace = root / owner_user_id / conversation_id
        resolved = (namespace / internal_filename).resolve()
        if not resolved.is_relative_to(namespace):
            raise IngestionFailedError("El adjunto esta fuera de su directorio privado.")
        return resolved, Path(*parts).as_posix()

    def _reserve_upload_quota(self, db: Session, *, owner_user_id: str, incoming_bytes: int) -> None:
        """Serializa la cuota por usuario y cuenta tambien bytes pendientes/huerfanos.

        Un rollback puede dejar un archivo sin manifest. Ese archivo no libera
        cuota por desaparecer SQL: permanece contabilizado hasta la purga de su
        namespace. Los archivos conocidos se cuentan una sola vez.
        """
        user = db.get(User, owner_user_id, with_for_update=True, populate_existing=True)
        if user is None:
            raise IngestionFailedError("El propietario del adjunto no existe.")
        rows = db.execute(select(Document.storage_path, Document.size_bytes).where(
            Document.owner_user_id == owner_user_id,
            Document.scope == SCOPE_CONVERSATION,
            (Document.deleted_at.is_(None)) | (Document.storage_path.is_not(None)),
        ).with_for_update()).all()
        stored_bytes = sum(row.size_bytes for row in rows)
        stored_count = len(rows)
        known_paths = {str(row.storage_path).replace("\\", "/") for row in rows if row.storage_path}
        root = self._settings.upload_storage_path.resolve()
        user_root = root / owner_user_id
        if user_root.exists():
            if user_root.is_symlink() or user_root.resolve() != user_root:
                raise ValidationFailedError("Su almacenamiento privado requiere revision.")
            for namespace in user_root.iterdir():
                if namespace.is_symlink() or not namespace.is_dir() or namespace.resolve().parent != user_root:
                    raise ValidationFailedError("Su almacenamiento privado requiere revision.")
                for path in namespace.iterdir():
                    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(namespace):
                        raise ValidationFailedError("Su almacenamiento privado requiere revision.")
                    portable = path.relative_to(root).as_posix()
                    # Manifests antiguos pueden guardar la ruta absoluta actual.
                    if portable not in known_paths and path.as_posix() not in known_paths:
                        stored_bytes += path.stat().st_size
                        stored_count += 1
                    if stored_count >= self._settings.upload_max_documents_per_user:
                        raise ValidationFailedError("La carga excede su cuota de documentos.")
                    if stored_bytes + incoming_bytes > self._settings.upload_max_total_bytes:
                        raise ValidationFailedError("La carga excede su cuota de almacenamiento.")
        if stored_count + 1 > self._settings.upload_max_documents_per_user:
            raise ValidationFailedError("La carga excede su cuota de documentos.")
        if stored_bytes + incoming_bytes > self._settings.upload_max_total_bytes:
            raise ValidationFailedError("La carga excede su cuota de almacenamiento.")

    def reindex_conversation_attachment(
        self, db: Session, *, document: Document, force: bool = False
    ) -> IngestionOutcome:
        """Migra el adjunto existente sin duplicarlo ni cambiar su propietario.

        Una ruta absoluta anterior (Windows o POSIX) aporta unicamente el nombre
        interno: los bytes deben estar en UPLOAD_STORAGE_ROOT/user/conversation.
        Su SHA debe coincidir con la carga original. La nueva generacion se activa
        con el commit del llamador, despues de completar todas las escrituras.
        """
        if (
            document.scope != SCOPE_CONVERSATION
            or document.deleted_at is not None
            or not document.owner_user_id
            or not document.conversation_id
            or not document.storage_path
        ):
            raise IngestionFailedError("El adjunto no tiene un manifest privado valido.")
        conversation = MemoryService._lock_conversation(db, document.conversation_id)
        if conversation.user_id != document.owner_user_id:
            raise IngestionFailedError("El adjunto no pertenece a esta conversacion.")
        storage_path, portable_path = self._attachment_storage_path(
            owner_user_id=document.owner_user_id,
            conversation_id=document.conversation_id,
            internal_filename=PureWindowsPath(document.storage_path).name,
        )
        with storage_path.open("rb") as stream:
            data = read_bounded(stream, max_bytes=self._settings.upload_max_bytes)
        if sha256_bytes(data) != document.sha256:
            raise IngestionFailedError("El SHA-256 del adjunto no coincide con la carga original.")

        document.storage_path = portable_path
        if (
            not force
            and document.status == "indexed"
            and document.ingestion_version == INGESTION_VERSION
            and document.index_fingerprint == indexing_fingerprint(self._llm)
        ):
            return IngestionOutcome(
                document_id=document.id, chunk_count=document.chunk_count, status="unchanged", skipped=True
            )

        document.ingestion_version = INGESTION_VERSION
        return self._index_document(db, document=document, data=data, category=None)

    # ------------------------------------------------------------------------
    # Núcleo comun
    # ------------------------------------------------------------------------
    def _index_document(
        self, db: Session, *, document: Document, data: bytes, category: str | None
    ) -> IngestionOutcome:
        """Extrae, chunkea, embebe e indexa. Actualiza el manifest."""
        settings = self._settings
        try:
            extracted = extract_document(data, filename=document.filename)
        except Exception as exc:
            document.status = "failed"
            document.error_message = str(getattr(exc, "message", exc))[:500]
            db.flush()
            raise

        if extracted.is_empty:
            document.status = "empty"
            document.chunk_count = 0
            document.error_message = "; ".join(extracted.warnings)[:500] or "sin texto extraible"
            db.flush()
            # El commit SQL retira la evidencia; un rollback conserva la anterior.
            document.active_generation = None
            document.index_cleanup_pending = True
            return IngestionOutcome(
                document_id=document.id,
                chunk_count=0,
                status="empty",
                warnings=tuple(extracted.warnings),
            )

        drafts = chunk_blocks(
            extracted.blocks,
            chunk_size_tokens=settings.rag_chunk_size_tokens,
            overlap_tokens=settings.rag_chunk_overlap_tokens,
        )
        if not drafts:
            document.status = "empty"
            document.chunk_count = 0
            document.active_generation = None
            document.index_cleanup_pending = True
            db.flush()
            return IngestionOutcome(document_id=document.id, chunk_count=0, status="empty")

        policy = get_registry().get(category) if category else None
        subpath = str(Path(document.relative_path).parent) if document.relative_path else ""

        chunks = self._build_chunks(
            document=document,
            drafts=drafts,
            category=category,
            policy=policy,
            subpath=subpath,
        )
        generation = new_id()
        fingerprint = indexing_fingerprint(self._llm)
        chunks = [
            replace(chunk, metadata=replace(chunk.metadata, generation=generation, index_fingerprint=fingerprint))
            for chunk in chunks
        ]
        vectors = self._embed_chunks(chunks)
        # La generacion anterior se conserva hasta el commit SQL. Una escritura
        # parcial o rollback deja candidatos invisibles, nunca reemplaza la valida.
        written = self._store.upsert_chunks(chunks, vectors)
        if written != len(chunks):
            raise IngestionFailedError(
                "No se indexaron todos los chunks del documento.",
                detail=f"esperados={len(chunks)} escritos={written}",
            )

        document.active_generation = generation
        document.index_fingerprint = fingerprint
        document.index_cleanup_pending = True
        document.status = "indexed"
        document.chunk_count = len(chunks)
        document.error_message = "; ".join(extracted.warnings)[:500] or None
        document.updated_at = utcnow_naive()
        db.add(
            DocumentVersion(
                id=new_id(),
                document_id=document.id,
                sha256=document.sha256,
                chunk_count=len(chunks),
                ingested_at=utcnow_naive(),
            )
        )
        db.flush()

        logger.info(
            "ingestion.document_indexed",
            extra={
                "document_id": document.id,
                "category": category,
                "chunk_count": len(chunks),
                "scope": document.scope,
            },
        )
        return IngestionOutcome(
            document_id=document.id,
            chunk_count=len(chunks),
            status="indexed",
            warnings=tuple(extracted.warnings),
        )

    def _build_chunks(
        self,
        *,
        document: Document,
        drafts: list[ChunkDraft],
        category: str | None,
        policy: CategoryPolicy | None,
        subpath: str,
    ) -> list[Chunk]:
        """Construye los chunks con su metadata de ACL a partir de los borradores.

        La ACL (``allowed_groups``/``sensitivity``) sale de la politica de la
        categoria; un documento sin categoria (adjunto privado) queda como
        ``__private__`` con sensibilidad ``private``.
        """
        settings = self._settings
        normalized_subpath = "" if subpath in (".", "") else subpath
        chunks: list[Chunk] = []
        for draft in drafts:
            metadata = ChunkMetadata(
                chunk_id=new_id(),
                document_id=document.id,
                document_sha256=document.sha256,
                filename=document.filename,
                relative_path=document.relative_path or document.filename,
                category=category or "__private__",
                subpath=normalized_subpath,
                mime_type=document.mime_type,
                page_or_sheet=draft.page_or_sheet,
                section=draft.section,
                chunk_index=draft.index,
                embedding_model=settings.ollama_embedding_model,
                embedding_dimension=settings.rag_embedding_dimension,
                ingestion_version=INGESTION_VERSION,
                allowed_roles=(),
                allowed_groups=tuple(policy.allowed_groups) if policy else (),
                sensitivity=policy.sensitivity if policy else "private",
                scope=document.scope,
                owner_user_id=document.owner_user_id,
                conversation_id=document.conversation_id,
            )
            chunks.append(Chunk(text=draft.text, metadata=metadata))
        return chunks

    def _embed_chunks(self, chunks: list[Chunk]) -> list[list[float]]:
        """Embebe los chunks por lotes con el prefijo de documento.

        Se usa el prefijo de documento de embeddinggemma (la consulta usa el de
        query); ambos deben coincidir con ``INGESTION_VERSION``.
        """
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[start : start + EMBED_BATCH_SIZE]
            vectors.extend(
                self._llm.embed(
                    [
                        format_document(
                            c.text, title=c.metadata.section or c.metadata.filename
                        )
                        for c in batch
                    ]
                )
            )
        return vectors

    def _ensure_access_policy(self, db: Session, document: Document, *, category: str) -> None:
        """Crea/actualiza la politica de acceso del documento a partir de la categoria."""
        policy = get_registry().get(category)
        existing = db.execute(
            select(DocumentAccessPolicy).where(DocumentAccessPolicy.document_id == document.id)
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                DocumentAccessPolicy(
                    id=new_id(),
                    document_id=document.id,
                    allowed_roles=[],
                    allowed_groups=list(policy.allowed_groups),
                    sensitivity=policy.sensitivity,
                    source_owner=policy.source_owner,
                    created_at=utcnow_naive(),
                )
            )
        else:
            existing.allowed_groups = list(policy.allowed_groups)
            existing.sensitivity = policy.sensitivity
            existing.source_owner = policy.source_owner
        db.flush()

    # ------------------------------------------------------------------------
    # Borrado
    # ------------------------------------------------------------------------
    def delete_document(self, db: Session, document: Document) -> None:
        """Confirma una baja logica antes de permitir la limpieza fisica.

        Qdrant no participa en la transaccion SQL. Borrar sus vectores antes del
        commit perdia evidencia aunque SQL hiciera rollback. El filtro de
        generaciones la oculta tras el commit y cleanup_generations la elimina.
        """
        document.deleted_at = utcnow_naive()
        document.status = "deleted"
        document.chunk_count = 0
        document.active_generation = None
        document.index_cleanup_pending = True
        document.updated_at = utcnow_naive()
        db.flush()
        logger.info("ingestion.document_deleted", extra={"document_id": document.id})

    @staticmethod
    def delete_conversation_documents(db: Session, conversation_id: str) -> int:
        """Prepara una baja reintentable, incluyendo adjuntos ya tombstoned.

        El llamador confirma SQL antes de ejecutar ``purge_conversation_documents``.
        No se liberan bytes de cuota hasta retirar tambien los archivos reales.
        """
        documents = db.execute(
            select(Document).where(
                Document.conversation_id == conversation_id,
                Document.scope == SCOPE_CONVERSATION,
            ).with_for_update()
        ).scalars().all()
        now = utcnow_naive()
        for document in documents:
            document.deleted_at = document.deleted_at or now
            document.status = "deleted"
            document.chunk_count = 0
            document.active_generation = None
            document.index_cleanup_pending = True
            document.filename = ""
            document.relative_path = None
            document.mime_type = "application/octet-stream"
            document.sha256 = "0" * 64
            document.error_message = None
            document.index_fingerprint = None
            document.updated_at = now
        document_ids = [document.id for document in documents]
        if document_ids:
            db.execute(delete(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids)))
            db.execute(delete(DocumentAccessPolicy).where(DocumentAccessPolicy.document_id.in_(document_ids)))
        db.flush()
        return len(documents)

    def purge_conversation_documents(self, db: Session, conversation_id: str) -> int:
        """Borra vectores, bytes y manifest despues del commit de la baja.

        Ningun fallo convierte una baja en una conversacion viva. Las rutas y
        bytes pendientes se conservan para reintentar y siguen contando en cuota.
        La fila vacia de conversacion bloquea publicaciones concurrentes.
        """
        conversation = MemoryService._lock_conversation(db, conversation_id, allow_deleted=True)
        if conversation.deleted_at is None:
            raise IngestionFailedError("La conversacion no esta eliminada.")
        documents = list(db.execute(
            select(Document).where(
                Document.conversation_id == conversation_id,
                Document.scope == SCOPE_CONVERSATION,
            ).with_for_update()
        ).scalars())
        try:
            paths = [self._attachment_cleanup_path(document) for document in documents if document.storage_path]
            # Incluye vectores huerfanos de cargas cuyo SQL hizo rollback.
            self._store.delete_conversation(conversation_id)
            for path in paths:
                path.unlink(missing_ok=True)
            self._remove_private_namespace(conversation.user_id, conversation_id)
        except Exception as exc:  # noqa: BLE001 - conserva el estado reintentable
            logger.warning(
                "ingestion.conversation_cleanup_pending",
                extra={"conversation_id": conversation_id, "error_type": type(exc).__name__},
            )
            raise ConversationCleanupPendingError(detail=type(exc).__name__) from exc
        document_ids = [document.id for document in documents]
        if document_ids:
            db.execute(delete(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids)))
            db.execute(delete(DocumentAccessPolicy).where(DocumentAccessPolicy.document_id.in_(document_ids)))
            db.execute(delete(Document).where(Document.id.in_(document_ids)))
        conversation.purged_at = utcnow_naive()
        db.flush()
        return len(documents)

    def _attachment_cleanup_path(self, document: Document) -> Path:
        """Solo el namespace exacto: nunca una ruta arbitraria guardada en SQL."""
        if not document.owner_user_id or not document.conversation_id or not document.storage_path:
            raise IngestionFailedError("El adjunto no tiene una ruta privada valida.")
        path, portable = self._attachment_storage_path(
            owner_user_id=document.owner_user_id,
            conversation_id=document.conversation_id,
            internal_filename=PureWindowsPath(document.storage_path).name,
        )
        raw = Path(document.storage_path)
        if raw.is_absolute():
            if raw.resolve() != path:
                raise IngestionFailedError("El adjunto esta fuera de su directorio privado.")
        elif document.storage_path.replace("\\", "/") != portable:
            raise IngestionFailedError("El adjunto tiene una ruta interna no valida.")
        return path

    def _remove_private_namespace(self, owner_user_id: str, conversation_id: str) -> None:
        """Retira tambien bytes huerfanos sin seguir enlaces ni directorios ajenos."""
        probe, _ = self._attachment_storage_path(
            owner_user_id=owner_user_id, conversation_id=conversation_id, internal_filename="probe"
        )
        namespace = probe.parent
        if not namespace.exists():
            return
        for path in namespace.iterdir():
            # El contrato de upload solo crea archivos en este nivel. Un
            # directorio inesperado requiere revision, nunca rmtree arbitrario.
            if path.is_symlink() or (path.is_file() and path.resolve().is_relative_to(namespace)):
                path.unlink()
            else:
                raise IngestionFailedError("El directorio privado contiene una entrada no valida.")
        namespace.rmdir()
        # Puede contener otras conversaciones del mismo propietario.
        with suppress(OSError):
            namespace.parent.rmdir()
