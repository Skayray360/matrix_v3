# Creado por Aldo Garcia.
"""Clasificacion y perfiles de tarea de un unico LLM local.

``gemma4:latest`` atiende conversacion, documentos, analisis y planificacion.
FAST y DEEP conservan el contrato del router y techos de salida por tarea;
son perfiles de ejecucion del mismo modelo, no dos cerebros instalados.

La decision es determinista y auditable. La baja similitud, la longitud y las
categorias autorizadas se registran como observaciones; no fuerzan DEEP. La
ruta profunda requiere una solicitud explicita de analisis/comparacion o un
resumen con evidencia extensa.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from app.config import get_settings


class ModelChoice(StrEnum):
    FAST = "fast"
    DEEP = "deep"


class Intent(StrEnum):
    """Intencion detectada por el router."""

    IDENTITY = "identity"
    CAPABILITIES = "capabilities"
    DOCUMENT_SUMMARY = "document_summary"
    DOCUMENTAL = "documental"
    STRUCTURED = "structured"
    MIXED = "mixed"
    CONVERSATIONAL = "conversational"
    GENERAL = "general"
    OUT_OF_SCOPE = "out_of_scope"


#: Marcadores que piden explicitamente un analisis profundo.
_DEEP_MARKERS = (
    "analisis profundo",
    "analiza a fondo",
    "analiza en profundidad",
    "razona paso a paso",
    "en profundidad",
)

_COMPARISON_MARKERS = (
    "compara", "contrasta", "comparativa entre", "comparacion entre", "analisis comparativo",
    "identifica contradicciones", "analiza contradicciones", "resuelve contradicciones",
    "diferencias entre",
    "ventajas y desventajas",
)
_COMPARISON_REQUEST = re.compile(
    r"\b(?:haz|realiza|elabora|dame|necesito|solicito|quiero|prepara) "
    r"(?:una? )?(?:comparacion|comparativa|contraste|evaluacion comparativa)\b"
)

#: Marcadores de consulta a datos estructurados.
#:
#: Deliberadamente NO incluyen "cuantos"/"cuantas" a secas: en un asistente de RH
#: la inmensa mayoria de las preguntas documentales empiezan asi ("cuantos dias
#: de vacaciones", "cuantos minutos de tolerancia") y clasificarlas como consulta
#: a base de datos las mandaba a la ruta profunda sin ninguna necesidad.
_STRUCTURED_MARKERS = (
    "cuantos empleados",
    "cuántos empleados",
    "cuantas personas",
    "cuántas personas",
    "cuantos trabajadores",
    "cuántos trabajadores",
    "total de empleados",
    "suma de",
    "promedio de",
    "listado de empleados",
    "numero de empleados",
    "número de empleados",
    "headcount",
    "plantilla activa",
    "por departamento",
    "por centro de trabajo",
    "rotacion mensual",
    "rotación mensual",
)

#: Marcadores de charla que no requieren recuperacion.
_CONVERSATIONAL_MARKERS = (
    "hola",
    "buenos dias",
    "buenas tardes",
    "buenas noches",
    "gracias",
    "adios",
    "hasta luego",
    "muchas gracias",
)

_IDENTITY_MARKERS = (
    "quien eres",
    "quien sos",
    "como te llamas",
    "cual es tu nombre",
    "dime tu nombre",
    "identificate",
    "presentate",
    "que asistente eres",
)

_CAPABILITY_REQUEST = re.compile(
    r"(?:(?:hola|buenos dias|buenas tardes)[, ]+)?"
    r"(?:(?:quien eres|presentate)[, ]+(?:y )?)?"
    r"(?:que puedes hacer|que sabes hacer|en que (?:me )?puedes ayudar(?:me)?|"
    r"como (?:me )?puedes ayudar(?:me)?|cuales son tus (?:capacidades|funciones)|"
    r"que (?:documentos|informacion) puedes consultar|"
    r"(?:tienes acceso a|puedes consultar) (?:la )?(?:documentacion interna|documentos autorizados))"
    r"(?: por mi| por nosotros)?"
)

# Se reconoce la forma de una referencia, no un catalogo de planes empresariales.
# Un nombre definido y desconocido requiere fuentes antes de darle significado.
_NAMED_BENEFIT_REFERENCE = re.compile(
    r"\b(?:el|del|al|este|ese|nuestro|mi) "
    r"(?:plan|programa|beneficio|apoyo|seguro|fondo|esquema) "
    r"(?!(?:de|del|para|en|que|es|son)\b)\w+"
)
_DEFINITE_BENEFIT_REFERENCE = re.compile(
    r"\b(?:el|la|los|las|del|al|este|ese|esa|nuestro|nuestra) "
    r"(?:plan(?:es)?|programas?|beneficios?|prestaciones?|apoyos?|seguros?|fondos?|esquemas?)\b"
)
_CONTEXT_REFERENCE = re.compile(
    r"\b(?:eso|esto|ello|ese|esa|esos|esas|dicho|dicha|lo anterior|lo mismo|"
    r"sus (?:condiciones|requisitos|excepciones|beneficios)|su (?:cobertura|vigencia))\b"
)
_ELLIPTICAL_FOLLOWUP = re.compile(
    r"(?:y |pero )?(?:en ese caso|en esa situacion|entonces|"
    r"con .+|para .+|cuanto(?:s|a|as)?(?: (?:es|seria|son|cuesta|recibo|"
    r"me (?:dan|toca|corresponde)|dias))?|que requisitos(?: tiene| hay| necesita| requiere)?|"
    r"como (?:lo |se )?(?:solicito|tramito|aplica)|desde cuando|hasta cuando)"
)


_SUMMARY_MARKERS = (
    "resume",
    "resumeme",
    "resumelo",
    "resumir",
    "resumen",
    "sintetiza",
    "sintetizalo",
    "sintesis",
    "puntos clave",
    "ideas principales",
    "extracto del",
)

# Una pregunta operativa desconocida sigue requiriendo recuperacion. Las
# explicaciones conceptuales completas pueden abarcar RH u otros temas.
_DOCUMENTAL_MARKERS = (
    "politica",
    "prestacion",
    "beneficio",
    "vacaciones",
    "aguinaldo",
    "nomina",
    "salario",
    "sueldo",
    "compensacion",
    "horario",
    "jornada",
    "incapacidad",
    "permiso laboral",
    "licencia laboral",
    "reclutamiento",
    "seleccion de personal",
    "capacitacion",
    "relaciones laborales",
    "contrato laboral",
    "despido",
    "finiquito",
    "antiguedad",
    "acoso",
    "hostigamiento",
    "denuncia",
    "prima vacacional",
    "dias festivos",
    "recursos humanos",
    "reglamento",
    "procedimiento",
    "requisito interno",
    "manual",
    "norma interna",
    "documento",
    "documentacion",
    "archivo",
    "adjunto",
    "contenido cargado",
    "fuentes",
    "segun la",
    "segun el",
    "que dice",
    "de acuerdo con",
    "me corresponde",
    "nos corresponde",
    "en nuestra empresa",
    "en la empresa",
    "en matrix rh",
    "empresa",
    "bono",
    "bonos",
    "retardo",
    "retardos",
    "imss",
    "me caso",
    "matrimonio",
)

# No hay una allowlist de temas generales. Se reconoce la forma conceptual,
# siempre que no incluya referencias internas, adjuntos o datos personales.
_GENERAL_REQUEST = re.compile(
    r"(?:por favor[, ]+)?"
    r"(?:explica(?:me)?(?: que (?:es|son))?|que (?:es|son)|define|como funciona(?:n)?|"
    r"en que consiste|cual es el significado de|que tipos de) "
    r"[^?¿.!;:\n]+"
)
_INTERNAL_REFERENCES = (
    "en nuestra empresa", "en la empresa", "de nuestra empresa", "de la empresa",
    "nuestra politica", "nuestro reglamento", "segun la", "segun el", "que dice",
    "de acuerdo con", "documento", "documentacion", "archivo", "adjunto",
    "contenido cargado", "fuentes", "matrix rh", "penoles", "fresnillo",
    "me corresponde", "nos corresponde", "que tenemos", "que tengo",
    "solicitar", "tramitar", "registrar", "requisitos", "procedimiento interno",
    "politica interna", "reglamento interno",
    "datos privados", "datos personales", "informacion confidencial", "datos confidenciales",
    "registros medicos", "historial medico", "tabuladores",
    "contrasena de", "contrasena del", "credenciales de", "credencial de",
    "codigo de empleado", "codigo del empleado", "numero de empleado", "id de empleado",
    "cifra exacta", "importe exacto",
    "entrego la incapacidad", "entregar la incapacidad", "reportar la incapacidad",
)
_PERSONAL_OR_CURRENT_REFERENCE = re.compile(
    r"\b(?:mi|mis|me|nos|nuestro|nuestra|nuestros|nuestras|recibo|tengo|tenemos|"
    r"hoy|actualmente|vigente|actual|cuantos|cuantas|importe)\b"
)
_SPECIFIC_PEOPLE_REFERENCE = re.compile(
    r"\b(?:salarios?|sueldos?|nominas?|expedientes?) (?:de|del) "
    r"(?!(?:un|una|cualquier)\b)\S"
)
_BENEFIT_AMOUNT = re.compile(r"\b(?:cuanto|cuanta|cuantos|cuantas|cantidad|monto|importe)\b")
_BENEFIT_NAMES = (
    "apoyo", "apoyos", "prestacion", "prestaciones", "vacaciones", "aguinaldo",
    "prima vacacional", "bono", "bonos", "casamiento", "fondo de ahorro",
)
_AMBIGUOUS_FOLLOWUP = re.compile(
    r"(?:y |pero )?(?:en ese caso|en esa situacion|entonces|eso|que pasa entonces|que hago ahora)"
)

_LONG_QUERY_CHARS = 320
_MANY_QUESTIONS = 2
_SUMMARY_DEEP_CHUNKS = 4
_SUMMARY_DEEP_CHARS = 10_000


def _normalized(text: str) -> str:
    """Minusculas sin diacriticos para clasificacion estable en espanol."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _complete_request(text: str) -> str:
    text = _normalized(text).strip(" ¿?¡!.,;:")
    # Una conjuncion no convierte un concepto independiente en seguimiento.
    return re.sub(r"^(?:y|por cierto|cambiando de tema|otra pregunta)[, :¿]+", "", text).strip(" ¿?¡!.,;:")


def _contains_phrase(text: str, markers: tuple[str, ...]) -> bool:
    """Busca frases completas; evita ``sintesis`` dentro de ``fotosintesis``."""
    return any(
        re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", text) is not None
        for marker in markers
    )


@dataclass(frozen=True, slots=True)
class GenerationProfile:
    """Presupuesto de inferencia efectivo para una llamada local."""

    num_ctx: int
    max_tokens: int
    temperature: float


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Modelo elegido y por que."""

    choice: ModelChoice
    model_name: str
    intent: Intent
    signals: tuple[str, ...] = field(default_factory=tuple)
    decisive_signals: tuple[str, ...] = field(default_factory=tuple)
    weak_signals: tuple[str, ...] = field(default_factory=tuple)

    def as_audit_dict(self) -> dict[str, object]:
        return {
            "selected_model": self.model_name,
            "model_route": str(self.choice),
            "intent": str(self.intent),
            "routing_signals": list(self.signals),
            "routing_decisive_signals": list(self.decisive_signals),
            "routing_weak_signals": list(self.weak_signals),
        }


class ModelPolicy:
    """Decide intencion y modelo. Sin estado: facil de probar unitariamente."""

    def __init__(self) -> None:
        settings = get_settings()
        self.fast_model = settings.ollama_fast_model
        self.deep_model = settings.ollama_deep_model
        self.embedding_model = settings.ollama_embedding_model

    @staticmethod
    def requires_internal_evidence(question: str) -> bool:
        """Guardia compartida: una referencia interna/personal no admite fallback general."""
        text = _normalized(question.strip())
        return bool(
            _contains_phrase(text, _INTERNAL_REFERENCES)
            or _PERSONAL_OR_CURRENT_REFERENCE.search(text)
            or _SPECIFIC_PEOPLE_REFERENCE.search(text)
            or (_BENEFIT_AMOUNT.search(text) and _contains_phrase(text, _BENEFIT_NAMES))
            or _AMBIGUOUS_FOLLOWUP.fullmatch(text.strip(" ¿?¡!.,;:"))
            or _NAMED_BENEFIT_REFERENCE.search(text)
            or _DEFINITE_BENEFIT_REFERENCE.search(text)
            or _CONTEXT_REFERENCE.search(text)
        )

    @staticmethod
    def is_benefit_reference(question: str) -> bool:
        """Referencia que puede necesitar precisar el nombre o documento del plan."""
        return bool(_DEFINITE_BENEFIT_REFERENCE.search(_normalized(question)))

    def contextual_reference(
        self, question: str, *, prior_questions: tuple[str, ...] = (),
        documented_indices: frozenset[int] = frozenset(),
    ) -> str:
        """Solo recibe preguntas del historial ya autorizado, nunca hechos como prueba.

        Mantiene el antecedente de seguimientos consecutivos, pero una pregunta
        independiente reemplaza el tema. No busca coincidencias en temas antiguos.
        """
        previous = ""
        documented = False
        for index, turn in enumerate(prior_questions):
            if self.classify_intent(turn) in (Intent.CONVERSATIONAL, Intent.IDENTITY, Intent.CAPABILITIES):
                continue
            follows = self._uses_previous_topic(turn, previous, documented=documented)
            previous = f"{previous}\n{turn}"[-1200:] if follows else turn
            documented = index in documented_indices or (follows and documented)
        return previous if self._uses_previous_topic(question, previous, documented=documented) else ""

    def _uses_previous_topic(self, question: str, previous: str, *, documented: bool = False) -> bool:
        if not previous or (not documented and self.classify_intent(previous) not in (
            Intent.DOCUMENTAL, Intent.DOCUMENT_SUMMARY, Intent.STRUCTURED, Intent.MIXED,
        )):
            return False
        text = _complete_request(question)
        # Una repeticion no necesita su propio texto como antecedente. Tampoco
        # una pregunta que ya identifica una fuente, fecha o beneficio distinto.
        if text in {_complete_request(turn) for turn in previous.splitlines()}:
            return False
        if re.match(r"^(?:por cierto|cambiando de tema|otra pregunta)\b", _normalized(question).lstrip("¿ ")):
            return False
        if re.search(r"\b(?:segun|de acuerdo con|que dice|documento|archivo|reglamento|manual)\b", text):
            return False
        if _NAMED_BENEFIT_REFERENCE.search(text):
            return False
        # El articulo definido no demuestra elipsis: 'el plan de ahorro' tiene
        # tema propio; 'ese plan' y 'sus requisitos' si necesitan antecedente.
        if re.search(
            r"\b(?:el|la|los|las|del|al) (?:plan(?:es)?|programas?|beneficios?|"
            r"prestaciones?|apoyos?|seguros?|fondos?|esquemas?)\s+(?:de|del|para)\s+\w+", text,
        ):
            return False
        if _CONTEXT_REFERENCE.search(text):
            return True
        if re.fullmatch(r"(?:y )?(?:que es|como funciona) (?:el|la) (?:plan|beneficio|prestacion|programa)", text):
            return True
        # Una explicacion completa prevalece sobre el prefijo "y".
        if _GENERAL_REQUEST.fullmatch(text):
            return False
        return bool(_ELLIPTICAL_FOLLOWUP.fullmatch(text))

    # -------------------------------------------------------------- intencion
    def classify_intent(self, question: str, *, previous_question: str = "") -> Intent:
        """Clasificacion heuristica previa al router.

        Se hace en codigo y no en el LLM porque una clasificacion determinista es
        auditable, no consume un modelo y no es manipulable por prompt injection.
        La etiqueta general permite una explicacion independiente si no hay
        evidencia pertinente. El orquestador consulta primero el alcance
        autorizado; identidad, capacidades y charla pura son las excepciones.
        """
        text = _normalized(question.strip())
        if not text:
            return Intent.CONVERSATIONAL

        if _CAPABILITY_REQUEST.fullmatch(text.strip(" ¿?¡!.,;:")):
            return Intent.CAPABILITIES
        identity_request = re.sub(
            r"^(?:hola|buenos dias|buenas tardes|buenas noches)[, :¿]+", "", _complete_request(question),
        )
        if identity_request in _IDENTITY_MARKERS:
            return Intent.IDENTITY

        has_structured = any(marker in text for marker in _STRUCTURED_MARKERS)
        has_documental = _contains_phrase(text, _DOCUMENTAL_MARKERS)

        if has_structured and has_documental:
            return Intent.MIXED
        if has_structured:
            return Intent.STRUCTURED
        # Un saludo inicial nunca convierte el resto de la pregunta en charla.
        complete_request = _complete_request(question)
        if complete_request in _CONVERSATIONAL_MARKERS:
            return Intent.CONVERSATIONAL
        if (
            _GENERAL_REQUEST.fullmatch(complete_request)
            and not self.requires_internal_evidence(question)
            and not self._uses_previous_topic(question, previous_question)
        ):
            return Intent.GENERAL
        if _contains_phrase(text, _SUMMARY_MARKERS):
            return Intent.DOCUMENT_SUMMARY
        return Intent.DOCUMENTAL

    # ----------------------------------------------------------------- router
    def route(
        self,
        question: str,
        *,
        intent: Intent | None = None,
        categories_in_scope: int = 0,
        multi_tool: bool = False,
        low_retrieval_confidence: bool = False,
        verifier_requested_deep: bool = False,
        evidence_count: int = 0,
        evidence_chars: int = 0,
    ) -> RoutingDecision:
        """Gemma en ambos perfiles; la complejidad decide el presupuesto."""
        resolved_intent = intent or self.classify_intent(question)
        text = _normalized(question.strip())
        decisive: list[str] = []
        weak: list[str] = []

        if _contains_phrase(text, _DEEP_MARKERS):
            decisive.append("deep_marker_in_query")
        if _contains_phrase(text, _COMPARISON_MARKERS) or _COMPARISON_REQUEST.search(text):
            decisive.append("explicit_comparison")
        if len(question) >= _LONG_QUERY_CHARS:
            weak.append("long_query")
        if question.count("?") > _MANY_QUESTIONS:
            weak.append("multiple_questions")
        if categories_in_scope >= 2 and resolved_intent is not Intent.CONVERSATIONAL:
            weak.append("multiple_authorized_categories")
        if multi_tool:
            weak.append("multi_tool_flow")
        if low_retrieval_confidence:
            weak.append("low_retrieval_confidence")
        if verifier_requested_deep:
            weak.append("verifier_requested_deep")
        if resolved_intent is Intent.MIXED:
            weak.append("mixed_intent")
        if resolved_intent is Intent.DOCUMENT_SUMMARY and (
            evidence_count > _SUMMARY_DEEP_CHUNKS or evidence_chars > _SUMMARY_DEEP_CHARS
        ):
            decisive.append("large_document_summary")

        if decisive:
            return RoutingDecision(
                choice=ModelChoice.DEEP,
                model_name=self.deep_model,
                intent=resolved_intent,
                signals=tuple(decisive + weak),
                decisive_signals=tuple(decisive),
                weak_signals=tuple(weak),
            )
        return RoutingDecision(
            choice=ModelChoice.FAST,
            model_name=self.fast_model,
            intent=resolved_intent,
            signals=("fast_path_sufficient", *weak),
            weak_signals=tuple(weak),
        )

    def model_for(self, choice: ModelChoice) -> str:
        return self.deep_model if choice is ModelChoice.DEEP else self.fast_model

    def fallback_for(self, model_name: str) -> str:
        """Alternativo configurado; si es identico, el agente no repite la llamada."""
        return self.fast_model if model_name == self.deep_model else self.deep_model

    def resolve_choice(self, model_name: str, choice: ModelChoice | None = None) -> ModelChoice:
        """Separa perfil de ejecucion del identificador servido por el runtime.

        Las rutas internas transmiten ``choice``. Para llamadas anteriores que
        solo incluyen nombre, se infiere el perfil; si FAST y DEEP comparten
        nombre, el default es FAST. Elegir DEEP requiere entonces ser explicito.
        """
        if choice is not None:
            return ModelChoice(choice)
        if model_name == self.deep_model and model_name != self.fast_model:
            return ModelChoice.DEEP
        return ModelChoice.FAST

    def generation_profile(
        self,
        model_name: str,
        *,
        intent: Intent,
        retry: bool = False,
        choice: ModelChoice | None = None,
    ) -> GenerationProfile:
        """Aprovecha cada modelo con un presupuesto apropiado y acotado.

        Los valores configurados son techos, no reservas obligatorias. Con una
        misma ventana, una comparativa DEEP no debe quitar contexto a las fuentes
        para reservar una salida mayor. Los resumenes si conservan esa reserva;
        el agente los divide en lotes cuando sea necesario.
        """
        settings = get_settings()
        deep = self.resolve_choice(model_name, choice) is ModelChoice.DEEP
        if intent in (Intent.GENERAL, Intent.CONVERSATIONAL):
            temperature = settings.llm_general_temperature
        elif intent is Intent.DOCUMENT_SUMMARY:
            temperature = settings.llm_summary_temperature
        else:
            temperature = settings.llm_temperature
        if retry:
            temperature = settings.llm_retry_temperature
        num_ctx = settings.ollama_deep_num_ctx if deep else settings.ollama_fast_num_ctx
        max_tokens = settings.ollama_deep_max_tokens if deep else settings.ollama_fast_max_tokens
        if deep and intent in (Intent.DOCUMENTAL, Intent.STRUCTURED, Intent.MIXED):
            # Solo una ventana mayor puede financiar mas salida sin desplazar
            # evidencia respecto de FAST. Nunca aumentar contexto sin medicion.
            extra_context = max(0, num_ctx - settings.ollama_fast_num_ctx)
            max_tokens = min(max_tokens, settings.ollama_fast_max_tokens + extra_context)
        return GenerationProfile(
            num_ctx=num_ctx,
            max_tokens=max_tokens,
            temperature=temperature,
        )


#: Expresion usada por las pruebas para comprobar que ningun modelo cloud se cuela.
FORBIDDEN_MODEL_PATTERN = re.compile(r"(gpt-|claude-|gemini-|mistral-api|openai|azure-openai)", re.IGNORECASE)
