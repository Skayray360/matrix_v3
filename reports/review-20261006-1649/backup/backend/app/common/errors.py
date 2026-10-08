# Creado por Aldo Garcia.
"""Errores tipados de Matrix RH.

Toda la aplicacion levanta subclases de :class:`MatrixError`. La capa HTTP las
traduce a una respuesta segura que contiene unicamente ``code``, ``message`` y
``request_id``: nunca stack traces, rutas internas, SQL ni secretos.

El catalogo de codigos es cerrado a proposito. Si aparece una condicion nueva se
agrega un codigo aqui en lugar de devolver texto libre, porque el frontend y las
pruebas automatizadas dependen del codigo, no del mensaje.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Codigos de error estables expuestos al cliente."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    UNSUPPORTED_FILE = "unsupported_file"
    FILE_TOO_LARGE = "file_too_large"
    EXTRACTION_FAILED = "extraction_failed"
    INGESTION_FAILED = "ingestion_failed"
    CONVERSATION_CLEANUP_PENDING = "conversation_cleanup_pending"
    OLLAMA_UNAVAILABLE = "ollama_unavailable"
    CHAT_CAPACITY_FULL = "chat_capacity_full"
    EMBEDDING_DIMENSION_MISMATCH = "embedding_dimension_mismatch"
    QDRANT_UNAVAILABLE = "qdrant_unavailable"
    DATABASE_UNAVAILABLE = "database_unavailable"
    STRUCTURED_QUERY_REJECTED = "structured_query_rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    OUT_OF_SCOPE = "out_of_scope"
    POLICY_MISSING = "policy_missing"
    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    CONFIGURATION_ERROR = "configuration_error"
    INTERNAL_ERROR = "internal_error"


class MatrixError(Exception):
    """Error base. ``status_code`` es el HTTP que la capa API debe devolver."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    status_code: int = 500
    #: Mensaje por defecto seguro para mostrar al usuario final.
    default_message: str = "Ocurrio un error interno."

    def __init__(self, message: str | None = None, *, detail: str | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        #: Detalle tecnico interno; no se envia al cliente ni se serializa en logs.
        self.detail = detail

    def to_public_dict(self, request_id: str | None = None) -> dict[str, str | None]:
        """Representacion segura para el cliente."""
        return {"code": str(self.code), "message": self.message, "request_id": request_id}


class CapacityFullError(MatrixError):
    code = ErrorCode.CHAT_CAPACITY_FULL
    status_code = 503
    default_message = "La cola esta completa. Su solicitud no fue aceptada. Intente de nuevo mas tarde."


class UnauthorizedError(MatrixError):
    code = ErrorCode.UNAUTHORIZED
    status_code = 401
    default_message = "No hay una sesion valida."


class ForbiddenError(MatrixError):
    """Denegacion de autorizacion.

    El mensaje es deliberadamente generico: revelar *que* recurso fue denegado
    permitiria inferir la existencia de informacion restringida (seccion 6.2).
    """

    code = ErrorCode.FORBIDDEN
    status_code = 403
    default_message = "No tiene acceso a esa informacion."


class NotFoundError(MatrixError):
    code = ErrorCode.NOT_FOUND
    status_code = 404
    default_message = "Recurso no encontrado."


class ValidationFailedError(MatrixError):
    code = ErrorCode.VALIDATION_ERROR
    status_code = 422
    default_message = "La solicitud no es valida."


class RateLimitedError(MatrixError):
    code = ErrorCode.RATE_LIMITED
    status_code = 429
    default_message = "Demasiados intentos. Intente de nuevo mas tarde."


class UnsupportedFileError(MatrixError):
    code = ErrorCode.UNSUPPORTED_FILE
    status_code = 415
    default_message = "Tipo de archivo no soportado."


class FileTooLargeError(MatrixError):
    code = ErrorCode.FILE_TOO_LARGE
    status_code = 413
    default_message = "El archivo excede el tamano maximo permitido."


class ExtractionFailedError(MatrixError):
    code = ErrorCode.EXTRACTION_FAILED
    status_code = 422
    default_message = "No fue posible extraer texto del archivo."


class IngestionFailedError(MatrixError):
    code = ErrorCode.INGESTION_FAILED
    status_code = 500
    default_message = "Fallo la ingesta del documento."


class ConversationCleanupPendingError(MatrixError):
    """La baja SQL esta confirmada; el mismo DELETE puede completar la limpieza."""

    code = ErrorCode.CONVERSATION_CLEANUP_PENDING
    status_code = 503
    default_message = (
        "Conversacion eliminada; la limpieza de sus adjuntos sigue pendiente. "
        "Reintente la eliminacion."
    )


class OllamaUnavailableError(MatrixError):
    code = ErrorCode.OLLAMA_UNAVAILABLE
    status_code = 503
    default_message = "El servicio de IA local no esta disponible."


class EmbeddingDimensionMismatchError(MatrixError):
    """Nunca se trunca ni se rellena un vector: se falla de forma diagnostica."""

    code = ErrorCode.EMBEDDING_DIMENSION_MISMATCH
    status_code = 503
    default_message = "La dimension de embeddings no coincide con la configurada."


class QdrantUnavailableError(MatrixError):
    code = ErrorCode.QDRANT_UNAVAILABLE
    status_code = 503
    default_message = "El almacen vectorial no esta disponible."


class DatabaseUnavailableError(MatrixError):
    code = ErrorCode.DATABASE_UNAVAILABLE
    status_code = 503
    default_message = "La base de datos no esta disponible."


class StructuredQueryRejectedError(MatrixError):
    code = ErrorCode.STRUCTURED_QUERY_REJECTED
    status_code = 400
    default_message = "La consulta estructurada fue rechazada por politica."


class InsufficientEvidenceError(MatrixError):
    code = ErrorCode.INSUFFICIENT_EVIDENCE
    status_code = 200
    default_message = "No hay evidencia documental suficiente para responder."


class OutOfScopeError(MatrixError):
    code = ErrorCode.OUT_OF_SCOPE
    status_code = 200
    default_message = "La consulta esta fuera del alcance de Matrix RH."


class PolicyMissingError(MatrixError):
    """Deny-by-default: si no existe politica explicita, se deniega."""

    code = ErrorCode.POLICY_MISSING
    status_code = 403
    default_message = "No existe una politica de acceso definida para ese recurso."


class ConfigurationError(MatrixError):
    code = ErrorCode.CONFIGURATION_ERROR
    status_code = 500
    default_message = "Configuracion invalida."
