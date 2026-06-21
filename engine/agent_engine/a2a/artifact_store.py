"""A2A Artifact Store — content-addressable artifact persistence.

Wraps the existing SQLite storage/run_repo.py artifacts table with:
  - Content-addressing: artifact ID = SHA-256(parts + metadata)
  - Lineage tracking via parentArtifactIds
  - A2A Artifact ↔ storage Artifact conversion
  - Typed retrieval by task ID content-addressing ID, or lineage chain
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..debug_log import write_debug_event
from ..storage.models import Artifact as StorageArtifact
from ..storage.run_repo import SQLiteRunRepository
from .types import Artifact as A2AArtifact
from .types import DataPart, FilePart, Message, Part, TextPart, part_from_dict, part_to_dict


def _storage_to_a2a_artifact(sa: StorageArtifact) -> A2AArtifact:
    """Convert storage Artifact to A2A Artifact."""
    parts: list[Part] = []
    if sa.artifact_type == "text":
        parts.append(TextPart(text=str(sa.metadata.get("content") or "")))
    elif sa.artifact_type == "data":
        data = sa.metadata.get("data")
        parts.append(DataPart(data=data if isinstance(data, dict) else {}))
    elif sa.artifact_type == "file":
        parts.append(FilePart(name=sa.name, uri=f"file://{sa.path}", mediaType=str(sa.metadata.get("mediaType") or "application/octet-stream")))
    elif sa.artifact_type == "diff":
        parts.append(TextPart(text=str(sa.metadata.get("content") or str(sa.metadata.get("diff") or ""))))
    else:
        parts.append(TextPart(text=json.dumps(sa.metadata, ensure_ascii=False)))
    parent_ids = list(sa.metadata.get("parentArtifactIds") or [])
    return A2AArtifact(
        name=sa.name,
        description=str(sa.metadata.get("description") or ""),
        parts=parts,
        artifactId=sa.id if sa.id.startswith("art-") else f"art-{sa.checksum[:20]}" if sa.checksum else "",
        parentArtifactIds=parent_ids,
        metadata={k: v for k, v in sa.metadata.items() if k not in {"content", "data", "diff", "parentArtifactIds", "description"}},
        createdAt=sa.created_at,
    )


def _a2a_to_storage_artifact(a2a: A2AArtifact, run_id: str = "", task_id: str = "") -> StorageArtifact:
    """Convert A2A Artifact to storage Artifact."""
    meta: dict[str, Any] = dict(a2a.metadata or {})
    if a2a.parentArtifactIds:
        meta["parentArtifactIds"] = a2a.parentArtifactIds
    if a2a.description:
        meta["description"] = a2a.description
    art_type = "text"
    path = ""
    for part in a2a.parts:
        if isinstance(part, TextPart):
            art_type = "text"
            meta["content"] = part.text
        elif isinstance(part, DataPart):
            art_type = "data"
            meta["data"] = part.data
        elif isinstance(part, FilePart):
            art_type = "file"
            path = part.uri or ""
            meta["mediaType"] = part.mediaType
    checksum = hashlib.sha256(
        json.dumps({"name": a2a.name, "parts": [part_to_dict(p) for p in a2a.parts]}, sort_keys=True).encode()
    ).hexdigest()
    return StorageArtifact(
        id=a2a.artifactId,
        run_id=run_id,
        artifact_type=art_type,
        name=a2a.name,
        path=path,
        checksum=checksum,
        size_bytes=sum(len(part_to_dict(p).get("text", "")) for p in a2a.parts if isinstance(p, TextPart)),
        metadata=meta,
        created_at=a2a.createdAt,
    )


class A2AArtifactStore:
    """Content-addressable artifact persistence backed by SQLite."""

    def __init__(self, state_dir: str | Path) -> None:
        from ..state_store import control_plane_path
        db_path = control_plane_path(state_dir)
        self._repo = SQLiteRunRepository(db_path)

    def put(self, task_id: str, artifact: A2AArtifact) -> str:
        """Persist an A2A artifact. Returns its content-addressed ID."""
        storage = _a2a_to_storage_artifact(artifact, run_id=task_id, task_id=task_id)
        if not storage.id:
            storage = StorageArtifact(
                **{**storage.__dict__, "id": artifact.artifactId},
            )
        try:
            saved = self._repo.create_artifact(storage)
            write_debug_event("a2a.artifact.put", {
                "artifactId": saved.id, "taskId": task_id, "name": artifact.name,
            })
            return saved.id
        except Exception as exc:
            write_debug_event("a2a.artifact.put_error", {"error": str(exc), "name": artifact.name})
            raise

    def get(self, artifact_id: str) -> A2AArtifact | None:
        """Retrieve a single artifact by its content-addressed ID."""
        try:
            storage_arts = self._repo.list_artifacts("")
        except Exception:
            return None
        for sa in storage_arts:
            if sa.id == artifact_id or (sa.checksum and f"art-{sa.checksum[:20]}" == artifact_id):
                return _storage_to_a2a_artifact(sa)
        return None

    def list(self, task_id: str) -> list[A2AArtifact]:
        """List all artifacts for a task."""
        try:
            storage_arts = self._repo.list_artifacts(task_id)
        except Exception:
            return []
        return [_storage_to_a2a_artifact(sa) for sa in storage_arts]

    def lineage(self, artifact_id: str) -> list[A2AArtifact]:
        """Build the lineage chain for an artifact (parent → child → ...)."""
        result: list[A2AArtifact] = []
        seen: set[str] = set()
        current = self.get(artifact_id)
        while current and current.artifactId not in seen:
            seen.add(current.artifactId)
            result.append(current)
            if not current.parentArtifactIds:
                break
            for pid in current.parentArtifactIds:
                parent = self.get(pid)
                if parent and parent.artifactId not in seen:
                    result.append(parent)
                    current = parent
                    break
                if pid != current.parentArtifactIds[-1]:
                    continue
            if current.artifactId == result[-1].artifactId:
                break  # no parent found
        return sorted(result, key=lambda a: a.createdAt, reverse=False)

    def put_from_dict(self, task_id: str, d: dict[str, Any]) -> str:
        """Convenience: persist an artifact from its wire dict."""
        artifact = A2AArtifact.from_dict(d)
        return self.put(task_id, artifact)
