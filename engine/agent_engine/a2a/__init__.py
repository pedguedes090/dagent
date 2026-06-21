"""A2A Protocol adapter — Agent-to-Agent communication layer.

This package implements the A2A protocol (a2a-protocol.org) as a Python
adapter around the existing agent engine. No external A2A SDK dependency.

Phase 0: Foundation types + artifact store + message bus.
Phase 1: Agent Cards + discovery + registry.
Phase 2: Task state machine + HTTP endpoints.
Phase 3: Streaming (SSE + A2A event types).
"""

from .types import (
    AgentCard,
    AgentCapability,
    AgentSkill,
    AgentSecurity,
    Artifact,
    DataPart,
    FilePart,
    Message,
    Part,
    Task,
    TaskState,
    TextPart,
    task_state_from_run_status,
)
from .types import __all__ as _types_all

__all__ = [
    "AgentCard",
    "AgentCapability",
    "AgentSkill",
    "AgentSecurity",
    "Artifact",
    "DataPart",
    "FilePart",
    "Message",
    "Part",
    "Task",
    "TaskState",
    "TextPart",
    "task_state_from_run_status",
    *_types_all,
]
