from __future__ import annotations

import os
import tempfile
import unittest

from agent_engine.graph import (
    _detect_task_intent,
    _normalize_worker_task_spec,
    _resolve_product_workspace,
    classify_execution,
)


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
        self.assertTrue(spec["targetProjectDir"].endswith("-app"))
        self.assertIn("python", spec["targetProjectDir"])
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

    def test_vocabulary_project_replaces_generic_app_target(self) -> None:
        state = {
            "task": "tạo trang web học từ vựng tiếng Anh bằng React",
            "problem": {"problemStatement": "Create a vocabulary web app"},
        }
        final = {
            "workerTaskSpec": {
                "targetProjectDir": "app",
                "projectRoot": "app",
                "allowedFiles": ["src/**/*", "package.json"],
            }
        }

        normalized = _normalize_worker_task_spec(final, state)
        spec = normalized["workerTaskSpec"]

        target = spec["targetProjectDir"]
        self.assertTrue(target.endswith("-app"))
        self.assertNotEqual(target, "app")
        self.assertEqual(spec["projectRoot"], target)
        self.assertEqual(spec["verificationCwd"], target)
        self.assertIn(f"{target}/**", spec["allowedFiles"])
        self.assertTrue(any(f"targetProjectDir '{target}'" in item for item in spec["constraints"]))

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

        target = spec["targetProjectDir"]
        self.assertIn("todo", target)
        self.assertTrue(target.endswith("-app"))
        self.assertEqual(spec["projectRoot"], target)
        self.assertEqual(spec["verificationCwd"], target)
        self.assertIn("package.json", spec["allowedFiles"])
        self.assertIn(f"{target}/**", spec["allowedFiles"])

    def test_read_only_intent_does_not_require_worker(self) -> None:
        intent = _detect_task_intent("đọc README.md và tóm tắt, không sửa")

        self.assertTrue(intent["readOnly"])
        self.assertFalse(intent["requiresWorker"])

    def test_scoped_no_edit_constraint_does_not_downgrade_mutation(self) -> None:
        task = (
            "AUTONOMOUS PRODUCT ITERATION — MUTATION_REQUIRED=true\n"
            "Original goal: code web nghe nhạc\n"
            "Không sửa agent platform. Phải tạo thay đổi hoạt động được."
        )
        admission = classify_execution(task)

        self.assertEqual(admission["executionClass"], "write")
        self.assertTrue(admission["taskIntent"]["requiresWorker"])
        self.assertFalse(admission["taskIntent"]["readOnly"])
        self.assertIn("không sửa", admission["taskIntent"]["signals"]["scopedNoEdit"])

    def test_structured_autonomous_contract_forces_mutation(self) -> None:
        admission = classify_execution(
            "Khảo sát rồi triển khai trong productRoot; không sửa agent platform.",
            {
                "executionClass": "write",
                "requiresMutation": True,
                "permissionProfile": "workspace-write",
            },
        )

        self.assertEqual(admission["executionClass"], "write")
        self.assertTrue(admission["taskIntent"]["forcedMutation"])
        self.assertTrue(admission["taskIntent"]["requiresWorker"])

    def test_product_noun_in_analysis_request_is_not_mutation(self) -> None:
        admission = classify_execution("phân tích website này và báo cáo kiến trúc")

        self.assertEqual(admission["executionClass"], "read_only")
        self.assertFalse(admission["taskIntent"]["requiresWorker"])

    def test_doctor_scope_resolves_to_product_root_and_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            product = os.path.join(root, "music-app")
            os.makedirs(product)

            self.assertEqual(
                str(_resolve_product_workspace(root, {"targetProjectDir": "music-app"})),
                os.path.realpath(product),
            )
            self.assertEqual(
                str(_resolve_product_workspace(root, {"targetProjectDir": "../outside"})),
                os.path.realpath(root),
            )


if __name__ == "__main__":
    unittest.main()
