from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_engine.autonomy import autonomy_status, run_idle_discovery


class AutonomyDiscoveryTests(unittest.TestCase):
    def test_idle_discovery_builds_l4_plan_l5_proposals_and_memory(self) -> None:
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as state_dir:
            root = Path(workspace)
            engine_dir = root / "engine" / "agent_engine"
            tests_dir = root / "tests"
            engine_dir.mkdir(parents=True)
            tests_dir.mkdir()
            risky_lines = [
                "import subprocess",
                "",
                "def run(value):",
                "    # TODO: replace shell escape with safe argument array",
                "    return subprocess.run(value, shell=True)",
            ]
            risky_lines.extend(f"# filler {index}" for index in range(670))
            (engine_dir / "risky.py").write_text("\n".join(risky_lines), encoding="utf-8")

            report = run_idle_discovery(workspace, state_dir, now=1_800_000_000.0)

            categories = {finding["category"] for finding in report["findings"]}
            self.assertTrue({"security", "technical_debt", "maintainability", "test_coverage"}.issubset(categories))
            self.assertFalse(report["safety"]["writesToWorkspace"])
            self.assertFalse(report["safety"]["executesCommands"])
            self.assertGreaterEqual(report["summary"]["initiativeCount"], 1)
            self.assertGreaterEqual(report["summary"]["skillProposalCount"], 1)
            self.assertEqual(report["longHorizonPlan"]["autonomyLevel"], "L4")
            self.assertTrue(all(proposal["autonomyLevel"] == "L5-proposal" for proposal in report["skillProposals"]))
            self.assertGreater(report["memory"]["total"], 0)
            self.assertTrue((Path(state_dir) / "autonomy" / "last-report.json").exists())

            status = autonomy_status(state_dir)
            self.assertTrue(status["ok"])
            self.assertEqual(status["lastReport"]["summary"]["findingCount"], report["summary"]["findingCount"])
            self.assertGreaterEqual(status["memory"]["total"], report["memory"]["total"])


if __name__ == "__main__":
    unittest.main()

