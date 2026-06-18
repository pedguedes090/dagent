from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import telemetry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _parse(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


class SQLiteAgentBroker:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              task TEXT NOT NULL,
              task_graph_json TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_subtasks (
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              role TEXT NOT NULL,
              title TEXT NOT NULL,
              input_json TEXT NOT NULL,
              output_json TEXT NOT NULL DEFAULT '{}',
              status TEXT NOT NULL,
              attempt INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS agent_events (
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              subtask_id TEXT,
              role TEXT,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_agent_subtasks_run_role ON agent_subtasks(run_id, role, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id, created_at);
            """
        )
        self.conn.commit()

        self._ensure_column("agent_runs", "correlation_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("agent_subtasks", "correlation_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("agent_events", "correlation_id", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def create_run(self, *, session_id: str, task: str, task_graph: dict[str, Any], correlation_id: str | None = None) -> str:
        run_id = str(uuid.uuid4())
        now = _now()
        cid = telemetry.set_correlation_id(correlation_id)
        self.conn.execute(
            "INSERT INTO agent_runs (id, session_id, task, task_graph_json, status, created_at, updated_at, correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, session_id, task, _json(task_graph), "running", now, now, cid),
        )
        self.conn.commit()
        return run_id

    def dispatch_subtasks(self, run_id: str, subtasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = _now()
        cid = self._run_correlation_id(run_id)
        telemetry.set_correlation_id(cid)
        rows = []
        for subtask in subtasks:
            subtask_id = str(uuid.uuid4())
            role = str(subtask["role"])
            title = str(subtask.get("title") or role)
            self.conn.execute(
                "INSERT INTO agent_subtasks (id, run_id, role, title, input_json, status, created_at, updated_at, correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (subtask_id, run_id, role, title, _json(subtask.get("input")), "queued", now, now, cid),
            )
            rows.append({"id": subtask_id, "runId": run_id, "role": role, "title": title, "status": "queued", "correlationId": cid})
        self.conn.commit()
        self.record_event(run_id, None, "orchestrator", "subtasks_dispatched", {"count": len(rows), "roles": [row["role"] for row in rows]})
        return rows

    def start_role(self, run_id: str, role: str, title: str, input_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        cid = self._run_correlation_id(run_id)
        telemetry.set_correlation_id(cid)
        row = self.conn.execute(
            "SELECT * FROM agent_subtasks WHERE run_id = ? AND role = ? AND status = 'queued' ORDER BY created_at LIMIT 1",
            (run_id, role),
        ).fetchone()
        now = _now()
        queued_at = now
        if row is None:
            attempt_row = self.conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS attempt FROM agent_subtasks WHERE run_id = ? AND role = ?",
                (run_id, role),
            ).fetchone()
            subtask_id = str(uuid.uuid4())
            attempt = int(attempt_row["attempt"] or 0) + 1
            self.conn.execute(
                "INSERT INTO agent_subtasks (id, run_id, role, title, input_json, status, attempt, created_at, updated_at, correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (subtask_id, run_id, role, title, _json(input_payload), "in_progress", attempt, now, now, cid),
            )
            row = self.conn.execute("SELECT * FROM agent_subtasks WHERE id = ?", (subtask_id,)).fetchone()
        else:
            queued_at = row["created_at"]
            self.conn.execute(
                "UPDATE agent_subtasks SET status = 'in_progress', input_json = ?, updated_at = ? WHERE id = ?",
                (_json(input_payload or _parse(row["input_json"])), now, row["id"]),
            )
            row = self.conn.execute("SELECT * FROM agent_subtasks WHERE id = ?", (row["id"],)).fetchone()
        self.conn.commit()
        queued_ms = telemetry.parse_iso_ms(queued_at)
        started_ms = telemetry.parse_iso_ms(now)
        if queued_ms is not None and started_ms is not None:
            telemetry.record_queue_latency(max(0.0, started_ms - queued_ms), role)
        result = self._subtask_dict(row)
        self.record_event(run_id, result["id"], role, "subtask_started", {"attempt": result["attempt"], "title": result["title"]})
        return result

    def complete_subtask(self, run_id: str, subtask_id: str, role: str, output: dict[str, Any], status: str = "completed") -> dict[str, Any]:
        now = _now()
        self.conn.execute(
            "UPDATE agent_subtasks SET status = ?, output_json = ?, updated_at = ? WHERE id = ?",
            (status, _json(output), now, subtask_id),
        )
        self.conn.commit()
        self.record_event(run_id, subtask_id, role, "subtask_completed", {"status": status})
        row = self.conn.execute("SELECT * FROM agent_subtasks WHERE id = ?", (subtask_id,)).fetchone()
        return self._subtask_dict(row)

    def finish_run(self, run_id: str, status: str, output: dict[str, Any] | None = None) -> None:
        now = _now()
        self.conn.execute("UPDATE agent_runs SET status = ?, updated_at = ? WHERE id = ?", (status, now, run_id))
        self.conn.commit()
        self.record_event(run_id, None, "orchestrator", "run_finished", {"status": status, "output": output or {}})

    def record_event(self, run_id: str, subtask_id: str | None, role: str | None, event_type: str, payload: dict[str, Any]) -> None:
        cid = str((payload or {}).get("correlationId") or self._run_correlation_id(run_id) or telemetry.get_correlation_id())
        telemetry.set_correlation_id(cid)
        event_payload = {**(payload or {}), "correlationId": cid}
        with telemetry.start_span(
            "broker.message",
            {
                "broker.run_id": run_id,
                "broker.subtask_id": subtask_id or "",
                "broker.role": role or "",
                "broker.event_type": event_type,
            },
        ):
            self.conn.execute(
                "INSERT INTO agent_events (id, run_id, subtask_id, role, event_type, payload_json, created_at, correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), run_id, subtask_id, role, event_type, _json(event_payload), _now(), cid),
            )
            self.conn.commit()
            telemetry.record_broker_message(event_type, role)

    def events(self, run_id: str, limit: int = 80) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_events WHERE run_id = ? ORDER BY created_at DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "runId": row["run_id"],
                "subtaskId": row["subtask_id"],
                "role": row["role"],
                "eventType": row["event_type"],
                "correlationId": row["correlation_id"],
                "payload": _parse(row["payload_json"]),
                "createdAt": row["created_at"],
            }
            for row in reversed(rows)
        ]

    def recover_incomplete_runs(self) -> int:
        rows = self.conn.execute(
            "SELECT id, correlation_id FROM agent_runs WHERE status IN ('running', 'needs_rework')"
        ).fetchall()
        if not rows:
            return 0
        now = _now()
        for row in rows:
            telemetry.set_correlation_id(row["correlation_id"] or None)
            self.conn.execute("UPDATE agent_runs SET status = 'recovered', updated_at = ? WHERE id = ?", (now, row["id"]))
            self.conn.execute(
                "UPDATE agent_subtasks SET status = 'recovered', updated_at = ? WHERE run_id = ? AND status IN ('queued', 'in_progress')",
                (now, row["id"]),
            )
            self.conn.commit()
            self.record_event(row["id"], None, "orchestrator", "crash_recovered", {"status": "recovered"})
        return len(rows)

    def _run_correlation_id(self, run_id: str) -> str:
        row = self.conn.execute("SELECT correlation_id FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        return str(row["correlation_id"] or "") if row else telemetry.get_correlation_id()

    def _subtask_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "runId": row["run_id"],
            "correlationId": row["correlation_id"],
            "role": row["role"],
            "title": row["title"],
            "input": _parse(row["input_json"]),
            "output": _parse(row["output_json"]),
            "status": row["status"],
            "attempt": row["attempt"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
