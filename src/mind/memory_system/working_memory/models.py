# src/mind/memory_system/working_memory/models.py
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MemoryFragment:
    """代表一个单一的、结构化的记忆片段."""
    cycle_ago: int
    status: str # "vivid", "active", "retained", "decaying"
    content: dict[str, Any]
