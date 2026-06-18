from __future__ import annotations

import os
import json
import operator
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from . import telemetry
from .broker import SQLiteAgentBroker
from .llm_client import ChatClient
from .multi_agent import (
    build_task_graph,
    code_review_fallback,
    governance_decision,
    release_deploy_plan,
    researcher_output,
    reviewer_decision as aggregate_reviewer_decision,
    security_review_fallback,
)
from .openhands_worker import run_openhands_worker
from .workspace import codegraph_affected_tests, codegraph_context, create_workspace_sandbox, get_snapshot, normalize_verification_commands, read_file, run_sandboxed_command, trusted_context


class PipelineState(TypedDict, total=False):
    task: str
    workspacePath: str
    settings: dict[str, Any]
    humanGateApproval: dict[str, Any]
    messages: list[dict[str, Any]]
    sessionId: str
    correlationId: str
    preflight: dict[str, Any]
    taskIntent: dict[str, Any]
    codegraphContext: dict[str, Any]
    trustedRepoContext: dict[str, Any]
    intakeFindings: Annotated[list[dict[str, Any]], operator.add]
    problem: dict[str, Any]
    candidatePlans: Annotated[list[dict[str, Any]], operator.add]
    critiqueFindings: Annotated[list[dict[str, Any]], operator.add]
    finalPlan: dict[str, Any]
    agentContracts: dict[str, Any]
    taskGraph: dict[str, Any]
    brokerRunId: str
    brokerEvents: list[dict[str, Any]]
    researcherContext: dict[str, Any]
    governanceDecision: dict[str, Any]
    contextFiles: list[dict[str, Any]]
    retryCount: int
    workerAttempts: Annotated[list[dict[str, Any]], operator.add]
    testerResult: dict[str, Any]
    securityReview: dict[str, Any]
    codeReview: dict[str, Any]
    releasePlan: dict[str, Any]
    reviewerDecision: dict[str, Any]
    reviewFindings: Annotated[list[dict[str, Any]], operator.add]
    latestReview: dict[str, Any]
    result: dict[str, Any]


def _client(state: PipelineState) -> ChatClient:
    settings = state["settings"]
    return ChatClient(settings["serverUrl"], settings["model"], settings.get("apiKey", ""))


def _json(state: PipelineState, prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
    return _client(state).json(prompt, fallback)


def _state_dir() -> Path:
    value = os.getenv("AGENT_ENGINE_STATE_DIR")
    root = Path(value).expanduser() if value else Path.cwd() / ".agent-state"
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _open_checkpointer(emit: Callable[[str, str], None]):
    db_path = _state_dir() / "langgraph-checkpoints.sqlite"
    manager = SqliteSaver.from_conn_string(str(db_path))
    if hasattr(manager, "__enter__"):
        with manager as checkpointer:
            emit("checkpoint", "SQLite checkpointer ready")
            yield checkpointer
    else:
        emit("checkpoint", "SQLite checkpointer ready")
        yield manager


@contextmanager
def _open_broker():
    broker = SQLiteAgentBroker(_state_dir() / "agent-broker.sqlite")
    try:
        yield broker
    finally:
        broker.close()


def _is_literal_context_path(value: Any) -> bool:
    path = str(value or "").strip()
    return bool(path) and not any(char in path for char in "*?[") and not path.endswith(("/", "\\"))


CHANGE_SIGNALS = [
    "sửa",
    "fix",
    "tạo",
    "thêm",
    "xóa",
    "xoá",
    "cập nhật",
    "triển khai",
    "làm ra",
    "xây",
    "khởi tạo",
    "chỉnh",
    "đổi",
    "thay",
    "bổ sung",
    "tối ưu",
    "nâng cấp",
    "cài",
    "implement",
    "write",
    "edit",
    "create",
    "update",
    "delete",
    "build",
    "scaffold",
    "refactor",
    "install",
    "generate",
]
READ_SIGNALS = [
    "đọc",
    "xem",
    "giải thích",
    "phân tích",
    "review",
    "tóm tắt",
    "trả lời",
    "là gì",
    "kiểm tra",
    "soi",
    "đánh giá",
    "tìm hiểu",
    "nghiên cứu",
    "explain",
    "summarize",
    "read",
    "analyze",
    "inspect",
]
NO_EDIT_SIGNALS = ["không sửa", "khong sua", "chỉ đọc", "chi doc", "read-only", "đừng sửa", "dung sua", "chưa sửa", "không đụng file"]
CONDITIONAL_EDIT_SIGNALS = ["sửa luôn", "fix luôn", "nếu sai thì sửa", "nếu có lỗi thì sửa", "nếu thấy lỗi thì sửa", "sai thì sửa"]
HIGH_RISK_SIGNALS = [
    "deploy",
    "production",
    "prod",
    "migration",
    "migrate",
    "database",
    "db",
    "secret",
    "token",
    ".env",
    "auth",
    "permission",
    "infra",
    "ci",
    "workflow",
    "drop",
    "remove data",
]


def _signals(text: str, patterns: list[str]) -> list[str]:
    value = text.lower()
    return [pattern for pattern in patterns if pattern in value]


def _risk_rank(value: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(str(value or "").lower(), 1)


def _merge_risk(*values: str) -> str:
    ranked = max((_risk_rank(value), str(value or "medium").lower()) for value in values)
    return ranked[1] if ranked[1] in {"low", "medium", "high"} else "medium"


def _detect_task_intent(task: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    value = task.lower().strip()
    no_edit = _signals(value, NO_EDIT_SIGNALS)
    conditional_edit = _signals(value, CONDITIONAL_EDIT_SIGNALS)
    change = [] if no_edit else _signals(value, CHANGE_SIGNALS)
    read = _signals(value, READ_SIGNALS)
    project_creation = _is_project_creation_task(task, {"problemStatement": task})
    command_only = bool(_signals(value, ["chạy test", "chạy build", "run test", "run build", "npm run", "pytest"]))
    requires_worker = bool((change or conditional_edit or project_creation or command_only) and not no_edit)
    explicit_read_only = bool((read or "?" in value or no_edit) and not requires_worker)
    mode = "modify"
    if project_creation:
        mode = "create_project"
    elif command_only and not change:
        mode = "command"
    elif explicit_read_only:
        mode = "review" if any(signal in read for signal in ["review", "kiểm tra", "soi", "đánh giá", "tìm hiểu"]) else "answer"
    elif not requires_worker:
        mode = "ambiguous"
    risk = "high" if _signals(value, HIGH_RISK_SIGNALS) else ("medium" if requires_worker else "low")
    needs_clarification = mode == "ambiguous" and len(value) < 60
    return {
        "mode": mode,
        "requiresWorker": requires_worker,
        "readOnly": not requires_worker,
        "explicitNoEdit": bool(no_edit),
        "isProjectCreation": project_creation,
        "needsClarification": needs_clarification,
        "riskClass": risk,
        "signals": {
            "change": change,
            "read": read,
            "noEdit": no_edit,
            "conditionalEdit": conditional_edit,
        },
    }


def _normalize_problem(problem: dict[str, Any], state: PipelineState) -> dict[str, Any]:
    intent = state.get("taskIntent") or _detect_task_intent(state["task"], state.get("preflight"))
    normalized = dict(problem or {})
    llm_task_type = str(normalized.get("taskType", "")).lower()
    llm_requires_worker = llm_task_type in {"modify", "edit", "create", "implement", "fix", "refactor", "build", "scaffold", "command"}
    requires_worker = bool(intent.get("requiresWorker") or (llm_requires_worker and not intent.get("explicitNoEdit")))
    if requires_worker:
        normalized["taskType"] = "create" if intent.get("isProjectCreation") else ("command" if intent.get("mode") == "command" else "modify")
    elif intent.get("mode") in {"review", "answer"}:
        normalized["taskType"] = intent["mode"]
    else:
        normalized["taskType"] = normalized.get("taskType") or "question"
    normalized["requiresWorker"] = requires_worker
    normalized["readOnly"] = not requires_worker
    normalized["taskIntent"] = intent
    normalized["riskClass"] = _merge_risk(str(normalized.get("riskClass", "medium")), str(intent.get("riskClass", "medium")))
    normalized.setdefault("problemStatement", state["task"])
    for key in ("constraints", "relevantFiles", "likelyCommands", "acceptanceCriteria", "ambiguities", "nonGoals"):
        if not isinstance(normalized.get(key), list):
            normalized[key] = []
    if intent.get("explicitNoEdit") and "Do not modify files; answer/report only." not in normalized["constraints"]:
        normalized["constraints"].append("Do not modify files; answer/report only.")
    if intent.get("needsClarification") and "Task is underspecified; ask a clarifying question before editing." not in normalized["constraints"]:
        normalized["constraints"].append("Task is underspecified; ask a clarifying question before editing.")
    return normalized


def _is_read_only(task: str, problem: dict[str, Any], intent: dict[str, Any] | None = None) -> bool:
    intent = intent or problem.get("taskIntent") or _detect_task_intent(task)
    if intent.get("explicitNoEdit"):
        return True
    if intent.get("requiresWorker"):
        return False
    task_type = str(problem.get("taskType", "")).lower()
    return task_type in {"question", "review", "explain", "answer"} or bool(intent.get("readOnly"))


def _is_project_creation_task(task: str, problem: dict[str, Any]) -> bool:
    value = f"{task} {problem.get('problemStatement', '')}".lower()
    return any(word in value for word in ["tạo", "thiết kế", "làm ra", "build", "create", "scaffold"]) and any(
        word in value for word in ["web", "app", "todo", "ứng dụng", "application"]
    )


def _default_project_dir(task: str) -> str:
    value = task.lower()
    if "todo" in value or "to-do" in value:
        return "todo-app"
    return "app"


def _normalize_worker_task_spec(final: dict[str, Any], state: PipelineState) -> dict[str, Any]:
    spec = dict(final.get("workerTaskSpec") or {})
    if _is_project_creation_task(state["task"], state["problem"]):
        target = str(spec.get("targetProjectDir") or spec.get("projectRoot") or _default_project_dir(state["task"])).strip().strip("/\\")
        spec["targetProjectDir"] = target
        spec.setdefault("projectRoot", target)
        spec.setdefault("verificationCwd", target)
        allowed = list(spec.get("allowedFiles") or [])
        if not allowed:
            allowed = [f"{target}/**"]
        spec["allowedFiles"] = allowed
        commands = [command for command in (spec.get("verificationCommands") or []) if isinstance(command, str)]
        if not any(command.strip().lower() == "npm run build" for command in commands):
            commands.append("npm run build")
        spec["verificationCommands"] = commands
        constraints = list(spec.get("constraints") or [])
        constraints.append("Scaffold/setup commands may be used by the OpenHands worker, but verification must run from targetProjectDir.")
        constraints.append("Do not use npm run dev/npm start as verification; they are long-running dev server commands.")
        spec["constraints"] = list(dict.fromkeys(constraints))
    final["workerTaskSpec"] = spec
    return final


def build_graph(emit: Callable[[str, str], None], checkpointer: Any):
    def preflight(state: PipelineState) -> dict[str, Any]:
        emit("preflight", "Repo snapshot + trusted context")
        snapshot = get_snapshot(state["workspacePath"])
        task_intent = _detect_task_intent(state["task"], snapshot)
        mode = task_intent.get("mode")
        route = "worker" if task_intent.get("requiresWorker") else "read-only"
        emit("task_intent", f"{mode} -> {route}; risk={task_intent.get('riskClass', 'medium')}")
        return {
            "preflight": snapshot,
            "taskIntent": task_intent,
            "trustedRepoContext": trusted_context(state["workspacePath"], snapshot),
            "retryCount": 0,
        }

    def codegraph_context_node(state: PipelineState) -> dict[str, Any]:
        emit("codegraph_context", "Checking semantic repo index")
        context = codegraph_context(state["workspacePath"], state["task"], auto_init=True)
        if context.get("enabled"):
            if context.get("autoInitialized"):
                emit("codegraph_context", "Index initialized for this workspace")
            detail = "Context ready"
            if context.get("truncated"):
                detail += " (truncated)"
            emit("codegraph_context", detail)
        else:
            emit("codegraph_context", f"Skipped: {context.get('reason') or context.get('status')}")
        return {"codegraphContext": context}

    def intake_user_intent(state: PipelineState) -> dict[str, Any]:
        emit("intake_user_intent", "Read-only user intent")
        finding = _json(
            state,
            "Read-only Intake Agent A: identify user intent. Return JSON with goal, taskType, expectedOutcome, nonGoals.\n"
            + json.dumps({"task": state["task"], "deterministicTaskIntent": state["taskIntent"], "snapshot": state["preflight"]}, ensure_ascii=False),
            {"goal": state["task"], "taskType": "modify", "expectedOutcome": "", "nonGoals": []},
        )
        return {"intakeFindings": [{"agent": "user_intent", **finding}]}

    def intake_ambiguity(state: PipelineState) -> dict[str, Any]:
        emit("intake_ambiguity", "Read-only ambiguity and edge cases")
        finding = _json(
            state,
            "Read-only Intake Agent B: find ambiguities, edge cases, and risk. Return JSON with ambiguities[], assumptions[], riskClass, needsHumanApproval.\n"
            + json.dumps({"task": state["task"], "deterministicTaskIntent": state["taskIntent"], "snapshot": state["preflight"]}, ensure_ascii=False),
            {"ambiguities": [], "assumptions": [], "riskClass": "medium", "needsHumanApproval": False},
        )
        return {"intakeFindings": [{"agent": "ambiguity_edge_cases", **finding}]}

    def intake_repo_context(state: PipelineState) -> dict[str, Any]:
        emit("intake_repo_context", "Read-only trusted repo context")
        finding = _json(
            state,
            "Read-only Intake Agent C: use trusted repo context and snapshot. Return JSON with relevantFiles[], likelyCommands[], repoConventions[], warnings[].\n"
            + json.dumps(
                {
                    "task": state["task"],
                    "deterministicTaskIntent": state["taskIntent"],
                    "trustedRepoContext": state["trustedRepoContext"],
                    "codegraphContext": state.get("codegraphContext"),
                    "snapshot": state["preflight"],
                },
                ensure_ascii=False,
            ),
            {"relevantFiles": [], "likelyCommands": [], "repoConventions": [], "warnings": []},
        )
        return {"intakeFindings": [{"agent": "trusted_repo_context", **finding}]}

    def intake_synthesizer(state: PipelineState) -> dict[str, Any]:
        emit("intake_synthesizer", "Problem statement + repro + risk class")
        problem = _json(
            state,
            "Intake Synthesizer: merge findings. Return JSON with problemStatement, taskType, observedBehavior, expectedBehavior, repro, constraints[], riskClass, relevantFiles[], likelyCommands[], acceptanceCriteria[].\n"
            "Respect deterministicTaskIntent for readOnly/requiresWorker; do not classify a task as read-only when it contains explicit edit/fix/create signals.\n"
            + json.dumps({"task": state["task"], "deterministicTaskIntent": state["taskIntent"], "codegraphContext": state.get("codegraphContext"), "findings": state["intakeFindings"]}, ensure_ascii=False),
            {"problemStatement": state["task"], "taskType": "modify", "constraints": [], "riskClass": "medium", "relevantFiles": [], "likelyCommands": [], "acceptanceCriteria": []},
        )
        problem = _normalize_problem(problem, state)
        return {"problem": problem}

    def plan_node(name: str, focus: str):
        def node(state: PipelineState) -> dict[str, Any]:
            emit(f"planning_{name}", focus)
            plan = _json(
                state,
                f"Read-only Planning Agent {name}: {focus}. Return JSON with name, rationale, steps[], filesToRead[], filesLikelyToEdit[], commandsToRun[], risks[].\n"
                + json.dumps({"taskIntent": state["taskIntent"], "problem": state["problem"], "codegraphContext": state.get("codegraphContext"), "snapshot": state["preflight"]}, ensure_ascii=False),
                {"name": name, "steps": [], "filesToRead": [], "filesLikelyToEdit": [], "commandsToRun": [], "risks": []},
            )
            return {"candidatePlans": [{"agent": name, **plan}]}

        return node

    def critique_node(name: str, focus: str):
        def node(state: PipelineState) -> dict[str, Any]:
            emit(f"critique_{name}", focus)
            critique = _json(
                state,
                f"Critique Layer {name}: {focus}. Return JSON with blockers[], warnings[], riskClass, acceptanceCriteria[], reviewFocus[], requiredCommands[].\n"
                + json.dumps({"taskIntent": state["taskIntent"], "problem": state["problem"], "candidatePlans": state["candidatePlans"]}, ensure_ascii=False),
                {"blockers": [], "warnings": [], "riskClass": state["problem"].get("riskClass", "medium"), "acceptanceCriteria": [], "reviewFocus": [], "requiredCommands": []},
            )
            return {"critiqueFindings": [{"agent": name, **critique}]}

        return node

    def plan_arbiter(state: PipelineState) -> dict[str, Any]:
        emit("plan_arbiter", "Final plan + acceptance criteria + worker task spec")
        final = _json(
            state,
            "Plan Arbiter: choose final plan and produce workerTaskSpec. Return JSON with selectedPlanName, finalSteps[], riskClass, humanGateReason, workerTaskSpec{objective, filesToRead[], allowedFiles[], forbiddenPaths[], commandsToRun[], verificationCommands[], acceptanceCriteria[], constraints[], maxReworkAttempts}.\n"
            "The workerTaskSpec must be a machine-executable contract: objective, allowed paths, forbidden actions, expected files, verification commands, definition of done, and human escalation conditions.\n"
            "For new web apps, set workerTaskSpec.targetProjectDir and verificationCwd to the app folder such as todo-app. Keep scaffold/setup/dev-server commands out of verificationCommands. Use verificationCommands only for build/test/check commands such as npm run build.\n"
            + json.dumps({"taskIntent": state["taskIntent"], "problem": state["problem"], "candidatePlans": state["candidatePlans"], "critiques": state["critiqueFindings"]}, ensure_ascii=False),
            {
                "selectedPlanName": "minimal",
                "finalSteps": [],
                "riskClass": state["problem"].get("riskClass", "medium"),
                "humanGateReason": "",
                "workerTaskSpec": {
                    "objective": state["problem"].get("problemStatement", state["task"]),
                    "filesToRead": state["problem"].get("relevantFiles", []),
                    "allowedFiles": [],
                    "forbiddenPaths": [],
                    "commandsToRun": [],
                    "verificationCommands": state["problem"].get("likelyCommands", []),
                    "acceptanceCriteria": state["problem"].get("acceptanceCriteria", []),
                    "constraints": state["problem"].get("constraints", []),
                    "maxReworkAttempts": 1,
                },
            },
        )
        final = _normalize_worker_task_spec(final, state)
        final["riskClass"] = _merge_risk(str(final.get("riskClass", "medium")), str(state["problem"].get("riskClass", "medium")))
        return {"finalPlan": final}

    def planner_task_graph(state: PipelineState) -> dict[str, Any]:
        emit("planner_agent", "Task graph + role contracts")
        task_graph = build_task_graph(state["task"], state["problem"], state["finalPlan"])
        with _open_broker() as broker:
            run_id = broker.create_run(
                session_id=state["sessionId"],
                task=state["task"],
                task_graph=task_graph,
                correlation_id=state.get("correlationId"),
            )
            broker.dispatch_subtasks(run_id, task_graph["subtasks"])
            planner = broker.start_role(run_id, "planner", "Create task graph and role routing", {"task": state["task"]})
            broker.complete_subtask(
                run_id,
                planner["id"],
                "planner",
                {"taskGraphVersion": task_graph["version"], "roles": task_graph["roles"], "subtaskCount": len(task_graph["subtasks"])},
            )
            events = broker.events(run_id)
        return {
            "taskGraph": task_graph,
            "agentContracts": task_graph.get("contracts", {}),
            "brokerRunId": run_id,
            "brokerEvents": events,
        }

    def researcher_context_agent(state: PipelineState) -> dict[str, Any]:
        emit("researcher_agent", "Repository context ownership")
        output = researcher_output(state["problem"], state.get("trustedRepoContext") or {}, state.get("codegraphContext"))
        run_id = state.get("brokerRunId")
        events = state.get("brokerEvents", [])
        if run_id:
            with _open_broker() as broker:
                subtask = broker.start_role(run_id, "researcher_context", "Ground task in trusted repository context", {"problem": state["problem"]})
                broker.complete_subtask(run_id, subtask["id"], "researcher_context", output)
                events = broker.events(run_id)
        return {"researcherContext": output, "brokerEvents": events}

    def governance_service(state: PipelineState) -> dict[str, Any]:
        emit("governance", "Approval policy and sensitive action routing")
        decision = governance_decision(state["task"], state["problem"], state["finalPlan"], state.get("taskGraph") or {})
        run_id = state.get("brokerRunId")
        events = state.get("brokerEvents", [])
        if run_id:
            with _open_broker() as broker:
                broker.record_event(run_id, None, "governance", "governance_decision", decision)
                events = broker.events(run_id)
        return {"governanceDecision": decision, "brokerEvents": events}

    def human_gate(state: PipelineState) -> dict[str, Any]:
        governance = state.get("governanceDecision") or {}
        risk = _merge_risk(
            str(state["finalPlan"].get("riskClass", state["problem"].get("riskClass", "medium"))),
            str(governance.get("riskClass", "medium")),
        )
        auto_confirm = bool((state.get("settings") or {}).get("autoConfirmHumanGate"))
        approval = state.get("humanGateApproval") or {}
        approved = str(approval.get("status") or "").lower() == "approved"
        needs_approval = risk == "high" or bool(governance.get("needsApproval"))
        if needs_approval and auto_confirm:
            emit("human_gate", "Auto-confirm enabled; gate passed")
            return {}
        if needs_approval and approved:
            emit("human_gate", "Durable approval found; gate passed")
            created_ms = telemetry.parse_iso_ms(approval.get("createdAt"))
            approved_ms = telemetry.parse_iso_ms(approval.get("approvedAt"))
            if created_ms is not None and approved_ms is not None:
                telemetry.record_approval_latency(max(0.0, approved_ms - created_ms), risk)
            return {}
        if needs_approval:
            emit("human_gate", "High-risk task requires confirmation")
            return {
                "result": {
                    "assistantText": "Tác vụ high-risk nên workflow dừng ở Human Gate. Gửi “xác nhận” trong cùng phiên để phê duyệt và chạy tiếp task gốc.",
                    "changedFiles": [],
                    "commandResults": [],
                    "review": None,
                    "humanGate": {
                        "status": "pending",
                        "originalTask": state["task"],
                        "correlationId": state.get("correlationId", ""),
                        "riskClass": risk,
                        "reason": state["finalPlan"].get("humanGateReason") or governance.get("approvalPolicy") or state["problem"].get("riskClass", "high"),
                        "createdAt": datetime.now(timezone.utc).isoformat(),
                    },
                }
            }
        emit("human_gate", "Gate passed")
        return {}

    def read_only_reporter(state: PipelineState) -> dict[str, Any]:
        emit("reporter", "Read-only answer")
        answer = _client(state).chat(
            [
                {"role": "system", "content": "Answer in Vietnamese, concise and practical."},
                {"role": "user", "content": json.dumps({"task": state["task"], "problem": state["problem"], "codegraphContext": state.get("codegraphContext"), "finalPlan": state["finalPlan"]}, ensure_ascii=False)},
            ]
        )
        return {"result": {"assistantText": answer, "changedFiles": [], "commandResults": [], "review": None}}

    def load_context_files(state: PipelineState) -> dict[str, Any]:
        emit("context", "Loading worker context files")
        spec = state["finalPlan"].get("workerTaskSpec", {})
        paths = list(
            dict.fromkeys(
                (state["problem"].get("relevantFiles") or [])
                + (spec.get("filesToRead") or [])
                + [path for path in (spec.get("allowedFiles") or []) if _is_literal_context_path(path)]
            )
        )[:12]
        files = []
        for path in paths:
            try:
                files.append({"path": path, "content": read_file(state["workspacePath"], path, 18000)})
            except Exception as exc:
                files.append({"path": path, "error": str(exc)})
        return {"contextFiles": files}

    def openhands_worker(state: PipelineState) -> dict[str, Any]:
        spec = state["finalPlan"].get("workerTaskSpec", {})
        emit("coder_agent", "Sandboxed OpenHands coding worker")
        run_id = state.get("brokerRunId")
        subtask_id = None
        if run_id:
            with _open_broker() as broker:
                subtask = broker.start_role(run_id, "coder", "Implement code changes inside allowedFiles", {"workerTaskSpec": spec, "researcherContext": state.get("researcherContext")})
                subtask_id = subtask["id"]
        worker_result = run_openhands_worker(
            workspace=state["workspacePath"],
            server_url=state["settings"]["serverUrl"],
            model=state["settings"]["model"],
            api_key=state["settings"].get("apiKey", ""),
            worker_task_spec={**spec, "contextFiles": state.get("contextFiles", []), "codegraphContext": state.get("codegraphContext")},
            rework_context=state.get("latestReview"),
            emit=emit,
        )
        events = state.get("brokerEvents", [])
        if run_id and subtask_id:
            with _open_broker() as broker:
                broker.complete_subtask(run_id, subtask_id, "coder", worker_result, "failed" if worker_result.get("error") else "completed")
                events = broker.events(run_id)
        return {"workerAttempts": [worker_result], "retryCount": state.get("retryCount", 0) + 1, "brokerEvents": events}

    def tester_agent(state: PipelineState) -> dict[str, Any]:
        emit("tester_agent", "Sandboxed verification")
        latest = state["workerAttempts"][-1]
        spec = state["finalPlan"].get("workerTaskSpec", {})
        raw_commands = list(dict.fromkeys((spec.get("commandsToRun") or []) + (spec.get("verificationCommands") or [])))
        commands = normalize_verification_commands(state["workspacePath"], raw_commands, latest, spec)
        command_results = [
            run_sandboxed_command(state["workspacePath"], item["command"], cwd=item.get("cwd", "."))
            for item in commands
        ]
        affected = codegraph_affected_tests(state["workspacePath"], latest.get("changedFiles") or [])
        if affected.get("enabled") and affected.get("status") == "ok":
            emit("codegraph_affected", "Affected test candidates ready")
        review = _json(
            state,
            "Tester Agent: interpret sandboxed verification results. Return JSON with blockers[], warnings[], passed boolean, finalMessage.\n"
            + json.dumps(
                {
                    "problem": state["problem"],
                    "workerTaskSpec": spec,
                    "workerResult": latest,
                    "verificationCommands": commands,
                    "commandResults": command_results,
                    "codegraphAffectedTests": affected,
                    "reviewPolicy": "Scaffold/setup/dev-server commands are intentionally excluded from verification. Review only commandResults and changedFiles.",
                },
                ensure_ascii=False,
            ),
            {"blockers": [], "warnings": [], "passed": True, "finalMessage": ""},
        )
        if latest.get("error"):
            review.setdefault("blockers", []).append(f"Coder agent error: {latest['error']}")
            review["passed"] = False
        if any((not item.get("skipped")) and (item.get("timedOut") or item.get("code") not in (0, None)) for item in command_results):
            review.setdefault("blockers", []).append("At least one verification command failed.")
            review["passed"] = False
        tester_result = {**review, "commandResults": command_results, "codegraphAffectedTests": affected}
        telemetry.record_verification(bool(tester_result.get("passed")))
        run_id = state.get("brokerRunId")
        events = state.get("brokerEvents", [])
        if run_id:
            with _open_broker() as broker:
                subtask = broker.start_role(run_id, "tester", "Run verification in sandbox", {"commands": commands})
                broker.complete_subtask(run_id, subtask["id"], "tester", tester_result, "failed" if tester_result.get("blockers") else "completed")
                events = broker.events(run_id)
        return {"testerResult": tester_result, "brokerEvents": events}

    def security_reviewer_agent(state: PipelineState) -> dict[str, Any]:
        emit("security_reviewer", "Security and policy review")
        latest = state["workerAttempts"][-1]
        fallback = security_review_fallback(state["problem"], latest, state.get("testerResult") or {}, state.get("governanceDecision") or {})
        with create_workspace_sandbox(state["workspacePath"]):
            review = _json(
                state,
                "Security Reviewer Agent: review policy, auth, secret, permission, injection, destructive action, and sandbox violations. Return JSON with blockers[], warnings[], riskClass, reviewFocus[], passed boolean.\n"
                + json.dumps(
                    {
                        "problem": state["problem"],
                        "workerTaskSpec": state["finalPlan"].get("workerTaskSpec", {}),
                        "workerResult": latest,
                        "testerResult": state.get("testerResult"),
                        "governanceDecision": state.get("governanceDecision"),
                        "sandbox": {"enabled": True, "mode": "ephemeral-read-only-copy"},
                    },
                    ensure_ascii=False,
                ),
                fallback,
            )
        review["blockers"] = list(dict.fromkeys([*map(str, fallback.get("blockers") or []), *map(str, review.get("blockers") or [])]))
        review["warnings"] = list(dict.fromkeys([*map(str, fallback.get("warnings") or []), *map(str, review.get("warnings") or [])]))
        review["sandboxed"] = True
        review["passed"] = not review.get("blockers")
        run_id = state.get("brokerRunId")
        events = state.get("brokerEvents", [])
        if run_id:
            with _open_broker() as broker:
                subtask = broker.start_role(run_id, "security_reviewer", "Review security and policy risk", {"changedFiles": latest.get("changedFiles", [])})
                broker.complete_subtask(run_id, subtask["id"], "security_reviewer", review, "failed" if review.get("blockers") else "completed")
                events = broker.events(run_id)
        return {"securityReview": review, "brokerEvents": events}

    def code_reviewer_agent(state: PipelineState) -> dict[str, Any]:
        emit("code_reviewer", "Correctness and merge readiness")
        fallback = code_review_fallback(state.get("testerResult") or {}, state.get("securityReview") or {})
        review = _json(
            state,
            "Code Reviewer Agent: decide correctness, maintainability, regression risk, and merge readiness. Return JSON with blockers[], warnings[], passed boolean, finalMessage.\n"
            + json.dumps(
                {
                    "problem": state["problem"],
                    "changedFiles": (state["workerAttempts"][-1] if state.get("workerAttempts") else {}).get("changedFiles", []),
                    "testerResult": state.get("testerResult"),
                    "securityReview": state.get("securityReview"),
                    "acceptanceCriteria": (state["finalPlan"].get("workerTaskSpec") or {}).get("acceptanceCriteria", []),
                },
                ensure_ascii=False,
            ),
            fallback,
        )
        review["blockers"] = list(dict.fromkeys([*map(str, fallback.get("blockers") or []), *map(str, review.get("blockers") or [])]))
        review["warnings"] = list(dict.fromkeys([*map(str, fallback.get("warnings") or []), *map(str, review.get("warnings") or [])]))
        review["passed"] = not review.get("blockers") and bool(review.get("passed", True))
        run_id = state.get("brokerRunId")
        events = state.get("brokerEvents", [])
        if run_id:
            with _open_broker() as broker:
                subtask = broker.start_role(run_id, "code_reviewer", "Review correctness and merge readiness", {"testerResult": state.get("testerResult")})
                broker.complete_subtask(run_id, subtask["id"], "code_reviewer", review, "failed" if review.get("blockers") else "completed")
                events = broker.events(run_id)
        return {"codeReview": review, "brokerEvents": events}

    def release_deploy_agent(state: PipelineState) -> dict[str, Any]:
        emit("release_deploy_agent", "Release and rollback plan")
        latest = state["workerAttempts"][-1] if state.get("workerAttempts") else {}
        plan = release_deploy_plan(state["finalPlan"], state.get("codeReview") or {}, latest.get("changedFiles") or [])
        run_id = state.get("brokerRunId")
        events = state.get("brokerEvents", [])
        if run_id:
            with _open_broker() as broker:
                subtask = broker.start_role(run_id, "release_deploy", "Prepare release/deploy and rollback notes", {"riskClass": state["finalPlan"].get("riskClass")})
                broker.complete_subtask(run_id, subtask["id"], "release_deploy", plan)
                events = broker.events(run_id)
        return {"releasePlan": plan, "brokerEvents": events}

    def reviewer_decision_node(state: PipelineState) -> dict[str, Any]:
        emit("reviewer_decision", "Merge or rollback decision")
        decision = aggregate_reviewer_decision(state.get("testerResult") or {}, state.get("securityReview") or {}, state.get("codeReview") or {}, state.get("releasePlan") or {})
        latest = state["workerAttempts"][-1] if state.get("workerAttempts") else {}
        review = {
            **decision,
            "commandResults": (state.get("testerResult") or {}).get("commandResults", []),
            "codegraphAffectedTests": (state.get("testerResult") or {}).get("codegraphAffectedTests", {}),
            "securityReview": state.get("securityReview"),
            "codeReview": state.get("codeReview"),
            "releasePlan": state.get("releasePlan"),
            "brokerEvents": state.get("brokerEvents", []),
        }
        if latest.get("error"):
            review.setdefault("blockers", []).append(f"Coder agent error: {latest['error']}")
            review["passed"] = False
        run_id = state.get("brokerRunId")
        events = state.get("brokerEvents", [])
        if run_id:
            with _open_broker() as broker:
                broker.record_event(run_id, None, "reviewer", "reviewer_decision", {"passed": review.get("passed"), "blockers": review.get("blockers", [])})
                spec = state["finalPlan"].get("workerTaskSpec", {})
                max_rework = min(2, int(spec.get("maxReworkAttempts") or 1))
                will_rework = bool(review.get("blockers")) and state.get("retryCount", 0) <= max_rework
                status = "needs_rework" if will_rework else ("completed" if review.get("passed") else "blocked")
                broker.finish_run(run_id, status, {"passed": review.get("passed"), "willRework": will_rework})
                events = broker.events(run_id)
                review["brokerEvents"] = events
        return {"latestReview": review, "reviewerDecision": decision, "reviewFindings": [review], "brokerEvents": events}

    def reporter(state: PipelineState) -> dict[str, Any]:
        emit("reporter", "Final report")
        attempts = state.get("workerAttempts", [])
        latest = attempts[-1] if attempts else {}
        review = state.get("latestReview", {})
        changed = latest.get("changedFiles", [])
        lines = [latest.get("summary") or state["problem"].get("problemStatement", state["task"])]
        roles = (state.get("taskGraph") or {}).get("roles") or []
        if roles:
            lines.append("\nMulti-agent roles:")
            lines.append("- " + ", ".join(map(str, roles)))
        if changed:
            lines.append("\nFile đã thay đổi:")
            for item in changed:
                lines.append(f"- {item.get('status')}: {item.get('path')}")
        else:
            lines.append("\nKhông có file nào bị thay đổi.")
        if review.get("commandResults"):
            lines.append("\nLệnh verification:")
            for item in review["commandResults"]:
                status = f"skipped: {item.get('reason')}" if item.get("skipped") else ("timeout" if item.get("timedOut") else f"exit {item.get('code')}")
                cwd = item.get("cwd") or "."
                lines.append(f"- ({cwd}) {item.get('command')}: {status}")
        affected = review.get("codegraphAffectedTests") or {}
        if affected.get("enabled") and affected.get("status") == "ok":
            lines.append("\nCodeGraph affected tests:")
            raw = str(affected.get("raw") or "").strip()
            lines.append(raw if raw else "- Không có test liên quan được phát hiện.")
        blockers = review.get("blockers") or []
        warnings = review.get("warnings") or []
        if blockers:
            lines.append("\nBlocker: " + "; ".join(map(str, blockers)))
        if warnings:
            lines.append("\nLưu ý: " + "; ".join(map(str, warnings)))
        if review.get("finalMessage"):
            lines.append("\n" + str(review["finalMessage"]))
        release_plan = review.get("releasePlan") or {}
        if release_plan:
            lines.append("\nRelease/rollback:")
            for note in release_plan.get("releaseNotes") or []:
                lines.append(f"- {note}")
            if release_plan.get("rollbackPlan"):
                lines.append(f"- Rollback: {release_plan.get('rollbackPlan')}")
        return {
            "result": {
                "assistantText": "\n".join(lines),
                "changedFiles": changed,
                "commandResults": review.get("commandResults", []),
                "review": review,
                "reworkAttempts": attempts,
            }
        }

    def route_after_gate(state: PipelineState) -> str:
        if state.get("result"):
            return "reporter_end"
        if _is_read_only(state["task"], state["problem"], state.get("taskIntent")):
            return "read_only_reporter"
        return "load_context_files"

    def route_after_review(state: PipelineState) -> str:
        review = state.get("latestReview", {})
        attempts = state.get("workerAttempts", [])
        if attempts and attempts[-1].get("error"):
            return "reporter"
        spec = state["finalPlan"].get("workerTaskSpec", {})
        max_rework = min(2, int(spec.get("maxReworkAttempts") or 1))
        if review.get("blockers") and state.get("retryCount", 0) <= max_rework:
            telemetry.record_rework()
            return "openhands_worker"
        return "reporter"

    agent_roles = {
        "preflight": "orchestrator",
        "codegraph_context": "researcher_context",
        "intake_user_intent": "intake",
        "intake_ambiguity": "intake",
        "intake_repo_context": "intake",
        "intake_synthesizer": "intake",
        "planning_minimal": "planner",
        "planning_robust": "planner",
        "planning_test_first": "planner",
        "critique_risk": "critic",
        "critique_test_coverage": "critic",
        "critique_security_regression": "critic",
        "plan_arbiter": "planner",
        "planner_task_graph": "planner",
        "researcher_context_agent": "researcher_context",
        "governance_service": "governance",
        "human_gate": "governance",
        "read_only_reporter": "reporter",
        "load_context_files": "researcher_context",
        "openhands_worker": "coder",
        "tester_agent": "tester",
        "security_reviewer_agent": "security_reviewer",
        "code_reviewer_agent": "code_reviewer",
        "release_deploy_agent": "release_deploy",
        "reviewer_decision": "reviewer",
        "reporter": "reporter",
        "reporter_end": "reporter",
    }

    def traced_node(node_name: str, fn: Callable[[PipelineState], dict[str, Any]]):
        def wrapped(state: PipelineState) -> dict[str, Any]:
            telemetry.set_correlation_id(state.get("correlationId"))
            with telemetry.start_span(
                "agent.step",
                {
                    "agent.step": node_name,
                    "agent.role": agent_roles.get(node_name, "agent"),
                    "session.id": state.get("sessionId", ""),
                    "workspace.path": state.get("workspacePath", ""),
                    "broker.run_id": state.get("brokerRunId", ""),
                },
            ):
                return fn(state)

        return wrapped

    builder = StateGraph(PipelineState)
    builder.add_node("preflight", traced_node("preflight", preflight))
    builder.add_node("codegraph_context", traced_node("codegraph_context", codegraph_context_node))
    builder.add_node("intake_user_intent", traced_node("intake_user_intent", intake_user_intent))
    builder.add_node("intake_ambiguity", traced_node("intake_ambiguity", intake_ambiguity))
    builder.add_node("intake_repo_context", traced_node("intake_repo_context", intake_repo_context))
    builder.add_node("intake_synthesizer", traced_node("intake_synthesizer", intake_synthesizer))
    builder.add_node("planning_minimal", traced_node("planning_minimal", plan_node("minimal", "minimal plan")))
    builder.add_node("planning_robust", traced_node("planning_robust", plan_node("robust", "robust plan")))
    builder.add_node("planning_test_first", traced_node("planning_test_first", plan_node("test_first", "test-first plan")))
    builder.add_node("critique_risk", traced_node("critique_risk", critique_node("risk", "risk")))
    builder.add_node("critique_test_coverage", traced_node("critique_test_coverage", critique_node("test_coverage", "test coverage")))
    builder.add_node("critique_security_regression", traced_node("critique_security_regression", critique_node("security_regression", "security/regression")))
    builder.add_node("plan_arbiter", traced_node("plan_arbiter", plan_arbiter))
    builder.add_node("planner_task_graph", traced_node("planner_task_graph", planner_task_graph))
    builder.add_node("researcher_context_agent", traced_node("researcher_context_agent", researcher_context_agent))
    builder.add_node("governance_service", traced_node("governance_service", governance_service))
    builder.add_node("human_gate", traced_node("human_gate", human_gate))
    builder.add_node("read_only_reporter", traced_node("read_only_reporter", read_only_reporter))
    builder.add_node("load_context_files", traced_node("load_context_files", load_context_files))
    builder.add_node("openhands_worker", traced_node("openhands_worker", openhands_worker))
    builder.add_node("tester_agent", traced_node("tester_agent", tester_agent))
    builder.add_node("security_reviewer_agent", traced_node("security_reviewer_agent", security_reviewer_agent))
    builder.add_node("code_reviewer_agent", traced_node("code_reviewer_agent", code_reviewer_agent))
    builder.add_node("release_deploy_agent", traced_node("release_deploy_agent", release_deploy_agent))
    builder.add_node("reviewer_decision", traced_node("reviewer_decision", reviewer_decision_node))
    builder.add_node("reporter", traced_node("reporter", reporter))
    builder.add_node("reporter_end", traced_node("reporter_end", lambda state: {}))

    builder.add_edge(START, "preflight")
    builder.add_edge("preflight", "codegraph_context")
    builder.add_edge("codegraph_context", "intake_user_intent")
    builder.add_edge("codegraph_context", "intake_ambiguity")
    builder.add_edge("codegraph_context", "intake_repo_context")
    builder.add_edge(["intake_user_intent", "intake_ambiguity", "intake_repo_context"], "intake_synthesizer")
    builder.add_edge("intake_synthesizer", "planning_minimal")
    builder.add_edge("intake_synthesizer", "planning_robust")
    builder.add_edge("intake_synthesizer", "planning_test_first")
    builder.add_edge(["planning_minimal", "planning_robust", "planning_test_first"], "critique_risk")
    builder.add_edge(["planning_minimal", "planning_robust", "planning_test_first"], "critique_test_coverage")
    builder.add_edge(["planning_minimal", "planning_robust", "planning_test_first"], "critique_security_regression")
    builder.add_edge(["critique_risk", "critique_test_coverage", "critique_security_regression"], "plan_arbiter")
    builder.add_edge("plan_arbiter", "planner_task_graph")
    builder.add_edge("planner_task_graph", "researcher_context_agent")
    builder.add_edge("researcher_context_agent", "governance_service")
    builder.add_edge("governance_service", "human_gate")
    builder.add_conditional_edges(
        "human_gate",
        route_after_gate,
        {"reporter_end": "reporter_end", "read_only_reporter": "read_only_reporter", "load_context_files": "load_context_files"},
    )
    builder.add_edge("read_only_reporter", END)
    builder.add_edge("reporter_end", END)
    builder.add_edge("load_context_files", "openhands_worker")
    builder.add_edge("openhands_worker", "tester_agent")
    builder.add_edge("tester_agent", "security_reviewer_agent")
    builder.add_edge("security_reviewer_agent", "code_reviewer_agent")
    builder.add_edge("code_reviewer_agent", "release_deploy_agent")
    builder.add_edge("release_deploy_agent", "reviewer_decision")
    builder.add_conditional_edges("reviewer_decision", route_after_review, {"openhands_worker": "openhands_worker", "reporter": "reporter"})
    builder.add_edge("reporter", END)

    return builder.compile(checkpointer=checkpointer)


def _run_metric_status(result: PipelineState) -> str:
    human_gate_status = ((result.get("result") or {}).get("humanGate") or {}).get("status")
    if human_gate_status == "pending":
        return "pending_approval"
    review = result.get("latestReview") or (result.get("result") or {}).get("review") or {}
    if review.get("passed") is True:
        return "success"
    if review.get("blockers"):
        return "blocked"
    attempts = result.get("workerAttempts") or []
    if attempts and attempts[-1].get("error"):
        return "error"
    return "success"


def run_pipeline(payload: dict[str, Any], emit: Callable[[str, str], None]) -> dict[str, Any]:
    telemetry.configure_telemetry()
    correlation_id = telemetry.set_correlation_id(payload.get("correlationId"))
    telemetry.reset_token_usage()
    state: PipelineState = {
        "task": payload["content"],
        "workspacePath": payload["workspacePath"],
        "settings": payload["settings"],
        "humanGateApproval": payload.get("humanGateApproval") or {},
        "messages": payload.get("messages", []),
        "sessionId": payload.get("sessionId") or str(uuid.uuid4()),
        "correlationId": correlation_id,
    }
    started_ms = telemetry.now_ms()
    with telemetry.start_span(
        "agent.task",
        {
            "task.preview": state["task"][:160],
            "session.id": state["sessionId"],
            "workspace.path": state["workspacePath"],
        },
    ) as span:
        try:
            with _open_checkpointer(emit) as checkpointer:
                graph = build_graph(emit, checkpointer)
                result = graph.invoke(state, config={"configurable": {"thread_id": state["sessionId"]}})
            status = _run_metric_status(result)
            telemetry.record_run_latency(telemetry.elapsed_ms(started_ms), status)
            if span:
                span.set_attribute("run.status", status)
        except Exception:
            telemetry.record_run_latency(telemetry.elapsed_ms(started_ms), "error")
            raise
    return {
        "id": str(uuid.uuid4()),
        "correlationId": correlation_id,
        "problem": result.get("problem"),
        "taskIntent": result.get("taskIntent"),
        "codegraphContext": result.get("codegraphContext"),
        "trustedRepoContext": result.get("trustedRepoContext"),
        "intake": result.get("intakeFindings", []),
        "plans": result.get("candidatePlans", []),
        "critiques": result.get("critiqueFindings", []),
        "finalPlan": result.get("finalPlan"),
        **(result.get("result") or {}),
        "tokenUsage": telemetry.get_token_usage(),
        "task": state["task"],
    }
