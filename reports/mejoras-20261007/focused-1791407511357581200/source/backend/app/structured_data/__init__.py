# Creado por Aldo Garcia.
"""Acceso controlado a bases de datos estructuradas (plan JSON validado, nunca SQL libre)."""

from app.structured_data.schemas import StructuredQueryPlan
from app.structured_data.tool import StructuredDataTool

__all__ = ["StructuredDataTool", "StructuredQueryPlan"]
