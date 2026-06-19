from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .debug_log import write_debug_event
from .durable_execution import execution_artifact_dir

_IGNORED_DIRS = {
    ".git",
    ".agent-state",
    ".codegraph",
    ".next",
    ".nuxt",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
    "target",
    "vendor",
}

_SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    ".npmrc",
    "**/.npmrc",
    ".pypirc",
    "**/.pypirc",
    ".netrc",
    "**/.netrc",
    "**/.ssh/**",
    ".ssh/**",
    "**/.aws/**",
    ".aws/**",
    "**/.azure/**",
    ".azure/**",
    "**/.kube/**",
    ".kube/**",
    "**/id_rsa",
    "id_rsa",
    "**/id_ed25519",
    "id_ed25519",
    "**/*.pem",
    "*.pem",
    "**/*.key",
    "*.key",
    "**/*.p12",
    "*.p12",
    "**/*.pfx",
    "*.pfx",
    "**/*credentials*.json",
    "*credentials*.json",
    "**/*secrets*.json",
    "*secrets*.json",
)


def _ignored_dirs() -> set[str]:
    names = set(_IGNORED_DIRS)
    state_dir = str(os.getenv("AGENT_ENGINE_STATE_DIR") or "").strip()
    if state_dir:
        names.add(Path(state_dir).name)
    return names


def _git(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_root(workspace: Path) -> Path | None:
    result = _git(["rev-parse", "--show-toplevel"], workspace)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _ensure_repository_head(repository: Path) -> tuple[bool, str]:
    head = _git(["rev-parse", "--verify", "HEAD"], repository)
    if head.returncode == 0:
        return False, ""
    commit = _git(
        [
            "-c",
            "user.name=Local Agent",
            "-c",
            "user.email=local-agent@localhost",
            "commit",
            "--allow-empty",
            "--only",
            "--no-gpg-sign",
            "-m",
            "Initialize workspace",
        ],
        repository,
    )
    if commit.returncode != 0:
        return False, (commit.stderr or commit.stdout or "initial git commit failed").strip()
    return True, ""


def _bootstrap_empty_repository(workspace: Path) -> tuple[Path | None, str]:
    try:
        is_empty = workspace.is_dir() and next(workspace.iterdir(), None) is None
    except OSError as exc:
        return None, f"Could not inspect the selected workspace: {exc}"
    if not is_empty:
        return (
            None,
            "The selected workspace contains files but is not inside a Git repository; "
            "secure write tasks require worktree-per-execution.",
        )

    git_dir = workspace / ".git"
    initialized = False
    try:
        init = _git(["init"], workspace)
        if init.returncode != 0:
            return None, (init.stderr or init.stdout or "git init failed").strip()
        initialized = True
        return workspace.resolve(), ""
    except Exception as exc:
        if initialized and git_dir.exists():
            shutil.rmtree(git_dir, ignore_errors=True)
        return None, f"Could not initialize Git for the empty workspace: {exc}"


def _safe_relative(path: Path, root: Path) -> str:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return "." if relative == "." else relative


def _normalize_policy_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").lstrip("./").strip("/")


def _matches_policy(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    normalized = _normalize_policy_path(path)
    for raw in patterns:
        pattern = _normalize_policy_path(raw)
        if not pattern:
            continue
        if pattern == normalized or fnmatch.fnmatch(normalized, pattern):
            return True
        if pattern.endswith("/**") and (normalized == pattern[:-3] or normalized.startswith(pattern[:-2])):
            return True
    return False


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _iter_files(root: Path):
    for current, dir_names, file_names in os.walk(root):
        current_path = Path(current)
        ignored = _ignored_dirs()
        dir_names[:] = [
            name
            for name in dir_names
            if name not in ignored and not _is_link_like(current_path / name)
        ]
        for name in file_names:
            path = current_path / name
            if path.name == ".git":
                continue
            if _is_link_like(path):
                continue
            yield path


def _hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in _iter_files(root):
        try:
            relative = path.relative_to(root).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    for current, dir_names, file_names in os.walk(root):
        current_path = Path(current)
        for name in [*dir_names, *file_names]:
            path = current_path / name
            if not _is_link_like(path):
                continue
            try:
                relative = path.relative_to(root).as_posix()
                result[relative] = f"link:{os.readlink(path)}"
            except OSError:
                continue
        dir_names[:] = [
            name
            for name in dir_names
            if name not in _ignored_dirs() and not _is_link_like(current_path / name)
        ]
    return result


def _sanitize_worktree(root: Path) -> list[str]:
    removed: list[str] = []
    for current, dir_names, file_names in os.walk(root, topdown=True):
        current_path = Path(current)
        ignored = _ignored_dirs()
        kept_dirs = []
        for name in dir_names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if name in ignored:
                try:
                    if _is_link_like(path):
                        path.unlink()
                    else:
                        shutil.rmtree(path)
                    removed.append(relative)
                except FileNotFoundError:
                    pass
                continue
            if _is_link_like(path) or _matches_policy(relative, _SENSITIVE_PATTERNS):
                try:
                    if _is_link_like(path):
                        path.unlink()
                    else:
                        shutil.rmtree(path)
                    removed.append(relative)
                except FileNotFoundError:
                    pass
                continue
            kept_dirs.append(name)
        dir_names[:] = kept_dirs
        for name in file_names:
            path = current_path / name
            if path.name == ".git":
                continue
            relative = path.relative_to(root).as_posix()
            if not _is_link_like(path) and not _matches_policy(relative, _SENSITIVE_PATTERNS):
                continue
            try:
                path.unlink()
                removed.append(relative)
            except FileNotFoundError:
                pass
    return removed


def _sync_source_to_worktree(source: Path, target: Path) -> None:
    source_files = {
        path.relative_to(source).as_posix(): path
        for path in _iter_files(source)
        if not _matches_policy(path.relative_to(source).as_posix(), _SENSITIVE_PATTERNS)
    }
    target_files = {path.relative_to(target).as_posix(): path for path in _iter_files(target)}

    for relative, target_path in target_files.items():
        if relative in source_files:
            continue
        try:
            target_path.unlink()
        except FileNotFoundError:
            pass

    for relative, source_path in source_files.items():
        target_path = target / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)


def prepare_execution_worktree(workspace: str, execution_id: str) -> dict[str, Any]:
    source_workspace = Path(workspace).resolve()
    source_repo = _git_root(source_workspace)
    initialized_repo = False
    if source_repo is None:
        source_repo, reason = _bootstrap_empty_repository(source_workspace)
        if source_repo is None:
            return {
                "ready": False,
                "mode": "unavailable",
                "reason": reason,
                "sourceWorkspace": str(source_workspace),
            }
        initialized_repo = True

    initialized_head, reason = _ensure_repository_head(source_repo)
    if reason:
        if initialized_repo:
            shutil.rmtree(source_repo / ".git", ignore_errors=True)
        return {
            "ready": False,
            "mode": "unavailable",
            "reason": f"Could not create an initial Git commit for worktree isolation: {reason}",
            "sourceWorkspace": str(source_workspace),
            "sourceRepoRoot": str(source_repo),
        }

    relative_workspace = _safe_relative(source_workspace, source_repo)
    artifact_root = execution_artifact_dir(execution_id)
    worktree_root = artifact_root / "git-worktree"
    baseline_path = artifact_root / "worktree-baseline.json"
    reused = worktree_root.exists() and (worktree_root / ".git").exists()

    if worktree_root.exists() and not reused:
        resolved = worktree_root.resolve()
        if artifact_root.resolve() not in resolved.parents:
            raise RuntimeError(f"Refusing to remove worktree outside execution artifacts: {resolved}")
        shutil.rmtree(worktree_root)

    if not reused:
        result = _git(["worktree", "add", "--detach", "--force", str(worktree_root), "HEAD"], source_repo, timeout=120)
        if result.returncode != 0:
            return {
                "ready": False,
                "mode": "unavailable",
                "reason": (result.stderr or result.stdout or "git worktree add failed").strip(),
                "sourceWorkspace": str(source_workspace),
                "sourceRepoRoot": str(source_repo),
            }
        removed = _sanitize_worktree(worktree_root)
        _sync_source_to_worktree(source_repo, worktree_root)
        removed.extend(_sanitize_worktree(worktree_root))
        baseline_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "sourceRepoRoot": str(source_repo),
                    "sourceWorkspace": str(source_workspace),
                    "relativeWorkspace": relative_workspace,
                    "hashes": _hashes(worktree_root),
                    "excludedPaths": sorted(set(removed)),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    selected_worktree = worktree_root if relative_workspace == "." else worktree_root / relative_workspace
    selected_worktree.mkdir(parents=True, exist_ok=True)
    info = {
        "ready": True,
        "mode": "git-worktree",
        "executionId": execution_id,
        "sourceWorkspace": str(source_workspace),
        "sourceRepoRoot": str(source_repo),
        "relativeWorkspace": relative_workspace,
        "worktreeRoot": str(worktree_root),
        "workspacePath": str(selected_worktree),
        "baselinePath": str(baseline_path),
        "reused": reused,
        "bootstrappedRepo": initialized_repo or initialized_head,
        "initializedHead": initialized_head,
    }
    write_debug_event("worktree.prepared", info)
    return info


def merge_execution_worktree(
    info: dict[str, Any],
    allowed_patterns: list[str] | None = None,
    forbidden_patterns: list[str] | None = None,
) -> dict[str, Any]:
    if not info.get("ready"):
        return {"applied": [], "conflicts": [], "changes": [], "policyViolations": []}
    source_root = Path(info["sourceRepoRoot"]).resolve()
    worktree_root = Path(info["worktreeRoot"]).resolve()
    baseline = json.loads(Path(info["baselinePath"]).read_text(encoding="utf-8"))
    baseline_hashes = dict(baseline.get("hashes") or {})
    source_hashes = _hashes(source_root)
    worktree_hashes = _hashes(worktree_root)
    changes = []
    for relative in sorted(set(baseline_hashes) | set(worktree_hashes)):
        if baseline_hashes.get(relative) == worktree_hashes.get(relative):
            continue
        status = "created" if relative not in baseline_hashes else ("deleted" if relative not in worktree_hashes else "modified")
        changes.append({"path": relative, "status": status})

    conflicts = []
    policy_violations = []
    mergeable = []
    forbidden = [*list(forbidden_patterns or []), *_SENSITIVE_PATTERNS]
    for change in changes:
        relative = change["path"]
        worktree_target = worktree_root / relative
        allowed = allowed_patterns is None or (bool(allowed_patterns) and _matches_policy(relative, allowed_patterns))
        blocked = _matches_policy(relative, forbidden)
        link_like = _is_link_like(worktree_target)
        if not allowed or blocked or link_like:
            policy_violations.append(
                {
                    **change,
                    "reason": (
                        "symlink_not_mergeable"
                        if link_like
                        else ("forbiddenPath" if blocked else "outsideAllowedFiles")
                    ),
                }
            )
            continue
        baseline_hash = baseline_hashes.get(relative)
        source_hash = source_hashes.get(relative)
        worktree_hash = worktree_hashes.get(relative)
        if source_hash != baseline_hash and source_hash != worktree_hash:
            conflicts.append({**change, "reason": "source_changed_since_worktree_created"})
            continue
        mergeable.append(change)

    if conflicts or policy_violations:
        result = {
            "changes": changes,
            "applied": [],
            "conflicts": conflicts,
            "policyViolations": policy_violations,
        }
        write_debug_event("worktree.merge", {**result, "executionId": info.get("executionId")})
        return result

    applied = []
    for change in mergeable:
        relative = change["path"]
        source_target = source_root / relative
        worktree_target = worktree_root / relative
        if change["status"] == "deleted":
            if source_target.exists() and source_target.is_file():
                source_target.unlink()
            applied.append(change)
            continue
        source_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(worktree_target, source_target)
        applied.append(change)

    result = {
        "changes": changes,
        "applied": applied,
        "conflicts": conflicts,
        "policyViolations": policy_violations,
    }
    write_debug_event("worktree.merge", {**result, "executionId": info.get("executionId")})
    return result


def cleanup_execution_worktree(info: dict[str, Any]) -> dict[str, Any]:
    if not info.get("ready") or not info.get("worktreeRoot") or not info.get("sourceRepoRoot"):
        return {"removed": False, "reason": "no_worktree"}
    source_root = Path(info["sourceRepoRoot"]).resolve()
    worktree_root = Path(info["worktreeRoot"]).resolve()
    result = _git(["worktree", "remove", "--force", str(worktree_root)], source_root, timeout=120)
    _git(["worktree", "prune"], source_root)
    removed = result.returncode == 0 or not worktree_root.exists()
    payload = {
        "removed": removed,
        "worktreeRoot": str(worktree_root),
        "reason": "" if removed else (result.stderr or result.stdout).strip(),
    }
    write_debug_event("worktree.cleanup", payload)
    return payload
