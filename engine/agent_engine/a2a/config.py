"""A2A Configuration and Feature Flags.

All new A2A code paths are gated behind feature flags. Default behavior
(all flags off) preserves existing pipeline behavior unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class A2AConfig:
    """Feature flags for A2A protocol integration."""

    enabled: bool = False

    # Per-feature flags
    routing_enabled: bool = False        # A2A Task routing replaces LangGraph fan-out
    streaming_enabled: bool = False       # SSE framing replaces NDJSON
    planning_enabled: bool = False        # PlanningCouncil replaces ad-hoc planners
    push_enabled: bool = False            # Webhook + IPC push notifications
    taste_review_enabled: bool = False    # Taste Director audit after each iteration
    message_bus_enabled: bool = False     # Agents communicate via A2AMessageBus

    # Operational
    continuation_token_interval: int = 50  # Emit continuation token every N events
    event_retention_hours: int = 1        # How long to keep replayable events
    max_delegation_depth: int = 5         # Prevent infinite agent delegation loops
    task_timeout_seconds: int = 600       # Default per-task timeout

    @classmethod
    def from_env(cls) -> "A2AConfig":
        return cls(
            enabled=_bool("A2A_ENABLED"),
            routing_enabled=_bool("A2A_ROUTING_ENABLED"),
            streaming_enabled=_bool("A2A_STREAMING_ENABLED"),
            planning_enabled=_bool("A2A_PLANNING_ENABLED"),
            push_enabled=_bool("A2A_PUSH_ENABLED"),
            taste_review_enabled=_bool("A2A_TASTE_REVIEW_ENABLED"),
            message_bus_enabled=_bool("A2A_MESSAGE_BUS_ENABLED"),
            continuation_token_interval=_int("A2A_CONTINUATION_TOKEN_INTERVAL", 50),
            event_retention_hours=_int("A2A_EVENT_RETENTION_HOURS", 1),
            max_delegation_depth=_int("A2A_MAX_DELEGATION_DEPTH", 5),
            task_timeout_seconds=_int("A2A_TASK_TIMEOUT_SECONDS", 600),
        )

    def to_dict(self) -> dict[str, bool | int]:
        return {
            "enabled": self.enabled,
            "routing": self.routing_enabled,
            "streaming": self.streaming_enabled,
            "planning": self.planning_enabled,
            "push": self.push_enabled,
            "tasteReview": self.taste_review_enabled,
            "messageBus": self.message_bus_enabled,
            "continuationTokenInterval": self.continuation_token_interval,
            "eventRetentionHours": self.event_retention_hours,
            "maxDelegationDepth": self.max_delegation_depth,
            "taskTimeoutSeconds": self.task_timeout_seconds,
        }


# Module-level singleton
_config: A2AConfig | None = None


def get_a2a_config() -> A2AConfig:
    global _config
    if _config is None:
        _config = A2AConfig.from_env()
    return _config


def reset_a2a_config() -> None:
    global _config
    _config = None


def _bool(key: str) -> bool:
    return os.environ.get(key, "").strip().lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default
