from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import telemetry
from .autonomy import autonomy_status, run_idle_discovery
from .broker import SQLiteAgentBroker
from .debug_log import write_debug_event
from .durable_execution import DurableExecutionStore
from .graph import classify_execution, run_pipeline
from .state_store import control_plane_path, migrate_legacy_tables

_WRITE_LOCK = threading.Lock()
_AUTONOMY_LOCK = threading.Lock()


def _state_dir_path() -> Path:
    return Path(os.getenv("AGENT_ENGINE_STATE_DIR") or str(Path.cwd() / ".agent-state"))


def _recent_debug_events(limit: int = 80) -> list[dict[str, Any]]:
    log_dir = _state_dir_path() / "logs"
    files = sorted(log_dir.glob("agent-debug-*.jsonl"))
    if not files:
        return []
    lines: deque[str] = deque(maxlen=max(1, int(limit)))
    try:
        with files[-1].open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events


def _json_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _progress(stage: str, detail: str) -> dict[str, Any]:
    return {
        "type": "progress",
        "stage": stage,
        "detail": detail,
        "at": datetime.now(timezone.utc).isoformat(),
    }


class AgentRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "HeThongAgentBackend/0.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        if self.path == "/v1/observability":
            state_dir = _state_dir_path()
            self._send_json(
                200,
                {
                    "ok": True,
                    "stateDir": str(state_dir),
                    "debugLogDir": str(state_dir / "logs"),
                    "runLockActive": _WRITE_LOCK.locked(),
                    "writeLockActive": _WRITE_LOCK.locked(),
                    "recentEvents": _recent_debug_events(),
                },
            )
            return
        if self.path == "/v1/autonomy/status":
            payload = autonomy_status(_state_dir_path())
            payload["runLockActive"] = _WRITE_LOCK.locked()
            payload["writeLockActive"] = _WRITE_LOCK.locked()
            payload["autonomyScanActive"] = _AUTONOMY_LOCK.locked()
            self._send_json(200, payload)
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/v1/autonomy/idle-scan":
            self._handle_autonomy_idle_scan()
            return
        if self.path != "/v1/runs":
            self._send_json(404, {"error": "not_found"})
            return

        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return

        headers = {key.lower(): value for key, value in self.headers.items()}
        correlation_id = telemetry.set_correlation_id(headers.get("x-correlation-id") or payload.get("correlationId"))
        payload["correlationId"] = correlation_id
        admission = classify_execution(str(payload.get("content") or payload.get("task") or ""))
        execution_class = str(admission["executionClass"])
        payload["executionClass"] = execution_class
        write_debug_event(
            "http.run_received",
            {
                "path": self.path,
                "sessionId": payload.get("sessionId"),
                "workspacePath": payload.get("workspacePath"),
                "taskPreview": str(payload.get("content") or payload.get("task") or "")[:500],
            },
        )

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("X-Correlation-Id", correlation_id)
        self.end_headers()

        def write_line(message: dict[str, Any]) -> None:
            self.wfile.write(_json_line(message))
            self.wfile.flush()

        def emit(stage: str, detail: str) -> None:
            message = _progress(stage, detail)
            write_debug_event("progress", {"stage": stage, "detail": detail})
            write_line(message)

        server_kind = telemetry.SpanKind.SERVER if telemetry.SpanKind else None
        with telemetry.start_span(
            "http.server.agent_run",
            {
                "http.method": "POST",
                "http.route": "/v1/runs",
                "session.id": payload.get("sessionId", ""),
                "correlation.id": correlation_id,
            },
            kind=server_kind,
            context=telemetry.extract_trace_context(headers),
        ) as span:
            lock_acquired = False
            try:
                if execution_class == "write":
                    if not _WRITE_LOCK.acquire(blocking=False):
                        write_debug_event("run.queued", {"correlationId": correlation_id, "executionClass": execution_class})
                        emit("queued", "Another write-capable run is active; waiting for the write lock")
                        _WRITE_LOCK.acquire()
                    lock_acquired = True
                    write_debug_event("run.running", {"correlationId": correlation_id, "executionClass": execution_class})
                    emit("running", "Write lane admitted")
                else:
                    write_debug_event("run.running", {"correlationId": correlation_id, "executionClass": execution_class})
                    emit("running", "Read-only lane admitted without the write lock")
                result = run_pipeline(payload, emit)
                if span:
                    span.set_attribute("run.id", result.get("id", ""))
                write_debug_event(
                    "run.result",
                    {
                        "runId": result.get("id"),
                        "changedFileCount": len(result.get("changedFiles") or []),
                        "reviewStatus": (result.get("review") or {}).get("status") if isinstance(result.get("review"), dict) else None,
                        "correlationId": correlation_id,
                    },
                )
                write_line({"type": "result", "result": result})
            except BrokenPipeError:
                return
            except Exception as exc:
                write_debug_event("run.error", {"error": str(exc), "correlationId": correlation_id})
                write_line({"type": "error", "error": str(exc)})
            finally:
                if lock_acquired:
                    _WRITE_LOCK.release()
                    write_debug_event("run.released", {"correlationId": correlation_id})

    def _handle_autonomy_idle_scan(self) -> None:
        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return

        raw_workspace_path = str(payload.get("workspacePath") or "").strip()
        if not raw_workspace_path:
            self._send_json(400, {"error": "workspacePath is required"})
            return
        workspace_path = Path(raw_workspace_path).expanduser()
        if not workspace_path.exists() or not workspace_path.is_dir():
            self._send_json(400, {"error": "workspacePath must be an existing directory"})
            return

        headers = {key.lower(): value for key, value in self.headers.items()}
        correlation_id = telemetry.set_correlation_id(headers.get("x-correlation-id") or payload.get("correlationId"))
        if not _AUTONOMY_LOCK.acquire(blocking=False):
            write_debug_event("autonomy.idle_scan_skipped", {"reason": "autonomy_scan_active", "correlationId": correlation_id})
            self._send_json(
                409,
                {
                    "ok": False,
                    "error": "autonomy_scan_active",
                    "runLockActive": _WRITE_LOCK.locked(),
                    "writeLockActive": _WRITE_LOCK.locked(),
                    "autonomyScanActive": True,
                },
            )
            return

        try:
            write_debug_event(
                "autonomy.idle_scan_requested",
                {"workspacePath": str(workspace_path.resolve()), "correlationId": correlation_id},
            )
            report = run_idle_discovery(workspace_path, _state_dir_path())
            self._send_json(
                200,
                {
                    "ok": True,
                    "correlationId": correlation_id,
                    "runLockActive": _WRITE_LOCK.locked(),
                    "writeLockActive": _WRITE_LOCK.locked(),
                    "autonomyScanActive": False,
                    "report": report,
                    "memory": report.get("memory"),
                },
            )
        except Exception as exc:
            write_debug_event("autonomy.idle_scan_error", {"error": str(exc), "correlationId": correlation_id})
            self._send_json(500, {"ok": False, "error": str(exc)})
        finally:
            _AUTONOMY_LOCK.release()


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    telemetry.configure_telemetry()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), AgentRequestHandler)
    try:
        state_dir = _state_dir_path()
        db_path = control_plane_path(state_dir)
        supervisor = DurableExecutionStore(db_path)
        migrate_legacy_tables(
            db_path,
            state_dir / "durable-executions.sqlite",
            ("durable_executions", "durable_steps"),
        )
        durable_recovered = supervisor.recover_incomplete()
        supervisor.close()
        broker = SQLiteAgentBroker(db_path)
        migrate_legacy_tables(
            db_path,
            state_dir / "agent-broker.sqlite",
            ("agent_runs", "agent_subtasks", "agent_events"),
        )
        recovered = broker.recover_incomplete_runs()
        broker.close()
        telemetry.record_crash_recoveries(recovered + durable_recovered)
    except Exception:
        pass
    print(json.dumps({"type": "ready", "host": args.host, "port": server.server_port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
