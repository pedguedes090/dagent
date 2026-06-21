"""A2A Protocol Core Types.

Implements the A2A specification data model as Python dataclasses with
to_dict()/from_dict() serialization. All types are immutable (frozen=True)
and carry JSON Schema field descriptions matching the A2A spec.

A2A Protocol version: 0.3.0 (current latest at a2a-protocol.org/latest)
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum, auto
from typing import Any, ClassVar

A2A_PROTOCOL_VERSION = "0.3.0"


# ── Task State ────────────────────────────────────────────────────────────────


class TaskState(str, Enum):
    """A2A TaskState per spec §4.2. Subset relevant to in-process agents."""

    SUBMITTED = "submitted"
    PROCESSING = "processing"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELED = "canceled"

    @classmethod
    def terminal(cls) -> set["TaskState"]:
        return {cls.COMPLETED, cls.FAILED, cls.REJECTED, cls.CANCELED}

    @property
    def is_terminal(self) -> bool:
        return self in self.terminal()

    @property
    def is_success(self) -> bool:
        return self == self.COMPLETED


def task_state_from_run_status(run_status: str) -> TaskState:
    """Map the existing RunStatus enum values to A2A TaskState."""
    mapping: dict[str, TaskState] = {
        "queued": TaskState.SUBMITTED,
        "running": TaskState.PROCESSING,
        "awaiting_approval": TaskState.INPUT_REQUIRED,
        "completed": TaskState.COMPLETED,
        "failed": TaskState.FAILED,
        "blocked": TaskState.REJECTED,
        "canceled": TaskState.CANCELED,
    }
    return mapping.get(run_status.lower(), TaskState.SUBMITTED)


# ── Message Parts ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TextPart:
    """A2A TextPart — plain text content block."""

    type: str = "text"
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TextPart":
        return cls(type="text", text=str(d.get("text") or ""))


@dataclass(frozen=True)
class DataPart:
    """A2A DataPart — structured data block with a content type hint."""

    type: str = "data"
    data: dict[str, Any] = field(default_factory=dict)
    mimeType: str = "application/json"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "mimeType": self.mimeType}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DataPart":
        return cls(
            type="data",
            data=dict(d.get("data") or {}),
            mimeType=str(d.get("mimeType") or "application/json"),
        )


@dataclass(frozen=True)
class FilePart:
    """A2A FilePart — reference to a file accessible by URL or inline content."""

    type: str = "file"
    name: str = ""
    mediaType: str = "application/octet-stream"
    uri: str | None = None
    content: str | None = None  # base64-encoded for inline

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "name": self.name, "mediaType": self.mediaType}
        if self.uri is not None:
            d["uri"] = self.uri
        if self.content is not None:
            d["content"] = self.content
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FilePart":
        return cls(
            type="file",
            name=str(d.get("name") or ""),
            mediaType=str(d.get("mediaType") or "application/octet-stream"),
            uri=d.get("uri"),
            content=d.get("content"),
        )


Part = TextPart | DataPart | FilePart


def part_from_dict(d: dict[str, Any]) -> Part:
    ptype = str(d.get("type") or "text").lower()
    if ptype == "text":
        return TextPart.from_dict(d)
    if ptype == "data":
        return DataPart.from_dict(d)
    if ptype == "file":
        return FilePart.from_dict(d)
    return TextPart(text=str(d.get("text") or str(d)))


def part_to_dict(part: Part) -> dict[str, Any]:
    return part.to_dict()


# ── Message ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Message:
    """A2A Message — the unit of communication between agents.

    Maps to the existing agent_engine storage/models.py Message where:
    - role: "user" | "assistant" | "system" | "agent"
    - parts: list of Part (TextPart | DataPart | FilePart)
    - messageId: unique per message
    - contextId: session/product context
    - taskId: the A2A Task this message belongs to
    - parentMessageId: for reply threading
    """

    role: str
    parts: list[Part]
    messageId: str = ""
    contextId: str = ""
    taskId: str = ""
    parentMessageId: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.messageId:
            object.__setattr__(self, "messageId", f"msg-{uuid.uuid4().hex[:12]}")

    @property
    def text(self) -> str:
        """Convenience: concatenate all TextPart content."""
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    @classmethod
    def from_text(cls, text: str, *, role: str = "user", **extra) -> "Message":
        """Shortcut: create a simple text message."""
        return cls(role=role, parts=[TextPart(text=text)], **extra)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "role": self.role,
            "parts": [part_to_dict(p) for p in self.parts],
            "messageId": self.messageId,
        }
        if self.contextId:
            d["contextId"] = self.contextId
        if self.taskId:
            d["taskId"] = self.taskId
        if self.parentMessageId:
            d["parentMessageId"] = self.parentMessageId
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        parts = [part_from_dict(p) for p in (d.get("parts") or [d.get("content") and {"type": "text", "text": d["content"]}]) if isinstance(p, dict)]
        return cls(
            role=str(d.get("role") or "user"),
            parts=parts or [TextPart(text=str(d.get("content") or ""))],
            messageId=str(d.get("messageId") or ""),
            contextId=str(d.get("contextId") or ""),
            taskId=str(d.get("taskId") or ""),
            parentMessageId=d.get("parentMessageId"),
            metadata=dict(d.get("metadata") or {}),
        )

    @classmethod
    def from_legacy_content(cls, role: str, content: str, **meta) -> "Message":
        """Adapter: convert old {role, content} dicts to A2A Message."""
        return cls(role=role, parts=[TextPart(text=content)], metadata=meta)


# ── Artifact ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Artifact:
    """A2A Artifact — a named, versioned piece of agent output.

    Content-addressed: artifactId = SHA-256(name + parts + metadata).
    Supports lineage via parentArtifactIds.
    """

    name: str
    description: str = ""
    parts: list[Part] = field(default_factory=list)
    artifactId: str = ""
    parentArtifactIds: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    createdAt: str = ""

    def __post_init__(self) -> None:
        if not self.artifactId:
            object.__setattr__(self, "artifactId", self._compute_id())
        if not self.createdAt:
            object.__setattr__(self, "createdAt", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def _compute_id(self) -> str:
        payload = json.dumps(
            {
                "name": self.name,
                "parts": [part_to_dict(p) for p in self.parts],
                "metadata": self.metadata,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return f"art-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "artifactId": self.artifactId,
            "name": self.name,
            "parts": [part_to_dict(p) for p in self.parts],
        }
        if self.description:
            d["description"] = self.description
        if self.parentArtifactIds:
            d["parentArtifactIds"] = self.parentArtifactIds
        if self.metadata:
            d["metadata"] = self.metadata
        if self.createdAt:
            d["createdAt"] = self.createdAt
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Artifact":
        parts = [part_from_dict(p) for p in (d.get("parts") or [])]
        return cls(
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            parts=parts,
            artifactId=str(d.get("artifactId") or ""),
            parentArtifactIds=list(d.get("parentArtifactIds") or []),
            metadata=dict(d.get("metadata") or {}),
            createdAt=str(d.get("createdAt") or ""),
        )

    @classmethod
    def from_text(cls, name: str, text: str, **extra) -> "Artifact":
        return cls(name=name, parts=[TextPart(text=text)], metadata=extra)

    @classmethod
    def from_data(cls, name: str, data: dict[str, Any], **extra) -> "Artifact":
        return cls(name=name, parts=[DataPart(data=data)], metadata=extra)


# ── Task ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Task:
    """A2A Task — a long-running unit of work.

    Maps to the existing agent_engine storage/models.py AgentRun.
    """

    id: str
    contextId: str = ""
    status: dict[str, Any] = field(default_factory=dict)  # {state, timestamp, message?}
    history: list[Message] = field(default_factory=list)
    artifacts: list[dict[str, str]] = field(default_factory=list)  # [{artifactId, name}]
    metadata: dict[str, Any] = field(default_factory=dict)
    subtasks: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.status:
            object.__setattr__(self, "status", {
                "state": TaskState.SUBMITTED.value,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })

    @property
    def state(self) -> TaskState:
        return TaskState(str(self.status.get("state") or "submitted"))

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
        }
        if self.contextId:
            d["contextId"] = self.contextId
        if self.history:
            d["history"] = [m.to_dict() for m in self.history]
        if self.artifacts:
            d["artifacts"] = self.artifacts
        if self.metadata:
            d["metadata"] = self.metadata
        if self.subtasks:
            d["subtasks"] = self.subtasks
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        return cls(
            id=str(d.get("id") or ""),
            contextId=str(d.get("contextId") or ""),
            status=dict(d.get("status") or {}),
            history=[Message.from_dict(m) for m in (d.get("history") or [])],
            artifacts=list(d.get("artifacts") or []),
            metadata=dict(d.get("metadata") or {}),
            subtasks=list(d.get("subtasks") or []),
        )


# ── Agent Card ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentCapability:
    name: str
    description: str = ""
    version: str = "1.0.0"
    tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.description:
            d["description"] = self.description
        if self.version:
            d["version"] = self.version
        if self.tools:
            d["tools"] = self.tools
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentCapability":
        return cls(
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            version=str(d.get("version") or "1.0.0"),
            tools=list(d.get("tools") or []),
        )


@dataclass(frozen=True)
class AgentSkill:
    """A named skill the agent can perform."""

    id: str
    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    inputModes: list[str] = field(default_factory=lambda: ["text"])
    outputModes: list[str] = field(default_factory=lambda: ["text"])

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.description:
            d["description"] = self.description
        if self.tags:
            d["tags"] = self.tags
        if self.examples:
            d["examples"] = self.examples
        if self.inputModes:
            d["inputModes"] = self.inputModes
        if self.outputModes:
            d["outputModes"] = self.outputModes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentSkill":
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            tags=list(d.get("tags") or []),
            examples=list(d.get("examples") or []),
            inputModes=list(d.get("inputModes") or ["text"]),
            outputModes=list(d.get("outputModes") or ["text"]),
        )


@dataclass(frozen=True)
class AgentSecurity:
    """Agent security profile."""

    sandboxed: bool = False
    containerRequired: bool = False
    networkAccess: bool = False
    allowedPaths: list[str] = field(default_factory=list)
    forbiddenPaths: list[str] = field(default_factory=list)
    allowedCommands: list[str] = field(default_factory=list)
    maxFileSizeBytes: int = 1_048_576
    authentication: dict[str, Any] = field(default_factory=dict)
    publicKeyFingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"sandboxed": self.sandboxed}
        if self.containerRequired:
            d["containerRequired"] = True
        if self.networkAccess:
            d["networkAccess"] = True
        if self.allowedPaths:
            d["allowedPaths"] = self.allowedPaths
        if self.forbiddenPaths:
            d["forbiddenPaths"] = self.forbiddenPaths
        if self.allowedCommands:
            d["allowedCommands"] = self.allowedCommands
        if self.maxFileSizeBytes:
            d["maxFileSizeBytes"] = self.maxFileSizeBytes
        if self.authentication:
            d["authentication"] = self.authentication
        if self.publicKeyFingerprint:
            d["publicKeyFingerprint"] = self.publicKeyFingerprint
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentSecurity":
        return cls(
            sandboxed=bool(d.get("sandboxed")),
            containerRequired=bool(d.get("containerRequired")),
            networkAccess=bool(d.get("networkAccess")),
            allowedPaths=list(d.get("allowedPaths") or []),
            forbiddenPaths=list(d.get("forbiddenPaths") or []),
            allowedCommands=list(d.get("allowedCommands") or []),
            maxFileSizeBytes=int(d.get("maxFileSizeBytes") or 1_048_576),
            authentication=dict(d.get("authentication") or {}),
            publicKeyFingerprint=str(d.get("publicKeyFingerprint") or ""),
        )


@dataclass(frozen=True)
class AgentCard:
    """A2A Agent Card — machine-readable description of an agent.

    Exposed at GET /.well-known/agent-card.json per A2A spec.
    """

    name: str
    description: str = ""
    url: str = ""
    version: str = "1.0.0"
    protocolVersion: str = A2A_PROTOCOL_VERSION
    capabilities: list[AgentCapability] = field(default_factory=list)
    skills: list[AgentSkill] = field(default_factory=list)
    defaultInputModes: list[str] = field(default_factory=lambda: ["text"])
    defaultOutputModes: list[str] = field(default_factory=lambda: ["text", "data"])
    security: AgentSecurity = field(default_factory=AgentSecurity)
    streaming: bool = False
    pushNotifications: bool = False
    stateHistory: dict[str, Any] = field(default_factory=dict)

    SCHEMA: ClassVar[dict[str, Any]] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["name", "description", "url", "protocolVersion", "capabilities"],
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "url": {"type": "string", "format": "uri"},
            "version": {"type": "string"},
            "protocolVersion": {"type": "string"},
            "capabilities": {"type": "array", "items": {"type": "object"}},
            "skills": {"type": "array", "items": {"type": "object"}},
            "defaultInputModes": {"type": "array", "items": {"type": "string"}},
            "defaultOutputModes": {"type": "array", "items": {"type": "string"}},
            "security": {"type": "object"},
            "streaming": {"type": "boolean"},
            "pushNotifications": {"type": "boolean"},
        },
    }

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "protocolVersion": self.protocolVersion,
            "capabilities": [c.to_dict() for c in self.capabilities],
        }
        if self.version and self.version != "1.0.0":
            d["version"] = self.version
        if self.skills:
            d["skills"] = [s.to_dict() for s in self.skills]
        if self.defaultInputModes:
            d["defaultInputModes"] = self.defaultInputModes
        if self.defaultOutputModes:
            d["defaultOutputModes"] = self.defaultOutputModes
        sec = self.security.to_dict()
        if any(sec.values()):
            d["security"] = sec
        if self.streaming:
            d["streaming"] = True
        if self.pushNotifications:
            d["pushNotifications"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentCard":
        return cls(
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            url=str(d.get("url") or ""),
            version=str(d.get("version") or "1.0.0"),
            protocolVersion=str(d.get("protocolVersion") or A2A_PROTOCOL_VERSION),
            capabilities=[AgentCapability.from_dict(c) for c in (d.get("capabilities") or [])],
            skills=[AgentSkill.from_dict(s) for s in (d.get("skills") or [])],
            defaultInputModes=list(d.get("defaultInputModes") or ["text"]),
            defaultOutputModes=list(d.get("defaultOutputModes") or ["text", "data"]),
            security=AgentSecurity.from_dict(d.get("security") or {}),
            streaming=bool(d.get("streaming")),
            pushNotifications=bool(d.get("pushNotifications")),
            stateHistory=dict(d.get("stateHistory") or {}),
        )


__all__ = [
    "A2A_PROTOCOL_VERSION",
    "AgentCapability",
    "AgentCard",
    "AgentSecurity",
    "AgentSkill",
    "Artifact",
    "DataPart",
    "FilePart",
    "Message",
    "Part",
    "Task",
    "TaskState",
    "TextPart",
    "part_from_dict",
    "part_to_dict",
    "task_state_from_run_status",
]
