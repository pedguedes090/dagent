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
from .autonomy import autonomy_status, run_idle_discovery, select_next_task
from .broker import SQLiteAgentBroker
from .debug_log import write_debug_event
from .deterministic_workflow import DEFAULT_WORKFLOW
from .durable_execution import DurableExecutionStore
from .graph import classify_execution, is_cancelled, request_cancel, run_pipeline
from .project_doctor import run_doctor
from .state_store import control_plane_path, migrate_legacy_tables


# Lane assignment for the pipeline view. Source of truth for which lane each
# node belongs to; the renderer mirrors this to lay out the DAG.
_NODE_LANES: dict[str, str] = {
    "preflight": "intake",
    "codegraph_context": "intake",
    "repo_intelligence": "intake",
    "intake_user_intent": "intake",
    "intake_ambiguity": "intake",
    "intake_repo_context": "intake",
    "intake_synthesizer": "intake",
    "planning_minimal": "planning",
    "planning_robust": "planning",
    "planning_test_first": "planning",
    "critique_risk": "planning",
    "critique_test_coverage": "planning",
    "critique_security_regression": "planning",
    "plan_arbiter": "planning",
    "planner_task_graph": "governance",
    "researcher_context_agent": "governance",
    "governance_service": "governance",
    "human_gate": "governance",
    "environment_gate": "governance",
    "read_only_reporter": "governance",
    "load_context_files": "execution",
    "openhands_worker": "execution",
    "tester_agent": "execution",
    "security_reviewer_agent": "review",
    "code_reviewer_agent": "review",
    "doctor_feedback": "review",
    "release_deploy_agent": "review",
    "reviewer_decision": "review",
    "execution_gate": "review",
    "reporter": "release",
    "finalize_workspace": "release",
    "reporter_end": "release",
}


def _topology_payload() -> dict[str, Any]:
    wf = DEFAULT_WORKFLOW
    raw = wf.raw
    nodes = [
        {"id": name, "lane": _NODE_LANES.get(name, "other"), "label": name.replace("_", " ").title()}
        for name in wf.nodes
    ]
    edges: list[dict[str, Any]] = []
    for edge in raw.get("edges") or []:
        src, dst = str(edge[0]), str(edge[1])
        edges.append({"from": src, "to": dst, "kind": "direct"})
    for src, targets in (raw.get("fanOut") or {}).items():
        for dst in targets or []:
            edges.append({"from": str(src), "to": str(dst), "kind": "fanout"})
    for join in raw.get("joins") or []:
        for src in join.get("sources") or []:
            edges.append({"from": str(src), "to": str(join.get("target")), "kind": "join"})
    for node, cfg in (raw.get("routes") or {}).items():
        if not isinstance(cfg, dict):
            continue
        edges.append({"from": str(node), "to": str(cfg.get("default")), "kind": "route", "label": "default"})
        for case in cfg.get("cases") or []:
            edges.append({
                "from": str(node),
                "to": str(case.get("target")),
                "kind": "route",
                "label": str(case.get("when") or ""),
            })
    # De-duplicate (src,dst,kind,label)
    seen: set[tuple[str, str, str, str]] = set()
    unique_edges: list[dict[str, Any]] = []
    for edge in edges:
        key = (edge["from"], edge["to"], edge.get("kind", ""), str(edge.get("label", "")))
        if key in seen:
            continue
        seen.add(key)
        unique_edges.append(edge)
    return {
        "workflow": {"name": wf.name, "version": wf.version},
        "lanes": [
            {"id": "intake", "title": "Intake"},
            {"id": "planning", "title": "Planning"},
            {"id": "governance", "title": "Governance"},
            {"id": "execution", "title": "Execution"},
            {"id": "review", "title": "Review"},
            {"id": "release", "title": "Release"},
        ],
        "nodes": nodes,
        "edges": unique_edges,
        "contextRoutes": {k: list(v) for k, v in wf.context_routes.items()},
    }

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

    def _serve_static(self, filename: str, content_type: str) -> None:
        renderer_dir = Path(__file__).resolve().parents[2] / "src" / "renderer"
        filepath = (renderer_dir / filename).resolve()
        if renderer_dir not in filepath.parents and filepath != renderer_dir:
            self._send_json(403, {"error": "forbidden"})
            return
        if not filepath.exists() or not filepath.is_file():
            self._send_json(404, {"error": "not_found"})
            return
        try:
            body = filepath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self._send_json(500, {"error": "serve_error"})

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
        # Static file serving for web browser access
        if self.path == "/" or self.path == "/index.html":
            self._serve_static("index.html", "text/html; charset=utf-8")
            return
        if self.path.endswith(".css"):
            self._serve_static(self.path.lstrip("/"), "text/css; charset=utf-8")
            return
        if self.path.endswith(".js"):
            self._serve_static(self.path.lstrip("/"), "application/javascript; charset=utf-8")
            return
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        if self.path == "/v1/topology":
            try:
                self._send_json(200, _topology_payload())
            except Exception as exc:
                self._send_json(500, {"error": "topology_error", "detail": str(exc)})
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
        if self.path.startswith("/v1/agents"):
            self._handle_agents()
            return
        # A2A well-known discovery
        if self.path == "/.well-known/agent-card.json":
            from .a2a.agent_registry import get_agent_registry
            orch = get_agent_registry().get("orchestrator")
            if orch:
                self._send_json(200, orch.to_dict())
            else:
                self._send_json(200, {"name": "orchestrator", "protocolVersion": "0.3.0", "capabilities": []})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/v1/autonomy/idle-scan":
            self._handle_autonomy_idle_scan()
            return
        if self.path == "/v1/autonomy/next-task":
            self._handle_autonomy_next_task()
            return
        if self.path == "/v1/memory/record":
            self._handle_memory_record()
            return
        if self.path == "/v1/memory/critique":
            self._handle_memory_critique()
            return
        if self.path == "/v1/runs/cancel":
            try:
                payload = self._read_json_body()
            except Exception as exc:
                self._send_json(400, {"error": f"invalid_json: {exc}"})
                return
            execution_id = str(payload.get("executionId") or "").strip()
            if not execution_id:
                self._send_json(400, {"error": "executionId required"})
                return
            cancelled_now = request_cancel(execution_id)
            write_debug_event("http.run_cancel", {
                "executionId": execution_id, "alreadyCancelled": not cancelled_now,
            })
            self._send_json(200, {"ok": True, "executionId": execution_id, "cancelled": cancelled_now})
            return
        if self.path == "/v1/doctor":
            self._handle_doctor()
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
        admission = classify_execution(
            str(payload.get("content") or payload.get("task") or ""),
            dict(payload.get("executionContext") or {}),
        )
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

        # Compute the executionId now so we can echo it to the client up-front;
        # the renderer needs this to POST /v1/runs/cancel if user clicks Stop.
        import uuid as _uuid
        execution_id_for_stream = str(payload.get("executionId") or correlation_id or _uuid.uuid4())
        payload["executionId"] = execution_id_for_stream

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("X-Correlation-Id", correlation_id)
        self.send_header("X-Execution-Id", execution_id_for_stream)
        self.end_headers()

        def write_line(message: dict[str, Any]) -> None:
            # Guard the socket write: if the renderer stalled or disconnected,
            # a blocking write can wedge the pipeline thread driving this run.
            # Swallow disconnect errors so the orchestrator can finish/cleanup.
            try:
                self.wfile.write(_json_line(message))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        # First line so the client learns the executionId before any work starts.
        write_line({"type": "ready", "executionId": execution_id_for_stream, "correlationId": correlation_id,
                    "sessionId": payload.get("sessionId")})

        import uuid as _emit_uuid
        last_event_id_by_node: dict[str, str] = {}

        def emit(stage: str, detail: str, **fields: Any) -> None:
            message = _progress(stage, detail)
            message["executionId"] = execution_id_for_stream
            message["sessionId"] = payload.get("sessionId")
            message["correlationId"] = correlation_id
            event_id = _emit_uuid.uuid4().hex
            message["eventId"] = event_id
            # Stamp node field so the UI's log-filter-by-node, flowView status,
            # and execution-tab grouping work consistently.  When the stage
            # string IS a known node id (e.g. "openhands_worker"), that is
            # the canonical node.  Sub-stages get the explicit `node=` kwarg
            # from build_graph's traced_node wrapper.
            node_name = fields.pop("node", None) or _NODE_LANES.get(stage)
            if node_name:
                message["node"] = node_name
                message["parentEventId"] = last_event_id_by_node.get(node_name)
                last_event_id_by_node[node_name] = event_id
            # Default eventType for free-form sub-stage emits (lifecycle uses
            # explicit `event_type=` from traced_node).
            event_type = fields.pop("event_type", None)
            if event_type:
                message["eventType"] = event_type
            # Allowlisted enrichment kwargs — copy non-None values through.
            # Keep in sync with src/main/backendService.js ENRICHED list.
            ALLOWED = (
                "agent_role", "from_agent", "to_agent",
                "duration_ms", "model", "tool", "status",
                "input_summary", "output_summary",
                "retry_count", "review_cycle",
                "token_usage", "warnings", "error", "route_label",
                # Agent I/O Inspector fields
                "prompt_template", "input", "output", "output_delta",
                "tool_input", "tool_result", "changed_files",
                "evidence", "blockers", "confidence",
                "sequence", "issue_count", "passed",
                # Live stream chunks (LLM text_delta + cmd stdout/stderr lines)
                "chunk_text",
            )
            for key in ALLOWED:
                if key in fields and fields[key] is not None:
                    # camelCase for the wire — matches existing UI convention.
                    camel = "".join(
                        part if i == 0 else part.title()
                        for i, part in enumerate(key.split("_"))
                    )
                    message[camel] = fields[key]
            write_debug_event("progress", {"stage": stage, "detail": detail, "node": node_name})
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
                        # Bounded wait so a single hung write run cannot queue
                        # every subsequent run (incl. Auto Loop) forever. Poll in
                        # slices, honoring cancel + an overall ceiling.
                        lock_wait_deadline = float(os.environ.get("AGENT_WRITE_LOCK_WAIT_SECONDS", "1800"))
                        waited = 0.0
                        while not _WRITE_LOCK.acquire(timeout=5):
                            waited += 5
                            if is_cancelled(execution_id_for_stream):
                                emit("cancelled", "Run cancelled while waiting for the write lock", status="cancelled")
                                write_line({"type": "error", "error": "cancelled_while_queued"})
                                return
                            if waited >= lock_wait_deadline:
                                emit("timeout", f"Write lock unavailable after {int(waited)}s; aborting to avoid a deadlock", status="error")
                                write_line({"type": "error", "error": f"write_lock_timeout_{int(waited)}s"})
                                return
                            emit("queued", f"Still waiting for write lock ({int(waited)}s)")
                    lock_acquired = True
                    write_debug_event("run.running", {"correlationId": correlation_id, "executionClass": execution_class})
                    emit(
                        "running",
                        "Write lane admitted",
                        status="running",
                        input_summary=str(payload.get("content") or "")[:500],
                        evidence={
                            "executionClass": execution_class,
                            "executionMode": (payload.get("executionContext") or {}).get("executionMode") or "interactive",
                            "permissionProfile": (payload.get("executionContext") or {}).get("permissionProfile") or "workspace-policy",
                            "requiresMutation": bool((payload.get("executionContext") or {}).get("requiresMutation")),
                            "originalUserGoal": (payload.get("executionContext") or {}).get("originalUserGoal") or payload.get("content"),
                        },
                    )
                else:
                    write_debug_event("run.running", {"correlationId": correlation_id, "executionClass": execution_class})
                    emit(
                        "running",
                        "Read-only lane admitted without the write lock",
                        status="running",
                        input_summary=str(payload.get("content") or "")[:500],
                        evidence={"executionClass": execution_class, "permissionProfile": "read-only"},
                    )
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
            raw_workspace_path = os.getcwd()
        workspace_path = Path(raw_workspace_path).expanduser()
        if not workspace_path.exists() or not workspace_path.is_dir():
            workspace_path = Path(os.getcwd())
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

    def _handle_autonomy_next_task(self) -> None:
        """Pick the next autonomous task.

        Body: {
          workspacePath: str,
          completedIds?: list[str],   # finding/idea ids already attempted this loop
          ideaCursor?: int,           # rotation index into enhancement pool
          rescanIfStale?: bool        # run idle-scan first if no report cached
        }
        Returns: { ok, task: {id,kind,category,title,task,source,priorityScore}|null,
                   nextIdeaCursor, source: "cache"|"fresh_scan"|"none" }
        """
        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return
        raw_workspace = str(payload.get("workspacePath") or "").strip()
        if not raw_workspace:
            raw_workspace = os.getcwd()
        workspace_path = Path(raw_workspace).expanduser()
        if not workspace_path.exists() or not workspace_path.is_dir():
            workspace_path = Path(os.getcwd())
            if not workspace_path.exists() or not workspace_path.is_dir():
                self._send_json(400, {"error": "workspacePath must be an existing directory"})
                return

        completed_ids = set(map(str, payload.get("completedIds") or []))
        idea_cursor = int(payload.get("ideaCursor") or 0)
        rescan_if_stale = bool(payload.get("rescanIfStale"))
        product_goal = str(payload.get("productGoal") or "").strip()
        iteration = int(payload.get("iteration") or 0)
        iteration_history = payload.get("iterationHistory") or []
        if not isinstance(iteration_history, list):
            iteration_history = []
        settings = payload.get("settings") or {}
        session_id = str(payload.get("sessionId") or "").strip()
        recent_browser_findings = payload.get("recentBrowserFindings") or []
        if not isinstance(recent_browser_findings, list):
            recent_browser_findings = []
        failing_tests = payload.get("failingTests") or []
        if not isinstance(failing_tests, list):
            failing_tests = []
        console_errors = payload.get("consoleErrors") or []
        if not isinstance(console_errors, list):
            console_errors = []

        state_dir = _state_dir_path()
        report = (autonomy_status(state_dir) or {}).get("lastReport")
        source = "cache"
        if (not report or rescan_if_stale) and not _AUTONOMY_LOCK.locked():
            if _AUTONOMY_LOCK.acquire(blocking=False):
                try:
                    write_debug_event(
                        "autonomy.next_task_rescan",
                        {"workspacePath": str(workspace_path.resolve())},
                    )
                    report = run_idle_discovery(workspace_path, state_dir)
                    source = "fresh_scan"
                finally:
                    _AUTONOMY_LOCK.release()

        def _decompose(goal: str, history: list[dict[str, Any]]) -> dict[str, Any] | None:
            """Ask the LLM for the next concrete sub-task given goal + history."""
            try:
                from .llm_client import ChatClient
                from .agent_memory import build_memory_context
                server_url = str(settings.get("serverUrl") or os.environ.get("AGENT_LLM_BASE_URL") or "").strip()
                model = str(settings.get("model") or os.environ.get("ANTHROPIC_MODEL") or "claude-opus-4-8").strip()
                api_key = str(settings.get("apiKey") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
                if not server_url or not api_key:
                    return None
                client = ChatClient(server_url, model, api_key)
                summarized = []
                for item in (history or [])[-6:]:
                    if not isinstance(item, dict):
                        continue
                    summarized.append({
                        "task": str(item.get("task") or item.get("title") or "")[:200],
                        "result": str(item.get("result") or item.get("verdict") or "")[:200],
                    })
                memory_ctx = build_memory_context(goal, k=3)
                sys_prompt = (
                    "You decompose a long-running product goal into the SINGLE next concrete sub-task. "
                    "Output strict JSON: {\"id\":\"<slug>\",\"title\":\"<8 words>\",\"task\":\"<imperative one-paragraph instruction>\"}. "
                    "Produce an EXECUTABLE mutation task, not a read-only analysis. The task must instruct the "
                    "worker to make real file edits, run commands, and verify with browser. "
                    "The sub-task must directly advance the user's goal, build on what's already done, "
                    "be doable in one agent run (file edits + 1 verification), and produce visible progress. "
                    "MUTATION_REQUIRED=true: Do NOT produce a plan/report. Produce working code changes. "
                    "Do NOT repeat completed work. Do NOT propose generic refactor/test/perf if a core feature is still missing."
                )
                if memory_ctx:
                    sys_prompt = sys_prompt + "\n\n" + memory_ctx
                user_msg = json.dumps({
                    "goal": goal,
                    "iteration": iteration,
                    "completed": summarized,
                }, ensure_ascii=False)
                raw = client.chat(
                    [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.4,
                    json_mode=True,
                )
                try:
                    parsed = json.loads(raw)
                except Exception:
                    return None
                if not isinstance(parsed, dict) or not parsed.get("task"):
                    return None
                return parsed
            except Exception as exc:
                write_debug_event("autonomy.decompose_error", {"error": str(exc)})
                return None

        def _council_round(*, goal: str, iteration: int, iteration_history: list[dict[str, Any]], session_id: str, workspace_path: str, anti_halt_instruction: str = "") -> dict[str, Any] | None:
            try:
                from .llm_client import ChatClient
                from .idea_council import (
                    scan_capabilities,
                    run_council_round,
                    format_winner_task,
                )
                server_url = str(settings.get("serverUrl") or os.environ.get("AGENT_LLM_BASE_URL") or "").strip()
                model = str(settings.get("model") or os.environ.get("ANTHROPIC_MODEL") or "claude-opus-4-8").strip()
                api_key = str(settings.get("apiKey") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
                if not server_url or not api_key:
                    write_debug_event("council.skipped", {"reason": "no_llm_credentials"})
                    return None
                client = ChatClient(server_url, model, api_key)
                cap_map = scan_capabilities(workspace_path, goal)

                def chat(messages: list[dict[str, str]], temperature: float, json_mode: bool) -> str:
                    return client.chat(messages, temperature=temperature, json_mode=json_mode)

                result = run_council_round(
                    goal=goal,
                    session_id=session_id,
                    workspace_path=workspace_path,
                    state_dir=Path(state_dir),
                    capability_map=cap_map,
                    iteration=iteration,
                    iteration_history=iteration_history,
                    chat=chat,
                    recent_browser_findings=recent_browser_findings,
                    failing_tests=failing_tests,
                    console_errors=console_errors,
                    anti_halt_instruction=anti_halt_instruction,
                )
                winner = result.get("winner") if isinstance(result, dict) else None
                if winner:
                    raw = winner.get("raw") or {}
                    raw["productRoot"] = cap_map.productRoot
                    raw["formattedTask"] = format_winner_task(winner, goal, cap_map.productRoot)
                    winner["raw"] = raw
                return result
            except Exception as exc:
                write_debug_event("council.error", {"error": str(exc)})
                return None

        task = select_next_task(
            report,
            completed_ids,
            idea_cursor=idea_cursor,
            product_goal=product_goal,
            iteration=iteration,
            iteration_history=iteration_history,
            decompose_subtask=_decompose,
            council_round=_council_round,
            session_id=session_id,
            workspace_path=str(workspace_path.resolve()),
        )
        # Ensure council-idea and autonomous-bootstrap tasks carry explicit write context
        if task and isinstance(task, dict):
            kind = str(task.get("kind") or "")
            if kind in {"council_idea", "autonomous_bootstrap", "user_goal", "user_goal_subtask"}:
                task["requiresWorker"] = True
                task["executionClass"] = "write"
                task["autonomous"] = True
                execution_context = dict(task.get("executionContext") or {})
                execution_context.update({
                    "originalUserGoal": str(task.get("originalUserGoal") or product_goal or "").strip(),
                    "executionClass": "write",
                    "executionMode": "autonomous",
                    "permissionProfile": "workspace-write",
                    "requiresMutation": True,
                    "reportOnly": False,
                    "autoResolveTechnicalChoices": True,
                    "productRoot": str(task.get("productRoot") or execution_context.get("productRoot") or ""),
                })
                task["executionContext"] = execution_context

        next_cursor = idea_cursor
        write_debug_event(
            "autonomy.next_task",
            {
                "selected": (task or {}).get("id"),
                "kind": (task or {}).get("kind"),
                "completedCount": len(completed_ids),
            },
        )
        self._send_json(
            200,
            {
                "ok": True,
                "task": task,
                "source": source if task else ("none" if not task else source),
                "nextIdeaCursor": next_cursor,
                "runLockActive": _WRITE_LOCK.locked(),
                "autonomyScanActive": _AUTONOMY_LOCK.locked(),
            },
        )

    def _handle_agents(self) -> None:
        """Handle GET /.well-known/agent-card.json, GET /v1/agents, GET /v1/agents/{id}."""
        from .a2a.agent_registry import get_agent_registry
        registry = get_agent_registry()
        path = self.path
        if path == "/v1/agents" or path == "/v1/agents/":
            cards = registry.list_all()
            self._send_json(200, {"agents": [c.to_dict() for c in cards], "count": len(cards)})
            return
        prefix = "/v1/agents/"
        if path.startswith(prefix):
            agent_id = path[len(prefix):].split("?")[0].strip("/")
            card = registry.get(agent_id)
            if card:
                self._send_json(200, card.to_dict())
            else:
                self._send_json(404, {"error": "agent_not_found", "agentId": agent_id, "validIds": registry.list_ids()})
            return
        self._send_json(404, {"error": "not_found"})

    def _handle_memory_record(self) -> None:
        """Persist one Auto Loop iteration outcome to the agent memory store."""
        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return
        try:
            from .agent_memory import record_iteration, get_memory
            record_iteration(
                goal=str(payload.get("goal") or ""),
                subtask=str(payload.get("subtask") or ""),
                verdict=str(payload.get("verdict") or "unknown"),
                lesson=str(payload.get("lesson") or ""),
                files=payload.get("files") or [],
                tokens_in=int(payload.get("tokensIn") or 0),
                tokens_out=int(payload.get("tokensOut") or 0),
                extra=payload.get("extra") if isinstance(payload.get("extra"), dict) else None,
            )
            self._send_json(200, {"ok": True, "summary": get_memory().summary()})
        except Exception as exc:
            write_debug_event("memory.record_error", {"error": str(exc)})
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _handle_memory_critique(self) -> None:
        """Generate a one-sentence lesson from a failed iteration via LLM."""
        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return
        try:
            from .llm_client import ChatClient
            settings = payload.get("settings") or {}
            server_url = str(settings.get("serverUrl") or os.environ.get("AGENT_LLM_BASE_URL") or "").strip()
            model = str(settings.get("model") or os.environ.get("ANTHROPIC_MODEL") or "claude-opus-4-8").strip()
            api_key = str(settings.get("apiKey") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
            if not server_url or not api_key:
                self._send_json(200, {"ok": False, "lesson": ""})
                return
            client = ChatClient(server_url, model, api_key)
            sys = (
                "You distill an iteration failure into ONE short lesson (one sentence, <30 words) "
                "that a future iteration should remember to avoid the same mistake. Output JSON: "
                "{\"lesson\": \"<sentence>\"}."
            )
            user_msg = json.dumps({
                "goal": payload.get("goal") or "",
                "subtask": payload.get("subtask") or "",
                "error": payload.get("error") or payload.get("reason") or "",
                "output": str(payload.get("output") or "")[:1200],
            }, ensure_ascii=False)
            raw = client.chat(
                [{"role": "system", "content": sys}, {"role": "user", "content": user_msg}],
                temperature=0.2,
                json_mode=True,
            )
            try:
                parsed = json.loads(raw)
                lesson = str(parsed.get("lesson") or "").strip()
            except Exception:
                lesson = ""
            self._send_json(200, {"ok": bool(lesson), "lesson": lesson})
        except Exception as exc:
            write_debug_event("memory.critique_error", {"error": str(exc)})
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _handle_doctor(self) -> None:
        """Stream scan→plan→patch→verify events as NDJSON.

        Body: { workspacePath: str, sessionId?: str, model?: str }
        Each line is { type: "progress", stage: str, detail: str, ... }
        Final line: { type: "doctor.result", ok: bool, result: {...} }
        """
        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return
        raw_workspace = str(payload.get("workspacePath") or "").strip()
        if not raw_workspace:
            self._send_json(400, {"error": "workspacePath is required"})
            return
        workspace_path = Path(raw_workspace).expanduser()
        if not workspace_path.exists() or not workspace_path.is_dir():
            self._send_json(400, {"error": "workspacePath must be an existing directory"})
            return

        headers = {key.lower(): value for key, value in self.headers.items()}
        correlation_id = telemetry.set_correlation_id(headers.get("x-correlation-id") or payload.get("correlationId"))
        session_id = payload.get("sessionId")

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("X-Correlation-Id", correlation_id)
        self.end_headers()

        def write_line(message: dict[str, Any]) -> None:
            try:
                self.wfile.write(_json_line(message))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Client disconnected mid-stream — let the orchestrator continue,
                # we just stop trying to write.
                pass

        write_line({"type": "ready", "correlationId": correlation_id, "sessionId": session_id, "route": "/v1/doctor"})

        def emit(stage: str, detail: str) -> None:
            message = _progress(stage, detail)
            message["sessionId"] = session_id
            write_debug_event("doctor", {"stage": stage, "detail": detail[:400]})
            write_line(message)

        # Optional LLM provider so the patch step can stream tokens.
        # Prefer claude-agent-sdk (Read/Edit/Bash tools). Fall back to the
        # lower-level anthropic SDK provider, which streams text only.
        provider: Any = None
        api_key = (payload.get("apiKey") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        model = str(payload.get("model") or os.environ.get("ANTHROPIC_MODEL") or "claude-opus-4-8")
        try:
            from .project_doctor.agent_sdk_provider import maybe_build_provider
            provider = maybe_build_provider(cwd=workspace_path, model=model, api_key=api_key)
            if provider is not None:
                emit("doctor.provider", "claude-agent-sdk ready")
        except Exception as exc:
            emit("doctor.provider.unavailable", f"Agent SDK init failed: {exc}")
        if provider is None and api_key:
            try:
                from .claude_adapter import ClaudeConfig, ClaudeProvider
                provider = ClaudeProvider(ClaudeConfig(api_key=api_key, model=model))
                emit("doctor.provider", "anthropic-sdk fallback ready")
            except Exception as exc:
                emit("doctor.provider.unavailable", f"Anthropic SDK fallback failed: {exc}")

        try:
            result = run_doctor(workspace_path, provider=provider, emit=emit)
            write_line({"type": "doctor.result", "ok": result["ok"], "result": result, "sessionId": session_id})
        except Exception as exc:
            write_debug_event("doctor.error", {"error": str(exc), "correlationId": correlation_id})
            emit("doctor.error", f"Doctor pipeline crashed: {exc}")
            write_line({"type": "doctor.result", "ok": False, "error": str(exc), "sessionId": session_id})


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

    # Kick off codebase-memory-mcp download into .tools/ on first boot.
    # Non-blocking — server starts immediately; binary is used as soon as it lands.
    try:
        from . import codebase_memory as _cm
        _cm.ensure_local_binary_async()
    except Exception:
        pass

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
