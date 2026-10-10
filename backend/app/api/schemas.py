# Creado por Aldo Garcia.
"""Modelos Pydantic de request/response.

Todos los limites de longitud son explicitos: un backend que acepta cadenas sin
acotar es un vector de denegacion de servicio y de abuso de contexto del LLM.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.common.answers import AnswerBasis


class LocalLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class LocalPasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=16, max_length=128)


class LocalUserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=16, max_length=128)
    role: Literal["matrix_admin", "prestaciones_reader"] = "prestaciones_reader"


class LocalUserResponse(BaseModel):
    user_id: str
    username: str
    display_name: str
    is_active: bool
    roles: list[str]


class MeResponse(BaseModel):
    user_id: str
    username: str
    display_name: str
    auth_source: str
    roles: list[str]
    permissions: list[str]
    allowed_categories: list[str]
    category_wildcard: bool
    csrf_token: str


class ConversationSummaryResponse(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    model: str | None = None
    intent: str | None = None
    answer_basis: AnswerBasis | None = None
    created_at: datetime
    sources: list[dict[str, Any]] = Field(default_factory=list)


class ConversationDetailResponse(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse]
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    next_before_seq: int | None = None


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=120)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: ``conversation_id`` opcional: si falta se crea una conversacion nueva.
    conversation_id: str | None = Field(default=None, max_length=36)
    message: str = Field(min_length=1, max_length=8000)
    client_request_id: str | None = Field(default=None, min_length=16, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


class ChatSubmitRequest(ChatRequest):
    client_request_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


class ChatResponse(BaseModel):
    conversation_id: str
    message_id: str
    answer: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    intent: str
    grounded: bool
    # Las respuestas de operaciones previas a 0009 no se reclasifican.
    answer_basis: AnswerBasis | None = None
    latency_ms: int


class DocumentStatusResponse(BaseModel):
    id: str
    filename: str
    status: str
    chunk_count: int
    scope: str
    category: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class AttachmentUploadResponse(BaseModel):
    documents: list[DocumentStatusResponse]


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str


class ReadyComponent(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class ReadyResponse(BaseModel):
    ready: bool
    components: list[ReadyComponent]


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class AdminIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False


class AdminIngestResponse(BaseModel):
    started: bool
    stats: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""


class AdminPromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(max_length=36)
    category: str = Field(min_length=1, max_length=64)
