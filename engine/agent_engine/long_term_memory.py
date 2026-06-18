from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


CORE_MEMORY_DECAY_RATE = 0.025
DEFAULT_MEMORY_DECAY_RATE = 0.055
ERROR_MEMORY_DECAY_RATE = 0.18

_TOKEN_RE = re.compile(r"[A-Za-zÀ-ỹ0-9_]{2,}", re.UNICODE)
_SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b(api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|secret|password|passwd|authorization)\b"
        r"(\s*[:=]\s*)(['\"]?)[A-Za-z0-9._~+/=-]{8,}\3"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    kind: str
    source: str
    content: str
    tags: list[str]
    importance: float
    activation: float
    accessCount: int
    lastAccessedAt: float
    createdAt: float
    updatedAt: float
    decayRate: float
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_memory_path(state_dir: str | Path) -> Path:
    return Path(state_dir).resolve() / "long-term-memory.sqlite"


def _now(now: float | None = None) -> float:
    return float(time.time() if now is None else now)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _tokenize(value: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(value or "")}


def _redact_sensitive(value: str) -> tuple[str, bool]:
    text = str(value or "")
    redacted = False
    for pattern in _SECRET_PATTERNS:
        next_text = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]" if len(match.groups()) >= 2 else "[redacted-secret]", text)
        redacted = redacted or next_text != text
        text = next_text
    return text, redacted


def _default_decay_rate(kind: str) -> float:
    normalized = str(kind or "").lower()
    if normalized in {"core", "code", "architecture", "decision", "invariant"}:
        return CORE_MEMORY_DECAY_RATE
    if normalized in {"error", "failure", "exception", "test_failure", "review_blocker", "incident"}:
        return ERROR_MEMORY_DECAY_RATE
    return DEFAULT_MEMORY_DECAY_RATE


def _memory_id(kind: str, source: str, content: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{source}\0{content}".encode("utf-8", errors="replace")).hexdigest()
    return f"mem-{digest[:24]}"


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


class ACTRMemoryStore:
    """SQLite-backed long-term memory with ACT-R-style rehearsal and decay.

    Activation is intentionally simple and explainable:
    importance + log(rehearsals) + lexical match - age decay.
    Frequently retrieved core memories remain active; stale errors decay faster.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.database_path))
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def _ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS actr_memories (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                importance REAL NOT NULL DEFAULT 0.5,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_accessed_at REAL NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                decay_rate REAL NOT NULL DEFAULT 0.055,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_actr_memories_kind ON actr_memories(kind);
            CREATE INDEX IF NOT EXISTS idx_actr_memories_source ON actr_memories(source);
            CREATE INDEX IF NOT EXISTS idx_actr_memories_last_accessed ON actr_memories(last_accessed_at);
            """
        )
        self.conn.commit()

    def remember(
        self,
        *,
        kind: str,
        content: str,
        source: str = "",
        tags: list[str] | None = None,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        memory_id: str | None = None,
        decay_rate: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now_ts = _now(now)
        safe_content, redacted = _redact_sensitive(content)
        safe_metadata = dict(metadata or {})
        if redacted:
            safe_metadata["redactedSensitiveContent"] = True
        normalized_kind = str(kind or "note").strip() or "note"
        normalized_source = str(source or "").strip()
        normalized_tags = sorted({str(tag).strip().lower() for tag in tags or [] if str(tag).strip()})
        target_id = memory_id or _memory_id(normalized_kind, normalized_source, safe_content)
        target_decay_rate = float(decay_rate if decay_rate is not None else _default_decay_rate(normalized_kind))
        target_importance = _clamp(float(importance))

        existing = self.conn.execute("SELECT tags, metadata, access_count, created_at FROM actr_memories WHERE id = ?", (target_id,)).fetchone()
        if existing:
            merged_tags = sorted(set(_loads_json(existing["tags"], [])) | set(normalized_tags))
            merged_metadata = {**(_loads_json(existing["metadata"], {}) or {}), **safe_metadata}
            access_count = int(existing["access_count"] or 0) + 1
            created_at = float(existing["created_at"] or now_ts)
        else:
            merged_tags = normalized_tags
            merged_metadata = safe_metadata
            access_count = 0
            created_at = now_ts

        self.conn.execute(
            """
            INSERT INTO actr_memories (
                id, kind, source, content, tags, importance, created_at, updated_at,
                last_accessed_at, access_count, decay_rate, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind = excluded.kind,
                source = excluded.source,
                content = excluded.content,
                tags = excluded.tags,
                importance = MAX(actr_memories.importance, excluded.importance),
                updated_at = excluded.updated_at,
                last_accessed_at = excluded.last_accessed_at,
                access_count = excluded.access_count,
                decay_rate = excluded.decay_rate,
                metadata = excluded.metadata
            """,
            (
                target_id,
                normalized_kind,
                normalized_source,
                safe_content,
                _canonical_json(merged_tags),
                target_importance,
                created_at,
                now_ts,
                now_ts,
                access_count,
                target_decay_rate,
                _canonical_json(merged_metadata),
            ),
        )
        self.conn.commit()
        return self.get(target_id, now=now_ts) or {"id": target_id}

    def get(self, memory_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM actr_memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return None
        return self._row_to_record(row, query_terms=set(), now_ts=_now(now)).to_dict()

    def retrieve(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        limit: int = 8,
        reinforce: bool = True,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        now_ts = _now(now)
        query_terms = _tokenize(query)
        required_tags = {str(tag).strip().lower() for tag in tags or [] if str(tag).strip()}
        rows = self.conn.execute("SELECT * FROM actr_memories").fetchall()
        records: list[MemoryRecord] = []
        for row in rows:
            row_tags = set(_loads_json(row["tags"], []))
            if required_tags and not required_tags.issubset(row_tags):
                continue
            record = self._row_to_record(row, query_terms=query_terms, now_ts=now_ts)
            if query_terms and record.activation <= -10:
                continue
            records.append(record)

        records.sort(key=lambda item: (item.activation, item.importance, item.accessCount), reverse=True)
        selected = records[: max(0, int(limit))]
        if reinforce and selected:
            ids = [item.id for item in selected]
            self.conn.executemany(
                "UPDATE actr_memories SET last_accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                [(now_ts, item_id) for item_id in ids],
            )
            self.conn.commit()
        return [item.to_dict() for item in selected]

    def stats(self, *, now: float | None = None) -> dict[str, Any]:
        now_ts = _now(now)
        rows = self.conn.execute("SELECT * FROM actr_memories").fetchall()
        records = [self._row_to_record(row, query_terms=set(), now_ts=now_ts) for row in rows]
        by_kind: dict[str, int] = {}
        for record in records:
            by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
        records.sort(key=lambda item: item.activation, reverse=True)
        average_activation = sum(record.activation for record in records) / max(1, len(records))
        return {
            "databasePath": str(self.database_path),
            "total": len(records),
            "byKind": by_kind,
            "averageActivation": round(average_activation, 4),
            "topMemories": [record.to_dict() for record in records[:5]],
        }

    def _row_to_record(self, row: sqlite3.Row, *, query_terms: set[str], now_ts: float) -> MemoryRecord:
        tags = list(_loads_json(row["tags"], []))
        metadata = _loads_json(row["metadata"], {}) or {}
        access_count = int(row["access_count"] or 0)
        last_accessed = float(row["last_accessed_at"] or row["created_at"] or now_ts)
        age_days = max(0.0, (now_ts - last_accessed) / 86400.0)
        decay_rate = float(row["decay_rate"] or DEFAULT_MEMORY_DECAY_RATE)
        searchable = " ".join([row["kind"], row["source"], row["content"], " ".join(tags)])
        similarity = self._similarity(query_terms, _tokenize(searchable))
        activation = float(row["importance"] or 0.0) + math.log1p(access_count) + (1.4 * similarity) - (decay_rate * age_days)
        return MemoryRecord(
            id=row["id"],
            kind=row["kind"],
            source=row["source"],
            content=row["content"],
            tags=tags,
            importance=round(float(row["importance"] or 0.0), 4),
            activation=round(activation, 4),
            accessCount=access_count,
            lastAccessedAt=last_accessed,
            createdAt=float(row["created_at"] or now_ts),
            updatedAt=float(row["updated_at"] or now_ts),
            decayRate=round(decay_rate, 4),
            metadata=metadata,
        )

    @staticmethod
    def _similarity(query_terms: set[str], item_terms: set[str]) -> float:
        if not query_terms:
            return 0.0
        overlap = len(query_terms & item_terms)
        if not overlap:
            return -8.0
        return overlap / math.sqrt(max(1, len(query_terms)) * max(1, len(item_terms)))

