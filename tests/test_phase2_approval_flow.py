from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_engine.graph import run_pipeline


class Phase2ApprovalFlowTests(unittest.TestCase):
    def test_rework_approval_reuses_execution_id(self) -> None:
        root = Path(__file__).resolve().parents[1]
        backend = (root / "src" / "main" / "backendService.js").read_text(encoding="utf-8")
        main = (root / "src" / "main" / "main.js").read_text(encoding="utf-8")
        graph = (root / "engine" / "agent_engine" / "graph.py").read_text(encoding="utf-8")

        self.assertIn("humanGateApproval?.executionId || crypto.randomUUID()", backend)
        self.assertIn("executionId: pendingHumanGate.executionId || null", main)
        self.assertIn("(item.executionId || item.id) !== runIdentity", main)
        self.assertIn('"kind": "rework_limit"', graph)
        self.assertIn('"grantAdditionalAttempts": grant', graph)
        self.assertIn('f"{execution_id}:approval:"', graph)

    def test_write_task_continues_without_container_using_policy_limited_host_fallback(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir

            def fake_json(_self, prompt, fallback):
                result = copy.deepcopy(fallback)
                if "Plan Arbiter:" in prompt:
                    result["workerTaskSpec"] = {
                        **result["workerTaskSpec"],
                        "allowedFiles": ["app.py"],
                        "projectStack": "python",
                        "verificationCommands": ["python -m compileall ."],
                    }
                return result

            worker_modes: list[str] = []

            def fake_worker(**kwargs):
                envelope = kwargs["worker_task_spec"]["contextEnvelope"]["inputs"]
                worker_modes.append(envelope["executionEnvironment"]["executionMode"])
                Path(kwargs["workspace"], "app.py").write_text("print('host fallback')\n", encoding="utf-8")
                return {
                    "summary": "host fallback worker",
                    "error": None,
                    "changedFiles": [{"path": "app.py", "status": "modified"}],
                    "appliedChanges": [{"path": "app.py", "status": "modified"}],
                    "sandboxDiff": [{"path": "app.py", "status": "modified"}],
                    "policyViolations": [],
                    "verificationSpec": {
                        "projectStack": "python",
                        "verificationCommands": ["python -m compileall ."],
                        "verificationCwd": ".",
                    },
                    "events": [],
                }

            payload = {
                "executionId": "exec-host-fallback",
                "correlationId": "cid-host-fallback",
                "sessionId": "session-host-fallback",
                "content": "sửa app.py",
                "workspacePath": str(repo),
                "settings": {
                    "serverUrl": "http://model.invalid/v1",
                    "model": "test",
                    "apiKey": "",
                    "autoConfirmHumanGate": False,
                },
                "messages": [],
            }
            try:
                with (
                    mock.patch("agent_engine.llm_client.ChatClient.json", autospec=True, side_effect=fake_json),
                    mock.patch(
                        "agent_engine.graph.codegraph_context",
                        return_value={"enabled": False, "status": "disabled", "reason": "test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.container_status",
                        return_value={"ready": False, "runtime": None, "image": "test", "reason": "Docker or Podman unavailable"},
                    ),
                    mock.patch("agent_engine.graph.run_container_command") as container_command,
                    mock.patch(
                        "agent_engine.graph.run_command",
                        return_value={
                            "command": "python -m compileall .",
                            "cwd": ".",
                            "code": 0,
                            "stdout": "",
                            "stderr": "",
                            "timedOut": False,
                            "sandboxed": True,
                        },
                    ) as host_command,
                    mock.patch("agent_engine.graph.run_openhands_worker", side_effect=fake_worker),
                ):
                    result = run_pipeline(payload, lambda _stage, _detail: None)

                self.assertEqual(result["executionId"], "exec-host-fallback")
                self.assertEqual(worker_modes, ["host_fallback"])
                self.assertEqual(result["review"]["passed"], True)
                self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "print('host fallback')\n")
                container_command.assert_not_called()
                host_command.assert_called()
                worktrees = subprocess.run(
                    ["git", "worktree", "list", "--porcelain"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                self.assertEqual(worktrees.count("worktree "), 1)
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_rework_loop_stops_at_yaml_limit_and_emits_execution_gate(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            worker_calls: list[int] = []

            def fake_json(_self, prompt, fallback):
                result = copy.deepcopy(fallback)
                if "Plan Arbiter:" in prompt:
                    result["workerTaskSpec"] = {
                        **result["workerTaskSpec"],
                        "allowedFiles": ["app.py"],
                        "projectStack": "python",
                        "verificationCommands": ["python -m compileall ."],
                    }
                return result

            def fake_worker(**kwargs):
                worker_calls.append(kwargs["worker_attempt"])
                Path(kwargs["workspace"], "app.py").write_text(
                    f"print('attempt {len(worker_calls)}')\n",
                    encoding="utf-8",
                )
                return {
                    "summary": "attempt",
                    "error": None,
                    "changedFiles": [{"path": "app.py", "status": "modified"}],
                    "appliedChanges": [{"path": "app.py", "status": "modified"}],
                    "sandboxDiff": [{"path": "app.py", "status": "modified"}],
                    "policyViolations": [],
                    "verificationSpec": {
                        "projectStack": "python",
                        "verificationCommands": ["python -m compileall ."],
                        "verificationCwd": ".",
                    },
                    "events": [],
                }

            payload = {
                "executionId": "exec-rework-gate",
                "correlationId": "cid-rework-gate",
                "sessionId": "session-rework-gate",
                "content": "sửa app.py",
                "workspacePath": str(repo),
                "settings": {
                    "serverUrl": "http://model.invalid/v1",
                    "model": "test",
                    "apiKey": "",
                    "autoConfirmHumanGate": False,
                },
                "messages": [],
            }
            try:
                with (
                    mock.patch("agent_engine.llm_client.ChatClient.json", autospec=True, side_effect=fake_json),
                    mock.patch(
                        "agent_engine.graph.codegraph_context",
                        return_value={"enabled": False, "status": "disabled", "reason": "test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.container_status",
                        return_value={"ready": True, "runtime": "docker", "image": "python:test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.run_container_command",
                        return_value={
                            "command": "python -m compileall .",
                            "cwd": ".",
                            "code": 1,
                            "stdout": "",
                            "stderr": "forced failure",
                            "timedOut": False,
                            "sandboxed": True,
                        },
                    ),
                    mock.patch("agent_engine.graph.run_openhands_worker", side_effect=fake_worker),
                ):
                    result = run_pipeline(payload, lambda _stage, _detail: None)

                self.assertEqual(result["humanGate"]["status"], "pending")
                self.assertEqual(result["humanGate"]["kind"], "rework_limit")
                self.assertEqual(result["humanGate"]["retryCount"], 3)
                self.assertEqual(len(worker_calls), 3)
                self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "print('base')\n")

                approved_payload = {
                    **payload,
                    "humanGateApproval": {
                        **result["humanGate"],
                        "id": "approval-rework-1",
                        "status": "approved",
                        "approvedAt": "2026-06-18T00:00:00+00:00",
                    },
                }
                with (
                    mock.patch("agent_engine.llm_client.ChatClient.json", autospec=True, side_effect=fake_json),
                    mock.patch(
                        "agent_engine.graph.codegraph_context",
                        return_value={"enabled": False, "status": "disabled", "reason": "test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.container_status",
                        return_value={"ready": True, "runtime": "docker", "image": "python:test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.run_container_command",
                        return_value={
                            "command": "python -m compileall .",
                            "cwd": ".",
                            "code": 1,
                            "stdout": "",
                            "stderr": "forced failure",
                            "timedOut": False,
                            "sandboxed": True,
                        },
                    ),
                    mock.patch("agent_engine.graph.run_openhands_worker", side_effect=fake_worker),
                ):
                    approved_result = run_pipeline(approved_payload, lambda _stage, _detail: None)

                self.assertEqual(approved_result["humanGate"]["kind"], "rework_limit")
                self.assertEqual(approved_result["humanGate"]["retryCount"], 4)
                self.assertEqual(len(worker_calls), 4)
            finally:
                worktree_output = subprocess.run(
                    ["git", "worktree", "list", "--porcelain"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                ).stdout
                worktree_paths = [
                    Path(line.removeprefix("worktree "))
                    for line in worktree_output.splitlines()
                    if line.startswith("worktree ")
                ]
                for path in worktree_paths[1:]:
                    subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=repo, capture_output=True)
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir


if __name__ == "__main__":
    unittest.main()
