from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_engine.debug_log import debug_log_path, write_debug_event


class DebugLogTests(unittest.TestCase):
    def test_write_debug_event_creates_jsonl_under_state_dir(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.environ["AGENT_ENGINE_STATE_DIR"] = temp_dir
                write_debug_event("test.event", {"hello": "world"})

                path = debug_log_path()
                self.assertTrue(path.is_file())
                self.assertEqual(Path(temp_dir) / "logs", path.parent)
                event = json.loads(path.read_text(encoding="utf-8").strip())
                self.assertEqual(event["eventType"], "test.event")
                self.assertEqual(event["payload"], {"hello": "world"})
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir


if __name__ == "__main__":
    unittest.main()
