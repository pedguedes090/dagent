from __future__ import annotations

import unittest
from pathlib import Path

from agent_engine.evaluation import _load_benchmark, _validate_benchmark


class EvaluationTests(unittest.TestCase):
    def test_internal_benchmark_has_required_categories_and_rubric(self) -> None:
        benchmark = _load_benchmark(Path("benchmarks/internal_benchmark.json"))

        _validate_benchmark(benchmark)

        self.assertEqual(len(benchmark["cases"]), 7)
        self.assertAlmostEqual(sum(float(value) for value in benchmark["rubric"].values()), 1.0)


if __name__ == "__main__":
    unittest.main()
