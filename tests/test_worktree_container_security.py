from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_engine.container_sandbox import _container_command, container_status, run_container_command
from agent_engine.worktree_manager import (
    cleanup_execution_worktree,
    merge_execution_worktree,
    prepare_execution_worktree,
)


def git(cwd: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)


class WorktreeContainerSecurityTests(unittest.TestCase):
    def test_worktree_preserves_dirty_baseline_and_merges_only_reviewed_delta(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                git(repo, "init")
                git(repo, "config", "user.email", "test@example.com")
                git(repo, "config", "user.name", "Test")
                (repo / "app.txt").write_text("committed", encoding="utf-8")
                git(repo, "add", "app.txt")
                git(repo, "commit", "-m", "initial")
                (repo / "app.txt").write_text("dirty baseline", encoding="utf-8")
                (repo / "local.txt").write_text("untracked baseline", encoding="utf-8")

                info = prepare_execution_worktree(str(repo), "exec-worktree")
                self.assertTrue(info["ready"])
                worktree = Path(info["workspacePath"])
                self.assertEqual((worktree / "app.txt").read_text(encoding="utf-8"), "dirty baseline")
                self.assertEqual((worktree / "local.txt").read_text(encoding="utf-8"), "untracked baseline")

                (worktree / "app.txt").write_text("agent change", encoding="utf-8")
                (worktree / "new.txt").write_text("new", encoding="utf-8")
                merged = merge_execution_worktree(info)

                self.assertEqual(merged["conflicts"], [])
                self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "agent change")
                self.assertEqual((repo / "new.txt").read_text(encoding="utf-8"), "new")
                self.assertTrue(cleanup_execution_worktree(info)["removed"])
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_worktree_merge_refuses_to_overwrite_concurrent_source_change(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                git(repo, "init")
                git(repo, "config", "user.email", "test@example.com")
                git(repo, "config", "user.name", "Test")
                (repo / "app.txt").write_text("base", encoding="utf-8")
                git(repo, "add", "app.txt")
                git(repo, "commit", "-m", "initial")
                info = prepare_execution_worktree(str(repo), "exec-conflict")
                worktree = Path(info["workspacePath"])
                (worktree / "app.txt").write_text("agent", encoding="utf-8")
                (repo / "app.txt").write_text("human", encoding="utf-8")

                merged = merge_execution_worktree(info)

                self.assertEqual(len(merged["conflicts"]), 1)
                self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "human")
                cleanup_execution_worktree(info)
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_worktree_excludes_sensitive_files_and_final_merge_rechecks_policy(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                git(repo, "init")
                git(repo, "config", "user.email", "test@example.com")
                git(repo, "config", "user.name", "Test")
                (repo / "app.txt").write_text("base", encoding="utf-8")
                (repo / ".env").write_text("TOKEN=do-not-copy", encoding="utf-8")
                git(repo, "add", "app.txt", ".env")
                git(repo, "commit", "-m", "initial")

                info = prepare_execution_worktree(str(repo), "exec-sensitive")
                worktree = Path(info["workspacePath"])
                self.assertFalse((worktree / ".env").exists())

                (worktree / "app.txt").write_text("agent", encoding="utf-8")
                (worktree / "outside.txt").write_text("blocked", encoding="utf-8")
                (worktree / ".env").write_text("TOKEN=agent", encoding="utf-8")
                merged = merge_execution_worktree(
                    info,
                    allowed_patterns=["app.txt"],
                    forbidden_patterns=[".env"],
                )

                self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "base")
                self.assertEqual((repo / ".env").read_text(encoding="utf-8"), "TOKEN=do-not-copy")
                self.assertFalse((repo / "outside.txt").exists())
                self.assertEqual(
                    {item["path"] for item in merged["policyViolations"]},
                    {".env", "outside.txt"},
                )
                cleanup_execution_worktree(info)
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_worktree_excludes_runtime_state_dir_inside_workspace_and_git_file_from_diff(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = str(repo / ".agent-state-real")
            try:
                git(repo, "init")
                git(repo, "config", "user.email", "test@example.com")
                git(repo, "config", "user.name", "Test")
                (repo / "README.md").write_text("base\n", encoding="utf-8")
                (repo / ".agent-state-real").mkdir()
                (repo / ".agent-state-real" / "debug.sqlite").write_text("runtime", encoding="utf-8")
                git(repo, "add", "README.md")
                git(repo, "commit", "-m", "initial")

                info = prepare_execution_worktree(str(repo), "exec-runtime-state")
                worktree = Path(info["workspacePath"])
                self.assertFalse((worktree / ".agent-state-real").exists())

                (worktree / "app.txt").write_text("agent", encoding="utf-8")
                merged = merge_execution_worktree(info, allowed_patterns=["app.txt"])

                self.assertEqual(merged["policyViolations"], [])
                self.assertEqual([item["path"] for item in merged["applied"]], ["app.txt"])
                self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "agent")
                cleanup_execution_worktree(info)
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_container_command_has_hardening_flags_and_no_network(self) -> None:
        args = _container_command(
            runtime="docker",
            image="python:3.12-slim",
            workspace=Path("C:/workspace"),
            command="pytest",
            cwd=".",
            dependency_workspace=None,
            allow_pull=False,
        )
        joined = " ".join(map(str, args))
        self.assertIn("--network none", joined)
        self.assertIn("--read-only", args)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("no-new-privileges", joined)
        self.assertIn("--pull never", joined)
        self.assertEqual(args[-3:], ["sh", "-lc", "pytest"])

    def test_container_execution_is_fail_closed_without_runtime(self) -> None:
        with mock.patch("agent_engine.container_sandbox.detect_container_runtime", return_value=None):
            status = container_status("python")
            result = run_container_command(".", "python -m pytest", stack="python")

        self.assertFalse(status["ready"])
        self.assertFalse(result["sandboxed"])
        self.assertIsNone(result["code"])
        self.assertIn("Docker or Podman", result["stderr"])

    def test_container_execution_rejects_cwd_escape_before_runtime(self) -> None:
        with mock.patch("agent_engine.container_sandbox.container_status") as status:
            result = run_container_command(".", "pytest", cwd="../outside", stack="python")

        status.assert_not_called()
        self.assertFalse(result["sandboxed"])
        self.assertIn("escapes", result["stderr"])


if __name__ == "__main__":
    unittest.main()
