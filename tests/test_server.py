from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from agent_engine import server as agent_server


class ServerSmokeTests(unittest.TestCase):
    def test_health_endpoint_starts_with_temp_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                **os.environ,
                "AGENT_ENGINE_STATE_DIR": temp_dir,
                "PYTHONPATH": os.pathsep.join(filter(None, [str(os.getcwd() + os.sep + "engine"), os.environ.get("PYTHONPATH", "")])),
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "agent_engine.server", "--host", "127.0.0.1", "--port", "0"],
                cwd=os.getcwd(),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                ready_line = proc.stdout.readline().strip() if proc.stdout else ""
                ready = json.loads(ready_line)
                with urllib.request.urlopen(f"http://{ready['host']}:{ready['port']}/health", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload, {"ok": True})
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()

    def test_backend_error_response_keeps_correlation_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
            os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
            httpd = agent_server.ThreadingHTTPServer(("127.0.0.1", 0), agent_server.AgentRequestHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(agent_server, "run_pipeline", side_effect=RuntimeError("forced backend failure")):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{httpd.server_port}/v1/runs",
                        data=json.dumps(
                            {
                                "sessionId": "session-1",
                                "workspacePath": temp_dir,
                                "content": "trigger failure",
                                "settings": {"serverUrl": "http://model.test/v1", "model": "test-model", "apiKey": ""},
                            }
                        ).encode("utf-8"),
                        headers={"Content-Type": "application/json", "X-Correlation-Id": "cid-test-error"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        body = response.read().decode("utf-8")
                        self.assertEqual(response.headers.get("X-Correlation-Id"), "cid-test-error")
                messages = [json.loads(line) for line in body.splitlines() if line.strip()]
                self.assertTrue(any(item.get("type") == "progress" and item.get("stage") == "running" for item in messages))
                self.assertTrue(any(item.get("type") == "error" and "forced backend failure" in item.get("error", "") for item in messages))

                log_files = list((Path(temp_dir) / "logs").glob("agent-debug-*.jsonl"))
                self.assertTrue(log_files)
                events = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
                self.assertTrue(any(event.get("eventType") == "run.error" and event.get("correlationId") == "cid-test-error" for event in events))
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_observability_endpoint_returns_recent_debug_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
            os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
            log_dir = Path(temp_dir) / "logs"
            log_dir.mkdir(parents=True)
            (log_dir / "agent-debug-20260618.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"eventType": "progress", "stage": "preflight", "detail": "ready"}),
                        json.dumps({"eventType": "run.result", "correlationId": "cid-observe"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            httpd = agent_server.ThreadingHTTPServer(("127.0.0.1", 0), agent_server.AgentRequestHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/v1/observability", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertFalse(payload["runLockActive"])
                self.assertEqual(Path(payload["debugLogDir"]), log_dir)
                self.assertTrue(any(event.get("eventType") == "run.result" for event in payload["recentEvents"]))
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_autonomy_idle_scan_endpoint_persists_l4_l5_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as workspace:
            old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
            os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
            root = Path(workspace)
            (root / "engine" / "agent_engine").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "engine" / "agent_engine" / "unsafe.py").write_text(
                "import subprocess\n"
                "def run(cmd):\n"
                "    # TODO: safe command wrapper\n"
                "    return subprocess.run(cmd, shell=True)\n",
                encoding="utf-8",
            )
            httpd = agent_server.ThreadingHTTPServer(("127.0.0.1", 0), agent_server.AgentRequestHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_port}/v1/autonomy/idle-scan",
                    data=json.dumps({"workspacePath": workspace}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Correlation-Id": "cid-autonomy"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["correlationId"], "cid-autonomy")
                self.assertEqual(payload["report"]["mode"], "idle_read_only")
                self.assertIn("L4", payload["report"]["autonomyLevels"])
                self.assertIn("L5-proposal", payload["report"]["autonomyLevels"])
                self.assertTrue((Path(temp_dir) / "autonomy" / "last-report.json").exists())

                with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/v1/autonomy/status", timeout=5) as response:
                    status = json.loads(response.read().decode("utf-8"))
                self.assertTrue(status["ok"])
                self.assertIsNotNone(status["lastReport"])
                self.assertGreater(status["memory"]["total"], 0)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_autonomy_idle_scan_runs_while_write_lock_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as workspace:
            old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
            os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
            httpd = agent_server.ThreadingHTTPServer(("127.0.0.1", 0), agent_server.AgentRequestHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            self.assertTrue(agent_server._WRITE_LOCK.acquire(blocking=False))
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_port}/v1/autonomy/idle-scan",
                    data=json.dumps({"workspacePath": workspace}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self.assertTrue(body["ok"])
                self.assertTrue(body["writeLockActive"])
            finally:
                if agent_server._WRITE_LOCK.locked():
                    agent_server._WRITE_LOCK.release()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_server_queues_second_write_run_behind_write_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
            os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
            httpd = agent_server.ThreadingHTTPServer(("127.0.0.1", 0), agent_server.AgentRequestHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            first_started = threading.Event()
            release_first = threading.Event()
            responses: dict[str, str] = {}
            errors: list[str] = []

            def fake_run_pipeline(payload, _emit):
                if payload.get("content") == "first":
                    first_started.set()
                    release_first.wait(timeout=5)
                return {
                    "id": payload.get("correlationId"),
                    "assistantText": "ok",
                    "changedFiles": [],
                    "commandResults": [],
                    "review": {},
                    "correlationId": payload.get("correlationId"),
                }

            def post_run(name: str) -> None:
                try:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{httpd.server_port}/v1/runs",
                        data=json.dumps(
                            {
                                "sessionId": name,
                                "workspacePath": temp_dir,
                                "content": name,
                                "settings": {"serverUrl": "http://model.test/v1", "model": "test-model", "apiKey": ""},
                            }
                        ).encode("utf-8"),
                        headers={"Content-Type": "application/json", "X-Correlation-Id": f"cid-{name}"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        responses[name] = response.read().decode("utf-8")
                except Exception as exc:
                    errors.append(str(exc))

            thread.start()
            try:
                with mock.patch.object(agent_server, "run_pipeline", side_effect=fake_run_pipeline):
                    first = threading.Thread(target=post_run, args=("first",))
                    second = threading.Thread(target=post_run, args=("second",))
                    first.start()
                    self.assertTrue(first_started.wait(timeout=5))
                    second.start()
                    time.sleep(0.2)
                    release_first.set()
                    first.join(timeout=10)
                    second.join(timeout=10)

                self.assertEqual(errors, [])
                second_messages = [json.loads(line) for line in responses["second"].splitlines() if line.strip()]
                self.assertTrue(any(item.get("type") == "progress" and item.get("stage") == "queued" for item in second_messages))
                self.assertTrue(any(item.get("type") == "result" for item in second_messages))
            finally:
                release_first.set()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_read_only_run_does_not_wait_for_write_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
            os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
            httpd = agent_server.ThreadingHTTPServer(("127.0.0.1", 0), agent_server.AgentRequestHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            self.assertTrue(agent_server._WRITE_LOCK.acquire(blocking=False))
            try:
                with mock.patch.object(
                    agent_server,
                    "run_pipeline",
                    return_value={
                        "id": "exec-read",
                        "executionId": "exec-read",
                        "assistantText": "summary",
                        "changedFiles": [],
                        "commandResults": [],
                        "review": {},
                    },
                ):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{httpd.server_port}/v1/runs",
                        data=json.dumps(
                            {
                                "sessionId": "session-read",
                                "workspacePath": temp_dir,
                                "content": "đọc README.md và tóm tắt, không sửa",
                                "settings": {"serverUrl": "http://model.test/v1", "model": "test-model", "apiKey": ""},
                            }
                        ).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        body = response.read().decode("utf-8")

                messages = [json.loads(line) for line in body.splitlines() if line.strip()]
                self.assertFalse(any(item.get("stage") == "queued" for item in messages))
                self.assertTrue(
                    any(
                        item.get("stage") == "running" and "Read-only lane" in item.get("detail", "")
                        for item in messages
                    )
                )
                self.assertTrue(any(item.get("type") == "result" for item in messages))
            finally:
                if agent_server._WRITE_LOCK.locked():
                    agent_server._WRITE_LOCK.release()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_autonomous_contract_routes_scoped_no_edit_task_to_write_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
            os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
            httpd = agent_server.ThreadingHTTPServer(("127.0.0.1", 0), agent_server.AgentRequestHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            observed: dict = {}

            def fake_pipeline(payload, _emit):
                observed.update(payload)
                return {
                    "id": payload["executionId"],
                    "executionId": payload["executionId"],
                    "status": "success",
                    "completed": True,
                    "assistantText": "done",
                    "changedFiles": [{"path": "music-app/src/app.tsx", "status": "modified"}],
                    "commandResults": [{"command": "npm run build", "code": 0}],
                    "review": {"passed": True},
                }

            try:
                with mock.patch.object(agent_server, "run_pipeline", side_effect=fake_pipeline):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{httpd.server_port}/v1/runs",
                        data=json.dumps(
                            {
                                "sessionId": "session-auto",
                                "workspacePath": temp_dir,
                                "content": "AUTONOMOUS PRODUCT ITERATION — không sửa agent platform; triển khai music app",
                                "executionContext": {
                                    "originalUserGoal": "code web nghe nhạc",
                                    "executionClass": "write",
                                    "executionMode": "autonomous",
                                    "permissionProfile": "workspace-write",
                                    "requiresMutation": True,
                                    "reportOnly": False,
                                },
                                "settings": {"serverUrl": "http://model.test/v1", "model": "test-model", "apiKey": ""},
                            },
                            ensure_ascii=False,
                        ).encode("utf-8"),
                        headers={"Content-Type": "application/json; charset=utf-8"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        body = response.read().decode("utf-8")

                messages = [json.loads(line) for line in body.splitlines() if line.strip()]
                self.assertTrue(any(item.get("stage") == "running" and "Write lane" in item.get("detail", "") for item in messages))
                self.assertFalse(any("Read-only lane" in item.get("detail", "") for item in messages))
                self.assertTrue(observed["executionContext"]["requiresMutation"])
                self.assertEqual(observed["executionContext"]["originalUserGoal"], "code web nghe nhạc")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir


if __name__ == "__main__":
    unittest.main()
