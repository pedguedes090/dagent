from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_engine.long_term_memory import ACTRMemoryStore


class ACTRLongTermMemoryTests(unittest.TestCase):
    def test_core_memory_is_reinforced_while_old_errors_decay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ACTRMemoryStore(Path(temp_dir) / "memory.sqlite")
            try:
                old = 1_800_000_000.0
                now = old + (20 * 86400)
                core = store.remember(
                    kind="core",
                    source="engine/agent_engine/deterministic_workflow.py",
                    tags=["workflow", "context"],
                    importance=0.86,
                    content="Deterministic workflow routes explicit context envelopes through each agent node.",
                    now=old,
                )
                error = store.remember(
                    kind="error",
                    source="old-run",
                    tags=["workflow", "error"],
                    importance=0.86,
                    content="Old transient workflow timeout that has not appeared again.",
                    now=old,
                )

                first = store.retrieve("deterministic workflow context envelopes", limit=1, now=now, reinforce=True)
                second = store.retrieve("deterministic workflow context envelopes", limit=1, now=now + 1, reinforce=True)

                self.assertEqual(first[0]["id"], core["id"])
                self.assertEqual(second[0]["id"], core["id"])
                self.assertGreaterEqual(store.get(core["id"])["accessCount"], 2)
                self.assertGreater(store.get(core["id"], now=now + 2)["activation"], store.get(error["id"], now=now + 2)["activation"])
            finally:
                store.close()

    def test_secret_like_content_is_redacted_before_persistent_memory_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ACTRMemoryStore(Path(temp_dir) / "memory.sqlite")
            try:
                record = store.remember(
                    kind="core",
                    source="README.md",
                    content="apiKey = sk-1234567890abcdefghijklmnop",
                    tags=["config"],
                )

                self.assertIn("[redacted]", record["content"])
                self.assertNotIn("1234567890abcdefghijklmnop", record["content"])
                self.assertTrue(record["metadata"]["redactedSensitiveContent"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

