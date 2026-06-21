from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_engine.durable_execution import (
    DurableExecutionStore,
    cached_tool_call,
    execution_context,
    sanitize_payload,
)
from agent_engine.graph import run_pipeline


class DurableExecutionTests(unittest.TestCase):
    def test_store_redacts_secrets_tracks_steps_and_recovers_running_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "durable.sqlite"
            store = DurableExecutionStore(db_path)
            try:
                execution = store.prepare(
                    execution_id="exec-1",
                    session_id="session-1",
                    correlation_id="cid-1",
                    task="task",
                    workspace_path=temp_dir,
                    input_payload={"settings": {"apiKey": "super-secret", "model": "test"}},
                )
                self.assertEqual(execution["input"]["settings"]["apiKey"], "[redacted]")
                owner = store.acquire("exec-1")
                self.assertTrue(store.heartbeat("exec-1", owner))
                step_id = store.start_step("exec-1", "tool", "read", {"path": "README.md"})
                store.finish_step(step_id, {"ok": True})

                self.assertEqual(store.recover_incomplete(), 1)
                recovered = store.get("exec-1")
                self.assertEqual(recovered["status"], "recoverable")
                self.assertEqual(store.steps("exec-1")[0]["status"], "completed")
            finally:
                store.close()

    def test_cached_tool_call_reuses_completed_output_in_same_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "durable.sqlite"
            store = DurableExecutionStore(db_path)
            store.prepare(
                execution_id="exec-cache",
                session_id="session-1",
                correlation_id="cid-1",
                task="task",
                workspace_path=temp_dir,
                input_payload={},
            )
            store.close()
            calls = []

            with execution_context(execution_id="exec-cache", database_path=db_path, runtime_settings={}):
                first, first_cached = cached_tool_call(
                    "tool",
                    "llm_chat",
                    {"prompt": "same"},
                    lambda: calls.append("called") or {"text": "answer"},
                )
                second, second_cached = cached_tool_call(
                    "tool",
                    "llm_chat",
                    {"prompt": "same"},
                    lambda: calls.append("called-again") or {"text": "different"},
                )

            self.assertEqual(first, {"text": "answer"})
            self.assertEqual(second, {"text": "answer"})
            self.assertFalse(first_cached)
            self.assertTrue(second_cached)
            self.assertEqual(calls, ["called"])

    def test_run_pipeline_resumes_pending_node_and_returns_cached_result(self) -> None:
        class FakeGraph:
            def __init__(self) -> None:
                self.invocations = []
                self.updates = []

            def get_state(self, _config):
                return SimpleNamespace(next=("failed_node",), values={"task": "task"})

            def update_state(self, config, values):
                self.updates.append((config, values))
                return config

            def invoke(self, input_value, config=None, durability=None):
                self.invocations.append((input_value, config, durability))
                return {
                    "task": "task",
                    "result": {"assistantText": "resumed", "changedFiles": [], "review": {"passed": True}},
                }

        @contextmanager
        def fake_checkpointer(_emit):
            yield object()

        with tempfile.TemporaryDirectory() as temp_dir:
            old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
            os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
            fake_graph = FakeGraph()
            payload = {
                "executionId": "exec-resume",
                "correlationId": "cid-resume",
                "sessionId": "session-1",
                "content": "đọc file config và giải thích",
                "workspacePath": temp_dir,
                "settings": {"serverUrl": "http://model.test/v1", "model": "test", "apiKey": "secret"},
                "messages": [],
            }
            try:
                with (
                    mock.patch("agent_engine.graph._open_checkpointer", fake_checkpointer),
                    mock.patch("agent_engine.graph.build_graph", return_value=fake_graph),
                ):
                    result = run_pipeline(payload, lambda _stage, _detail: None)

                self.assertEqual(result["id"], "exec-resume")
                self.assertEqual(result["assistantText"], "resumed")
                self.assertEqual(fake_graph.invocations[0][0], None)
                self.assertEqual(fake_graph.invocations[0][2], "sync")
                self.assertNotIn("apiKey", fake_graph.updates[0][1]["settings"])

                with mock.patch("agent_engine.graph.build_graph", side_effect=AssertionError("cached result should bypass graph")):
                    cached = run_pipeline(payload, lambda _stage, _detail: None)
                self.assertEqual(cached, result)
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_read_only_pipeline_skips_git_worktree(self) -> None:
        class FakeGraph:
            def get_state(self, _config):
                return SimpleNamespace(next=(), values={})

            def invoke(self, input_value, config=None, durability=None):
                self.input_value = input_value
                return {
                    "task": input_value["task"],
                    "taskIntent": input_value["taskIntent"],
                    "result": {"assistantText": "summary", "changedFiles": [], "review": {"passed": True}},
                }

        @contextmanager
        def fake_checkpointer(_emit):
            yield object()

        with tempfile.TemporaryDirectory() as temp_dir:
            old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
            os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
            payload = {
                "executionId": "exec-read-only",
                "correlationId": "cid-read-only",
                "sessionId": "session-1",
                "content": "đọc README.md và tóm tắt, không sửa file",
                "workspacePath": temp_dir,
                "settings": {"serverUrl": "http://model.test/v1", "model": "test", "apiKey": ""},
                "messages": [],
            }
            try:
                with (
                    mock.patch("agent_engine.graph._open_checkpointer", fake_checkpointer),
                    mock.patch("agent_engine.graph.build_graph", return_value=FakeGraph()),
                    mock.patch("agent_engine.graph.prepare_execution_worktree") as prepare_worktree,
                ):
                    result = run_pipeline(payload, lambda _stage, _detail: None)

                prepare_worktree.assert_not_called()
                self.assertEqual(result["executionId"], "exec-read-only")
                self.assertEqual(result["changedFiles"], [])
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_write_pipeline_uses_opened_workspace_directly_by_default(self) -> None:
        class FakeGraph:
            def get_state(self, _config):
                return SimpleNamespace(next=(), values={})

            def invoke(self, input_value, config=None, durability=None):
                self.input_value = input_value
                return {
                    "task": input_value["task"],
                    "result": {"assistantText": "done", "changedFiles": [], "review": {"passed": True}},
                }

        @contextmanager
        def fake_checkpointer(_emit):
            yield object()

        with tempfile.TemporaryDirectory() as temp_dir:
            old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
            os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
            fake_graph = FakeGraph()
            payload = {
                "executionId": "exec-direct-workspace",
                "correlationId": "cid-direct-workspace",
                "sessionId": "session-1",
                "content": "tạo app React",
                "workspacePath": temp_dir,
                "settings": {"serverUrl": "http://model.test/v1", "model": "test", "apiKey": ""},
                "messages": [],
            }
            try:
                with (
                    mock.patch("agent_engine.graph._open_checkpointer", fake_checkpointer),
                    mock.patch("agent_engine.graph.build_graph", return_value=fake_graph),
                    mock.patch("agent_engine.graph.prepare_execution_worktree") as prepare_worktree,
                ):
                    run_pipeline(payload, lambda _stage, _detail: None)

                prepare_worktree.assert_not_called()
                self.assertEqual(fake_graph.input_value["workspacePath"], str(Path(temp_dir).resolve()))
                self.assertEqual(fake_graph.input_value["worktreeInfo"]["mode"], "direct-workspace")
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_sanitize_payload_redacts_nested_credentials(self) -> None:
        value = sanitize_payload({"authorization": "Bearer x", "nested": {"access_token": "x", "safe": "ok"}})
        self.assertEqual(value["authorization"], "[redacted]")
        self.assertEqual(value["nested"]["access_token"], "[redacted]")
        self.assertEqual(value["nested"]["safe"], "ok")


if __name__ == "__main__":
    unittest.main()
