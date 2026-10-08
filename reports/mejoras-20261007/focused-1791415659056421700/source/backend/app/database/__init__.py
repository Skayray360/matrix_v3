# Creado por Aldo Garcia.
"""Persistencia interna de Matrix RH (MySQL/MariaDB via SQLAlchemy 2)."""

from app.database.engine import get_engine, get_sessionmaker, session_scope
from app.database.models import Base

__all__ = ["Base", "get_engine", "get_sessionmaker", "session_scope"]
