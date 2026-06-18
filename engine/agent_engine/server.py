from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import telemetry
from .broker import SQLiteAgentBroker
from .graph import run_pipeline


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

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/v1/runs":
            self._send_json(404, {"error": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return

        headers = {key.lower(): value for key, value in self.headers.items()}
        correlation_id = telemetry.set_correlation_id(headers.get("x-correlation-id") or payload.get("correlationId"))
        payload["correlationId"] = correlation_id

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("X-Correlation-Id", correlation_id)
        self.end_headers()

        def write_line(message: dict[str, Any]) -> None:
            self.wfile.write(_json_line(message))
            self.wfile.flush()

        def emit(stage: str, detail: str) -> None:
            write_line(_progress(stage, detail))

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
            try:
                result = run_pipeline(payload, emit)
                if span:
                    span.set_attribute("run.id", result.get("id", ""))
                write_line({"type": "result", "result": result})
            except BrokenPipeError:
                return
            except Exception as exc:
                write_line({"type": "error", "error": str(exc)})


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
        state_dir = os.getenv("AGENT_ENGINE_STATE_DIR") or str(Path.cwd() / ".agent-state")
        broker = SQLiteAgentBroker(Path(state_dir) / "agent-broker.sqlite")
        recovered = broker.recover_incomplete_runs()
        broker.close()
        telemetry.record_crash_recoveries(recovered)
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
