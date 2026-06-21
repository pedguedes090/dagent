"""Contract tests for the autonomous next-task selector.

After the council refactor, select_next_task has two lanes:
  - PRODUCT lane (whenever product_goal is set):
    iteration 0 returns the literal user goal; iteration >= 1 delegates to the
    council. NO hard-coded enhancement pool exists.
  - PLATFORM_MAINTENANCE lane (only when no product goal is set):
    findings ranked by category (security > test_coverage > maintainability >
    technical_debt). Returns None when findings are exhausted — no pool fallback.
"""
from __future__ import annotations

import unittest

from agent_engine.autonomy import build_autonomous_bootstrap, select_next_task


def _f(fid: str, category: str, priority: float, *, title: str = "t") -> dict:
    return {
        "id": fid,
        "category": category,
        "title": title,
        "source": f"src/{fid}.py:1",
        "evidence": "evidence",
        "recommendation": "rec",
        "priorityScore": priority,
        "confidence": 0.8,
        "impact": 2.0,
        "effort": 1.0,
        "severity": "high",
    }


class PlatformMaintenanceLaneTests(unittest.TestCase):
    """No product_goal → findings-only platform maintenance ordering."""

    def test_security_beats_test_coverage_beats_maintainability(self) -> None:
        report = {
            "findings": [
                _f("a", "maintainability", 9.0),
                _f("b", "test_coverage", 1.0),
                _f("c", "security", 0.5),
            ]
        }
        task = select_next_task(report, completed_ids=set())
        self.assertIsNotNone(task)
        self.assertEqual(task["id"], "c")
        self.assertEqual(task["category"], "security")
        self.assertEqual(task["kind"], "finding")

    def test_higher_priority_score_wins_within_category(self) -> None:
        report = {
            "findings": [
                _f("low", "security", 1.0),
                _f("high", "security", 5.0),
            ]
        }
        task = select_next_task(report, completed_ids=set())
        self.assertEqual(task["id"], "high")

    def test_completed_ids_skip_to_next(self) -> None:
        report = {"findings": [_f("a", "security", 5.0), _f("b", "security", 4.0)]}
        task = select_next_task(report, completed_ids={"a"})
        self.assertEqual(task["id"], "b")

    def test_returns_none_when_no_goal_and_no_findings(self) -> None:
        self.assertIsNone(select_next_task({"findings": []}, completed_ids=set()))

    def test_returns_none_when_all_findings_completed(self) -> None:
        report = {"findings": [_f("a", "security", 5.0)]}
        self.assertIsNone(select_next_task(report, completed_ids={"a"}))

    def test_finding_task_body_includes_recommendation(self) -> None:
        report = {"findings": [_f("x", "security", 5.0, title="risky pattern")]}
        task = select_next_task(report, completed_ids=set())
        self.assertIn("risky pattern", task["task"])
        self.assertIn("rec", task["task"])
        self.assertIn("src/x.py:1", task["task"])


class ProductLaneTests(unittest.TestCase):
    """product_goal set + workspace is a user product → council path; iter 0
    returns the user prompt verbatim; if no council winner, returns None — no
    hard-coded fallback."""

    def test_iteration_zero_returns_user_goal_verbatim(self) -> None:
        task = select_next_task(
            {"workspacePath": "/tmp/music-app", "findings": []},
            completed_ids=set(),
            product_goal="code web nghe nhạc",
            iteration=0,
            council_round=lambda **_: None,
            session_id="s",
            workspace_path="/tmp/music-app",
        )
        self.assertEqual(task["kind"], "user_goal")
        self.assertEqual(task["task"], "code web nghe nhạc")
        self.assertTrue(task["executionContext"]["requiresMutation"])

    def test_product_goal_wins_even_when_workspace_is_agent_platform(self) -> None:
        task = select_next_task(
            {"workspacePath": "C:/fractal-agent-system", "findings": [_f("platform", "security", 9.0)]},
            completed_ids=set(),
            product_goal="code web nghe nhạc",
            iteration=0,
            workspace_path="C:/fractal-agent-system",
        )

        self.assertEqual(task["kind"], "user_goal")
        self.assertEqual(task["originalUserGoal"], "code web nghe nhạc")
        self.assertNotEqual(task["id"], "platform")

    def test_bootstrap_uses_goal_specific_product_root_and_write_contract(self) -> None:
        task = build_autonomous_bootstrap("tạo web xem phim", "C:/workspace")

        self.assertTrue(task["productRoot"].replace("\\", "/").endswith("/movie-app"))
        self.assertEqual(task["executionContext"]["executionClass"], "write")
        self.assertFalse(task["executionContext"]["reportOnly"])

    def test_no_council_winner_returns_none(self) -> None:
        task = select_next_task(
            {"workspacePath": "/tmp/music-app", "findings": []},
            completed_ids={"user-goal-iter0"},
            product_goal="code web nghe nhạc",
            iteration=2,
            council_round=lambda **_: {"winner": None, "proposals": []},
            session_id="s",
            workspace_path="/tmp/music-app",
        )
        self.assertIsNone(task)


if __name__ == "__main__":
    unittest.main()
