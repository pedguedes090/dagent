from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .evaluation import DEFAULT_REGISTRY, REQUIRED_CATEGORIES


def _as_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def adaptation_decision(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    model: str,
    prompt_version: str,
    policy_version: str,
    minimum_average_score: float = 0.85,
    require_live: bool = True,
) -> dict[str, Any]:
    if not registry_path.exists():
        return {"allowed": False, "reason": "evaluation_registry_missing", "registry": str(registry_path)}

    conn = sqlite3.connect(str(registry_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, mode, status
            FROM evaluation_experiments
            WHERE model = ? AND prompt_version = ? AND policy_version = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (model, prompt_version, policy_version),
        ).fetchone()
        if not row:
            return {"allowed": False, "reason": "candidate_not_evaluated"}
        if require_live and row["mode"] != "live":
            return {"allowed": False, "reason": "latest_evaluation_not_live", "experimentId": row["id"], "mode": row["mode"]}
        if row["status"] != "passed":
            return {"allowed": False, "reason": "latest_evaluation_not_passed", "experimentId": row["id"], "status": row["status"]}

        cases = conn.execute(
            "SELECT category, status, score FROM evaluation_case_results WHERE experiment_id = ?",
            (row["id"],),
        ).fetchall()
        categories = {case["category"] for case in cases if case["status"] == "passed"}
        missing = REQUIRED_CATEGORIES - categories
        if missing:
            return {"allowed": False, "reason": "missing_passed_categories", "experimentId": row["id"], "missing": sorted(missing)}

        average = sum(float(case["score"]) for case in cases) / max(1, len(cases))
        if average < minimum_average_score:
            return {
                "allowed": False,
                "reason": "average_score_below_threshold",
                "experimentId": row["id"],
                "averageScore": round(average, 4),
                "minimumAverageScore": minimum_average_score,
            }
        return {
            "allowed": True,
            "reason": "evaluation_gate_passed",
            "experimentId": row["id"],
            "averageScore": round(average, 4),
            "categories": sorted(categories),
        }
    finally:
        conn.close()


def online_adaptation_enabled() -> bool:
    return _as_bool(os.getenv("AGENT_ENGINE_ENABLE_ONLINE_ADAPTATION"))


def require_online_adaptation_gate(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    model: str,
    prompt_version: str,
    policy_version: str,
    minimum_average_score: float = 0.85,
) -> dict[str, Any]:
    if not online_adaptation_enabled():
        return {"allowed": False, "reason": "online_adaptation_disabled_by_default"}
    return adaptation_decision(
        registry_path=registry_path,
        model=model,
        prompt_version=prompt_version,
        policy_version=policy_version,
        minimum_average_score=minimum_average_score,
        require_live=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gate online self-improvement on live evaluation evidence.")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt-version", required=True)
    parser.add_argument("--policy-version", required=True)
    parser.add_argument("--minimum-average-score", type=float, default=0.85)
    parser.add_argument("--allow-mock", action="store_true", help="Only for local diagnostics; online adaptation still requires live evals.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    decision = adaptation_decision(
        registry_path=Path(args.registry),
        model=args.model,
        prompt_version=args.prompt_version,
        policy_version=args.policy_version,
        minimum_average_score=args.minimum_average_score,
        require_live=not args.allow_mock,
    )
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return 0 if decision["allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
