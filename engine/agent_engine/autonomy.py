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

# Priority order across categories. Critical/security blockers first, then
# verification gaps, then maintainability, then debt cleanup, then enhancement
# ideas from the rotating pool.
_CATEGORY_ORDER = {
    "security": 0,
    "test_coverage": 1,
    "maintainability": 2,
    "technical_debt": 3,
}

# Rotating enhancement-idea pool. Used when there are no high-priority
# findings left, so the loop never starves. Each idea is a small, scoped,
# safe-by-default improvement the pipeline can attempt end-to-end.
_ENHANCEMENT_IDEAS: list[dict[str, Any]] = [
    {
        "id": "idea-ui-keyboard-shortcuts",
        "category": "enhancement_ui",
        "title": "Bổ sung phím tắt cho FlowView",
        "task": (
            "Thêm phím tắt cho FlowView trong tab Luồng Agent: phím mũi tên để di chuyển node "
            "được chọn theo upstream/downstream, phím Esc để bỏ chọn (về Global Live Activity), "
            "phím số 1-7 để chuyển nhanh giữa các subtab Overview/Activity/I/O/Messages/Tools/Health/Raw. "
            "Cập nhật hint hiển thị các phím trong panel."
        ),
    },
    {
        "id": "idea-ui-event-filter",
        "category": "enhancement_ui",
        "title": "Filter sự kiện theo eventType và status",
        "task": (
            "Thêm filter pills phía trên subtab Activity của Agent Inspector: cho phép lọc theo "
            "eventType (node_started/node_completed/tool_call/llm_call/warning) và status. "
            "Khi bật filter chỉ render các event matching, vẫn giữ cap 200/node."
        ),
    },
    {
        "id": "idea-perf-render-throttle",
        "category": "enhancement_perf",
        "title": "Throttle re-render Global panel",
        "task": (
            "Throttle _renderInactivePanel xuống ~250ms để tránh jank khi backend emit burst progress "
            "events (>5 events/giây). Dùng requestAnimationFrame hoặc một guard timer; vẫn đảm bảo "
            "lần render cuối phản ánh state mới nhất."
        ),
    },
    {
        "id": "idea-test-replay-roundtrip",
        "category": "enhancement_tests",
        "title": "Test replay round-trip cho event schema mới",
        "task": (
            "Viết test JS hoặc Python kiểm tra round-trip của progress event qua persistence: "
            "tạo event với eventId/parentEventId/agentRole/durationMs/tokenUsage, qua "
            "backendService normalization và sessionStore, replay lại flowView.setEventHistory "
            "phải tái dựng đầy đủ các field."
        ),
    },
    {
        "id": "idea-obs-bottleneck-export",
        "category": "enhancement_observability",
        "title": "Export bottleneck report ra JSON",
        "task": (
            "Thêm nút Export ở Global Live Activity panel: xuất ra JSON danh sách top 5 node có "
            "durationMs cao nhất trong session, kèm retryCount và tokenUsage. Lưu xuống "
            ".agent-state/reports/bottleneck-<timestamp>.json."
        ),
    },
    {
        "id": "idea-ui-search-nodes",
        "category": "enhancement_ui",
        "title": "Ô search node trong FlowView",
        "task": (
            "Thêm input search nhỏ ở header FlowView; gõ vào sẽ highlight các node có id/label/role "
            "matching và dim các node còn lại; Enter sẽ chọn node đầu tiên match."
        ),
    },
    {
        "id": "idea-doctor-history",
        "category": "enhancement_doctor",
        "title": "Lưu lịch sử Project Doctor scan",
        "task": (
            "Lưu mỗi lần Project Doctor scan vào .agent-state/doctor-history.jsonl, "
            "thêm endpoint GET /v1/doctor/history trả về 20 lần scan gần nhất, hiển thị trong "
            "dashboard tab Doctor (nếu chưa có thì render mới một panel nhỏ)."
        ),
    },
    {
        "id": "idea-flow-mini-timeline",
        "category": "enhancement_ui",
        "title": "Mini timeline ở dưới FlowView",
        "task": (
            "Thêm một mini timeline strip ở dưới flow canvas hiển thị các event lifecycle "
            "(node_started/node_completed/node_error) theo trục thời gian; click một marker "
            "để select node và mở subtab Activity tại event đó."
        ),
    },
]


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


def _build_product_enhancement_pool(
    goal: str,
    report: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Generate 6–8 product-aligned task templates from the user's goal.
    Uses static templates with keyword substitution when the goal matches
    common product categories (movie/streaming, e-commerce, dashboard, etc.),
    falling back to a generic capability-builder set."""
    goal_lower = goal.lower()
    workspace = str((report or {}).get("workspacePath") or "")
    wp = workspace.replace("\\", "/").lower()

    # Detect product type
    product_type = _detect_product_type(goal_lower, wp)
    if product_type:
        return _build_typed_pool(goal, product_type, report)
    return _build_generic_pool(goal, report)


def _detect_product_type(goal: str, wp: str) -> str | None:
    signals = {
        "streaming_movie": ["phim", "xem phim", "movie", "streaming", "video", "netflix", "flix", "stream", "playlist", "cinema"],
        "ecommerce": ["shop", "bán hàng", "mua", "cart", "order", "checkout", "product"],
        "dashboard": ["dashboard", "bảng điều khiển", "analytics", "chart", "biểu đồ", "report", "báo cáo"],
        "blog": ["blog", "post", "article", "bài viết", "cms", "content"],
        "game": ["game", "trò chơi", "play", "score", "level"],
        "chat": ["chat", "message", "messenger", "nhắn tin", "realtime"],
    }
    for ptype, words in signals.items():
        if any(w in goal or w in wp for w in words):
            return ptype
    return None


def _build_typed_pool(
    goal: str,
    product_type: str,
    report: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return capability-builder tasks specific to the detected product type."""
    templates: dict[str, list[dict[str, Any]]] = {
        "streaming_movie": [
            {"id": "prod-home", "title": "Trang chủ/catalog phim", "task": "Xây dựng hoặc cải thiện trang chủ hiển thị danh sách phim: carousel phim nổi bật, grid phim mới nhất, lọc theo thể loại. Kiểm tra responsive và loading/error states.", "alignmentScore": 0.95},
            {"id": "prod-search", "title": "Tìm kiếm & khám phá phim", "task": "Thêm chức năng tìm kiếm phim theo từ khóa (tên phim, diễn viên, đạo diễn). Debounce input, hiển thị results dropdown, empty state khi không tìm thấy.", "alignmentScore": 0.92},
            {"id": "prod-detail", "title": "Trang chi tiết phim", "task": "Xây dựng trang chi tiết phim: poster, tiêu đề, năm, rating IMDb, mô tả, danh sách diễn viên, phim liên quan. Layout responsive với video player embed.", "alignmentScore": 0.94},
            {"id": "prod-player", "title": "Video player + controls", "task": "Cải thiện video player: play/pause, seek, volume, fullscreen, quality selector, loading spinner, error fallback (video không khả dụng). Kiểm tra cross-browser.", "alignmentScore": 0.90},
            {"id": "prod-watchlist", "title": "Watchlist / danh sách yêu thích", "task": "Thêm nút 'Yêu thích'/'Xem sau' vào mỗi movie card. Lưu danh sách vào localStorage/IndexedDB. Hiển thị trang Watchlist riêng, hỗ trợ sort theo ngày thêm và tên.", "alignmentScore": 0.91},
            {"id": "prod-categories", "title": "Thể loại và lọc phim", "task": "Thêm navigation theo thể loại (Hành động, Hài, Kinh dị, ...). Filter bar trên catalog: genre, year range, rating min. URL-driven filter state (query params).", "alignmentScore": 0.88},
            {"id": "prod-continue", "title": "Tiếp tục xem / lịch sử", "task": "Lưu tiến độ xem (phút:giây) vào localStorage. Hiển thị 'Tiếp tục xem' section trên trang chủ. Khi quay lại phim → resume từ vị trí đã lưu.", "alignmentScore": 0.86},
            {"id": "prod-test", "title": "Tests cho các component hiện có", "task": "Viết unit test và integration test cho các component đã có (movie card, player, search, watchlist). Target 70%+ coverage. Dùng Vitest/Jest + Testing Library.", "alignmentScore": 0.82},
        ],
        "ecommerce": [
            {"id": "prod-catalog", "title": "Product catalog grid", "task": "Xây dựng grid sản phẩm với ảnh, giá, rating. Hỗ trợ sort (giá, tên, mới nhất) và filter (category, price range). Responsive 2/3/4 columns.", "alignmentScore": 0.94},
            {"id": "prod-cart", "title": "Shopping cart + checkout flow", "task": "Thêm giỏ hàng (add/remove/update quantity). Tính tổng tiền. Checkout form với validation. Lưu cart state vào localStorage.", "alignmentScore": 0.92},
        ],
        "dashboard": [
            {"id": "prod-charts", "title": "Data visualization charts", "task": "Thêm chart components (line, bar, pie) hiển thị dữ liệu. Responsive resize. Loading skeleton. Empty state khi chưa có dữ liệu.", "alignmentScore": 0.93},
            {"id": "prod-filters", "title": "Filter + date-range controls", "task": "Thêm filter controls: date-range picker, dropdown filters, search input. URL-driven state. Debounced fetch.", "alignmentScore": 0.90},
        ],
        "blog": [
            {"id": "prod-post", "title": "Blog post page with markdown", "task": "Xây dựng trang bài viết: markdown render, syntax highlight, table of contents, breadcrumb. Responsive typography.", "alignmentScore": 0.93},
            {"id": "prod-list", "title": "Blog post list with pagination", "task": "Danh sách bài viết: card grid, pagination/infinite scroll, category filter, search. Loading skeleton.", "alignmentScore": 0.91},
        ],
    }
    defaults = [
        {"id": "prod-feature", "title": "Core user feature from goal", "task": f"Từ mục tiêu sản phẩm '{goal[:120]}', phân tích code hiện tại và xây dựng hoặc cải thiện một tính năng cốt lõi mà người dùng sẽ tương tác trực tiếp. Ưu tiên tính năng có UI visible. Viết test và verify trong browser.", "alignmentScore": 0.80},
        {"id": "prod-fix", "title": "Fix regression từ lần thay đổi trước", "task": f"Kiểm tra code đã sửa trong session này liên quan đến '{goal[:100]}'. Chạy test suite, phát hiện test fail hoặc regression, fix từng cái một. Không thêm tính năng mới.", "alignmentScore": 0.75},
    ]
    pool = templates.get(product_type, defaults)
    return pool if len(pool) >= 3 else pool + defaults


def _build_generic_pool(
    goal: str,
    report: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Capability-builder templates for any product type."""
    return [
        {"id": "prod-ui-core", "title": "Xây dựng UI chính của sản phẩm", "task": f"Từ mục tiêu '{goal[:120]}', phân tích code hiện tại. Xác định UI chính mà người dùng sẽ thấy đầu tiên. Xây dựng hoặc cải thiện UI đó: layout, component, responsive. Viết test verify.", "alignmentScore": 0.85},
        {"id": "prod-feature-gap", "title": "Lấp khoảng trống tính năng", "task": f"Khảo sát code hiện tại trong workspace. So sánh với mục tiêu '{goal[:100]}'. Tìm một tính năng còn thiếu hoặc chưa hoàn thiện. Implement nó. Test.", "alignmentScore": 0.82},
        {"id": "prod-bug-fix", "title": "Sửa lỗi hiển thị / UX", "task": f"Chạy ứng dụng trong browser. Ghi nhận mọi lỗi hiển thị, broken link, layout xấu, console error trong workspace '{goal[:80]}'. Fix từng cái. Verify sau khi fix.", "alignmentScore": 0.78},
        {"id": "prod-test-gap", "title": "Test gap coverage", "task": "Kiểm tra test coverage của các component chính. Viết test cho component chưa có test. Target mỗi component có ít nhất 1 unit test + 1 interaction test.", "alignmentScore": 0.76},
        {"id": "prod-perf", "title": "Performance & loading optimization", "task": "Kiểm tra loading time, image optimization, code splitting. Thêm loading skeletons, lazy load images, optimize bundle. Đo Lighthouse score trước và sau.", "alignmentScore": 0.72},
        {"id": "prod-a11y", "title": "Accessibility audit", "task": "Kiểm tra keyboard navigation, screen reader, color contrast, focus management. Sửa các vấn đề accessibility cơ bản (alt text, aria labels, focus order).", "alignmentScore": 0.70},
    ]


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
) -> dict[str, Any] | None:
    """Pick the single highest-priority task aligned with product_goal.

    Order:
      1. Product-aligned findings (workspace scan filtered by goal similarity).
      2. Product-aligned enhancement ideas (template-based for detected product type).
      3. PLATFORM_MAINTENANCE pool — only when product_goal is empty or
         the workspace IS the agent engine itself.
    """
    completed_ids = completed_ids or set()
    findings = list((report or {}).get("findings") or [])
    goal = (product_goal or "").strip()
    workspace_is_agent_engine = _detect_agent_engine_workspace(report)

    def _sort_key(item: dict[str, Any]) -> tuple[int, float]:
        cat = str(item.get("category") or "")
        cat_rank = _CATEGORY_ORDER.get(cat, 10)
        return (cat_rank, -float(item.get("priorityScore") or 0.0))

    # Priority 1: product-aligned workspace findings
    if goal:
        for finding in sorted(findings, key=_sort_key):
            fid = str(finding.get("id") or "")
            if not fid or fid in completed_ids:
                continue
            alignment = _compute_goal_alignment(goal, finding)
            if alignment >= 0.70:
                return {
                    "id": fid,
                    "kind": "product_finding",
                    "category": finding.get("category"),
                    "title": finding.get("title"),
                    "source": finding.get("source"),
                    "priorityScore": finding.get("priorityScore"),
                    "alignmentScore": round(alignment, 3),
                    "task": _format_finding_task(finding),
                }

    # Priority 2: product-aligned enhancement ideas
    if goal:
        ideas = _build_product_enhancement_pool(goal, report)
        pool_size = len(ideas)
        if pool_size > 0:
            for offset in range(pool_size):
                idea = ideas[(idea_cursor + offset) % pool_size]
                if idea["id"] in completed_ids:
                    continue
                return {
                    **idea,
                    "kind": "product_enhancement",
                    "alignmentScore": round(float(idea.get("alignmentScore") or 0.95), 3),
                }

    # Priority 3: PLATFORM_MAINTENANCE — only when no product_goal or the
    # workspace IS the agent engine itself
    if workspace_is_agent_engine or not goal:
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
        pool_size = len(_ENHANCEMENT_IDEAS)
        if pool_size > 0:
            for offset in range(pool_size):
                idea = _ENHANCEMENT_IDEAS[(idea_cursor + offset) % pool_size]
                if idea["id"] in completed_ids:
                    continue
                return {
                    "id": idea["id"],
                    "kind": "enhancement_idea",
                    "category": idea["category"],
                    "title": idea["title"],
                    "source": "autonomy.enhancement_pool",
                    "priorityScore": None,
                    "task": idea["task"],
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

