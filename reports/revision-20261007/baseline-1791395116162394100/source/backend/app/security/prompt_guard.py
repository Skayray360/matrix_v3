# Creado por Aldo Garcia.
"""Defensas frente a prompt injection en documentos y mensajes.

Principio de la seccion 18: **documentos, resultados SQL y mensajes anteriores
son contenido no confiable**. Solo el system policy y el Authorization Guard
definen permisos y reglas operativas.

Este modulo no intenta "detectar todas las inyecciones" -- eso no es alcanzable.
Lo que hace es reducir su superficie:

* delimita la evidencia con marcadores explicitos para que el modelo distinga
  datos de instrucciones;
* neutraliza secuencias que imitan marcadores de rol del protocolo de chat;
* marca (para auditoria) los patrones imperativos tipicos.

La defensa real no es textual sino arquitectonica: el modelo **no tiene** ninguna
herramienta capaz de ampliar permisos, y el filtro ACL se aplica en Qdrant antes
de que el texto llegue al prompt. Aunque una inyeccion convenza al modelo, no hay
nada que pueda ejecutar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.common.logging import get_logger

logger = get_logger(__name__)

#: Marcadores de rol que un documento podria usar para simular un turno del
#: sistema. Se neutralizan sustituyendo los dos puntos por un caracter visualmente
#: equivalente pero inerte.
_ROLE_MARKER_RE = re.compile(
    r"(?im)^\s*(system|assistant|user|tool|developer|sistema|asistente|usuario)\s*:",
)

#: Delimitadores del propio prompt de Matrix RH: si aparecen dentro de un
#: documento se escapan para que no puedan cerrar un bloque de evidencia.
_FENCE_RE = re.compile(r"(<<<|>>>)")

#: Patrones imperativos frecuentes en inyecciones. Solo se registran.
_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignora (todas )?las (instrucciones|reglas)"),
    re.compile(r"(?i)ignore (all )?(previous )?(instructions|rules)"),
    re.compile(r"(?i)olvida (tus|las) (reglas|instrucciones)"),
    re.compile(r"(?i)(muestra|revela|dime|imprime).{0,30}(system prompt|prompt del sistema)"),
    re.compile(r"(?i)(revela|muestra|dame).{0,30}(contrase|password|secreto|api key|token)"),
    re.compile(r"(?i)eres ahora|a partir de ahora eres|actua como si tuvieras acceso"),
    re.compile(r"(?i)(desactiva|omite|salta).{0,25}(seguridad|autorizacion|autorización|filtro)"),
)


@dataclass(frozen=True, slots=True)
class SanitizedContent:
    text: str
    injection_signals: tuple[str, ...] = ()

    @property
    def suspicious(self) -> bool:
        return bool(self.injection_signals)


def sanitize_untrusted_text(text: str, *, source_label: str = "") -> SanitizedContent:
    """Neutraliza marcadores estructurales y reporta senales de inyeccion."""
    if not text:
        return SanitizedContent(text="")

    cleaned = _ROLE_MARKER_RE.sub(lambda m: m.group(0).replace(":", "ː"), text)
    cleaned = _FENCE_RE.sub(lambda m: m.group(0).replace("<", "‹").replace(">", "›"), cleaned)

    signals = tuple(
        pattern.pattern for pattern in _INJECTION_PATTERNS if pattern.search(text)
    )
    if signals:
        logger.warning(
            "security.prompt_injection_signal",
            extra={"signal_count": len(signals), "source_label": source_label},
        )
    return SanitizedContent(text=cleaned, injection_signals=signals)


def sanitize_user_message(message: str, *, max_chars: int = 8000) -> SanitizedContent:
    """Sanea el mensaje del usuario.

    El usuario puede pedir lo que quiera: la peticion no cambia sus permisos. Se
    acota la longitud y se registran senales para auditoria, pero **no** se
    bloquea la consulta -- bloquearla filtraria que el sistema reacciona a ciertas
    palabras, lo que es en si mismo informacion util para un atacante.
    """
    trimmed = (message or "").strip()[:max_chars]
    return sanitize_untrusted_text(trimmed, source_label="user_message")
