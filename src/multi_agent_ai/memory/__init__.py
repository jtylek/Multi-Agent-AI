"""Durable, project-scoped agent knowledge. No provider calls."""
from .store import MemoryStore, MemoryConflict

__all__ = ["MemoryStore", "MemoryConflict"]
