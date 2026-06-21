"""Static scanner — deterministic checks before any LLM involvement.

The scanner only reads the project; it never edits. Its job is to populate
ScanReport with concrete, line-anchored Issue records that the planner
and patcher can act on.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .models import Issue, IssueGroup, IssueSeverity, ScanReport

# Walked-into directories that almost never contain user-fixable issues.
_SKIP_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".tools", ".agent-state", ".codegraph", "dist", "build", "out",
    ".pytest-tmp", ".next", ".nuxt", "target", "vendor",
}

# Patterns that strongly suggest a real secret left in source.
# We require the assignment shape so plain mentions in docs/markdown don't trip.
_SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*["\']([A-Za-z0-9_\-]{16,})["\']'),
    re.compile(r'sk-[A-Za-z0-9]{20,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'ghp_[A-Za-z0-9]{20,}'),
    re.compile(r'xox[bp]-[A-Za-z0-9-]{10,}'),
]


def _issue_id() -> str:
    return uuid.uuid4().hex[:12]


def _walk_files(root: Path, exts: set[str]) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext in exts:
                out.append(Path(dirpath) / name)
    return out


def _check_python_syntax(root: Path, report: ScanReport) -> None:
    for path in _walk_files(root, {".py"}):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            report.add(Issue(
                id=_issue_id(),
                group=IssueGroup.CRITICAL,
                severity=IssueSeverity.BLOCKER,
                file=str(path.relative_to(root)),
                line=exc.lineno,
                title=f"Python syntax error: {exc.msg}",
                detail=f"{exc.msg} at line {exc.lineno}, offset {exc.offset}",
                root_cause="Source file cannot be parsed by the Python AST.",
                suggested_fix=f"Fix the syntax near line {exc.lineno}.",
                autofix_safe=False,  # syntax fixes need the LLM patch path
            ))


def _check_js_syntax(root: Path, report: ScanReport) -> None:
    node_bin = shutil.which("node")
    if not node_bin:
        return
    for path in _walk_files(root, {".js", ".mjs", ".cjs"}):
        try:
            result = subprocess.run(
                [node_bin, "--check", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            continue
        stderr = result.stderr.strip() or "node --check failed"
        line_match = re.search(r":(\d+)", stderr.splitlines()[0]) if stderr else None
        line = int(line_match.group(1)) if line_match else None
        # Distill the message — first 200 chars of last meaningful line.
        msg = next((ln.strip() for ln in stderr.splitlines() if "Error" in ln or "SyntaxError" in ln), stderr.splitlines()[0])
        report.add(Issue(
            id=_issue_id(),
            group=IssueGroup.CRITICAL,
            severity=IssueSeverity.BLOCKER,
            file=str(path.relative_to(root)),
            line=line,
            title=f"JS syntax error: {msg[:120]}",
            detail=stderr[:800],
            root_cause="`node --check` rejected the file.",
            suggested_fix="Restore the missing brace/paren/semicolon at the indicated location.",
            autofix_safe=False,
        ))


def _scan_secrets(root: Path, report: ScanReport) -> None:
    text_exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".env", ".cfg", ".toml"}
    for path in _walk_files(root, text_exts):
        # Skip lockfiles and obvious example/template files
        name = path.name.lower()
        if name.endswith(".lock") or name == "package-lock.json" or "example" in name or name == ".env.example":
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(source):
                line = source[:match.start()].count("\n") + 1
                report.add(Issue(
                    id=_issue_id(),
                    group=IssueGroup.SECURITY,
                    severity=IssueSeverity.MAJOR,
                    file=str(path.relative_to(root)),
                    line=line,
                    title="Possible secret committed in source",
                    detail=f"Pattern matched near line {line}: {match.group(0)[:60]}",
                    root_cause="Long, secret-shaped literal sits in tracked source.",
                    suggested_fix="Move the value to an environment variable and read via os.getenv / process.env.",
                    autofix_safe=False,
                ))


def _check_gitignore_basics(root: Path, report: ScanReport) -> None:
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return
    content = gitignore.read_text(encoding="utf-8", errors="ignore")
    must_have = [".env", "node_modules", "__pycache__"]
    missing = [item for item in must_have if item not in content]
    if missing:
        report.add(Issue(
            id=_issue_id(),
            group=IssueGroup.SECURITY,
            severity=IssueSeverity.MINOR,
            file=".gitignore",
            line=None,
            title=f".gitignore missing common entries: {', '.join(missing)}",
            detail=f"Entries that should be ignored: {missing}",
            root_cause="Project may accidentally commit env files or dependency caches.",
            suggested_fix=f"Append the missing entries to .gitignore: {missing}",
            autofix_safe=True,
        ))


def _check_dep_lock_drift(root: Path, report: ScanReport) -> None:
    pkg = root / "package.json"
    lock = root / "pnpm-lock.yaml"
    if not pkg.exists():
        return
    try:
        manifest = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    declared = set(manifest.get("dependencies", {}).keys()) | set(manifest.get("devDependencies", {}).keys())
    if not declared or not lock.exists():
        return
    try:
        lock_text = lock.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    # Cheap heuristic: every declared dep should appear in the lockfile text.
    missing = [name for name in declared if f"'{name}'" not in lock_text and f'"{name}"' not in lock_text and f"\n  {name}:" not in lock_text]
    if missing:
        report.add(Issue(
            id=_issue_id(),
            group=IssueGroup.CRITICAL,
            severity=IssueSeverity.MAJOR,
            file="pnpm-lock.yaml",
            line=None,
            title=f"Lockfile out of sync with package.json ({len(missing)} deps)",
            detail=f"Declared but missing in lockfile: {missing[:10]}",
            root_cause="pnpm install was not re-run after package.json was edited.",
            suggested_fix="Run `pnpm install` to regenerate the lockfile.",
            autofix_safe=True,
        ))


def _resolve_python(root: Path) -> str:
    for cand in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return "python"


def _run_check(cmd: list[str], root: Path, timeout: int) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd, cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


# pytest failure line:  tests/test_x.py::test_y  or  FAILED tests/test_x.py::test_y - AssertionError
_PYTEST_FAIL = re.compile(r'(?:FAILED|ERROR)\s+([^\s:]+\.py)::([^\s-]+)', re.MULTILINE)
# tsc:  src/foo.ts(12,5): error TS2304: Cannot find name 'bar'.
_TSC_FAIL = re.compile(r'^([^\s(][^(]*\.[tj]sx?)\((\d+),\d+\):\s*error\s+(TS\d+):\s*(.+)$', re.MULTILINE)


def _check_runtime_failures(root: Path, report: ScanReport) -> None:
    """Run the stack's own check commands ONCE and convert real failures
    (failing tests, type errors, build errors) into line-anchored Issue
    records so the patcher has concrete, fixable work — not just syntax/hygiene.

    Gated by AGENT_DOCTOR_RUN_CHECKS (default on); set to 0 to skip in CI/tests
    where running the suite recursively is undesirable.
    """
    if os.environ.get("AGENT_DOCTOR_RUN_CHECKS", "1") == "0":
        return

    # ── Python: pytest ──
    if (root / "pyproject.toml").exists() and (root / "tests").is_dir():
        proc = _run_check(
            [_resolve_python(root), "-m", "pytest", "tests/", "-q", "--timeout=60", "-x"],
            root, timeout=600,
        )
        if proc is not None and proc.returncode != 0:
            combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
            seen: set[str] = set()
            for m in _PYTEST_FAIL.finditer(combined):
                file, test = m.group(1), m.group(2)
                key = f"{file}::{test}"
                if key in seen:
                    continue
                seen.add(key)
                report.add(Issue(
                    id=_issue_id(),
                    group=IssueGroup.LOGIC,
                    severity=IssueSeverity.MAJOR,
                    file=file,
                    line=None,
                    title=f"Failing test: {test}",
                    detail=f"pytest reported {key} as failing. Tail:\n" + "\n".join(combined.splitlines()[-25:]),
                    root_cause="Test assertion failed or raised — implementation does not satisfy the test.",
                    suggested_fix=f"Fix the code under test so {test} passes; do not weaken the test.",
                    autofix_safe=False,
                ))
            if not seen:
                # Suite failed but no parseable per-test line (collection/import error).
                report.add(Issue(
                    id=_issue_id(),
                    group=IssueGroup.CRITICAL,
                    severity=IssueSeverity.BLOCKER,
                    file="tests/",
                    line=None,
                    title=f"Test suite failed (exit {proc.returncode})",
                    detail="pytest exited non-zero with no parseable test id (likely a collection/import error). Tail:\n"
                           + "\n".join(combined.splitlines()[-25:]),
                    root_cause="Import error, fixture failure, or syntax error preventing test collection.",
                    suggested_fix="Resolve the import/collection error reported in the traceback.",
                    autofix_safe=False,
                ))

    # ── Node/TS: type-check via the project's own check script, else tsc ──
    pkg = root / "package.json"
    if pkg.exists() and (root / "node_modules").exists():
        try:
            manifest = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        scripts = manifest.get("scripts") or {}
        npx = "npx.cmd" if os.name == "nt" else "npx"
        proc = None
        if "typecheck" in scripts:
            pnpm = "pnpm.cmd" if os.name == "nt" else "pnpm"
            proc = _run_check([pnpm, "run", "typecheck"], root, timeout=300)
        elif (root / "tsconfig.json").exists():
            proc = _run_check([npx, "tsc", "--noEmit"], root, timeout=300)
        if proc is not None and proc.returncode != 0:
            combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
            count = 0
            for m in _TSC_FAIL.finditer(combined):
                if count >= 30:
                    break
                count += 1
                file, line, code, msg = m.group(1), int(m.group(2)), m.group(3), m.group(4).strip()
                report.add(Issue(
                    id=_issue_id(),
                    group=IssueGroup.CRITICAL,
                    severity=IssueSeverity.MAJOR,
                    file=file.replace("\\", "/"),
                    line=line,
                    title=f"Type error {code}: {msg[:80]}",
                    detail=f"{file}({line}): {code}: {msg}",
                    root_cause="TypeScript type check failed.",
                    suggested_fix=f"Resolve {code} at {file}:{line}.",
                    autofix_safe=False,
                ))


def scan_project(root: Path) -> ScanReport:
    report = ScanReport(project_root=str(root))
    started = time.monotonic()
    _check_python_syntax(root, report)
    _check_js_syntax(root, report)
    _scan_secrets(root, report)
    _check_gitignore_basics(root, report)
    _check_dep_lock_drift(root, report)
    _check_runtime_failures(root, report)
    report.duration_ms = int((time.monotonic() - started) * 1000)
    return report
