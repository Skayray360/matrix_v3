# Creado por Aldo Garcia.
"""Especificacion publica del generador; embeddings conservan su propio contrato."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from app.common.errors import ConfigurationError
from app.config import Settings

if TYPE_CHECKING:
    from app.llm.provider import ModelClient


@dataclass(frozen=True, slots=True)
class GeneratorConfiguration:
    profile: Literal["fast", "deep"]
    provider: str
    model: str
    expected_digest: str
    context_tokens: int
    max_output_tokens: int
    timeout_seconds: float
    thinking_policy: str

    @classmethod
    def from_settings(cls, settings: Settings, profile: Literal["fast", "deep"]):
        return cls(
            profile=profile,
            provider=settings.llm_deep_provider if profile == "deep" else settings.llm_provider,
            model=getattr(settings, f"ollama_{profile}_model"),
            expected_digest=getattr(settings, f"llm_{profile}_digest"),
            context_tokens=getattr(settings, f"ollama_{profile}_num_ctx"),
            max_output_tokens=getattr(settings, f"ollama_{profile}_max_tokens"),
            timeout_seconds=getattr(settings, f"llm_{profile}_timeout_seconds"),
            thinking_policy=getattr(settings, f"llm_{profile}_thinking"),
        )


def validate_generator(client: ModelClient, *, profile: Literal["fast", "deep"]) -> dict:
    """Sonda de metadata, sin inferir ni descargar; falla si no acredita el contrato."""
    spec = GeneratorConfiguration.from_settings(client.settings, profile)
    if spec.provider not in {"ollama", "openai_compatible"}:
        raise ConfigurationError("El cambio de generador requiere un proveedor local compatible.")
    inventory = (client._get("/api/tags") if spec.provider == "ollama" else None)
    if inventory is not None:
        rows = inventory.get("models")
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            raise ConfigurationError("El inventario local no es valido.")
        match = next((item for item in rows if item.get("name", item.get("model")) == spec.model), None)
        if match is None:
            raise ConfigurationError(f"El generador configurado {spec.model} no esta instalado en su runtime.")
        digest = client._canonical_digest(str(match.get("digest", "")))
        metadata = client._request_json(
            "POST", client.base_url + "/api/show", payload={"model": spec.model}, headers=None,
            remaining=client._remaining(10), max_bytes=4_194_304,
        )
    else:
        local = client._compatible_inventory()
        if not local.has(spec.model):
            raise ConfigurationError(f"El generador configurado {spec.model} no esta instalado en su runtime.")
        digest = dict(local.digests).get(spec.model, "")
        # /models no normaliza capacidades/contexto en todos los runtimes;
        # una prueba sintetica de texto/JSON acredita el contrato en model_change.
        metadata = {}
    if spec.expected_digest and digest != spec.expected_digest:
        raise ConfigurationError("El digest observado del generador difiere del configurado.")
    capabilities = metadata.get("capabilities")
    if spec.provider == "ollama":
        if not isinstance(capabilities, list) or "completion" not in capabilities:
            raise ConfigurationError("El runtime no acredita capacidad completion del generador.")
        if spec.thinking_policy == "enabled" and "thinking" not in capabilities:
            raise ConfigurationError("Se solicito thinking pero el generador no acredita esa capacidad.")
    model_info = metadata.get("model_info")
    model_info = model_info if isinstance(model_info, dict) else {}
    context_limits = [value for key, value in model_info.items()
                      if key.endswith(".context_length") and type(value) is int and value > 0]
    if context_limits and spec.context_tokens > min(context_limits):
        raise ConfigurationError("El contexto configurado supera el limite declarado del generador.")
    if spec.max_output_tokens >= spec.context_tokens:
        raise ConfigurationError("La salida configurada debe dejar espacio para entrada en el contexto.")
    return {
        **asdict(spec), "observed_digest": digest or None,
        "mutable_tag": ":" not in spec.model or spec.model.endswith(":latest"),
        "capabilities": sorted({item for item in capabilities if isinstance(item, str)
                                and item in {"completion", "thinking", "tools", "vision"}})
                        if isinstance(capabilities, list) else None,
        "declared_context_limit": min(context_limits) if context_limits else None,
        "text_and_json_contract": "not_tested",
    }
