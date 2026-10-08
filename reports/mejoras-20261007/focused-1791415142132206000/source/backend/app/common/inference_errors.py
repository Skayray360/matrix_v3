# Creado por Aldo Garcia.
"""Causas de inferencia finitas y seguras, sin conservar cuerpos del proveedor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from app.common.errors import OllamaUnavailableError


class InferenceFailureKind(StrEnum):
    TIMEOUT = "timeout"
    DEADLINE = "deadline"
    HTTP = "http"
    BUSY = "busy"
    TRANSPORT = "transport"
    FORMAT = "format"
    CONTEXT_LIMIT = "context_limit"
    INCOMPLETE = "incomplete"
    EMPTY = "empty"
    REASONING = "reasoning"
    SCHEMA = "schema"
    RESPONSE_SIZE = "response_size"


@dataclass(frozen=True, slots=True)
class CompletionDiagnostics:
    """Metadatos cerrados: nunca contienen respuestas, razonamiento ni prompts."""

    completed: bool
    finish_reason: Literal["stop", "length", "not_done", "unknown"]
    response_chars: int
    thinking_chars: int
    output_tokens: int | None = None
    output_token_limit: int | None = None

    @property
    def can_regenerate(self) -> bool:
        # Solo una respuesta ya terminada permite otro POST sin duplicar una
        # generacion que podria seguir activa tras un timeout o corte de red.
        return self.completed and (
            self.finish_reason == "length"
            or (self.finish_reason == "stop" and self.response_chars == 0)
        )

    def log_fields(self) -> dict[str, bool | str | int | None]:
        return {
            "generation_completed": self.completed,
            "finish_reason": self.finish_reason,
            "response_chars": self.response_chars,
            "thinking_chars": self.thinking_chars,
            "output_tokens": self.output_tokens,
            "output_token_limit": self.output_token_limit,
        }


class InferenceFailureError(OllamaUnavailableError):
    """Error compatible con la API previa, con causa estable para cola/fallback.

    ``detail`` contiene exclusivamente un valor del enum. Ni el cuerpo HTTP,
    ni el prompt, ni el texto de una excepcion de terceros se copian aqui.
    """

    def __init__(
        self,
        failure_kind: InferenceFailureKind,
        message: str | None = None,
        *,
        http_status: int | None = None,
        completion: CompletionDiagnostics | None = None,
    ) -> None:
        self.failure_kind = InferenceFailureKind(failure_kind)
        self.http_status = http_status
        self.completion = completion
        super().__init__(message, detail=str(self.failure_kind))
