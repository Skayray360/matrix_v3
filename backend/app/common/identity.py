# Creado por Aldo Garcia.
"""Reconocimiento puro de identidad publica, compartido por router y memoria."""

from __future__ import annotations

import re
import unicodedata

_IDENTITY_REQUESTS = frozenset({
    "quien eres", "quien sos", "como te llamas", "cual es tu nombre",
    "dime tu nombre", "identificate", "presentate", "que asistente eres",
})


def is_identity_question(question: str) -> bool:
    """Solo solicitudes completas de identidad; no acepta preguntas compuestas.

    No lee Settings, archivos de politica, historial ni fuentes. La coincidencia
    completa evita tratar como publica una consulta RH que empiece por un saludo
    o por 'quien eres' y despues incluya datos o pida informacion empresarial.
    """
    decomposed = unicodedata.normalize("NFKD", question.strip().casefold())
    text = "".join(char for char in decomposed if not unicodedata.combining(char)).strip(" ¿?¡!.,;:")
    text = re.sub(r"^(?:y|por cierto|cambiando de tema|otra pregunta)[, :¿]+", "", text).strip(" ¿?¡!.,;:")
    text = re.sub(r"^(?:hola|buenos dias|buenas tardes|buenas noches)[, :¿]+", "", text)
    return text in _IDENTITY_REQUESTS
