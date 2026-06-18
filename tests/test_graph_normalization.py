from __future__ import annotations

import os
import tempfile
import unittest

from agent_engine.graph import _detect_task_intent, _normalize_worker_task_spec


class GraphNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        self._temp_state = tempfile.TemporaryDirectory()
        os.environ["AGENT_ENGINE_STATE_DIR"] = self._temp_state.name

    def tearDown(self) -> None:
        if self._old_state_dir is None:
            os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
        else:
            os.environ["AGENT_ENGINE_STATE_DIR"] = self._old_state_dir
        self._temp_state.cleanup()

    def test_python_project_creation_does_not_force_npm_build(self) -> None:
        state = {
            "task": "Create a Python service with a small package",
            "problem": {"problemStatement": "Create a Python service"},
        }
        final = {"workerTaskSpec": {}}

        normalized = _normalize_worker_task_spec(final, state)
        spec = normalized["workerTaskSpec"]

        self.assertEqual(spec["projectStack"], "python")
        self.assertEqual(spec["targetProjectDir"], "python-app")
        self.assertEqual(spec["verificationCommands"], ["python -m compileall ."])
        self.assertNotIn("npm run build", spec["verificationCommands"])

    def test_node_todo_project_keeps_node_build_default(self) -> None:
        state = {
            "task": "Create a todo web app",
            "problem": {"problemStatement": "Create a todo web app"},
        }
        final = {"workerTaskSpec": {}}

        normalized = _normalize_worker_task_spec(final, state)
        spec = normalized["workerTaskSpec"]

        self.assertEqual(spec["projectStack"], "node")
        self.assertEqual(spec["targetProjectDir"], "todo-app")
        self.assertEqual(spec["verificationCommands"], ["npm run build"])

    def test_todo_project_overrides_root_cwd_and_allows_target_dir(self) -> None:
        state = {
            "task": "tạo todo app với css hiện đại phù hợp với điện thoại máy tính tablet",
            "problem": {"problemStatement": "tạo todo app responsive"},
        }
        final = {
            "workerTaskSpec": {
                "projectRoot": ".",
                "verificationCwd": ".",
                "allowedFiles": ["package.json"],
                "verificationCommands": ["npm run build"],
            }
        }

        normalized = _normalize_worker_task_spec(final, state)
        spec = normalized["workerTaskSpec"]

        self.assertEqual(spec["targetProjectDir"], "todo-app")
        self.assertEqual(spec["projectRoot"], "todo-app")
        self.assertEqual(spec["verificationCwd"], "todo-app")
        self.assertIn("package.json", spec["allowedFiles"])
        self.assertIn("todo-app/**", spec["allowedFiles"])

    def test_read_only_intent_does_not_require_worker(self) -> None:
        intent = _detect_task_intent("đọc README.md và tóm tắt, không sửa")

        self.assertTrue(intent["readOnly"])
        self.assertFalse(intent["requiresWorker"])


if __name__ == "__main__":
    unittest.main()
