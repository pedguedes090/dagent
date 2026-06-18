from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .workspace import changed_files, file_hashes

REQUIRED_CATEGORIES = {
    "read-only",
    "bugfix",
    "refactor",
    "scaffold-project",
    "security-patch",
    "migration",
    "ci-repair",
}
REQUIRED_RUBRIC = {
    "functional_correctness",
    "diff_minimality",
    "test_pass",
    "security_regressions",
    "latency",
    "cost",
}
DEFAULT_BENCHMARK = Path("benchmarks/internal_benchmark.json")
DEFAULT_REGISTRY = Path(".agent-state/evaluation-registry.sqlite")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_benchmark(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_benchmark(benchmark: dict[str, Any]) -> None:
    rubric = benchmark.get("rubric") or {}
    if set(rubric) != REQUIRED_RUBRIC:
        raise ValueError(f"Benchmark rubric must contain exactly {sorted(REQUIRED_RUBRIC)}.")
    total_weight = sum(float(value) for value in rubric.values())
    if abs(total_weight - 1.0) > 0.001:
        raise ValueError(f"Benchmark rubric weights must sum to 1.0, got {total_weight}.")

    cases = benchmark.get("cases") or []
    categories = {case.get("category") for case in cases}
    missing = REQUIRED_CATEGORIES - categories
    if missing:
        raise ValueError(f"Benchmark is missing categories: {sorted(missing)}.")
    for case in cases:
        if not case.get("id") or not case.get("task"):
            raise ValueError("Every benchmark case must have id and task.")
        if not isinstance(case.get("files"), dict):
            raise ValueError(f"Case {case.get('id')} must define fixture files.")
        if not isinstance(case.get("expected"), dict):
            raise ValueError(f"Case {case.get('id')} must define expected checks.")


def _write_files(root: Path, files: dict[str, str | None]) -> None:
    for relative, content in files.items():
        target = (root / relative).resolve()
        if root not in target.parents and target != root:
            raise ValueError(f"Fixture path escapes workspace: {relative}")
        if content is None:
            if target.exists() and target.is_file():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")


def _quote_command_path(value: str) -> str:
    if os.name == "nt":
        return f'"{value}"'
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _run_command(workspace: Path, command: str, timeout: int = 120) -> dict[str, Any]:
    command = command.replace("{python}", _quote_command_path(sys.executable))
    started = time.perf_counter()
    try:
        proc = subprocess.run(command, cwd=str(workspace), shell=True, capture_output=True, text=True, timeout=timeout)
        return {
            "command": command,
            "code": proc.returncode,
            "stdout": proc.stdout[-12000:],
            "stderr": proc.stderr[-12000:],
            "timedOut": False,
            "latencyMs": round((time.perf_counter() - started) * 1000, 2),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "code": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timedOut": True,
            "latencyMs": round((time.perf_counter() - started) * 1000, 2),
        }


def _run_verification(workspace: Path, commands: list[str]) -> list[dict[str, Any]]:
    return [_run_command(workspace, command) for command in commands]


def _match_any(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if pattern == "**":
            return True
        if fnmatch.fnmatch(normalized, pattern.replace("\\", "/")):
            return True
    return False


def _all_workspace_text(workspace: Path) -> dict[str, str]:
    texts: dict[str, str] = {}
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(workspace).as_posix()
            texts[relative] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return texts


def _score_functional(case: dict[str, Any], result: dict[str, Any], workspace: Path, changed: list[dict[str, Any]]) -> tuple[float, list[str]]:
    expected = case.get("expected") or {}
    failures: list[str] = []
    changed_paths = {item.get("path") for item in changed}

    for path in expected.get("requiredChangedFiles") or []:
        if path not in changed_paths:
            failures.append(f"Required changed file missing: {path}")

    assistant = str(result.get("assistantText") or "")
    for text in expected.get("assistantContains") or []:
        if text.lower() not in assistant.lower():
            failures.append(f"Assistant output missing text: {text}")

    for check in expected.get("requiredFileContains") or []:
        path = str(check.get("path") or "")
        needle = str(check.get("contains") or "")
        target = workspace / path
        if not target.exists():
            failures.append(f"Required file missing: {path}")
            continue
        content = target.read_text(encoding="utf-8", errors="replace")
        if needle not in content:
            failures.append(f"{path} missing required content: {needle}")

    if not failures:
        return 1.0, []
    total_checks = (
        len(expected.get("requiredChangedFiles") or [])
        + len(expected.get("assistantContains") or [])
        + len(expected.get("requiredFileContains") or [])
    )
    if total_checks <= 0:
        return 0.0, failures
    return max(0.0, 1.0 - (len(failures) / total_checks)), failures


def _score_diff(case: dict[str, Any], changed: list[dict[str, Any]]) -> tuple[float, list[str]]:
    expected = case.get("expected") or {}
    failures: list[str] = []
    changed_paths = [str(item.get("path") or "") for item in changed]
    max_changed = expected.get("maxChangedFiles")
    if isinstance(max_changed, int) and len(changed_paths) > max_changed:
        failures.append(f"Changed {len(changed_paths)} files, max allowed {max_changed}.")
    forbidden = list(expected.get("forbiddenChangedFiles") or [])
    for path in changed_paths:
        if _match_any(path, forbidden):
            failures.append(f"Forbidden changed file: {path}")
    return (1.0 if not failures else 0.0), failures


def _score_tests(command_results: list[dict[str, Any]]) -> tuple[float, list[str]]:
    if not command_results:
        return 1.0, []
    failures = [
        f"{item.get('command')}: timeout" if item.get("timedOut") else f"{item.get('command')}: exit {item.get('code')}"
        for item in command_results
        if item.get("timedOut") or item.get("code") != 0
    ]
    return (1.0 if not failures else 0.0), failures


def _score_security(case: dict[str, Any], result: dict[str, Any], workspace: Path) -> tuple[float, list[str]]:
    expected = case.get("expected") or {}
    failures: list[str] = []
    patterns = [re.compile(pattern) for pattern in expected.get("forbiddenContentPatterns") or []]
    if patterns:
        for path, content in _all_workspace_text(workspace).items():
            for pattern in patterns:
                if pattern.search(content):
                    failures.append(f"{path} still matches forbidden pattern: {pattern.pattern}")
    if result.get("policyViolations"):
        failures.append("Policy violations were reported by the agent.")
    review = result.get("review") or {}
    security_review = review.get("securityReview") if isinstance(review, dict) else None
    if isinstance(security_review, dict) and security_review.get("blockers"):
        failures.append("Security reviewer reported blockers.")
    return (1.0 if not failures else 0.0), failures


def _ratio_score(observed: float, budget: float) -> float:
    if budget <= 0:
        return 1.0
    if observed <= budget:
        return 1.0
    return max(0.0, min(1.0, budget / observed))


def _score_case(
    benchmark: dict[str, Any],
    case: dict[str, Any],
    result: dict[str, Any],
    workspace: Path,
    changed: list[dict[str, Any]],
    command_results: list[dict[str, Any]],
    latency_ms: float,
) -> dict[str, Any]:
    rubric = benchmark["rubric"]
    token_usage = int(result.get("tokenUsage") or 0)
    latency_target = float(case.get("latencyTargetMs") or benchmark.get("latencyTargetMs") or 600000)
    token_budget = float(case.get("tokenBudget") or benchmark.get("tokenBudget") or 180000)

    functional_score, functional_failures = _score_functional(case, result, workspace, changed)
    diff_score, diff_failures = _score_diff(case, changed)
    test_score, test_failures = _score_tests(command_results)
    security_score, security_failures = _score_security(case, result, workspace)
    latency_score = _ratio_score(latency_ms, latency_target)
    cost_score = _ratio_score(float(token_usage), token_budget)

    scores = {
        "functional_correctness": functional_score,
        "diff_minimality": diff_score,
        "test_pass": test_score,
        "security_regressions": security_score,
        "latency": latency_score,
        "cost": cost_score,
    }
    weighted = sum(float(rubric[key]) * scores[key] for key in rubric)
    return {
        "score": round(weighted, 4),
        "rubric": scores,
        "failures": {
            "functional_correctness": functional_failures,
            "diff_minimality": diff_failures,
            "test_pass": test_failures,
            "security_regressions": security_failures,
        },
        "metrics": {
            "latencyMs": round(latency_ms, 2),
            "tokenUsage": token_usage,
            "changedFileCount": len(changed),
            "commandCount": len(command_results),
        },
    }


class EvaluationRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS evaluation_experiments (
              id TEXT PRIMARY KEY,
              benchmark_id TEXT NOT NULL,
              benchmark_version TEXT NOT NULL,
              mode TEXT NOT NULL,
              model TEXT NOT NULL,
              server_url TEXT NOT NULL,
              prompt_version TEXT NOT NULL,
              policy_version TEXT NOT NULL,
              git_commit TEXT NOT NULL,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS evaluation_case_results (
              id TEXT PRIMARY KEY,
              experiment_id TEXT NOT NULL,
              case_id TEXT NOT NULL,
              category TEXT NOT NULL,
              status TEXT NOT NULL,
              score REAL NOT NULL,
              rubric_json TEXT NOT NULL,
              metrics_json TEXT NOT NULL,
              result_json TEXT NOT NULL,
              artifacts_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY (experiment_id) REFERENCES evaluation_experiments(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS evaluation_registry (
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              name TEXT NOT NULL,
              version TEXT NOT NULL,
              status TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              UNIQUE(kind, name, version)
            );

            CREATE INDEX IF NOT EXISTS idx_eval_cases_experiment ON evaluation_case_results(experiment_id, category);
            CREATE INDEX IF NOT EXISTS idx_eval_experiments_versions ON evaluation_experiments(model, prompt_version, policy_version, started_at);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def register_variant(self, kind: str, name: str, version: str, metadata: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO evaluation_registry (id, kind, name, version, status, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), kind, name, version, "candidate", _json(metadata or {}), _now()),
        )
        self.conn.commit()

    def create_experiment(self, benchmark: dict[str, Any], args: argparse.Namespace, git_commit: str) -> str:
        experiment_id = str(uuid.uuid4())
        self.register_variant("model", args.model, args.model, {"serverUrl": args.server_url})
        self.register_variant("prompt", "pipeline-prompts", args.prompt_version)
        self.register_variant("policy", "agent-policy", args.policy_version)
        self.conn.execute(
            """
            INSERT INTO evaluation_experiments (
              id, benchmark_id, benchmark_version, mode, model, server_url, prompt_version,
              policy_version, git_commit, status, started_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id,
                benchmark["id"],
                benchmark["version"],
                args.mode,
                args.model,
                args.server_url,
                args.prompt_version,
                args.policy_version,
                git_commit,
                "running",
                _now(),
                _json({"passScore": args.pass_score}),
            ),
        )
        self.conn.commit()
        return experiment_id

    def record_case(
        self,
        experiment_id: str,
        case: dict[str, Any],
        status: str,
        score: dict[str, Any],
        result: dict[str, Any],
        artifacts: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO evaluation_case_results (
              id, experiment_id, case_id, category, status, score, rubric_json,
              metrics_json, result_json, artifacts_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                experiment_id,
                case["id"],
                case["category"],
                status,
                float(score["score"]),
                _json({"scores": score["rubric"], "failures": score["failures"]}),
                _json(score["metrics"]),
                _json(result),
                _json(artifacts),
                _now(),
            ),
        )
        self.conn.commit()

    def finish_experiment(self, experiment_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE evaluation_experiments SET status = ?, finished_at = ? WHERE id = ?",
            (status, _now(), experiment_id),
        )
        self.conn.commit()

    def comparison(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              e.id, e.benchmark_id, e.benchmark_version, e.mode, e.model,
              e.prompt_version, e.policy_version, e.status, e.started_at,
              COUNT(c.id) AS case_count,
              AVG(c.score) AS average_score,
              SUM(CASE WHEN c.status = 'passed' THEN 1 ELSE 0 END) AS passed_cases
            FROM evaluation_experiments e
            LEFT JOIN evaluation_case_results c ON c.experiment_id = e.id
            GROUP BY e.id
            ORDER BY e.started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "benchmarkId": row["benchmark_id"],
                "benchmarkVersion": row["benchmark_version"],
                "mode": row["mode"],
                "model": row["model"],
                "promptVersion": row["prompt_version"],
                "policyVersion": row["policy_version"],
                "status": row["status"],
                "startedAt": row["started_at"],
                "caseCount": int(row["case_count"] or 0),
                "passedCases": int(row["passed_cases"] or 0),
                "averageScore": round(float(row["average_score"] or 0), 4),
            }
            for row in rows
        ]


def _git_commit() -> str:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(_project_root()), capture_output=True, text=True, timeout=5)
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _emit_silent(_stage: str, _detail: str) -> None:
    return None


def _run_live_case(case: dict[str, Any], workspace: Path, args: argparse.Namespace, experiment_id: str) -> dict[str, Any]:
    from .graph import run_pipeline

    previous_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
    state_dir = workspace / ".eval-agent-state"
    os.environ["AGENT_ENGINE_STATE_DIR"] = str(state_dir)
    try:
        return run_pipeline(
            {
                "content": case["task"],
                "workspacePath": str(workspace),
                "settings": {
                    "serverUrl": args.server_url,
                    "model": args.model,
                    "apiKey": args.api_key or "",
                    "autoConfirmHumanGate": True,
                },
                "messages": [],
                "sessionId": f"eval-{experiment_id}-{case['id']}",
                "correlationId": f"eval-{experiment_id}-{case['id']}",
            },
            _emit_silent,
        )
    finally:
        if previous_state_dir is None:
            os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
        else:
            os.environ["AGENT_ENGINE_STATE_DIR"] = previous_state_dir


def _copy_artifacts(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".eval-agent-state", "__pycache__"))


def _run_case(
    benchmark: dict[str, Any],
    case: dict[str, Any],
    args: argparse.Namespace,
    experiment_id: str,
    registry: EvaluationRegistry,
) -> dict[str, Any]:
    keep_artifacts = bool(args.keep_artifacts)
    artifact_root = Path(args.artifacts_dir) / experiment_id / case["id"]
    temp_manager = tempfile.TemporaryDirectory(prefix=f"hethongagent-eval-{case['id']}-")
    workspace = Path(temp_manager.name)
    try:
        _write_files(workspace, case["files"])
        before = file_hashes(str(workspace))
        started = time.perf_counter()
        if args.mode == "mock":
            _write_files(workspace, case.get("solutionFiles") or {})
            result = dict(case.get("mockResult") or {})
            result.setdefault("tokenUsage", 0)
        else:
            result = _run_live_case(case, workspace, args, experiment_id)
        latency_ms = (time.perf_counter() - started) * 1000
        after = file_hashes(str(workspace))
        changed = changed_files(before, after)
        verification = _run_verification(workspace, list(case.get("verificationCommands") or []))
        result["changedFiles"] = result.get("changedFiles") or changed
        score = _score_case(benchmark, case, result, workspace, changed, verification, latency_ms)
        passed = (
            score["score"] >= args.pass_score
            and score["rubric"]["test_pass"] == 1.0
            and score["rubric"]["security_regressions"] == 1.0
        )
        status = "passed" if passed else "failed"
        artifacts = {
            "workspace": str(artifact_root) if keep_artifacts else "",
            "changedFiles": changed,
            "verificationCommands": verification,
        }
        if keep_artifacts:
            _copy_artifacts(workspace, artifact_root)
        registry.record_case(experiment_id, case, status, score, result, artifacts)
        return {"caseId": case["id"], "category": case["category"], "status": status, "score": score["score"]}
    except Exception as exc:
        score = {
            "score": 0.0,
            "rubric": {key: 0.0 for key in REQUIRED_RUBRIC},
            "failures": {"error": [str(exc)]},
            "metrics": {"latencyMs": 0, "tokenUsage": 0, "changedFileCount": 0, "commandCount": 0},
        }
        result = {"error": str(exc), "tokenUsage": 0}
        registry.record_case(experiment_id, case, "error", score, result, {"workspace": ""})
        return {"caseId": case["id"], "category": case["category"], "status": "error", "score": 0.0, "error": str(exc)}
    finally:
        temp_manager.cleanup()


def run_experiment(args: argparse.Namespace) -> int:
    benchmark = _load_benchmark(Path(args.benchmark))
    _validate_benchmark(benchmark)
    registry = EvaluationRegistry(Path(args.registry))
    experiment_id = registry.create_experiment(benchmark, args, _git_commit())
    try:
        results = [_run_case(benchmark, case, args, experiment_id, registry) for case in benchmark["cases"]]
        status = "passed" if all(item["status"] == "passed" for item in results) else "failed"
        registry.finish_experiment(experiment_id, status)
        summary = {
            "experimentId": experiment_id,
            "status": status,
            "benchmarkId": benchmark["id"],
            "benchmarkVersion": benchmark["version"],
            "mode": args.mode,
            "averageScore": round(sum(float(item["score"]) for item in results) / max(1, len(results)), 4),
            "results": results,
            "registry": str(Path(args.registry).resolve()),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if status == "passed" else 1
    finally:
        registry.close()


def validate_benchmark(args: argparse.Namespace) -> int:
    benchmark = _load_benchmark(Path(args.benchmark))
    _validate_benchmark(benchmark)
    print(json.dumps({"ok": True, "benchmarkId": benchmark["id"], "caseCount": len(benchmark["cases"])}, indent=2))
    return 0


def compare_experiments(args: argparse.Namespace) -> int:
    registry = EvaluationRegistry(Path(args.registry))
    try:
        print(json.dumps({"experiments": registry.comparison(limit=args.limit)}, ensure_ascii=False, indent=2))
    finally:
        registry.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run and compare HeThongAgent internal evaluations.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate benchmark structure.")
    validate.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK))
    validate.set_defaults(func=validate_benchmark)

    run = subparsers.add_parser("run", help="Run benchmark cases and log results.")
    run.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK))
    run.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    run.add_argument("--artifacts-dir", default=".agent-state/evaluations")
    run.add_argument("--mode", choices=["live", "mock"], default="live")
    run.add_argument("--server-url", default=os.getenv("AGENT_EVAL_SERVER_URL", "http://localhost:20128/v1"))
    run.add_argument("--model", default=os.getenv("AGENT_EVAL_MODEL", "gemini/gemini-3.1-flash-lite"))
    run.add_argument("--api-key", default=os.getenv("AGENT_EVAL_API_KEY", ""))
    run.add_argument("--prompt-version", default=os.getenv("AGENT_PROMPT_VERSION", "local-current"))
    run.add_argument("--policy-version", default=os.getenv("AGENT_POLICY_VERSION", "agent-contracts-v1"))
    run.add_argument("--pass-score", type=float, default=0.85)
    run.add_argument("--keep-artifacts", action="store_true")
    run.set_defaults(func=run_experiment)

    compare = subparsers.add_parser("compare", help="Compare logged experiment runs.")
    compare.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    compare.add_argument("--limit", type=int, default=20)
    compare.set_defaults(func=compare_experiments)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
