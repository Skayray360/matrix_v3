# Creado por Aldo Garcia.
"""Memoria conversacional.

Garantias (seccion 13):

* cada conversacion pertenece a un usuario y **todas** las rutas verifican
  ownership -- no basta con ocultar la conversacion en la UI;
* jamas se mezclan conversaciones de distintos usuarios;
* al reconstruir el contexto se descartan los turnos que se apoyaron en
  categorias que el rol actual **ya no** tiene autorizadas. Si a un usuario se le
  retira `nomina`, la respuesta que le dimos ayer sobre nomina no puede volver al
  prompt de hoy;
* no se almacena chain-of-thought interno del modelo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256

from sqlalchemy import delete, func, null, select, update
from sqlalchemy.orm import Session

from app.authorization.context import UserContext
from app.common.errors import ForbiddenError, NotFoundError
from app.common.ids import new_id, utcnow_naive
from app.common.logging import get_logger
from app.database.models import (
    AuditEvent,
    ChatOperation,
    Conversation,
    ConversationMessage,
    ConversationSummary,
    StructuredSourcePermission,
    UserRole,
)

logger = get_logger(__name__)

#: Numero de turnos recientes que se envian textualmente al modelo.
DEFAULT_RECENT_TURNS = 6
#: A partir de este numero de mensajes se genera/actualiza un resumen.
SUMMARY_THRESHOLD = 12
#: Longitud maxima del texto de un mensaje que se reenvia al prompt.
MAX_MESSAGE_CHARS_IN_CONTEXT = 1200

# Antes de v1.2.4 la huella conocia row_filter pero SQL no lo aplicaba. Esas
# respuestas no pueden conservar una huella valida despues de activar el fix.
STRUCTURED_SCOPE_VERSION = 2


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """Contexto conversacional ya filtrado por permisos vigentes."""

    conversation_id: str
    summary: str = ""
    turns: tuple[ConversationTurn, ...] = field(default_factory=tuple)
    dropped_turns: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.summary and not self.turns


def authorization_fingerprint(db: Session, ctx: UserContext, categories: frozenset[str]) -> str:
    # Incluye filtros de fila/entidad: retirar filas sin cambiar nombre del rol
    # tambien invalida texto derivado de esas consultas.
    rows = (
        db.execute(
            select(StructuredSourcePermission)
            .join(UserRole, UserRole.role_id == StructuredSourcePermission.role_id)
            .where(UserRole.user_id == ctx.user_id)
        )
        .scalars()
        .all()
    )
    grants = sorted((row.source_name, str(row.allowed_entities), str(row.row_filter)) for row in rows)
    payload = [sorted(categories), sorted(ctx.roles), sorted(ctx.permissions), sorted(ctx.allowed_sources), grants]
    if ctx.allowed_sources:
        from app.structured_data.sources import authorization_catalog_fingerprint

        payload.append({
            "structured_scope_version": STRUCTURED_SCOPE_VERSION,
            "user_attributes": [ctx.user_id, ctx.username, ctx.email],
            "source_catalog": authorization_catalog_fingerprint(ctx.allowed_sources),
        })
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class MemoryService:
    """CRUD de conversaciones y ensamblado del contexto."""

    # ------------------------------------------------------------ conversacion
    def create_conversation(self, db: Session, ctx: UserContext, *, title: str = "") -> Conversation:
        now = utcnow_naive()
        conversation = Conversation(
            id=new_id(),
            user_id=ctx.user_id,
            title=(title or "Nueva conversacion")[:255],
            created_at=now,
            updated_at=now,
        )
        db.add(conversation)
        db.flush()
        return conversation

    def get_owned_conversation(self, db: Session, ctx: UserContext, conversation_id: str) -> Conversation:
        """Recupera una conversacion propia.

        Ante una conversacion de otro usuario se devuelve **404**, no 403: un 403
        confirmaria que el identificador existe, que es exactamente la fuga que
        busca un ataque IDOR/BOLA.
        """
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.deleted_at is not None:
            raise NotFoundError("Conversacion no encontrada.")
        if conversation.user_id != ctx.user_id:
            logger.warning(
                "memory.ownership_violation",
                extra={
                    "user_opaque_id": ctx.user_id,
                    "conversation_id": conversation_id,
                    "authorization_decision": "DENY",
                },
            )
            raise NotFoundError("Conversacion no encontrada.")
        return conversation

    def list_conversations(self, db: Session, ctx: UserContext, *, limit: int = 100) -> list[Conversation]:
        """Solo conversaciones propias, nunca de otros usuarios."""
        return list(
            db.execute(
                select(Conversation)
                .where(Conversation.user_id == ctx.user_id, Conversation.deleted_at.is_(None))
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
            ).scalars().all()
        )

    def delete_conversation(self, db: Session, ctx: UserContext, conversation_id: str) -> Conversation:
        """Retira el contenido y conserva la baja que bloquea workers tardios.

        Se permite repetir una eliminacion propia: Qdrant o el filesystem pueden
        haber fallado despues de confirmar la baja SQL. La fila de la operacion
        permanece sin payload, para que su clave nunca vuelva a ejecutar IA.
        """
        conversation = self._lock_conversation(db, conversation_id, allow_deleted=True)
        if conversation.user_id != ctx.user_id:
            raise NotFoundError("Conversacion no encontrada.")
        self._erase_content(db, conversation)
        return conversation

    def purge_deleted_conversation(self, db: Session, conversation_id: str) -> Conversation:
        """Mismo borrado para bajas historicas; nunca borra una conversacion viva."""
        conversation = self._lock_conversation(db, conversation_id, allow_deleted=True)
        if conversation.deleted_at is None:
            raise NotFoundError("La conversacion no esta eliminada.")
        self._erase_content(db, conversation)
        return conversation

    def expire_conversation(
        self, db: Session, conversation_id: str, *, cutoff: datetime
    ) -> Conversation | None:
        """Aplica un plazo autorizado, comprobando la actividad bajo el lock."""
        conversation = self._lock_conversation(db, conversation_id, allow_deleted=True)
        if conversation.deleted_at is not None or conversation.updated_at >= cutoff:
            return None
        self._erase_content(db, conversation)
        return conversation

    @staticmethod
    def _lock_conversation(
        db: Session, conversation_id: str, *, allow_deleted: bool = False
    ) -> Conversation:
        # Una lectura FOR UPDATE es actual incluso bajo REPEATABLE READ. Evita
        # publicar usando un objeto ORM que sobrevivio al commit previo a IA.
        with db.no_autoflush:
            conversation = db.execute(
                select(Conversation).where(Conversation.id == conversation_id)
                .with_for_update().execution_options(populate_existing=True)
            ).scalar_one_or_none()
        if conversation is None or (conversation.deleted_at is not None and not allow_deleted):
            raise NotFoundError("Conversacion no encontrada.")
        return conversation

    @staticmethod
    def _erase_content(db: Session, conversation: Conversation) -> None:
        now = utcnow_naive()
        db.execute(delete(ConversationSummary).where(ConversationSummary.conversation_id == conversation.id))
        db.execute(delete(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id))
        db.execute(
            update(ChatOperation).where(ChatOperation.conversation_id == conversation.id)
            .values(status="cancelled", response=null(), message=None, session_id=None, request_id=None)
        )
        # Las cargas historicas auditaban el nombre visible: tambien puede
        # contener datos personales. Se conserva evento/ID/decision, no nombres.
        db.execute(
            update(AuditEvent).where(AuditEvent.conversation_id == conversation.id)
            .values(resource=None, source_ids=None)
        )
        conversation.title = "Conversacion eliminada"
        conversation.deleted_at = conversation.deleted_at or now
        conversation.purged_at = None
        conversation.updated_at = now
        db.flush()

    def rename_if_untitled(self, db: Session, conversation: Conversation, question: str) -> None:
        """Titula la conversacion con la primera pregunta del usuario."""
        conversation = self._lock_conversation(db, conversation.id)
        if conversation.title in ("", "Nueva conversacion"):
            conversation.title = (question.strip().splitlines()[0] or "Conversacion")[:120]
            db.flush()

    # ---------------------------------------------------------------- mensajes
    def append_message(
        self,
        db: Session,
        conversation: Conversation,
        *,
        role: str,
        content: str,
        model: str | None = None,
        intent: str | None = None,
        answer_basis: str | None = None,
        source_ids: tuple[str, ...] = (),
        authorized_categories: tuple[str, ...] = (),
    ) -> ConversationMessage:
        """Persiste un turno.

        ``authorized_categories`` deja constancia del alcance con el que se produjo
        el mensaje: es lo que permite descartarlo despues si los permisos cambian.
        """
        conversation = self._lock_conversation(db, conversation.id)
        message = ConversationMessage(
            id=new_id(),
            conversation_id=conversation.id,
            user_id=conversation.user_id,
            role=role,
            content=content,
            model=model,
            intent=intent,
            answer_basis=answer_basis,
            authorization_scope=db.info.get("authorization_scope"),
            source_ids=list(source_ids) or None,
            authorized_categories=list(authorized_categories) or None,
            created_at=utcnow_naive(),
        )
        db.add(message)
        conversation.updated_at = utcnow_naive()
        db.flush()
        return message

    def list_messages(
        self, db: Session, conversation_id: str, *, limit: int = 200, before_seq: int | None = None
    ) -> list[ConversationMessage]:
        """Turnos en orden de llegada.

        Se ordena por ``seq`` y no por ``created_at``: la resolucion del reloj no
        garantiza marcas distintas entre dos inserciones consecutivas, y el hilo
        se mostraria invertido.
        """
        query = select(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id)
        if before_seq is not None:
            query = query.where(ConversationMessage.seq < before_seq)
        return list(reversed(db.execute(query.order_by(ConversationMessage.seq.desc()).limit(limit)).scalars().all()))

    @staticmethod
    def message_visible(message: ConversationMessage, categories: frozenset[str], scope: str) -> bool:
        # La identidad fija no usa modelos, fuentes ni permisos. Su respuesta
        # exacta sigue siendo publica incluso en conversaciones anteriores al
        # registro de procedencia; una etiqueta "identity" sola no basta.
        if (
            message.role == "assistant"
            and message.intent == "identity"
            and message.content == "Soy Matrix RH."
            and not message.source_ids
            and not message.authorized_categories
        ):
            return True
        if not set(message.authorized_categories or []).issubset(categories):
            return False
        # No se reutilizan respuestas previas al registro de procedencia: pueden
        # contener datos heredados por memoria, aun sin citas.
        if message.role == "assistant" and not message.authorization_scope:
            return False
        return message.authorization_scope in (None, scope)

    # ----------------------------------------------------------------- contexto
    def build_context(
        self,
        db: Session,
        ctx: UserContext,
        conversation: Conversation,
        *,
        authorized_categories: frozenset[str],
        recent_turns: int = DEFAULT_RECENT_TURNS,
    ) -> ConversationContext:
        """Ensambla resumen + ultimos turnos, filtrando lo ya no autorizado."""
        if conversation.user_id != ctx.user_id:
            # Defensa en profundidad: nunca se ensambla contexto ajeno.
            raise ForbiddenError()

        messages = list(
            db.execute(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conversation.id)
                .order_by(ConversationMessage.seq.desc())
                .limit(recent_turns * 3)
            )
            .scalars()
            .all()
        )
        messages.reverse()

        scope = authorization_fingerprint(db, ctx, authorized_categories)
        turns: list[ConversationTurn] = []
        dropped = 0
        for message in messages:
            if not self.message_visible(message, authorized_categories, scope):
                # El turno se apoyo en categorias que el rol actual ya no tiene.
                dropped += 1
                continue
            turns.append(
                ConversationTurn(
                    role=message.role,
                    content=message.content[:MAX_MESSAGE_CHARS_IN_CONTEXT],
                )
            )

        summary_row = db.execute(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conversation.id)
            .order_by(ConversationSummary.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if dropped:
            logger.info(
                "memory.turns_dropped_by_policy",
                extra={"conversation_id": conversation.id, "dropped_turns": dropped},
            )

        return ConversationContext(
            conversation_id=conversation.id,
            summary=summary_row.summary if summary_row and summary_row.authorization_scope == scope else "",
            turns=tuple(turns[-recent_turns:]),
            dropped_turns=dropped,
        )

    # ----------------------------------------------------------------- resumen
    def needs_summary(self, db: Session, conversation_id: str) -> bool:
        total = db.execute(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
        ).scalar_one()
        last = db.execute(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conversation_id)
            .order_by(ConversationSummary.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        covered = last.message_count if last else 0
        return (total - covered) >= SUMMARY_THRESHOLD

    def store_summary(
        self, db: Session, conversation_id: str, *, summary: str, message_count: int
    ) -> ConversationSummary:
        self._lock_conversation(db, conversation_id)
        record = ConversationSummary(
            id=new_id(),
            conversation_id=conversation_id,
            summary=summary.strip()[:8000],
            message_count=message_count,
            authorization_scope=db.info.get("authorization_scope"),
            covered_until_message_id=db.execute(
                select(ConversationMessage.id)
                .where(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.seq.desc())
                .limit(1)
            ).scalar_one_or_none(),
            created_at=utcnow_naive(),
        )
        db.add(record)
        db.flush()
        return record
