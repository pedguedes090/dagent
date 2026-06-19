from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_engine.broker import SQLiteAgentBroker


class BrokerTests(unittest.TestCase):
    def test_broker_carries_correlation_id_and_recovers_incomplete_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            broker = SQLiteAgentBroker(Path(temp_dir) / "agent-broker.sqlite")
            try:
                run_id = broker.create_run(
                    session_id="session-1",
                    task="task",
                    task_graph={"subtasks": [{"role": "planner"}]},
                    correlation_id="cid-test",
                    execution_id="exec-test",
                )
                resumed_run_id = broker.create_run(
                    session_id="session-1",
                    task="task",
                    task_graph={"subtasks": [{"role": "planner"}]},
                    correlation_id="cid-test",
                    execution_id="exec-test",
                )
                self.assertEqual(resumed_run_id, run_id)
                self.assertEqual(run_id, "exec-test")
                broker.dispatch_subtasks(run_id, [{"role": "planner", "title": "Plan", "input": {}}])
                broker.dispatch_subtasks(run_id, [{"role": "planner", "title": "Plan", "input": {}}])
                count = broker.conn.execute("SELECT COUNT(*) AS count FROM agent_subtasks WHERE run_id = ?", (run_id,)).fetchone()
                self.assertEqual(count["count"], 1)
                subtask = broker.start_role(run_id, "planner", "Plan", {})
                broker.complete_subtask(run_id, subtask["id"], "planner", {"ok": True})
                events = broker.events(run_id)

                self.assertTrue(events)
                self.assertTrue(all(event["correlationId"] == "cid-test" for event in events))
                self.assertEqual(broker.recover_incomplete_runs(), 1)
            finally:
                broker.close()


if __name__ == "__main__":
    unittest.main()
