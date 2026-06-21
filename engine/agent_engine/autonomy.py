from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .debug_log import write_debug_event
from .long_term_memory import ACTRMemoryStore, default_memory_path
from .workspace import IGNORED_DIRS, TEXT_EXTENSIONS, relpath


AUTONOMY_REPORT_VERSION = 1
AUTONOMY_IGNORE_DIRS = set(IGNORED_DIRS) | {".agent-state", ".agent", ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache"}
SEVERITY_SCORE = {"low": 1.0, "medium": 2.0, "high": 3.0, "critical": 4.0}

_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s-]*(.*)", re.IGNORECASE)
_PY_SECURITY_PATTERNS = [
    ("shell_true", re.compile(r"\bshell\s*=\s*True\b"), "Shell command uses shell=True; prefer argument arrays or a constrained allowlist."),
    ("eval", re.compile(r"\beval\s*\("), "Dynamic eval can execute untrusted data as code."),
    ("exec", re.compile(r"\bexec\s*\("), "Dynamic exec can execute untrusted data as code."),
    ("pickle_loads", re.compile(r"\bpickle\.loads?\s*\("), "Pickle loading can execute attacker-controlled payloads."),
    ("yaml_load", re.compile(r"\byaml\.load\s*\((?![^)]*SafeLoader)"), "yaml.load without SafeLoader can deserialize unsafe objects."),
]
_JS_SECURITY_PATTERNS = [
    ("inner_html", re.compile(r"\.innerHTML\s*="), "Direct innerHTML assignment needs sanitization or textContent."),
    ("eval", re.compile(r"\beval\s*\("), "Dynamic eval can execute untrusted data as code."),
    ("node_integration", re.compile(r"nodeIntegration\s*:\s*true"), "Electron renderer should not enable nodeIntegration."),
    ("context_isolation", re.compile(r"contextIsolation\s*:\s*false"), "Electron renderer should keep contextIsolation enabled."),
]


@dataclass(frozen=True)
class AutonomyFinding:
    id: str
    category: str
    title: str
    severity: str
    confidence: float
    impact: float
    effort: float
    priorityScore: float
    source: str
    evidence: str
    recommendation: str
    tags: list[str]
    memory: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def _report_path(state_dir: str | Path) -> Path:
    return Path(state_dir).resolve() / "autonomy" / "last-report.json"


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _finding_id(category: str, source: str, evidence: str) -> str:
    digest = hashlib.sha1(f"{category}\0{source}\0{evidence}".encode("utf-8", errors="replace")).hexdigest()
    return f"auto-{digest[:14]}"


def _iter_text_files(workspace: str | Path, *, max_files: int = 500, max_bytes: int = 180_000) -> list[Path]:
    root = Path(workspace).resolve()
    files: list[Path] = []

    def walk(current: Path, depth: int) -> None:
        if len(files) >= max_files or depth > 8:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            return
        for entry in entries:
            if len(files) >= max_files:
                return
            if entry.is_dir():
                if entry.name not in AUTONOMY_IGNORE_DIRS:
                    walk(entry, depth + 1)
                continue
            if entry.is_file() and entry.suffix.lower() in TEXT_EXTENSIONS:
                try:
                    if entry.stat().st_size <= max_bytes:
                        files.append(entry)
                except OSError:
                    continue

    walk(root, 0)
    return files


def _base_finding(
    *,
    category: str,
    title: str,
    severity: str,
    confidence: float,
    impact: float,
    effort: float,
    source: str,
    evidence: str,
    recommendation: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": _finding_id(category, source, evidence),
        "category": category,
        "title": title,
        "severity": severity,
        "confidence": round(confidence, 2),
        "impact": round(impact, 2),
        "effort": round(effort, 2),
        "source": source,
        "evidence": evidence,
        "recommendation": recommendation,
        "tags": sorted({category, severity, *(tags or [])}),
    }


def _scan_file(root: Path, path: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    relative = relpath(path, root)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    lines = text.splitlines()

    for index, line in enumerate(lines, start=1):
        todo = _TODO_RE.search(line)
        if todo:
            marker = todo.group(1).upper()
            severity = "medium" if marker in {"FIXME", "HACK"} else "low"
            findings.append(
                _base_finding(
                    category="technical_debt",
                    title=f"{marker} debt marker",
                    severity=severity,
                    confidence=0.88,
                    impact=1.8 if severity == "low" else 2.5,
                    effort=1.2,
                    source=f"{relative}:{index}",
                    evidence=line.strip()[:220],
                    recommendation="Convert the marker into an owned issue or remove it with a focused fix.",
                    tags=["intrinsic_motivation", "debt_marker"],
                )
            )

    if path.suffix.lower() == ".py":
        patterns = _PY_SECURITY_PATTERNS
    elif path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx", ".mjs"}:
        patterns = _JS_SECURITY_PATTERNS
    else:
        patterns = []
    for index, line in enumerate(lines, start=1):
        for key, pattern, message in patterns:
            if not pattern.search(line):
                continue
            findings.append(
                _base_finding(
                    category="security",
                    title=f"Unsafe pattern: {key}",
                    severity="high",
                    confidence=0.76,
                    impact=3.4,
                    effort=2.1,
                    source=f"{relative}:{index}",
                    evidence=line.strip()[:220],
                    recommendation=message,
                    tags=["security_review", key],
                )
            )

    if len(lines) > 650:
        findings.append(
            _base_finding(
                category="maintainability",
                title="Large source file",
                severity="medium",
                confidence=0.72,
                impact=2.4,
                effort=3.8,
                source=relative,
                evidence=f"{relative} has {len(lines)} lines.",
                recommendation="Plan a module-boundary split with tests around the seams before refactoring.",
                tags=["long_horizon", "module_boundary"],
            )
        )

    return findings


def _scan_missing_tests(workspace: str | Path) -> list[dict[str, Any]]:
    root = Path(workspace).resolve()
    # Discover source directories containing Python modules — check top-level
    # dirs AND one level deeper (common patterns: src/, engine/, lib/).
    candidate_dirs: list[Path] = []
    try:
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir() or entry.name in AUTONOMY_IGNORE_DIRS:
                continue
            if list(entry.glob("*.py")):
                candidate_dirs.append(entry)
            # Also check one level deeper (e.g. engine/agent_engine/)
            try:
                for sub in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                    if sub.is_dir() and sub.name not in AUTONOMY_IGNORE_DIRS:
                        if list(sub.glob("*.py")):
                            candidate_dirs.append(sub)
            except OSError:
                continue
    except OSError:
        return []
    if not candidate_dirs:
        return []
    findings: list[dict[str, Any]] = []
    tests_dir = root / "tests"
    existing_tests: set[str] = set()
    if tests_dir.exists():
        existing_tests = {item.name for item in tests_dir.glob("test_*.py")}
    for src_dir in candidate_dirs:
        for module in sorted(src_dir.glob("*.py"), key=lambda item: item.name):
            if module.name == "__init__.py":
                continue
            expected = f"test_{module.stem}.py"
            if expected in existing_tests:
                continue
            findings.append(
                _base_finding(
                    category="test_coverage",
                    title="Missing focused test module",
                    severity="medium",
                    confidence=0.7,
                    impact=2.2,
                    effort=2.0,
                    source=relpath(module, root),
                    evidence=f"No tests/{expected} found for {relpath(module, root)}.",
                    recommendation="Add focused unit tests or explicitly document why coverage is exercised elsewhere.",
                    tags=["verification", "coverage_gap"],
                )
            )
    return findings[:12]


def discover_autonomous_findings(workspace: str | Path) -> list[dict[str, Any]]:
    root = Path(workspace).resolve()
    findings: list[dict[str, Any]] = []
    for path in _iter_text_files(root):
        findings.extend(_scan_file(root, path))
    findings.extend(_scan_missing_tests(root))

    deduped: dict[str, dict[str, Any]] = {}
    for finding in findings:
        deduped[finding["id"]] = finding
    return list(deduped.values())


def _rank_findings(findings: list[dict[str, Any]], memory: ACTRMemoryStore, *, now: float | None = None) -> list[AutonomyFinding]:
    ranked: list[AutonomyFinding] = []
    for finding in findings:
        query = f"{finding['category']} {finding['source']} {finding['title']} {finding['evidence']}"
        related = memory.retrieve(query, limit=3, reinforce=True, now=now)
        memory_boost = max([float(item.get("activation") or 0.0) for item in related] or [0.0]) / 5.0
        severity = SEVERITY_SCORE.get(finding["severity"], 1.0)
        priority = ((float(finding["impact"]) * severity * float(finding["confidence"])) / max(0.5, float(finding["effort"]))) + max(0.0, memory_boost)
        ranked.append(AutonomyFinding(priorityScore=round(priority, 4), memory=related, **finding))
    ranked.sort(key=lambda item: (item.priorityScore, item.impact, item.confidence), reverse=True)
    return ranked


def _remember_findings(memory: ACTRMemoryStore, findings: list[AutonomyFinding], *, now: float | None = None) -> None:
    for finding in findings:
        memory.remember(
            kind="finding",
            source=finding.source,
            tags=finding.tags,
            importance=min(1.0, finding.impact / 4.0),
            content=f"{finding.title}\n{finding.evidence}\nRecommendation: {finding.recommendation}",
            metadata={
                "findingId": finding.id,
                "category": finding.category,
                "severity": finding.severity,
                "priorityScore": finding.priorityScore,
                "autonomyLevel": "L4",
            },
            now=now,
        )


def build_long_horizon_plan(findings: list[dict[str, Any]], *, now: float | None = None) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        groups.setdefault(str(finding["category"]), []).append(finding)

    blueprints = {
        "security": {
            "title": "Security hardening initiative",
            "objective": "Reduce exploit blast radius before adding more autonomous write capability.",
            "tradeoff": "Spend time tightening risky seams now to keep future L4/L5 automation from amplifying unsafe primitives.",
            "milestones": ["Inventory unsafe primitives", "Replace or gate risky calls", "Add regression tests", "Run security review gate"],
        },
        "test_coverage": {
            "title": "Verification moat initiative",
            "objective": "Increase confidence around modules that future autonomous changes will touch repeatedly.",
            "tradeoff": "Accept short-term test-writing cost to lower review/rework cost over many future runs.",
            "milestones": ["Map uncovered modules", "Add focused tests for high-change surfaces", "Wire tests into affected-test routing"],
        },
        "maintainability": {
            "title": "Architecture simplification initiative",
            "objective": "Split large or tangled surfaces into bounded modules with explicit contracts.",
            "tradeoff": "Pay a controlled refactor cost now to preserve planning quality as workflows grow.",
            "milestones": ["Identify module seams", "Freeze behavior with tests", "Extract one boundary at a time", "Measure reduced file complexity"],
        },
        "technical_debt": {
            "title": "Debt burn-down initiative",
            "objective": "Convert ambient TODO/FIXME markers into prioritized, owned work.",
            "tradeoff": "Reserve idle cycles for small debt removal so planned work is not repeatedly slowed by stale uncertainty.",
            "milestones": ["Cluster markers by component", "Close low-effort items", "Escalate product/architecture questions", "Retire obsolete comments"],
        },
    }

    initiatives: list[dict[str, Any]] = []
    for category, category_findings in groups.items():
        blueprint = blueprints.get(
            category,
            {
                "title": f"{category.replace('_', ' ').title()} initiative",
                "objective": "Reduce recurring friction detected by autonomous discovery.",
                "tradeoff": "Trade bounded cleanup work for future planning and execution reliability.",
                "milestones": ["Triage findings", "Implement the safest high-leverage item", "Verify and document the outcome"],
            },
        )
        top = sorted(category_findings, key=lambda item: float(item.get("priorityScore") or 0.0), reverse=True)[:8]
        priority = sum(float(item.get("priorityScore") or 0.0) for item in top)
        initiatives.append(
            {
                "id": f"initiative-{category}",
                "category": category,
                "title": blueprint["title"],
                "objective": blueprint["objective"],
                "strategicTradeoff": blueprint["tradeoff"],
                "horizon": "2-6 weeks",
                "priorityScore": round(priority, 4),
                "findingIds": [item["id"] for item in top],
                "milestones": blueprint["milestones"],
                "acceptanceCriteria": [
                    "Findings are resolved, explicitly accepted, or converted into tracked work.",
                    "Regression/security tests cover the highest-risk touched paths.",
                    "The autonomy memory store records the decision so stale issues decay instead of resurfacing forever.",
                ],
            }
        )

    initiatives.sort(key=lambda item: item["priorityScore"], reverse=True)
    return {
        "autonomyLevel": "L4",
        "generatedAt": _now_iso(now),
        "summary": "Long-horizon plan generated from autonomous read-only repository discovery.",
        "initiatives": initiatives,
        "globalTradeoffs": [
            "Prioritize security and verification before increasing autonomous write scope.",
            "Prefer small reversible refactors over broad rewrites unless memory evidence shows repeated pain in the same boundary.",
            "Decay stale historical errors unless a finding is repeatedly rediscovered or retrieved.",
        ],
    }


def generate_skill_proposals(findings: list[dict[str, Any]], *, now: float | None = None) -> list[dict[str, Any]]:
    categories = {str(finding.get("category")) for finding in findings}
    proposals: list[dict[str, Any]] = []
    if "security" in categories:
        proposals.append(
            {
                "id": "skill-proposal-security-seam-scanner",
                "autonomyLevel": "L5-proposal",
                "name": "security-seam-scanner",
                "problemPattern": "Repeated unsafe primitives or policy-sensitive code paths need review before autonomous edits.",
                "proposedModel": "A deterministic static-analysis skill that maps risky calls to allowed safer replacements and required tests.",
                "inputs": ["workspace tree", "changed files", "security policy"],
                "outputs": ["risk findings", "replacement strategy", "verification commands"],
                "safetyConstraints": ["proposal-only", "no code execution", "requires evaluation gate before activation"],
                "validationPlan": ["seed with known unsafe patterns", "verify zero secret persistence", "measure false positives on existing tests"],
            }
        )
    if "test_coverage" in categories:
        proposals.append(
            {
                "id": "skill-proposal-test-gap-mapper",
                "autonomyLevel": "L5-proposal",
                "name": "test-gap-mapper",
                "problemPattern": "Modules without focused tests make long-horizon autonomous refactors brittle.",
                "proposedModel": "A coverage-intent mapper that proposes test seams from public functions, imports, and workflow roles.",
                "inputs": ["module source", "existing tests", "workflow contracts"],
                "outputs": ["test seam map", "candidate fixtures", "risk-ranked test plan"],
                "safetyConstraints": ["proposal-only", "human approval before writing generated tests"],
                "validationPlan": ["compare proposed seams against accepted tests", "score generated tests through live evaluation registry"],
            }
        )
    if "maintainability" in categories or "technical_debt" in categories:
        proposals.append(
            {
                "id": "skill-proposal-debt-memory-cartographer",
                "autonomyLevel": "L5-proposal",
                "name": "debt-memory-cartographer",
                "problemPattern": "Debt markers, large files, and recurring findings need a memory-aware map rather than one-off TODO cleanup.",
                "proposedModel": "An ACT-R-backed analyzer that clusters debt by component, tracks rehearsed pain points, and lets old resolved issues fade.",
                "inputs": ["ACT-R memory", "repository scan findings", "recent review blockers"],
                "outputs": ["debt clusters", "decay state", "initiative candidates"],
                "safetyConstraints": ["proposal-only", "no automatic refactor", "requires benchmark proof before becoming an executable skill"],
                "validationPlan": ["run on historical findings", "confirm repeated discoveries rise in priority", "confirm stale resolved errors decay"],
            }
        )
    for proposal in proposals:
        proposal["generatedAt"] = _now_iso(now)
    return proposals


def run_idle_discovery(workspace: str | Path, state_dir: str | Path, *, limit: int = 40, now: float | None = None) -> dict[str, Any]:
    workspace_path = Path(workspace).resolve()
    memory = ACTRMemoryStore(default_memory_path(state_dir))
    try:
        raw_findings = discover_autonomous_findings(workspace_path)
        ranked = _rank_findings(raw_findings, memory, now=now)
        selected = ranked[: max(1, int(limit))]
        _remember_findings(memory, selected, now=now)
        finding_dicts = [finding.to_dict() for finding in selected]
        plan = build_long_horizon_plan(finding_dicts, now=now)
        skill_proposals = generate_skill_proposals(finding_dicts, now=now)
        for proposal in skill_proposals:
            memory.remember(
                kind="skill_proposal",
                source=proposal["id"],
                tags=["l5", "proposal", "skill_discovery"],
                importance=0.7,
                content=f"{proposal['name']}: {proposal['proposedModel']}",
                metadata={"proposalId": proposal["id"], "autonomyLevel": "L5-proposal"},
                now=now,
            )
        report = {
            "ok": True,
            "schemaVersion": AUTONOMY_REPORT_VERSION,
            "generatedAt": _now_iso(now),
            "workspacePath": str(workspace_path),
            "mode": "idle_read_only",
            "autonomyLevels": ["L4", "L5-proposal"],
            "summary": {
                "findingCount": len(finding_dicts),
                "initiativeCount": len(plan["initiatives"]),
                "skillProposalCount": len(skill_proposals),
            },
            "findings": finding_dicts,
            "longHorizonPlan": plan,
            "skillProposals": skill_proposals,
            "memory": memory.stats(now=now),
            "safety": {
                "writesToWorkspace": False,
                "executesCommands": False,
                "requiresHumanApprovalBeforeImplementation": True,
            },
        }
        _write_json(_report_path(state_dir), report)
        write_debug_event(
            "autonomy.idle_discovery",
            {
                "workspacePath": str(workspace_path),
                "findingCount": len(finding_dicts),
                "initiativeCount": len(plan["initiatives"]),
                "skillProposalCount": len(skill_proposals),
            },
        )
        return report
    finally:
        memory.close()


# ── Autonomous loop: pick a single task from the report ───────────────────

# Priority order across categories for PLATFORM_MAINTENANCE fallback only —
# used when no product goal is set OR the workspace IS the agent engine itself.

# ── ANTI-HALT: patterns LLM workers/councils must never emit ───────────────────────────────────

_CLARIFICATION_PATTERNS = (
    (re.compile(r"Cần\s+làm\s+rõ", re.IGNORECASE), "vn_clarify"),
    (re.compile(r"Chọn\s+\d+\s+hoặc\s+\d+", re.IGNORECASE), "vn_choose"),
    (re.compile(r"Xác\s+nhận\s+trước", re.IGNORECASE), "vn_confirm"),
    (re.compile(r"Halt", re.IGNORECASE), "halt"),
    (re.compile(r"chờ\s+câu\s+trả\s+lời", re.IGNORECASE), "vn_wait_answer"),
    (re.compile(r"chờ\s+xác\s+nhận", re.IGNORECASE), "vn_wait_confirm"),
    (re.compile(r"Workspace\s+không\s+liên\s+quan", re.IGNORECASE), "vn_irrelevant_ws"),
    (re.compile(r"You should (not|never|avoid)\s+\w+ing\s+this\s+in", re.IGNORECASE), "en_discourage"),
    (re.compile(r"Please\s+(confirm|choose|select|pick|decide)\b", re.IGNORECASE), "en_please_decide"),
    (re.compile(r"clarifying\s+question", re.IGNORECASE), "en_clarify"),
    (re.compile(r"Before\s+I\s+(proceed|start|begin|continue|edit)", re.IGNORECASE), "en_before_gate"),
    (re.compile(r"Do you want me to", re.IGNORECASE), "en_do_you_want"),
    (re.compile(r"I (need|require) (you|the user) to", re.IGNORECASE), "en_need_user"),
    (re.compile(r"What (stack|theme|framework|library|database|backend|frontend|approach|architecture|scope|feature)", re.IGNORECASE), "en_what_choice"),
    (re.compile(r"Should I (use|pick|choose|build|create|scaffold|install|add)\b", re.IGNORECASE), "en_should_i"),
    (re.compile(r"Which (one|approach|option|way|pattern|strategy|method)", re.IGNORECASE), "en_which"),
    (re.compile(r"\?\s*$", re.MULTILINE), "question_mark_eol"),
)

_CLARIFICATION_ANSWERS = {
    "vn_clarify": "Autonomous mode: không cần làm rõ. Dùng default decision policy.",
    "vn_choose": "Autonomous mode: tự chọn option 1 làm default, record assumption.",
    "vn_confirm": "Autonomous mode: bỏ qua xác nhận. Giả sử approved.",
    "halt": "Autonomous mode: bỏ qua halt. Tiếp tục.",
    "vn_wait_answer": "Autonomous mode: không chờ. Tiến hành với default.",
    "vn_wait_confirm": "Autonomous mode: không chờ xác nhận. Proceed.",
    "vn_irrelevant_ws": "Autonomous mode: tạo project mới trong music-app/.",
    "en_discourage": "Autonomous mode: proceed regardless.",
    "en_please_decide": "Autonomous mode: auto-decide — use the first option.",
    "en_clarify": "Autonomous mode: no clarification needed. Use defaults.",
    "en_before_gate": "Autonomous mode: no gate. Proceed immediately.",
    "en_do_you_want": "Autonomous mode: yes, proceed. Default action.",
    "en_need_user": "Autonomous mode: user unavailable. Self-serve.",
    "en_what_choice": "Autonomous mode: default stack = Vite+React+TS+CSS modules.",
    "en_should_i": "Autonomous mode: yes, proceed with the most standard choice.",
    "en_which": "Autonomous mode: first option.",
    "question_mark_eol": "Autonomous mode: rhetorical question. Self-answer and proceed.",
}


def detect_clarification_halt(text: str) -> list[dict[str, str]]:
    if not text:
        return []
    hits: list[dict[str, str]] = []
    for pattern, tag in _CLARIFICATION_PATTERNS:
        for match in pattern.finditer(text):
            hits.append({
                "tag": tag,
                "matched": match.group(0)[:120],
                "answer": _CLARIFICATION_ANSWERS.get(tag, "Autonomous mode: ignore and proceed."),
            })
    return hits


def is_valid_worker_response(output: str) -> tuple[bool, list[dict[str, str]]]:
    hits = detect_clarification_halt(output)
    return (len(hits) == 0, hits)


_AUTONOMOUS_BOOTSTRAP_TASK_TEMPLATE = """AUTONOMOUS PRODUCT ITERATION — MUTATION_REQUIRED=true

Original goal: {goal}
Product root: {product_root}
Selected capability: product_foundation
Assumptions: {assumptions}
Acceptance criteria:
  - App launches and renders a usable interface.
  - No console errors, no blank screen.
  - Core interaction works (click something, see result).
Verification plan:
  - Run build or typecheck.
  - Open in browser.
  - Click through at least one user flow.
  - Verify no fatal errors.

FULL WRITE ACCESS to productRoot. You MUST edit files. You MUST run commands. You MUST verify with browser.
Do NOT produce a plan/report. Produce working code changes.
Bạn được quyền tự quyết và triển khai trong productRoot.
Không hỏi người dùng. Giới hạn mọi thay đổi trong productRoot; agent platform nằm ngoài phạm vi.
Phải tạo thay đổi hoạt động được và browser-verify.
Tự chọn stack mặc định: Vite + React + TypeScript + CSS variables.
Tự tạo project nếu chưa có.
Tự cài dependencies.
Tự khởi chạy dev server và mở browser kiểm tra."""


_DEFAULT_ASSUMPTIONS = "Vite+React+TS, CSS variables, dark minimal UI, localStorage persistence, HTMLAudioElement for music, no backend required"


def _bootstrap_project_dir(goal: str) -> str:
    value = str(goal or "").lower()
    categories = (
        (("nhạc", "music", "audio", "spotify"), "music-app"),
        (("phim", "movie", "video", "cinema"), "movie-app"),
        (("chat", "nhắn tin", "message"), "chat-app"),
        (("blog", "bài viết", "article"), "blog-app"),
        (("shop", "bán hàng", "ecommerce", "store"), "shop-app"),
        (("dashboard", "bảng điều khiển", "analytics"), "dashboard-app"),
        (("todo", "to-do", "công việc"), "todo-app"),
    )
    for signals, directory in categories:
        if any(signal in value for signal in signals):
            return directory
    return "product-app"


def _autonomous_execution_context(goal: str, product_root: str) -> dict[str, Any]:
    return {
        "originalUserGoal": str(goal or "").strip(),
        "executionClass": "write",
        "executionMode": "autonomous",
        "permissionProfile": "workspace-write",
        "requiresMutation": True,
        "reportOnly": False,
        "autoResolveTechnicalChoices": True,
        "productRoot": product_root,
    }


def build_autonomous_bootstrap(goal: str, workspace_path: str | None = None) -> dict[str, Any]:
    """Deterministic fallback task when council/worker produce a clarification halt.
    Returns a self-contained task dict suitable for direct consumption by the worker."""
    ws = workspace_path or ""
    from pathlib import Path
    project_dir = _bootstrap_project_dir(goal)
    product_root = str(Path(ws).resolve() / project_dir) if ws else project_dir
    return {
        "id": f"auto-bootstrap-{int(time.time())}",
        "kind": "autonomous_bootstrap",
        "category": "PRODUCT_FOUNDATION",
        "title": "Autonomous product foundation bootstrap",
        "source": "anti_halt_controller",
        "priorityScore": 1.0,
        "alignmentScore": 1.0,
        "task": _AUTONOMOUS_BOOTSTRAP_TASK_TEMPLATE.format(
            goal=goal,
            product_root=product_root,
            assumptions=_DEFAULT_ASSUMPTIONS,
        ),
        "productRoot": product_root,
        "originalUserGoal": goal,
        "executionContext": _autonomous_execution_context(goal, product_root),
        "assumptions": _DEFAULT_ASSUMPTIONS,
    }


_CATEGORY_ORDER = {
    "security": 0,
    "test_coverage": 1,
    "maintainability": 2,
    "technical_debt": 3,
}


def _format_finding_task(finding: dict[str, Any]) -> str:
    title = finding.get("title", "")
    source = finding.get("source", "")
    evidence = (finding.get("evidence") or "").strip()
    rec = (finding.get("recommendation") or "").strip()
    cat = finding.get("category", "")
    lines = [
        f"Xử lý finding tự động phát hiện ({cat}): {title}",
        f"Vị trí: {source}",
    ]
    if evidence:
        lines.append(f"Bằng chứng: {evidence}")
    if rec:
        lines.append(f"Khuyến nghị: {rec}")
    lines.append(
        "Hãy đề xuất bản vá nhỏ nhất, có chủ đích, vẫn giữ tests pass; "
        "nếu cần đổi public API hãy nói rõ tradeoff."
    )
    return "\n".join(lines)


def _compute_goal_alignment(goal: str, finding: dict[str, Any]) -> float:
    """Crude but fast keyword-overlap similarity between user goal and finding.
    Returns 0.0–1.0. Embedding-based would be better but requires an LLM call;
    this runs locally in <1ms as a first-pass filter."""
    goal_words = set(_tokenize(goal))
    finding_text = " ".join(
        str(finding.get(key, ""))
        for key in ("title", "source", "evidence", "recommendation")
    ).lower()
    finding_words = set(_tokenize(finding_text))
    if not goal_words or not finding_words:
        return 0.0
    overlap = goal_words & finding_words
    return min(1.0, len(overlap) / max(1, len(goal_words)) * 0.7 + len(overlap) / max(1, len(finding_words)) * 0.3)


def _tokenize(text: str) -> set[str]:
    """Simple word tokenizer for Vietnamese + English text."""
    import re as _re
    return set(
        w for w in _re.split(r"[^\w-ɏ]+", text.lower())
        if len(w) > 1 and w not in STOP_WORDS
    )


def _detect_agent_engine_workspace(report: dict[str, Any] | None) -> bool:
    """Returns True when the workspace IS the fractal-agent-system itself."""
    if not report:
        return False
    wp = str(report.get("workspacePath") or "").lower()
    return any(
        marker in wp
        for marker in ("fractal-agent", "agent_engine", "agent-engine")
    )


STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "and", "but", "or",
    "nor", "not", "so", "yet", "both", "either", "neither", "each",
    "every", "all", "any", "few", "more", "most", "other", "some", "such",
    "only", "own", "same", "than", "too", "very", "just", "about", "also",
    "và", "của", "một", "tôi", "cho", "có", "không", "được", "này",
    "những", "các", "để", "là", "trong", "khi", "hoặc",
}


def select_next_task(
    report: dict[str, Any] | None,
    completed_ids: set[str] | None = None,
    *,
    idea_cursor: int = 0,
    product_goal: str | None = None,
    iteration: int = 0,
    iteration_history: list[dict[str, Any]] | None = None,
    decompose_subtask: Any = None,
    council_round: Any = None,
    session_id: str | None = None,
    workspace_path: str | None = None,
    anti_halt_retries: int = 2,
) -> dict[str, Any] | None:
    """Pick the single next task.

    Order whenever ``product_goal`` is non-empty (including when the opened
    workspace also contains the agent platform):
      0. USER GOAL (iter 0) — feed user's literal prompt as the first task.
      1. DYNAMIC IDEA COUNCIL — invoke multi-agent council via ``council_round``
         which must produce a winning proposal (>= 75). No hard-coded backlog.
         If council output contains clarification halt patterns, mark
         `invalid_clarification_response`, retry council up to anti_halt_retries
         times, and if all fail, fall back to deterministic autonomous bootstrap.
      2. LLM decompose (legacy fallback) — only if ``council_round`` is None.

    When ``product_goal`` is empty OR workspace IS the agent engine: fall back
    to the read-only ``findings`` list ranked by category. This is the
    PLATFORM_MAINTENANCE lane and is the ONLY place hard-coded prioritization
    survives, because there is no user product to align against.
    """
    completed_ids = completed_ids or set()
    findings = list((report or {}).get("findings") or [])
    goal = (product_goal or "").strip()
    # A product goal always owns the lane, even when the opened workspace is
    # the agent platform. In that case the worker creates/uses a productRoot
    # subdirectory instead of silently switching to platform maintenance.
    product_goal_active = bool(goal)

    if product_goal_active:
        if iteration <= 0:
            user_id = "user-goal-iter0"
            if user_id not in completed_ids:
                return {
                    "id": user_id,
                    "kind": "user_goal",
                    "category": "USER_REQUEST",
                    "title": goal[:80],
                    "source": "user_prompt",
                    "priorityScore": 1.0,
                    "alignmentScore": 1.0,
                    "task": goal,
                    "originalUserGoal": goal,
                    "executionContext": _autonomous_execution_context(goal, ""),
                }
        # Iter >= 1: dynamic council generates the next idea from real
        # repository evidence. NO hard-coded backlog rotation.
        if callable(council_round):
            council_result = None
            halt_hits: list[dict[str, str]] = []
            for attempt in range(max(1, anti_halt_retries + 1)):
                try:
                    council_result = council_round(
                        goal=goal,
                        iteration=iteration,
                        iteration_history=iteration_history or [],
                        session_id=session_id or "",
                        workspace_path=workspace_path or "",
                        # On retry, pass the anti-halt instruction so the council
                        # can self-correct and generate actual proposals.
                        anti_halt_instruction=(
                            "PREVIOUS ATTEMPT RETURNED CLARIFICATION HALT. "
                            "This is AUTONOMOUS mode — you MUST NOT ask questions or return 'Cần làm rõ' / 'Halt' / 'chờ câu trả lời'. "
                            "Use the Default Decision Policy: pick the first reasonable option, "
                            "record the assumption, and produce actionable proposals."
                            if attempt > 0 else ""
                        ),
                    )
                except Exception as exc:
                    write_debug_event("autonomy.council_error", {"error": str(exc), "attempt": attempt})
                    council_result = None
                    continue
                if isinstance(council_result, dict):
                    winner = council_result.get("winner")
                    if winner:
                        raw = winner.get("raw") or {}
                        formatted = str(raw.get("formattedTask") or "")
                        ok, hits = is_valid_worker_response(formatted)
                        if not ok:
                            halt_hits = hits
                            write_debug_event(
                                "autonomy.anti_halt.council_winner_retry",
                                {"attempt": attempt, "hits": hits, "winnerTitle": raw.get("title")},
                            )
                            continue
                        fp = winner.get("fingerprint") or ""
                        if fp not in completed_ids:
                            return {
                                "id": fp,
                                "kind": "council_idea",
                                "category": raw.get("productCapability") or "PRODUCT_ITERATION",
                                "title": str(raw.get("title") or goal[:80]),
                                "source": "idea_council",
                                "priorityScore": winner.get("score"),
                                "scoreBreakdown": winner.get("scoreBreakdown"),
                                "alignmentScore": min(1.0, (winner.get("score") or 0) / 100.0),
                                "council": council_result,
                                "task": formatted,
                                "originalUserGoal": goal,
                                "productRoot": str(raw.get("productRoot") or ""),
                                "executionContext": _autonomous_execution_context(
                                    goal,
                                    str(raw.get("productRoot") or workspace_path or ""),
                                ),
                            }
                # Check council proposals for clarification halt patterns.
                text_blob = json.dumps(council_result, ensure_ascii=False) if council_result else ""
                ok, hits = is_valid_worker_response(text_blob)
                if not ok:
                    halt_hits = hits
                    write_debug_event(
                        "autonomy.anti_halt.council_blob_retry",
                        {"attempt": attempt, "hits": hits},
                    )
                    continue
                # No winner but no halt either — legitimate zero-proposal round.
                return None

            # All attempts exhausted → deterministic bootstrap.
            write_debug_event(
                "autonomy.anti_halt.bootstrap",
                {"goal": goal, "totalAttempts": anti_halt_retries + 1, "lastHits": halt_hits},
            )
            bootstrap = build_autonomous_bootstrap(goal, workspace_path)
            if bootstrap["id"] not in completed_ids:
                return bootstrap
            return None

        if callable(decompose_subtask):
            try:
                sub = decompose_subtask(goal, iteration_history or [])
            except Exception as exc:
                write_debug_event("autonomy.decompose_error", {"error": str(exc)})
                sub = None
            if sub and isinstance(sub, dict) and sub.get("task"):
                sub_id = str(sub.get("id") or f"user-goal-iter{iteration}")
                if sub_id not in completed_ids:
                    return {
                        "id": sub_id,
                        "kind": "user_goal_subtask",
                        "category": "USER_REQUEST",
                        "title": str(sub.get("title") or goal[:80]),
                        "source": "llm_decompose",
                        "priorityScore": 0.95,
                        "alignmentScore": 1.0,
                        "task": str(sub["task"]),
                        "originalUserGoal": goal,
                        "executionContext": _autonomous_execution_context(goal, ""),
                    }
        return None

    # PLATFORM_MAINTENANCE — workspace is the agent engine itself OR no goal.
    def _sort_key(item: dict[str, Any]) -> tuple[int, float]:
        cat = str(item.get("category") or "")
        cat_rank = _CATEGORY_ORDER.get(cat, 10)
        return (cat_rank, -float(item.get("priorityScore") or 0.0))

    for finding in sorted(findings, key=_sort_key):
        fid = str(finding.get("id") or "")
        if not fid or fid in completed_ids:
            continue
        return {
            "id": fid,
            "kind": "finding",
            "category": finding.get("category"),
            "title": finding.get("title"),
            "source": finding.get("source"),
            "priorityScore": finding.get("priorityScore"),
            "task": _format_finding_task(finding),
        }
    return None


def autonomy_status(state_dir: str | Path) -> dict[str, Any]:
    memory = ACTRMemoryStore(default_memory_path(state_dir))
    try:
        report_path = _report_path(state_dir)
        return {
            "ok": True,
            "reportPath": str(report_path),
            "memory": memory.stats(),
            "lastReport": _safe_read_json(report_path),
        }
    finally:
        memory.close()

