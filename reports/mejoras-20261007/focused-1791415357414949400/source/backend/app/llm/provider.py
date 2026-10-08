# Creado por Aldo Garcia.
"""Contrato de inferencia y adapters HTTP. El negocio no construye payloads de proveedor."""

from __future__ import annotations

import json
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlparse

import httpx
from jsonschema import Draft202012Validator

from app.common.errors import ConfigurationError, EmbeddingDimensionMismatchError
from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
from app.common.logging import get_logger
from app.config import get_settings
from app.llm.ollama_client import ChatResult, ModelInventory, OllamaClient, _metric
from app.security.admission import admission

_deadline: ContextVar[float | None] = ContextVar("matrix_inference_deadline", default=None)
_stage_deadline: ContextVar[float | None] = ContextVar("matrix_generation_stage_deadline", default=None)
_stage_profile: ContextVar[str | None] = ContextVar("matrix_generation_stage_profile", default=None)
logger = get_logger(__name__)


def _time_limit_kind() -> InferenceFailureKind:
    """Un intento agotado conserva el presupuesto de otras etapas de la consulta."""
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        return InferenceFailureKind.DEADLINE
    return InferenceFailureKind.TIMEOUT


def _transport_failure_kind(exc: Exception) -> InferenceFailureKind:
    if isinstance(exc, httpx.TimeoutException):
        return InferenceFailureKind.TIMEOUT
    if isinstance(exc, httpx.HTTPStatusError):
        return InferenceFailureKind.HTTP
    if isinstance(exc, httpx.HTTPError):
        return InferenceFailureKind.TRANSPORT
    return InferenceFailureKind.FORMAT


@contextmanager
def inference_deadline():
    configured = time.monotonic() + get_settings().llm_request_deadline_seconds
    existing = _deadline.get()
    token = _deadline.set(min(configured, existing) if existing is not None else configured)
    try:
        yield
    finally:
        _deadline.reset(token)


@contextmanager
def _generation_stage(seconds: float, profile: str):
    """Una etapa no renueva el presupuesto global ni el de una etapa externa."""
    configured = time.monotonic() + seconds
    existing = _stage_deadline.get()
    token = _stage_deadline.set(min(configured, existing) if existing is not None else configured)
    profile_token = _stage_profile.set(profile)
    try:
        yield
    finally:
        _stage_profile.reset(profile_token)
        _stage_deadline.reset(token)


class InferenceClient(Protocol):
    def chat(
        self, *, model: str, messages: list[dict[str, str]], temperature: float | None = None,
        num_ctx: int | None = None, max_tokens: int | None = None, stop: list[str] | None = None,
        response_schema: dict[str, Any] | None = None, think: bool | None = None, top_k: int | None = None,
        execution_profile: Literal["fast", "deep"] | None = None,
    ) -> ChatResult: ...
    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]: ...
    def embed_one(self, text: str, *, model: str | None = None) -> list[float]: ...


def vertex_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Subset explicito de responseSchema. No se eliminan restricciones en silencio.

    La validacion completa de JSON Schema ocurre otra vez al recibir la respuesta.
    """
    definitions = schema.get("$defs", {})

    def convert(node: dict[str, Any], depth: int = 0) -> dict[str, Any]:
        if depth > 24:
            raise ConfigurationError("El schema es recursivo o demasiado profundo.")
        if "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/$defs/") or ref[8:] not in definitions:
                raise ConfigurationError("Solo se admiten referencias locales en el schema.")
            return convert(definitions[ref[8:]], depth + 1)
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$defs", "title", "default", "additionalProperties"):
                # Vertex no soporta estos campos; jsonschema los exige en la salida.
                continue
            if key == "type":
                if value == "null":
                    out["nullable"] = True
                else:
                    out[key] = value.upper()
            elif key == "properties":
                out[key] = {name: convert(child, depth + 1) for name, child in value.items()}
            elif key == "items":
                out[key] = convert(value, depth + 1)
            elif key == "anyOf":
                children = [child for child in value if child.get("type") != "null"]
                if len(children) != len(value):
                    out["nullable"] = True
                if len(children) == 1:
                    out.update(convert(children[0], depth + 1))
                else:
                    out[key] = [convert(child, depth + 1) for child in children]
            elif key in ("description", "enum", "required", "format", "minimum", "maximum", "minItems", "maxItems"):
                out[key] = value
            else:
                raise ConfigurationError(f"responseSchema no admite la restriccion {key}.")
        return out

    return convert(schema)


class ModelClient(OllamaClient):
    """Fachada compatible con el cliente previo; modelos y protocolos vienen de .env.

    API compatible: /v1/chat/completions y /v1/embeddings (vLLM/llama.cpp).
    Vertex: /publishers/google/models/{modelo}:generateContent, opt-in cloud.
    """

    def __init__(self, *, client: httpx.Client | None = None, **kwargs: Any) -> None:
        self.settings = get_settings()
        s = self.settings
        providers = (s.llm_provider, s.llm_deep_provider, s.llm_embedding_provider)
        urls = []
        if "ollama" in providers:
            urls.append(s.ollama_base_url)
        if "openai_compatible" in providers:
            urls.append(s.llm_api_base_url)
        if "vertex" in providers:
            if s.llm_local_only:
                raise ConfigurationError("Vertex requiere LLM_LOCAL_ONLY=false: envia datos a cloud.")
            if not s.llm_vertex_base_url.startswith("https://"):
                raise ConfigurationError("LLM_VERTEX_BASE_URL debe ser HTTPS.")
        hosts = {host.strip().casefold() for host in s.llm_local_hosts.split(",")}
        for url in urls:
            parsed = urlparse(url)
            if (
                parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            ):
                raise ConfigurationError("Endpoint de inferencia invalido.")
            if s.llm_local_only and parsed.hostname not in hosts:
                raise ConfigurationError("El endpoint no pertenece a LLM_LOCAL_HOSTS aprobado por TI.")
        if s.llm_local_only and any(
            "cloud" in m.casefold() for m in (s.ollama_fast_model, s.ollama_deep_model, s.ollama_embedding_model)
        ):
            raise ConfigurationError("Los modelos cloud de Ollama estan deshabilitados en modo local.")
        super().__init__(client=client, **kwargs)
        self._vertex_validated: dict[str, float] = {}

    @staticmethod
    def _canonical_digest(value: str) -> str:
        normalized = value.removeprefix("sha256:").lower()
        return normalized if re.fullmatch(r"[0-9a-f]{64}", normalized) else ""

    def _inventory_get(self, url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Las sondas de revision consumen el mismo presupuesto que la consulta."""
        remaining = min(10.0, self.settings.llm_request_deadline_seconds)
        deadline = _deadline.get()
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        stage_deadline = _stage_deadline.get()
        if stage_deadline is not None:
            remaining = min(remaining, stage_deadline - time.monotonic())
        if remaining <= 0:
            raise InferenceFailureError(_time_limit_kind(), "La consulta alcanzo su tiempo limite.")
        started = time.monotonic()
        try:
            with self._client.stream(
                "GET", url, headers=headers, timeout=httpx.Timeout(remaining, connect=min(10, remaining))
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if time.monotonic() - started > remaining:
                        raise InferenceFailureError(
                            _time_limit_kind(), "La consulta alcanzo su tiempo limite.",
                        )
                    if size > 4_194_304:
                        raise InferenceFailureError(InferenceFailureKind.RESPONSE_SIZE)
                    chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
            if time.monotonic() - started > remaining:
                raise InferenceFailureError(_time_limit_kind(), "La consulta alcanzo su tiempo limite.")
            if not isinstance(payload, dict):
                raise ValueError("inventario no valido")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise InferenceFailureError(
                _transport_failure_kind(exc),
                "No fue posible verificar el inventario local.",
            ) from exc

    def _get(self, path: str) -> dict[str, Any]:
        return self._inventory_get(f"{self.base_url}{path}")

    def _compatible_inventory(self) -> ModelInventory:
        """El runtime compatible debe exponer digest por modelo para fijar pesos.

        /models sin metadata digest sigue disponible en development/test sin pin;
        nunca acredita un pin de produccion usando solamente el nombre del modelo.
        """
        s = self.settings
        secret = s.llm_api_key.get_secret_value()
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        payload = self._inventory_get(s.llm_api_base_url.rstrip("/") + "/models", headers=headers)
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict) or not isinstance(entry.get("id"), str) for entry in entries
        ):
            raise ConfigurationError("El runtime compatible no reporto un inventario de modelos valido.")
        return ModelInventory(
            names=tuple(entry["id"] for entry in entries),
            digests=tuple(
                (entry["id"], self._canonical_digest(str(entry.get("digest", "")))) for entry in entries
            ),
        )

    def _require_model_pin(self, *, provider: str, model: str, expected: str) -> None:
        if not expected:
            return
        inventory = super().list_models() if provider == "ollama" else self._compatible_inventory()
        actual = self._canonical_digest(dict(inventory.digests).get(model, ""))
        if not actual or actual != expected:
            raise ConfigurationError("El runtime no acredita el digest fijado del modelo en .env.")

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Un intento por llamada: HTTP ambiguo no se reenvia automaticamente.
        deadline = _deadline.get()
        remaining = min(self.timeout, self.settings.llm_request_deadline_seconds)
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        stage_deadline = _stage_deadline.get()
        if stage_deadline is not None:
            remaining = min(remaining, stage_deadline - time.monotonic())
        if remaining <= 0:
            raise InferenceFailureError(_time_limit_kind(), "La consulta alcanzo su tiempo limite.")
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = {}
        if url.startswith(self.settings.llm_api_base_url.rstrip("/") + "/"):
            secret = self.settings.llm_api_key.get_secret_value()
            if secret:
                headers["Authorization"] = f"Bearer {secret}"
        elif self.settings.llm_vertex_base_url and url.startswith(self.settings.llm_vertex_base_url.rstrip("/") + "/"):
            # Se carga google-auth solo cuando TI habilita el adapter cloud.
            import google.auth
            from google.auth.transport.requests import Request

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            credentials.refresh(Request())
            headers["Authorization"] = f"Bearer {credentials.token}"
        with admission("inference", self.settings.inference_max_inflight):
            # La espera de un worker consume el mismo presupuesto de la consulta.
            if deadline is not None:
                remaining = min(remaining, deadline - time.monotonic())
            if stage_deadline is not None:
                remaining = min(remaining, stage_deadline - time.monotonic())
            if remaining <= 0:
                raise InferenceFailureError(_time_limit_kind(), "La consulta alcanzo su tiempo limite.")
            started = time.monotonic()
            try:
                with self._client.stream(
                    "POST",
                    url, json=payload, headers=headers, timeout=httpx.Timeout(remaining, connect=min(10, remaining))
                ) as response:
                    if response.status_code >= 400:
                        # No lee ni registra el cuerpo de error del proveedor.
                        logger.warning(
                            "llm.request_failed",
                            extra={
                                "failure_kind": "busy" if response.status_code in (429, 503) else "http",
                                "http_status": response.status_code,
                                "selected_model": payload.get("model"),
                                "execution_profile": _stage_profile.get(),
                            },
                        )
                        raise InferenceFailureError(
                            InferenceFailureKind.BUSY if response.status_code in (429, 503)
                            else InferenceFailureKind.HTTP,
                            http_status=response.status_code,
                        )
                    chunks: list[bytes] = []
                    size = 0
                    # HTTPX limita inactividad por fase. Comprobar cada bloque
                    # evita retener el cupo por un cuerpo que llega a goteo.
                    for chunk in response.iter_bytes():
                        if time.monotonic() - started > remaining:
                            raise InferenceFailureError(
                                _time_limit_kind(), "La consulta alcanzo su tiempo limite.",
                            )
                        size += len(chunk)
                        if size > 8_388_608:
                            raise InferenceFailureError(InferenceFailureKind.RESPONSE_SIZE)
                        chunks.append(chunk)
                    data = json.loads(b"".join(chunks))
                if time.monotonic() - started > remaining:
                    raise InferenceFailureError(
                        _time_limit_kind(), "La consulta alcanzo su tiempo limite.",
                    )
                if not isinstance(data, dict):
                    raise ValueError("respuesta no es objeto")
                return data
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "llm.request_failed",
                    extra={
                        "failure_kind": str(_transport_failure_kind(exc)),
                        "error_type": type(exc).__name__,
                        "selected_model": payload.get("model"),
                        "execution_profile": _stage_profile.get(),
                        "stage_budget_seconds": round(remaining, 3),
                    },
                )
                raise InferenceFailureError(
                    _transport_failure_kind(exc),
                    "Respuesta de inferencia no valida.",
                ) from exc

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        num_ctx: int | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        response_schema: dict[str, Any] | None = None,
        think: bool | None = None,
        top_k: int | None = None,
        execution_profile: Literal["fast", "deep"] | None = None,
    ) -> ChatResult:
        s = self.settings
        if execution_profile not in (None, "fast", "deep"):
            raise ConfigurationError("Perfil de inferencia no valido.")
        deep = execution_profile == "deep" if execution_profile else (
            model == s.ollama_deep_model and model != s.ollama_fast_model
        )
        stage_seconds = s.llm_deep_timeout_seconds if deep else s.llm_fast_timeout_seconds
        with _generation_stage(stage_seconds, "deep" if deep else "fast"):
            try:
                return self._chat(
                    model=model, messages=messages, temperature=temperature, num_ctx=num_ctx,
                    max_tokens=max_tokens, stop=stop, response_schema=response_schema,
                    think=think, top_k=top_k, execution_profile=execution_profile,
                )
            except InferenceFailureError as exc:
                logger.warning(
                    "llm.generation_failed",
                    extra={
                        "failure_kind": str(exc.failure_kind), "selected_model": model,
                        "execution_profile": "deep" if deep else "fast",
                        "http_status": exc.http_status,
                        **(exc.completion.log_fields() if exc.completion is not None else {}),
                    },
                )
                raise

    def _chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        num_ctx: int | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        response_schema: dict[str, Any] | None = None,
        think: bool | None = None,
        top_k: int | None = None,
        execution_profile: Literal["fast", "deep"] | None = None,
    ) -> ChatResult:
        s = self.settings
        if execution_profile not in (None, "fast", "deep"):
            raise ConfigurationError("Perfil de inferencia no valido.")
        if execution_profile is not None and model != (
            s.ollama_deep_model if execution_profile == "deep" else s.ollama_fast_model
        ):
            raise ConfigurationError("El modelo no corresponde al perfil de inferencia.")
        # Un mismo checkpoint puede atender ambos perfiles sin perder sus limites.
        deep = execution_profile == "deep" if execution_profile else (
            model == s.ollama_deep_model and model != s.ollama_fast_model
        )
        provider = s.llm_deep_provider if deep else s.llm_provider
        expected_digest = s.llm_deep_digest if deep else s.llm_fast_digest
        if expected_digest:
            configured_model = s.ollama_deep_model if deep else s.ollama_fast_model
            if model != configured_model or provider not in ("ollama", "openai_compatible"):
                raise ConfigurationError("La generacion requiere el modelo local fijado para su perfil.")
            # No se cachea por etiqueta: un pull durante la vida del proceso debe
            # detectarse antes de la siguiente generacion, sin esperar reinicio.
            self._require_model_pin(provider=provider, model=model, expected=expected_digest)
        thinking = s.llm_deep_thinking if deep else s.llm_fast_thinking
        if response_schema is not None and s.llm_structured_thinking != "default":
            thinking = s.llm_structured_thinking
        if think is None:
            if thinking == "auto":
                think = False if provider == "ollama" else None
            else:
                think = None if thinking == "default" else thinking == "enabled"
        if top_k is None:
            top_k = s.llm_deep_top_k if deep else s.llm_fast_top_k
        temperature = s.llm_temperature if temperature is None else temperature
        if response_schema is not None:
            Draft202012Validator.check_schema(response_schema)
        messages = [dict(message) for message in messages]
        if s.llm_system_prefix:
            messages.insert(0, {"role": "system", "content": s.llm_system_prefix})
        started = time.perf_counter()
        if provider == "ollama":
            result = self._ollama_chat(
                model=model,
                messages=messages,
                temperature=temperature,
                num_ctx=num_ctx,
                max_tokens=max_tokens,
                stop=stop,
                response_schema=response_schema,
                think=think,
                top_k=top_k,
            )
        elif provider == "openai_compatible":
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "temperature": temperature,
                "top_p": s.llm_top_p,
            }
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            if stop:
                payload["stop"] = stop
            if think is not None:
                payload["chat_template_kwargs"] = {"enable_thinking": think}
            if top_k is not None:
                payload["top_k"] = top_k
            if response_schema is not None:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "matrix_response", "schema": response_schema},
                }
            data = self._post(s.llm_api_base_url.rstrip("/") + "/chat/completions", payload)
            choices = data.get("choices") or []
            if (
                not isinstance(choices, list)
                or not choices
                or not isinstance(choices[0], dict)
                or choices[0].get("finish_reason") not in ("stop", None)
            ):
                raise InferenceFailureError(InferenceFailureKind.INCOMPLETE, "El modelo no completo la respuesta.")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise InferenceFailureError(InferenceFailureKind.FORMAT, "Respuesta de chat no valida.")
            content = message.get("content")
            # El parser del runtime separa reasoning/reasoning_content. Nunca
            # se usan como fallback de contenido final ni se persisten.
            raw_usage = data.get("usage")
            usage = raw_usage if isinstance(raw_usage, dict) else {}
            result = ChatResult(
                content=content or "", model=model, latency_ms=int((time.perf_counter() - started) * 1000),
                prompt_eval_count=_metric(usage, "prompt_tokens"),
                eval_count=_metric(usage, "completion_tokens"),
            )
        else:
            if think is not None or top_k is not None:
                raise ConfigurationError("Thinking/top_k por perfil requieren un adapter local compatible.")
            config: dict[str, Any] = {"temperature": temperature, "topP": s.llm_top_p}
            if max_tokens is not None:
                config["maxOutputTokens"] = max_tokens
            if stop:
                config["stopSequences"] = stop
            if response_schema is not None:
                config.update(responseMimeType="application/json", responseSchema=vertex_schema(response_schema))
            payload = {
                "contents": [
                    {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages
                    if m["role"] != "system"
                ],
                "generationConfig": config,
            }
            systems = [m["content"] for m in messages if m["role"] == "system"]
            if systems:
                payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(systems)}]}
            data = self._post(
                s.llm_vertex_base_url.rstrip("/") + "/" + quote(model, safe="") + ":generateContent", payload
            )
            candidates = data.get("candidates") or []
            if (
                not isinstance(candidates, list)
                or not candidates
                or not isinstance(candidates[0], dict)
                or candidates[0].get("finishReason") != "STOP"
            ):
                raise InferenceFailureError(InferenceFailureKind.INCOMPLETE, "Vertex rechazo o trunco la respuesta.")
            candidate_content = candidates[0].get("content")
            parts = candidate_content.get("parts") if isinstance(candidate_content, dict) else None
            if not isinstance(parts, list) or any(
                not isinstance(part, dict) or not isinstance(part.get("text", ""), str) for part in parts
            ):
                raise InferenceFailureError(InferenceFailureKind.FORMAT, "Vertex devolvio contenido no valido.")
            content = "".join(part.get("text", "") for part in parts if not part.get("thought"))
            result = ChatResult(content=content, model=model, latency_ms=int((time.perf_counter() - started) * 1000))
        if not isinstance(result.content, str):
            raise InferenceFailureError(InferenceFailureKind.FORMAT, "Respuesta de chat no valida.")
        if not result.content.strip():
            raise InferenceFailureError(InferenceFailureKind.EMPTY, "El modelo devolvio una respuesta vacia.")
        if re.search(
            r"</?think>|\[/?THINK\]|<\|channel\|?>|<channel\|>", result.content, re.IGNORECASE
        ):
            raise InferenceFailureError(
                InferenceFailureKind.REASONING, "El runtime no separo el razonamiento del contenido final.",
            )
        if response_schema is not None:
            try:
                value = json.loads(result.content)
                Draft202012Validator(response_schema).validate(value)
            except Exception as exc:
                raise InferenceFailureError(
                    InferenceFailureKind.SCHEMA, "La respuesta no cumple el schema solicitado.",
                ) from exc
        return result

    def _ollama_chat(self, **arguments: Any) -> ChatResult:
        """Regenera una sola salida terminada; conserva etapa, pin y evidencia.

        No concatena texto cortado, no reutiliza thinking, no aumenta el limite
        configurado y no repite fallos de transporte ni respuestas ambiguas.
        """
        started = time.perf_counter()
        try:
            return super().chat(**arguments)
        except InferenceFailureError as exc:
            if (
                not self.settings.llm_completion_retries
                or exc.failure_kind not in (InferenceFailureKind.INCOMPLETE, InferenceFailureKind.EMPTY)
                or exc.completion is None
                or not exc.completion.can_regenerate
            ):
                raise
            diagnostic = exc.completion
        # Un pull entre ambos intentos tambien debe invalidar el pin.
        profile = _stage_profile.get()
        expected = self.settings.llm_deep_digest if profile == "deep" else self.settings.llm_fast_digest
        if expected:
            self._require_model_pin(provider="ollama", model=arguments["model"], expected=expected)
        messages = [dict(message) for message in arguments["messages"]]
        instruction = (
            "Entrega solo una respuesta final completa y breve. Omite el razonamiento interno. "
            "Conserva las citas y condiciones necesarias; no inventes informacion que falte. "
            "Evita repetir evidencia e introducciones. Respeta el formato solicitado."
        )
        if arguments.get("response_schema") is not None:
            instruction += " Devuelve exclusivamente el JSON solicitado, sin explicaciones."
        if messages and messages[0]["role"] == "system":
            messages[0]["content"] += "\n\n" + instruction
        else:
            messages.insert(0, {"role": "system", "content": instruction})
        logger.warning(
            "llm.completion_regenerating",
            extra={"selected_model": arguments["model"], "execution_profile": profile,
                   **diagnostic.log_fields()},
        )
        result = super().chat(**{**arguments, "messages": messages, "think": False})
        logger.info(
            "llm.completion_recovered",
            extra={"selected_model": arguments["model"], "execution_profile": profile,
                   "generation_attempts": 2},
        )
        return replace(result, latency_ms=int((time.perf_counter() - started) * 1000), generation_attempts=2)

    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        if self.settings.llm_embedding_provider == "ollama":
            vectors = super().embed(texts, model=model)
        else:
            data = self._post(
                self.settings.llm_api_base_url.rstrip("/") + "/embeddings",
                {"model": model or self.embedding_model, "input": texts},
            )
            entries = data.get("data") or []
            if (
                not isinstance(entries, list)
                or any(
                    not isinstance(item, dict) or type(item.get("index")) is not int or "embedding" not in item
                    for item in entries
                )
                or sorted(item["index"] for item in entries) != list(range(len(texts)))
            ):
                raise EmbeddingDimensionMismatchError("Indices de embeddings invalidos.")
            vectors = [item["embedding"] for item in sorted(entries, key=lambda item: item["index"])]
        if len(vectors) != len(texts) or any(
            not isinstance(v, list)
            or len(v) != self.expected_dimension
            or any(isinstance(n, bool) or not isinstance(n, float | int) or not math.isfinite(n) for n in v)
            for v in vectors
        ):
            raise EmbeddingDimensionMismatchError()
        return vectors

    def embedding_revision(self) -> str:
        if self.settings.llm_embedding_provider == "ollama":
            # Digest real: una etiqueta latest que cambia invalida el indice.
            digest = dict(super().list_models().digests).get(self.embedding_model)
            if not digest:
                raise ConfigurationError("Ollama no reporto digest del modelo de embeddings.")
            if (
                self.settings.llm_embedding_digest
                and self._canonical_digest(digest) != self.settings.llm_embedding_digest
            ):
                raise ConfigurationError("El digest del modelo de embeddings no coincide con .env.")
            return digest
        revision = self.settings.llm_embedding_revision
        if not revision or revision == "unverified":
            raise ConfigurationError("LLM_EMBEDDING_REVISION es obligatoria para el runtime compatible.")
        return revision

    def probe_embedding_dimension(self, *, model: str | None = None) -> int:
        return len(self.embed(["matrix rh dimension probe"], model=model)[0])

    def ping(self) -> bool:
        try:
            self.list_models()
            return True
        except Exception:
            return False

    def list_models(self) -> ModelInventory:
        s = self.settings
        names: list[str] = []
        digests: list[tuple[str, str]] = []
        if "ollama" in (s.llm_provider, s.llm_deep_provider, s.llm_embedding_provider):
            inventory = super().list_models()
            names.extend(inventory.names)
            digests.extend(inventory.digests)
            for provider, model, expected in (
                (s.llm_provider, s.ollama_fast_model, s.llm_fast_digest),
                (s.llm_deep_provider, s.ollama_deep_model, s.llm_deep_digest),
                (s.llm_embedding_provider, s.ollama_embedding_model, s.llm_embedding_digest),
            ):
                if provider == "ollama" and expected and self._canonical_digest(
                    dict(inventory.digests).get(model, "")
                ) != expected:
                    raise ConfigurationError("El digest del modelo no coincide con .env.")
        if "openai_compatible" in (s.llm_provider, s.llm_deep_provider, s.llm_embedding_provider):
            inventory = self._compatible_inventory()
            names.extend(inventory.names)
            digests.extend(inventory.digests)
            for provider, model, expected in (
                (s.llm_provider, s.ollama_fast_model, s.llm_fast_digest),
                (s.llm_deep_provider, s.ollama_deep_model, s.llm_deep_digest),
                (s.llm_embedding_provider, s.ollama_embedding_model, s.llm_embedding_digest),
            ):
                if provider == "openai_compatible" and expected and dict(inventory.digests).get(model) != expected:
                    raise ConfigurationError("El runtime compatible no acredita el digest fijado en .env.")
        # Sonda sintetica solo con opt-in cloud; cache de cinco minutos por proceso.
        for provider, model in ((s.llm_provider, s.ollama_fast_model), (s.llm_deep_provider, s.ollama_deep_model)):
            if provider == "vertex":
                if self._vertex_validated.get(model, 0) < time.monotonic():
                    self.chat(
                        model=model,
                        messages=[{"role": "user", "content": "Reply only OK."}],
                        temperature=0,
                        max_tokens=128,
                    )
                    self._vertex_validated[model] = time.monotonic() + 300
                names.append(model)
        return ModelInventory(names=tuple(names), digests=tuple(digests))
