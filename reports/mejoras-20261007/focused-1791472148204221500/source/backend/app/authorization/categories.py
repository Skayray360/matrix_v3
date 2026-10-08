# Creado por Aldo Garcia.
"""Registro de categorias documentales y su politica declarativa.

El arbol oficial separa conocimiento ``general`` de repositorios
``especializadas/<dominio>``. Las carpetas de categoria del formato anterior se
siguen aceptando para que una actualizacion no borre ni reclasifique documentos
existentes sin intervencion del operador.

Las categorias iniciales son **semillas**, no una lista cerrada: cuando el
servicio de ingesta encuentra un dominio nuevo bajo ``especializadas`` (o una
carpeta legacy nueva) lo registra automaticamente y le aplica
``deny-by-default`` hasta que exista una regla explicita.

El archivo ``config/authorization/categories.yaml`` es declarativo y validado por
esquema: nunca se evalua como codigo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import RLock

import yaml
from pydantic import BaseModel, Field, field_validator

from app.common.errors import ConfigurationError
from app.common.logging import get_logger
from app.config import get_settings

logger = get_logger(__name__)

#: Un nombre de categoria es tambien un nombre de carpeta: se restringe a un slug
#: seguro para cortar de raiz cualquier intento de path traversal.
CATEGORY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")

#: Contenedores reservados del arbol documental oficial. No son equivalentes:
#: ``general`` es una categoria explicita, mientras que ``especializadas`` solo
#: agrupa categorias de dominio y nunca se indexa como categoria por si misma.
GENERAL_CATEGORY = "general"
SPECIALIZED_CONTAINER = "especializadas"

SEED_CATEGORIES: tuple[str, ...] = (
    GENERAL_CATEGORY,
    "prestaciones",
    "nomina",
    "administracion_personal",
    "nomina_general",
    "nomina_confidencial",
    "compensaciones",
    "talento",
    "reclutamiento",
    "capacitacion",
    "relaciones_laborales",
    "salud_ambiental",
    "matrix_rh_ia",
)


class CategoryPolicy(BaseModel):
    """Politica declarativa de una categoria."""

    name: str
    description: str = ""
    sensitivity: str = Field(default="internal")
    #: Si es False, ni siquiera el wildcard de negocio concede acceso: hace falta
    #: una concesion nominal. Sirve para categorias especialmente sensibles.
    wildcard_eligible: bool = True
    #: Grupos de Entra ID que, al mapearse, conceden lectura de la categoria.
    allowed_groups: list[str] = Field(default_factory=list)
    source_owner: str = "rh"

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not CATEGORY_NAME_RE.match(value):
            raise ValueError(
                f"Nombre de categoria invalido: {value!r}. Solo minusculas, digitos, '_' y '-'."
            )
        return value

    @field_validator("sensitivity")
    @classmethod
    def _validate_sensitivity(cls, value: str) -> str:
        allowed = {"public", "internal", "confidential", "restricted"}
        if value not in allowed:
            raise ValueError(f"sensitivity invalida: {value}. Valores: {sorted(allowed)}")
        return value


class CategoryPolicyFile(BaseModel):
    """Esquema del archivo ``categories.yaml``."""

    version: int = 1
    #: Comportamiento para categorias descubiertas y no declaradas.
    default_sensitivity: str = "internal"
    default_wildcard_eligible: bool = False
    categories: list[CategoryPolicy] = Field(default_factory=list)


@dataclass(frozen=True)
class CategoryRegistry:
    """Vista consolidada de las categorias conocidas por el sistema."""

    policies: dict[str, CategoryPolicy]
    default_wildcard_eligible: bool
    default_sensitivity: str

    def known(self) -> frozenset[str]:
        return frozenset(self.policies)

    def get(self, name: str) -> CategoryPolicy:
        """Devuelve la politica declarada o una implicita deny-friendly.

        Una categoria no declarada NO obtiene privilegios por conveniencia: se
        crea con los valores por defecto del archivo y su acceso sigue
        dependiendo de que exista una concesion (nominal o wildcard).
        """
        existing = self.policies.get(name)
        if existing is not None:
            return existing
        return CategoryPolicy(
            name=name,
            description="Categoria descubierta automaticamente, sin politica declarada.",
            sensitivity=self.default_sensitivity,
            wildcard_eligible=self.default_wildcard_eligible,
        )

    def is_wildcard_eligible(self, name: str) -> bool:
        return self.get(name).wildcard_eligible


def validate_category_name(name: str) -> str:
    """Valida y normaliza un nombre de categoria proveniente del exterior."""
    normalized = (name or "").strip().lower()
    if not CATEGORY_NAME_RE.match(normalized):
        raise ConfigurationError(f"Nombre de categoria invalido: {name!r}")
    return normalized


def load_category_policy_file(path: Path | None = None) -> CategoryPolicyFile:
    """Carga y valida el YAML de politicas de categoria."""
    settings = get_settings()
    target = path or settings.authorization_policy_path
    if not target.exists():
        logger.warning("authorization.category_policy_missing", extra={"policy_path": str(target)})
        return CategoryPolicyFile()
    try:
        # safe_load nunca instancia objetos Python arbitrarios.
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        return CategoryPolicyFile.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(
            f"El archivo de politicas de categoria es invalido: {target.name}", detail=str(exc)
        ) from exc


def discover_filesystem_categories(knowledge_root: Path | None = None) -> list[str]:
    """Descubre categorias del arbol oficial y del formato anterior.

    Reglas:

    * la existencia de ``general`` declara la categoria explicita ``general``;
    * cada carpeta ``especializadas/<dominio>`` declara ``<dominio>``;
    * una carpeta legacy ``<categoria>`` conserva esa categoria;
    * ``especializadas`` nunca se convierte en una categoria implicita.

    Se ignoran carpetas ocultas y nombres que no cumplen el slug seguro, para no
    registrar como categoria algo que despues no se podria resolver a una ruta
    controlada.
    """
    root = knowledge_root or get_settings().knowledge_root_path
    if not root.is_dir():
        return []
    found: set[str] = set()
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        name = entry.name.lower()
        if name == GENERAL_CATEGORY:
            found.add(GENERAL_CATEGORY)
            continue
        if name == SPECIALIZED_CONTAINER:
            for domain in sorted(entry.iterdir()):
                if not domain.is_dir() or domain.name.startswith("."):
                    continue
                domain_name = domain.name.lower()
                if domain_name in (GENERAL_CATEGORY, SPECIALIZED_CONTAINER):
                    logger.warning(
                        "ingestion.reserved_domain_rejected", extra={"folder": domain.name}
                    )
                elif CATEGORY_NAME_RE.match(domain_name):
                    found.add(domain_name)
                else:
                    logger.warning(
                        "ingestion.category_name_rejected", extra={"folder": domain.name}
                    )
            continue
        if CATEGORY_NAME_RE.match(name):
            found.add(name)
        else:
            logger.warning("ingestion.category_name_rejected", extra={"folder": entry.name})
    return sorted(found)


def build_registry(*, extra_categories: list[str] | None = None) -> CategoryRegistry:
    """Combina el YAML declarado, las semillas y lo descubierto en disco."""
    policy_file = load_category_policy_file()
    policies: dict[str, CategoryPolicy] = {p.name: p for p in policy_file.categories}

    discovered = set(discover_filesystem_categories()) | set(SEED_CATEGORIES)
    discovered.update(extra_categories or [])

    for name in sorted(discovered):
        if name not in policies:
            policies[name] = CategoryPolicy(
                name=name,
                description="Categoria detectada automaticamente (deny-by-default).",
                sensitivity=policy_file.default_sensitivity,
                wildcard_eligible=policy_file.default_wildcard_eligible,
            )
    return CategoryRegistry(
        policies=policies,
        default_wildcard_eligible=policy_file.default_wildcard_eligible,
        default_sensitivity=policy_file.default_sensitivity,
    )


_registry_lock = RLock()


@lru_cache(maxsize=1)
def _cached_registry() -> CategoryRegistry:
    return build_registry()


def get_registry() -> CategoryRegistry:
    """Devuelve una version consistente; no lee durante una recarga en curso."""
    with _registry_lock:
        return _cached_registry()


def refresh_registry() -> CategoryRegistry:
    """Publica una nueva version para todos los consumidores existentes.

    Si la nueva configuracion es invalida, no se conserva la version permisiva:
    la cache queda vacia y las decisiones siguientes fallan cerrado al cargar.
    """
    with _registry_lock:
        _cached_registry.cache_clear()
        return _cached_registry()
