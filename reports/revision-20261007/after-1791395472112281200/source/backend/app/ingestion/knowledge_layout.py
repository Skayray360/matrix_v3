# Creado por Aldo Garcia.
"""Fuentes documentales explicitas, inventario y rutas portables del corpus.

La carpeta data no se recorre como una carpeta de documentos generica. Solo se
abren las carpetas declaradas en knowledge-layout.yaml y las raices oficiales.
La clasificacion documental nunca concede permisos de lectura.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.authorization.categories import (
    CATEGORY_NAME_RE,
    GENERAL_CATEGORY,
    SPECIALIZED_CONTAINER,
    load_category_policy_file,
)
from app.common.errors import ConfigurationError
from app.config import get_settings
from app.config.settings import PROJECT_ROOT
from app.ingestion.loaders import supported_extensions

_RESERVED_DATA_FOLDERS = frozenset({
    "knowledge", "synthetic_test_data", "uploads", "var", "config", "backend", "frontend",
})


def normalize_folder(value: str) -> str:
    """Compara alias Windows sin distinguir mayusculas ni acentos."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character)).strip()


class KnowledgeLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    data_aliases: dict[str, str | None] = Field(default_factory=dict)

    @field_validator("data_aliases")
    @classmethod
    def safe_aliases(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        normalized: dict[str, str | None] = {}
        namespaces: set[str] = set()
        for folder, category in value.items():
            key = normalize_folder(folder)
            namespace = _alias_namespace(key)
            if (
                not key or key in {".", ".."} or key.startswith(".")
                or any(character in folder for character in "/\\:\x00<>\"|?*")
                or any(ord(character) < 32 for character in folder)
                or key in _RESERVED_DATA_FOLDERS or key in normalized or namespace in namespaces
            ):
                raise ValueError("Alias documental duplicado, reservado o con ruta no permitida.")
            if category is not None and not CATEGORY_NAME_RE.fullmatch(category):
                raise ValueError("La categoria del alias debe ser un identificador declarado.")
            normalized[key] = category
            namespaces.add(namespace)
        return normalized


def _alias_namespace(folder: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "_", folder).strip("_")
    if not slug:
        raise ValueError("El alias documental no tiene un identificador portable.")
    return f"data-alias/{slug}"


class _UniqueSafeLoader(yaml.SafeLoader):
    """No se aceptan claves YAML duplicadas que oculten un mapeo anterior."""


def _unique_mapping(loader, node, deep=False):  # noqa: ANN001, ANN201
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError("La politica documental contiene una clave duplicada.")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_knowledge_layout(path: Path | None = None) -> KnowledgeLayout:
    target = path or PROJECT_ROOT / "config" / "knowledge-layout.yaml"
    if not target.is_file():
        return KnowledgeLayout()
    try:
        if target.stat().st_size > 64 * 1024:
            raise ValueError("La politica documental excede el limite.")
        # Hereda exclusivamente SafeLoader; agrega rechazo de claves repetidas.
        loader = _UniqueSafeLoader(target.read_text(encoding="utf-8-sig"))
        try:
            raw = loader.get_single_data() or {}
        finally:
            loader.dispose()
        return KnowledgeLayout.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - no publicar contenido de la configuracion
        raise ConfigurationError("config/knowledge-layout.yaml no es valido.", detail=type(exc).__name__) from exc


@dataclass(frozen=True)
class KnowledgeSource:
    source_id: str
    root: Path
    namespace: str
    category: str | None = None


@dataclass(frozen=True)
class KnowledgeFile:
    absolute_path: Path
    relative_path: str
    category: str
    source_id: str
    legacy_relative_paths: tuple[str, ...] = ()


@dataclass
class KnowledgeScan:
    sources: list[KnowledgeSource] = field(default_factory=list)
    files: list[KnowledgeFile] = field(default_factory=list)
    warnings: list[dict[str, object]] = field(default_factory=list)
    unavailable_sources: set[str] = field(default_factory=set)
    incomplete_sources: set[str] = field(default_factory=set)
    ignored_files: int = 0
    private_roots: tuple[Path, ...] = ()
    test_roots: tuple[Path, ...] = ()

    def as_dict(self) -> dict[str, object]:
        counts = Counter(item.source_id for item in self.files)
        pdfs = Counter(item.source_id for item in self.files if item.absolute_path.suffix.casefold() == ".pdf")
        categories = Counter(item.category for item in self.files)
        return {
            "roots": [
                {"source_id": source.source_id, "path": str(source.root), "category": source.category,
                 "available": source.source_id not in self.unavailable_sources,
                 "scan_complete": source.source_id not in self.incomplete_sources,
                 "indexable_files": counts[source.source_id], "pdf_files": pdfs[source.source_id]}
                for source in self.sources
            ],
            "indexable_files": len(self.files),
            "pdf_files": sum(pdfs.values()),
            "files_by_category": dict(sorted(categories.items())),
            "ignored_files": self.ignored_files,
            "warnings": self.warnings,
            "pdf_text_check": "El texto se valida durante la ingesta aislada; no hay OCR automatico.",
        }

    def warn(self, code: str, message: str, *, count: int = 1) -> None:
        for warning in self.warnings:
            if warning["code"] == code and warning["message"] == message:
                previous_count = warning["count"]
                warning["count"] = (previous_count if isinstance(previous_count, int) else 0) + count
                return
        self.warnings.append({"code": code, "message": message, "count": count})


def _has_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _safe_directory(path: Path) -> bool:
    return path.is_dir() and not _has_link(path) and all(not _has_link(parent) for parent in path.parents)


def _path_key(path: Path | str) -> str:
    key = str(path).replace("\\", "/").rstrip("/")
    return key.casefold() if os.name == "nt" else key


def category_from_path(relative_path: Path | str) -> str | None:
    """Clasifica un archivo dentro de una raiz oficial, sin interpretar alias."""
    raw = str(relative_path).replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return None
    parts = PurePosixPath(raw).parts
    if len(parts) < 2 or any(part == ".." for part in parts):
        return None
    first = normalize_folder(parts[0])
    if first == GENERAL_CATEGORY:
        return GENERAL_CATEGORY
    if first == SPECIALIZED_CONTAINER:
        if len(parts) < 3:
            return None
        domain = normalize_folder(parts[1])
        if domain in {GENERAL_CATEGORY, SPECIALIZED_CONTAINER}:
            return None
        return domain if CATEGORY_NAME_RE.fullmatch(domain) else None
    return first if CATEGORY_NAME_RE.fullmatch(first) else None


def scan_knowledge(
    *, knowledge_root: Path | None = None, project_root: Path | None = None,
    layout_path: Path | None = None, declared_categories: set[str] | None = None,
) -> KnowledgeScan:
    """Inventario de metadatos sin abrir parsers, Qdrant ni generar embeddings.

    La raiz original conserva sus identificadores. Los alias tienen un namespace
    estable, independiente de RAG_KNOWLEDGE_ROOT. Las rutas fisicas repetidas se
    leen una sola vez y las raices que contienen otras fuentes no las duplican.
    """
    project = Path(os.path.abspath(project_root or PROJECT_ROOT))
    primary = Path(os.path.abspath(knowledge_root or get_settings().knowledge_root_path))
    data_root = project / "data"
    canonical = data_root / "knowledge"
    layout = load_knowledge_layout(layout_path or project / "config" / "knowledge-layout.yaml")
    declared = declared_categories
    if declared is None:
        declared = {policy.name for policy in load_category_policy_file().categories}
    bad_targets = {value for value in layout.data_aliases.values() if value is not None and value not in declared}
    if bad_targets:
        raise ConfigurationError("knowledge-layout.yaml asigna carpetas a categorias no declaradas.")

    scan = KnowledgeScan(
        sources=[KnowledgeSource("knowledge", canonical, "")],
        private_roots=(project / "var" / "uploads", get_settings().upload_storage_path),
        test_roots=(project / "data" / "synthetic_test_data",),
    )
    if _path_key(primary) != _path_key(canonical) and not primary.resolve().is_relative_to(canonical.resolve()):
        scan.sources.append(KnowledgeSource("configured-knowledge", primary, "configured-knowledge"))
    actual_folders: dict[str, list[Path]] = {}
    if _safe_directory(data_root):
        for entry in sorted(data_root.iterdir()):
            if entry.is_dir() or _has_link(entry):
                actual_folders.setdefault(normalize_folder(entry.name), []).append(entry)
    for alias, category in layout.data_aliases.items():
        matches = actual_folders.get(alias, [])
        if category is None:
            if matches:
                scan.warn("unmapped_alias", f"Carpeta {alias}: sin categoria aprobada; no se indexa.")
            continue
        if len(matches) > 1:
            scan.warn(
                "ambiguous_alias", f"Carpeta {alias}: nombres equivalentes ambiguos; no se indexan.", count=len(matches)
            )
            continue
        root = matches[0] if matches else data_root / alias
        namespace = _alias_namespace(alias)
        scan.sources.append(KnowledgeSource(namespace, root, namespace, category))
    unknown = set(actual_folders) - set(layout.data_aliases) - _RESERVED_DATA_FOLDERS
    if unknown:
        scan.warn("unmapped_data_folders", "Hay carpetas de data sin mapeo aprobado; se omiten.", count=len(unknown))

    # Una raiz personalizada que coincide con un alias conserva la categoria
    # del mapeo aprobado, no se escanea dos veces ni pierde archivos directos.
    unique_sources: dict[str, KnowledgeSource] = {}
    for source in scan.sources:
        key = _path_key(source.root)
        previous = unique_sources.get(key)
        if previous is None or (source.category is not None and previous.category is None):
            unique_sources[key] = source
    scan.sources = list(unique_sources.values())

    seen: set[str] = set()
    for source in scan.sources:
        _scan_source(scan, source=source, declared=declared, seen=seen)
    return scan


def _scan_source(scan: KnowledgeScan, *, source: KnowledgeSource, declared: set[str], seen: set[str]) -> None:
    if any(source.root.resolve().is_relative_to(test_root.resolve()) for test_root in scan.test_roots):
        scan.unavailable_sources.add(source.source_id)
        scan.warn(
            "test_root_rejected", f"Fuente {source.source_id}: son fixtures sinteticos; no es corpus corporativo."
        )
        return
    if any(source.root.resolve().is_relative_to(private.resolve()) for private in scan.private_roots):
        scan.unavailable_sources.add(source.source_id)
        scan.warn("private_root_rejected", f"Fuente {source.source_id}: es almacenamiento privado; no se indexa.")
        return
    if not _safe_directory(source.root):
        scan.unavailable_sources.add(source.source_id)
        # Los alias opcionales ausentes no convierten un corpus valido en fallo.
        if source.root.exists() or source.category is None:
            scan.warn(
                "root_unavailable", f"Fuente {source.source_id}: no disponible o contiene un enlace; "
                "se conservan sus documentos previos.",
            )
        return
    root = source.root.resolve()
    other_roots = {
        item.root.resolve() for item in scan.sources if item.source_id != source.source_id and item.root.exists()
    }

    def failed_walk(_error: OSError) -> None:
        scan.incomplete_sources.add(source.source_id)
        scan.warn("scan_incomplete", f"Fuente {source.source_id}: no se pudo recorrer completa; no se aplican bajas.")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=failed_walk):
        current = Path(dirpath)
        relative_dir = current.relative_to(root)
        safe_dirs = []
        for dirname in sorted(dirnames):
            child = current / dirname
            if (
                dirname.startswith(".") or _has_link(child) or child.resolve() in other_roots
                or any(child.resolve().is_relative_to(excluded.resolve())
                       for excluded in (*scan.private_roots, *scan.test_roots))
            ):
                continue
            if source.category is None:
                probe = child.relative_to(root) / "_document.md"
                category = category_from_path(probe)
                # especializadas es un contenedor, no una categoria.
                is_container = len(probe.parts) == 2 and normalize_folder(dirname) == SPECIALIZED_CONTAINER
                if not is_container and category not in declared:
                    scan.warn(
                        "undeclared_category",
                        f"Fuente {source.source_id}: una carpeta sin categoria declarada se omite.",
                    )
                    continue
            safe_dirs.append(dirname)
        dirnames[:] = safe_dirs
        for filename in sorted(filenames):
            path = current / filename
            if (
                filename.startswith(".") or filename.casefold() == "readme.md"
                or path.suffix.casefold() not in supported_extensions()
            ):
                scan.ignored_files += 1
                continue
            relative = (relative_dir / filename).as_posix()
            category = source.category or category_from_path(relative)
            if category is None or category not in declared or _has_link(path) or not path.is_file():
                scan.ignored_files += 1
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                scan.ignored_files += 1
                continue
            key = _path_key(resolved)
            if key in seen:
                continue
            seen.add(key)
            portable = f"{source.namespace}/{relative}" if source.namespace else relative
            legacy_paths = (f"{source.root.name}/{relative}",) if source.category is not None else ()
            scan.files.append(KnowledgeFile(path, portable, category, source.source_id, legacy_paths))


def inventory_knowledge(**kwargs) -> KnowledgeScan:  # noqa: ANN003
    """API administrativa de solo lectura compartida con preflight/bootstrap."""
    return scan_knowledge(**kwargs)


def source_for_document(scan: KnowledgeScan, *, relative_path: str, storage_path: str | None) -> KnowledgeSource | None:
    """Resuelve quien puede aplicar una baja sin asumir que cambiar raiz borra todo."""
    for source in scan.sources:
        if source.namespace and relative_path.startswith(source.namespace + "/"):
            if source.source_id == "configured-knowledge":
                suffix = relative_path.removeprefix(source.namespace + "/")
                expected = (source.root / suffix).as_posix()
                if not storage_path or _path_key(storage_path) != _path_key(expected):
                    continue
            return source
    if not storage_path:
        return None
    portable_storage = str(storage_path).replace("\\", "/").rstrip("/")
    for source in scan.sources:
        expected = (source.root / relative_path).as_posix()
        if _path_key(portable_storage) == _path_key(expected):
            return source
    # Actualizar la carpeta del proyecto conserva el corpus original portable.
    original_suffix = "/data/knowledge/" + relative_path
    if normalize_folder(portable_storage).endswith(normalize_folder(original_suffix)):
        return next((source for source in scan.sources if source.source_id == "knowledge"), None)
    return None
