"""Lightweight long-running agent memory.

Storage: append-only JSONL at ``<state_dir>/agent_memory.jsonl``.
Retrieval: in-memory TF-IDF over the ``goal + result + lesson`` text of each
record. Top-K cosine-similar records are returned in O(N·avg_term_count).

Pure-Python, zero external deps. Read on startup; one syscall (append) per
write. No locks, no WAL, no SQLite — restart cost <100ms for 500 records.

Records (one per Auto Loop iteration):

    {
      "ts": 1719000000.123,
      "goal": "code web nghe nhạc",
      "subtask": "Add search bar with debounce",
      "files": ["src/Search.tsx", "src/api.ts"],
      "verdict": "pass" | "fail",
      "lesson": "fts5 is overkill for 50 rows; use list.filter instead",
      "tokens_in": 12345,
      "tokens_out": 678
    }
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_RECORDS_IN_RAM = 500
MAX_HISTORY_PROMPT = 6  # how many prior records to surface in a planner prompt

_TOKEN_RE = re.compile(r"[A-Za-zÀ-ỹ]{2,}")
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on", "for",
    "and", "or", "but", "with", "this", "that", "it", "be", "as", "by", "at",
    "from", "you", "we", "they", "i", "me", "my", "our", "your",
    "và", "của", "một", "tôi", "cho", "có", "không", "được", "này",
    "những", "các", "để", "là", "trong", "khi", "hoặc",
}


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS]


class AgentMemory:
    """In-RAM LRU + JSONL append. Thread-safe append; reads are unlocked."""

    def __init__(self, path: Path | str, cap: int = MAX_RECORDS_IN_RAM) -> None:
        self.path = Path(path)
        self.cap = cap
        self._records: list[dict[str, Any]] = []
        self._token_sets: list[Counter[str]] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    self._records.append(rec)
                    self._token_sets.append(Counter(_tokenize(self._record_text(rec))))
        except Exception:
            pass
        if len(self._records) > self.cap:
            drop = len(self._records) - self.cap
            self._records = self._records[drop:]
            self._token_sets = self._token_sets[drop:]

    @staticmethod
    def _record_text(rec: dict[str, Any]) -> str:
        return " ".join(
            str(rec.get(k) or "")
            for k in ("goal", "subtask", "verdict", "lesson")
        )

    def add(self, record: dict[str, Any]) -> None:
        record = {"ts": time.time(), **record}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
            self._records.append(record)
            self._token_sets.append(Counter(_tokenize(self._record_text(record))))
            if len(self._records) > self.cap:
                self._records.pop(0)
                self._token_sets.pop(0)

    def _idf(self) -> dict[str, float]:
        n = len(self._records) or 1
        df: Counter[str] = Counter()
        for ts in self._token_sets:
            for term in ts:
                df[term] += 1
        return {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}

    def search(self, query: str, k: int = 3) -> list[dict[str, Any]]:
        if not self._records:
            return []
        q_tokens = Counter(_tokenize(query))
        if not q_tokens:
            return []
        idf = self._idf()
        q_vec: dict[str, float] = {t: c * idf.get(t, 1.0) for t, c in q_tokens.items()}
        q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

        scores: list[tuple[float, int]] = []
        for i, doc_tokens in enumerate(self._token_sets):
            if not doc_tokens:
                continue
            d_vec = {t: c * idf.get(t, 1.0) for t, c in doc_tokens.items()}
            d_norm = math.sqrt(sum(v * v for v in d_vec.values())) or 1.0
            dot = sum(q_vec.get(t, 0.0) * d_vec.get(t, 0.0) for t in q_vec)
            sim = dot / (q_norm * d_norm)
            if sim > 0.0:
                scores.append((sim, i))
        scores.sort(reverse=True)
        out: list[dict[str, Any]] = []
        for sim, i in scores[:k]:
            rec = {**self._records[i], "_similarity": round(sim, 4)}
            out.append(rec)
        return out

    def recent(self, n: int = MAX_HISTORY_PROMPT) -> list[dict[str, Any]]:
        return list(self._records[-n:])

    def summary(self) -> dict[str, Any]:
        n = len(self._records)
        passes = sum(1 for r in self._records if str(r.get("verdict") or "").lower() in {"pass", "success", "ok"})
        return {"records": n, "passes": passes, "fails": n - passes, "path": str(self.path)}


_MEMORY: AgentMemory | None = None
_MEMORY_LOCK = threading.Lock()


def get_memory(state_dir: str | Path | None = None) -> AgentMemory:
    global _MEMORY
    with _MEMORY_LOCK:
        if _MEMORY is not None:
            return _MEMORY
        sdir = Path(state_dir) if state_dir else Path(os.getenv("AGENT_ENGINE_STATE_DIR") or ".agent-state")
        _MEMORY = AgentMemory(sdir / "agent_memory.jsonl")
        return _MEMORY


def build_memory_context(goal: str, k: int = 3) -> str:
    """Render top-K relevant prior records as a system-prompt snippet."""
    mem = get_memory()
    hits = mem.search(goal, k=k)
    if not hits:
        return ""
    lines: list[str] = ["# Relevant prior iterations"]
    for h in hits:
        verdict = str(h.get("verdict") or "")
        lesson = str(h.get("lesson") or "")[:200]
        subtask = str(h.get("subtask") or "")[:200]
        lines.append(f"- [{verdict}] {subtask} → {lesson}")
    return "\n".join(lines)


def record_iteration(
    *,
    goal: str,
    subtask: str,
    verdict: str,
    lesson: str = "",
    files: Iterable[str] | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    extra: dict[str, Any] | None = None,
) -> None:
    mem = get_memory()
    rec = {
        "goal": goal[:400],
        "subtask": subtask[:600],
        "verdict": verdict,
        "lesson": lesson[:400],
        "files": list(files or [])[:30],
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
    }
    if extra:
        rec["extra"] = extra
    mem.add(rec)
