from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .debug_log import write_debug_event

_execution_id: ContextVar[str] = ContextVar("durable_execution_id", default="")
_database_path: ContextVar[str] = ContextVar("durable_database_path", default="")
_runtime_settings: ContextVar[dict[str, Any]] = ContextVar("runtime_settings", default={})

_SECRET_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _parse(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _is_secret_key(value: Any) -> bool:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized in {item.replace("-", "_") for item in _SECRET_KEYS} or normalized.endswith(("_token", "_secret", "_password", "_api_key"))


def sanitize_payload(value: Any, *, max_string: int = 100_000, depth: int = 0) -> Any:
    if depth > 8:
        return "[max-depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + f"...[truncated {len(value) - max_string} chars]"
    if isinstance(value, bytes):
        return f"[bytes:{len(value)}]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 200:
                result["__truncated__"] = f"{len(value) - 200} keys"
                break
            result[str(key)] = "[redacted]" if _is_secret_key(key) else sanitize_payload(item, max_string=max_string, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [sanitize_payload(item, max_string=max_string, depth=depth + 1) for item in items[:200]]
        if len(items) > 200:
            result.append(f"[truncated {len(items) - 200} items]")
        return result
    if hasattr(value, "model_dump"):
        try:
            return sanitize_payload(value.model_dump(mode="json", exclude_none=True), max_string=max_string, depth=depth + 1)
        except Exception:
            pass
    return sanitize_payload(str(value), max_string=max_string, depth=depth + 1)


def _fingerprint(value: Any) -> str:
    payload = _json(sanitize_payload(value))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DurableExecutionStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS durable_executions (
                  id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  correlation_id TEXT NOT NULL,
                  request_fingerprint TEXT NOT NULL,
                  task TEXT NOT NULL,
                  workspace_path TEXT NOT NULL,
                  input_json TEXT NOT NULL,
                  result_json TEXT,
                  status TEXT NOT NULL,
                  attempt INTEGER NOT NULL DEFAULT 0,
                  lease_owner TEXT,
                  heartbeat_at TEXT,
                  last_error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS durable_steps (
                  id TEXT PRIMARY KEY,
                  execution_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  name TEXT NOT NULL,
                  idempotency_key TEXT,
                  status TEXT NOT NULL,
                  input_json TEXT NOT NULL,
                  output_json TEXT,
                  error TEXT,
                  started_at TEXT NOT NULL,
                  completed_at TEXT,
                  FOREIGN KEY (execution_id) REFERENCES durable_executions(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_durable_executions_status ON durable_executions(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_durable_steps_execution_sequence ON durable_steps(execution_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_durable_steps_cache ON durable_steps(execution_id, idempotency_key, status);
                """
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def prepare(
        self,
        *,
        execution_id: str,
        session_id: str,
        correlation_id: str,
        task: str,
        workspace_path: str,
        input_payload: dict[str, Any],
    ) -> dict[str, Any]:
        request_fingerprint = _fingerprint({"task": task, "workspacePath": str(Path(workspace_path).resolve())})
        now = _now()
        safe_input = sanitize_payload(input_payload)
        with self._lock:
            row = self.conn.execute("SELECT * FROM durable_executions WHERE id = ?", (execution_id,)).fetchone()
            if row:
                if row["request_fingerprint"] != request_fingerprint:
                    raise ValueError(f"Execution ID {execution_id} is already bound to a different task or workspace.")
                self.conn.execute(
                    "UPDATE durable_executions SET input_json = ?, correlation_id = ?, updated_at = ? WHERE id = ?",
                    (_json(safe_input), correlation_id, now, execution_id),
                )
                self.conn.commit()
                return self.get(execution_id) or {}
            self.conn.execute(
                """
                INSERT INTO durable_executions (
                  id, session_id, correlation_id, request_fingerprint, task, workspace_path,
                  input_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    execution_id,
                    session_id,
                    correlation_id,
                    request_fingerprint,
                    task,
                    str(Path(workspace_path).resolve()),
                    _json(safe_input),
                    now,
                    now,
                ),
            )
            self.conn.commit()
        return self.get(execution_id) or {}

    def acquire(self, execution_id: str) -> str:
        owner = str(uuid.uuid4())
        now = _now()
        with self._lock:
            self.conn.execute(
                """
                UPDATE durable_executions
                SET status = 'running', attempt = attempt + 1, lease_owner = ?,
                    heartbeat_at = ?, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (owner, now, now, execution_id),
            )
            self.conn.commit()
        self.record_event(execution_id, "supervisor", "lease_acquired", {"leaseOwner": owner})
        return owner

    def heartbeat(self, execution_id: str, owner: str) -> bool:
        now = _now()
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE durable_executions
                SET heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND status = 'running'
                """,
                (now, now, execution_id, owner),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def mark_recoverable(self, execution_id: str, error: BaseException | str) -> None:
        now = _now()
        message = str(error)[:4000]
        with self._lock:
            self.conn.execute(
                """
                UPDATE durable_executions
                SET status = 'recoverable', lease_owner = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (message, now, execution_id),
            )
            self.conn.commit()
        self.record_event(execution_id, "supervisor", "execution_recoverable", {"error": message})

    def complete(self, execution_id: str, result: dict[str, Any], status: str = "completed") -> None:
        now = _now()
        with self._lock:
            self.conn.execute(
                """
                UPDATE durable_executions
                SET status = ?, result_json = ?, lease_owner = NULL, last_error = NULL,
                    heartbeat_at = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, _json(sanitize_payload(result)), now, now, now, execution_id),
            )
            self.conn.commit()
        self.record_event(execution_id, "supervisor", "execution_completed", {"status": status})

    def get(self, execution_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM durable_executions WHERE id = ?", (execution_id,)).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "sessionId": row["session_id"],
            "correlationId": row["correlation_id"],
            "task": row["task"],
            "workspacePath": row["workspace_path"],
            "input": _parse(row["input_json"], {}),
            "result": _parse(row["result_json"], None),
            "status": row["status"],
            "attempt": row["attempt"],
            "leaseOwner": row["lease_owner"],
            "heartbeatAt": row["heartbeat_at"],
            "lastError": row["last_error"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "completedAt": row["completed_at"],
        }

    def recover_incomplete(self) -> int:
        now = _now()
        with self._lock:
            rows = self.conn.execute("SELECT id FROM durable_executions WHERE status = 'running'").fetchall()
            for row in rows:
                self.conn.execute(
                    """
                    UPDATE durable_executions
                    SET status = 'recoverable', lease_owner = NULL,
                        last_error = 'Backend stopped before the execution completed.', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
            self.conn.commit()
        for row in rows:
            self.record_event(row["id"], "supervisor", "startup_recovered", {"status": "recoverable"})
        return len(rows)

    def start_step(
        self,
        execution_id: str,
        kind: str,
        name: str,
        input_payload: Any,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        step_id = str(uuid.uuid4())
        now = _now()
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                sequence_row = self.conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM durable_steps WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                sequence = int(sequence_row["next_sequence"])
                self.conn.execute(
                    """
                    INSERT INTO durable_steps (
                      id, execution_id, sequence, kind, name, idempotency_key,
                      status, input_json, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        step_id,
                        execution_id,
                        sequence,
                        kind,
                        name,
                        idempotency_key,
                        _json(sanitize_payload(input_payload)),
                        now,
                    ),
                )
                self.conn.execute(
                    "UPDATE durable_executions SET heartbeat_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, execution_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return step_id

    def finish_step(self, step_id: str, output: Any = None, *, error: BaseException | str | None = None) -> None:
        now = _now()
        status = "failed" if error is not None else "completed"
        message = str(error)[:4000] if error is not None else None
        with self._lock:
            self.conn.execute(
                """
                UPDATE durable_steps
                SET status = ?, output_json = ?, error = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, _json(sanitize_payload(output)) if output is not None else None, message, now, step_id),
            )
            self.conn.commit()

    def cached_step(self, execution_id: str, idempotency_key: str) -> Any:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT output_json FROM durable_steps
                WHERE execution_id = ? AND idempotency_key = ? AND status = 'completed'
                ORDER BY sequence DESC LIMIT 1
                """,
                (execution_id, idempotency_key),
            ).fetchone()
        return _parse(row["output_json"], None) if row else None

    def record_event(self, execution_id: str, kind: str, name: str, payload: Any) -> None:
        step_id = self.start_step(execution_id, kind, name, payload)
        self.finish_step(step_id, payload)

    def steps(self, execution_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM durable_steps WHERE execution_id = ? ORDER BY sequence",
                (execution_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "executionId": row["execution_id"],
                "sequence": row["sequence"],
                "kind": row["kind"],
                "name": row["name"],
                "idempotencyKey": row["idempotency_key"],
                "status": row["status"],
                "input": _parse(row["input_json"], {}),
                "output": _parse(row["output_json"], None),
                "error": row["error"],
                "startedAt": row["started_at"],
                "completedAt": row["completed_at"],
            }
            for row in rows
        ]


class ExecutionHeartbeat:
    def __init__(self, db_path: Path, execution_id: str, owner: str, interval_seconds: float | None = None) -> None:
        self.db_path = Path(db_path)
        self.execution_id = execution_id
        self.owner = owner
        self.interval_seconds = interval_seconds or max(1.0, float(os.getenv("AGENT_HEARTBEAT_SECONDS", "5")))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"execution-heartbeat-{execution_id[:8]}", daemon=True)

    def __enter__(self) -> "ExecutionHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> bool:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 1)
        return False

    def _run(self) -> None:
        store = DurableExecutionStore(self.db_path)
        try:
            while not self._stop.wait(self.interval_seconds):
                if not store.heartbeat(self.execution_id, self.owner):
                    return
        finally:
            store.close()


@contextmanager
def execution_context(
    *,
    execution_id: str,
    database_path: Path,
    runtime_settings: dict[str, Any] | None = None,
) -> Iterator[None]:
    execution_token = _execution_id.set(execution_id)
    database_token = _database_path.set(str(database_path))
    settings_token = _runtime_settings.set(dict(runtime_settings or {}))
    try:
        yield
    finally:
        _runtime_settings.reset(settings_token)
        _database_path.reset(database_token)
        _execution_id.reset(execution_token)


def current_execution_id() -> str:
    return _execution_id.get()


def runtime_settings() -> dict[str, Any]:
    return dict(_runtime_settings.get())


def _active_store() -> tuple[DurableExecutionStore | None, str]:
    execution_id = current_execution_id()
    database_path = _database_path.get()
    if not execution_id or not database_path:
        return None, ""
    return DurableExecutionStore(Path(database_path)), execution_id


class StepCheckpoint:
    def __init__(self, step_id: str | None = None) -> None:
        self.step_id = step_id
        self.output: Any = None

    def set_output(self, output: Any) -> Any:
        self.output = output
        return output


@contextmanager
def checkpoint_step(kind: str, name: str, input_payload: Any = None) -> Iterator[StepCheckpoint]:
    store, execution_id = _active_store()
    checkpoint = StepCheckpoint()
    if not store:
        yield checkpoint
        return
    try:
        checkpoint.step_id = store.start_step(execution_id, kind, name, input_payload)
        yield checkpoint
    except Exception as exc:
        store.finish_step(checkpoint.step_id, checkpoint.output, error=exc)
        raise
    else:
        store.finish_step(checkpoint.step_id, checkpoint.output)
    finally:
        store.close()


def record_checkpoint(kind: str, name: str, payload: Any = None) -> None:
    store, execution_id = _active_store()
    if not store:
        return
    try:
        store.record_event(execution_id, kind, name, payload)
    finally:
        store.close()


def cached_tool_call(kind: str, name: str, input_payload: Any, callback: Callable[[], Any]) -> tuple[Any, bool]:
    store, execution_id = _active_store()
    if not store:
        return callback(), False
    idempotency_key = f"{kind}:{name}:{_fingerprint(input_payload)}"
    try:
        cached = store.cached_step(execution_id, idempotency_key)
        if cached is not None:
            store.record_event(execution_id, kind, f"{name}.cache_hit", {"idempotencyKey": idempotency_key})
            return cached, True
        step_id = store.start_step(execution_id, kind, name, input_payload, idempotency_key=idempotency_key)
        try:
            output = callback()
        except Exception as exc:
            store.finish_step(step_id, error=exc)
            raise
        store.finish_step(step_id, output)
        return output, False
    finally:
        store.close()


def durable_state_dir() -> Path:
    value = os.getenv("AGENT_ENGINE_STATE_DIR")
    root = Path(value).expanduser() if value else Path.cwd() / ".agent-state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def execution_artifact_dir(execution_id: str | None = None) -> Path:
    value = str(execution_id or current_execution_id() or "unscoped")
    safe_id = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    path = durable_state_dir() / "executions" / safe_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_transient_error(error: BaseException | str) -> bool:
    value = str(error).lower()
    transient_signals = (
        "429",
        "500",
        "502",
        "503",
        "504",
        "connection",
        "temporarily unavailable",
        "timed out",
        "timeout",
        "rate limit",
        "remote end closed",
        "network",
        "broken pipe",
    )
    return any(signal in value for signal in transient_signals)


def log_resume(execution_id: str, detail: str) -> None:
    write_debug_event("durable.resume", {"executionId": execution_id, "detail": detail})
