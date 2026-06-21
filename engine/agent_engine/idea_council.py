"""Idea Council — dynamic multi-agent product idea generator.

Replaces the static `_ENHANCEMENT_IDEAS` / `_build_typed_pool` hard-coded backlog
with a runtime council of independent LLM agents (Product, UX, Architect, QA,
Security/Performance) that propose structured ideas from real repository
evidence; a Critic rejects malformed/duplicate/off-goal ideas; an Arbiter scores
and selects one idea per iteration.

The council is invoked per Auto Loop iteration when a `productGoal` is present.
Every proposal must cite repositoryEvidence or browserEvidence (except the very
first scaffold iteration). Ideas not scoring >= MIN_ARBITER_SCORE are rejected;
the loop then either triggers a fresh capability scan or transitions to Final
Audit — it never falls back to a hard-coded pool.

Memory is namespaced by (goal, session, workspace) so switching projects starts
a fresh deduplication scope.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from .debug_log import write_debug_event
from .workspace import IGNORED_DIRS, TEXT_EXTENSIONS, relpath


MIN_ARBITER_SCORE = 75
MAX_EVIDENCE_FILES = 60
MAX_FILE_BYTES = 80_000
PROPOSER_AGENTS: tuple[str, ...] = ("product", "ux", "frontend", "architect", "qa", "security_performance")


# ─────────────────────────────────────────────────────────────────────────────
# Capability scanner — builds a dynamic capability map from the actual workspace
# ─────────────────────────────────────────────────────────────────────────────

_ROUTE_PATTERNS = (
    re.compile(r"<Route\s+[^>]*path=['\"]([^'\"]+)['\"]", re.IGNORECASE),
    re.compile(r"router\.(get|post|put|patch|delete)\(['\"]([^'\"]+)['\"]", re.IGNORECASE),
    re.compile(r"@app\.(get|post|put|patch|delete)\(['\"]([^'\"]+)['\"]", re.IGNORECASE),
    re.compile(r"app\.(get|post|put|patch|delete)\(['\"]([^'\"]+)['\"]", re.IGNORECASE),
)
_COMPONENT_HINTS = re.compile(
    r"\b(export\s+(default\s+)?(?:function|class|const)\s+([A-Z][A-Za-z0-9_]+)"
    r"|function\s+([A-Z][A-Za-z0-9_]+)\s*\([^)]*\)\s*\{[^}]*return\s*[<(]"
    r"|const\s+([A-Z][A-Za-z0-9_]+)\s*=\s*\([^)]*\)\s*=>\s*[<(])"
)
_AUDIO_HINTS = re.compile(r"<audio|HTMLAudioElement|new\s+Audio\(|\.play\(\)|\.pause\(\)", re.IGNORECASE)
_VIDEO_HINTS = re.compile(r"<video|HTMLVideoElement", re.IGNORECASE)
_STATE_HINTS = re.compile(r"\b(useState|useReducer|createStore|writable\(|reactive\(|atom\(|signal\()", re.IGNORECASE)
_PERSIST_HINTS = re.compile(r"\b(localStorage|sessionStorage|indexedDB|sqlite|prisma|mongoose)\b", re.IGNORECASE)
_FETCH_HINTS = re.compile(r"\b(fetch\(|axios\.|XMLHttpRequest|api\.|httpx\.|requests\.)", re.IGNORECASE)


@dataclass
class CapabilityMapEntry:
    name: str
    status: str            # missing | partial | implemented_unverified | verified | broken | blocked
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""


@dataclass
class CapabilityMap:
    productRoot: str
    routes: list[dict[str, Any]] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)
    apiEndpoints: list[dict[str, Any]] = field(default_factory=list)
    stateStores: list[dict[str, Any]] = field(default_factory=list)
    persistence: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    capabilities: list[CapabilityMapEntry] = field(default_factory=list)
    rawFiles: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["capabilities"] = [asdict(c) for c in self.capabilities]
        return d


def _resolve_product_root(workspace: Path, goal: str) -> Path:
    """Pick the most-likely product folder: prefer a sibling that looks like an
    app over the workspace itself (which may be the agent platform)."""
    try:
        if any(workspace.glob("package.json")) or any(workspace.glob("index.html")):
            return workspace
        for child in sorted(workspace.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name in IGNORED_DIRS:
                continue
            if (child / "package.json").exists() or (child / "index.html").exists() or (child / "pyproject.toml").exists():
                return child
    except OSError:
        pass
    return workspace


def _iter_files(root: Path, limit: int = MAX_EVIDENCE_FILES) -> list[Path]:
    out: list[Path] = []

    def walk(current: Path, depth: int) -> None:
        if len(out) >= limit or depth > 6:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            if len(out) >= limit:
                return
            if entry.is_dir():
                if entry.name not in IGNORED_DIRS and entry.name not in {".agent-state", "__pycache__", "node_modules"}:
                    walk(entry, depth + 1)
                continue
            if entry.is_file() and entry.suffix.lower() in TEXT_EXTENSIONS:
                try:
                    if entry.stat().st_size <= MAX_FILE_BYTES:
                        out.append(entry)
                except OSError:
                    continue

    walk(root, 0)
    return out


def scan_capabilities(workspace: str | Path, goal: str) -> CapabilityMap:
    """Walk the workspace, build a structural capability map purely from what's
    on disk. No keyword whitelist of product types — the map describes the code
    as-is; downstream agents reason about gaps against the goal."""
    root = Path(workspace).resolve()
    product_root = _resolve_product_root(root, goal)
    files = _iter_files(product_root)
    cap = CapabilityMap(productRoot=str(product_root))
    cap.rawFiles = len(files)

    routes_seen: set[str] = set()
    components_seen: set[str] = set()
    api_seen: set[str] = set()
    has_audio = False
    has_video = False
    has_state = False
    has_persist = False
    has_fetch = False
    test_files: list[str] = []

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = relpath(path, product_root)
        for pattern in _ROUTE_PATTERNS:
            for match in pattern.finditer(text):
                groups = match.groups()
                p = groups[-1] if groups else None
                if not p or not isinstance(p, str):
                    continue
                if not p.startswith("/") and len(p) > 80:
                    continue
                key = f"{p}@{rel}"
                if key in routes_seen:
                    continue
                routes_seen.add(key)
                target = cap.apiEndpoints if any(verb in pattern.pattern.lower() for verb in ("get", "post")) and "Route" not in pattern.pattern else cap.routes
                if target is cap.apiEndpoints:
                    api_seen.add(p)
                target.append({"path": p, "file": rel})
        for match in _COMPONENT_HINTS.finditer(text):
            name = next((g for g in match.groups() if g and g[:1].isupper()), None)
            if not name or name in components_seen:
                continue
            if name in {"Object", "Array", "String", "Number", "React", "Component", "Promise"}:
                continue
            components_seen.add(name)
            cap.components.append({"name": name, "file": rel})
        if _AUDIO_HINTS.search(text):
            has_audio = True
        if _VIDEO_HINTS.search(text):
            has_video = True
        if _STATE_HINTS.search(text):
            has_state = True
        if _PERSIST_HINTS.search(text):
            has_persist = True
        if _FETCH_HINTS.search(text):
            has_fetch = True
        if path.name.startswith("test_") or "/__tests__/" in str(path).replace("\\", "/") or path.suffix == ".test.js" or path.suffix == ".spec.ts":
            test_files.append(rel)

    cap.tests = [{"file": t} for t in sorted(test_files)[:40]]
    if has_state:
        cap.stateStores.append({"kind": "in_memory_state"})
    if has_persist:
        cap.persistence.append({"kind": "browser_storage_or_db"})
    if has_fetch:
        cap.apiEndpoints.append({"kind": "http_client_present", "path": "_client_"})

    has_routes = bool(cap.routes)
    has_components = bool(cap.components)
    cap.capabilities = [
        CapabilityMapEntry(
            name="ui_entrypoint",
            status="implemented_unverified" if has_components or has_routes else "missing",
            evidence=[{"file": c["file"]} for c in cap.components[:3]] or [{"file": r["file"]} for r in cap.routes[:3]],
        ),
        CapabilityMapEntry(
            name="routing",
            status="implemented_unverified" if has_routes else "missing",
            evidence=[{"path": r["path"], "file": r["file"]} for r in cap.routes[:5]],
        ),
        CapabilityMapEntry(
            name="state_management",
            status="implemented_unverified" if has_state else "missing",
        ),
        CapabilityMapEntry(
            name="persistence",
            status="implemented_unverified" if has_persist else "missing",
        ),
        CapabilityMapEntry(
            name="data_fetching",
            status="implemented_unverified" if has_fetch else "missing",
        ),
        CapabilityMapEntry(
            name="audio_playback",
            status="implemented_unverified" if has_audio else "missing",
        ),
        CapabilityMapEntry(
            name="video_playback",
            status="implemented_unverified" if has_video else "missing",
        ),
        CapabilityMapEntry(
            name="automated_tests",
            status="implemented_unverified" if test_files else "missing",
            evidence=[{"file": t} for t in test_files[:3]],
        ),
    ]
    return cap


# ─────────────────────────────────────────────────────────────────────────────
# Council protocol — proposal schema + JSON-mode multi-agent invocation
# ─────────────────────────────────────────────────────────────────────────────

PROPOSAL_KEYS = (
    "title", "proposer", "productCapability", "userProblem", "goalConnection",
    "repositoryEvidence", "browserEvidence", "expectedUserValue",
    "implementationScope", "acceptanceCriteria", "verificationPlan",
    "risks", "dependencies", "estimatedEffort",
    "goalRelevance", "userValue", "evidenceStrength",
    "productCompleteness", "feasibility",
)


@dataclass
class Proposal:
    raw: dict[str, Any]
    proposer: str
    fingerprint: str
    rejected: bool = False
    rejectionReason: str = ""
    score: int = 0
    scoreBreakdown: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "proposer": self.proposer,
            "fingerprint": self.fingerprint,
            "rejected": self.rejected,
            "rejectionReason": self.rejectionReason,
            "score": self.score,
            "scoreBreakdown": self.scoreBreakdown,
        }


def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def fingerprint_proposal(goal: str, raw: dict[str, Any]) -> str:
    parts = [
        _normalize_title(goal),
        _normalize_title(raw.get("productCapability") or ""),
        _normalize_title(raw.get("title") or ""),
        _normalize_title(",".join(sorted(str(s) for s in raw.get("implementationScope") or []))),
    ]
    return hashlib.sha1("".join(parts).encode("utf-8", errors="replace")).hexdigest()[:18]


def _agent_brief(agent: str) -> str:
    return {
        "product": "Product Agent: propose up to 5 ideas that directly advance the CORE user journey of the goal. No platform/agent-engine ideas. For a music app: catalog, player, search, playlist, favorites, continue-listening.",
        "ux": "UX Agent: propose up to 3 ideas from concrete usability gaps — missing states (loading/empty/error), responsive layout, visual hierarchy, keyboard nav. Cite which file/component shows the gap.",
        "frontend": "Frontend Agent: propose up to 3 ideas about component architecture and state management — data flow between key components, state normalization, caching strategy. Only if they unblock product features.",
        "architect": "Architect Agent: propose AT MOST 3 ideas about data flow or architecture, AND ONLY if they unblock a visible product capability. No speculative refactors.",
        "qa": "QA Agent: propose ideas from concrete failures — broken interactions, console errors, missing verification of core flows. Cite the failing artifact.",
        "security_performance": "Security/Performance Agent: ONLY propose when there is measurable evidence (a specific unsafe pattern, a measured slow path). If none, return empty list.",
    }.get(agent, agent)


def _proposer_system_prompt(agent: str, anti_halt_instruction: str = "") -> str:
    return (
        f"You are the {agent.upper()} member of a product idea council operating in AUTONOMOUS NO-QUESTION mode. "
        + _agent_brief(agent) + "\n"
        "ANTI-HALT: you MUST NOT return 'Cần làm rõ' / 'Halt' / 'chờ câu trả lời' / 'Xác nhận trước' / 'Chọn 1 hoặc 2' / 'Workspace không liên quan' / "
        "numbered choice lists for the user / 'Do you want me to' / 'Before I proceed' / 'Should I use'. "
        "For EVERY decision, pick the FIRST reasonable default, record the assumption, produce actual proposals.\n"
        "OUTPUT ONLY valid JSON with key 'ideas' containing an array.\n"
        "If you have ZERO useful ideas, return {\"ideas\": []} — do NOT write a paragraph about why or ask what to do.\n"
        + (f"\n{anti_halt_instruction}\n" if anti_halt_instruction else "")
        + f"Each proposal MUST contain ALL these keys: {', '.join(PROPOSAL_KEYS)}. "
        "repositoryEvidence is a list of {file, symbol, observation}; cite REAL file paths from the capability map. "
        "browserEvidence is a list of {url, observation} — leave empty if no browser audit yet. "
        "Scoring fields (goalRelevance/userValue/evidenceStrength/productCompleteness/feasibility) are your self-assessment 0-100. "
        "An idea with no repositoryEvidence AND no browserEvidence is only acceptable for an empty/scaffold workspace."
    )


def _proposer_user_payload(agent: str, context: dict[str, Any]) -> str:
    return json.dumps({"agentRole": agent, **context}, ensure_ascii=False)


def _safe_parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
        return None


def _ensure_proposal_shape(raw: Any, proposer: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in PROPOSAL_KEYS:
        out[key] = raw.get(key)
    out["proposer"] = proposer
    if not out.get("title") or not out.get("productCapability"):
        return None
    out["repositoryEvidence"] = list(out.get("repositoryEvidence") or [])
    out["browserEvidence"] = list(out.get("browserEvidence") or [])
    out["implementationScope"] = list(out.get("implementationScope") or [])
    out["acceptanceCriteria"] = list(out.get("acceptanceCriteria") or [])
    out["verificationPlan"] = list(out.get("verificationPlan") or [])
    out["risks"] = list(out.get("risks") or [])
    out["dependencies"] = list(out.get("dependencies") or [])
    for nkey in ("estimatedEffort", "goalRelevance", "userValue", "evidenceStrength", "productCompleteness", "feasibility"):
        try:
            out[nkey] = int(out.get(nkey) or 0)
        except Exception:
            out[nkey] = 0
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Critic and Arbiter
# ─────────────────────────────────────────────────────────────────────────────

_PLATFORM_OFFGOAL = re.compile(
    r"\b(flowview|flow view|classifier smoke|bottleneck export|agent observability|agent inspector|"
    r"auto loop|telemetry dashboard|agent platform|agent engine|claude-?adapter)\b",
    re.IGNORECASE,
)
_CLARIFICATION_OFFGOAL = re.compile(
    r"\b(cần\s+làm\s+rõ|halt|chờ\s+câu\s+trả\s+lời|chọn\s+\d+\s+hoặc\s+\d+"
    r"|xác\s+nhận\s+trước|workspace\s+không\s+liên\s+quan"
    r"|should\s+i\s+(use|pick|choose|build)|"
    r"do\s+you\s+want\s+me\s+to|please\s+(confirm|choose|select|pick|decide)"
    r"|before\s+i\s+(proceed|start|begin)|"
    r"what\s+(stack|theme|framework|library|database|backend|frontend|approach|architecture|scope|feature)"
    r"|which\s+(one|approach|option|way|pattern|strategy|method))",
    re.IGNORECASE,
)


def critic_reject(
    proposal: dict[str, Any],
    *,
    goal: str,
    capability_map: CapabilityMap,
    completed_fingerprints: set[str],
    rejected_fingerprints: set[str],
    is_scaffold_iteration: bool,
) -> str | None:
    text_blob = json.dumps(proposal, ensure_ascii=False).lower()
    title = str(proposal.get("title") or "")
    if _PLATFORM_OFFGOAL.search(text_blob):
        return "off-goal: targets the agent platform itself, not the user product"
    if "smoke" in title.lower() and "test" in title.lower():
        return "smoke script is not a product capability"
    if _CLARIFICATION_OFFGOAL.search(text_blob):
        return "invalid_clarification_response: proposal contains halt/clarification language — anti-halt violation"
    if not proposal.get("goalConnection"):
        return "missing goalConnection — cannot tie back to original user goal"
    if not is_scaffold_iteration:
        if not (proposal.get("repositoryEvidence") or proposal.get("browserEvidence")):
            return "no repositoryEvidence or browserEvidence cited"
    if not proposal.get("acceptanceCriteria"):
        return "missing acceptanceCriteria"
    if not proposal.get("verificationPlan"):
        return "missing verificationPlan"
    cap_name = str(proposal.get("productCapability") or "")
    if cap_name:
        match = next((c for c in capability_map.capabilities if c.name == cap_name), None)
        if match and match.status == "verified":
            return f"capability '{cap_name}' is already verified; no regression cited"
    fp = fingerprint_proposal(goal, proposal)
    if fp in completed_fingerprints:
        return "duplicate of previously completed idea"
    if fp in rejected_fingerprints:
        return "duplicate of previously rejected idea"
    return None


def arbiter_score(proposal: dict[str, Any]) -> tuple[int, dict[str, int]]:
    def clamp(x: Any) -> int:
        try:
            return max(0, min(100, int(x)))
        except Exception:
            return 0

    goal_rel = clamp(proposal.get("goalRelevance")) * 30 // 100
    user_val = clamp(proposal.get("userValue")) * 20 // 100
    completeness = clamp(proposal.get("productCompleteness")) * 15 // 100
    evidence = clamp(proposal.get("evidenceStrength")) * 15 // 100
    feasibility = clamp(proposal.get("feasibility")) * 10 // 100
    risk_reduction = 10 - min(10, len(proposal.get("risks") or []) * 2)
    total = goal_rel + user_val + completeness + evidence + feasibility + risk_reduction
    return total, {
        "goalRelevance": goal_rel,
        "userValue": user_val,
        "productCompleteness": completeness,
        "evidenceStrength": evidence,
        "feasibility": feasibility,
        "riskReduction": risk_reduction,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Memory namespace (goal × session × workspace)
# ─────────────────────────────────────────────────────────────────────────────


def memory_namespace(goal: str, session_id: str, workspace_path: str) -> str:
    seed = "".join((_normalize_title(goal), str(session_id or ""), str(workspace_path or "")))
    return hashlib.sha1(seed.encode("utf-8", errors="replace")).hexdigest()[:14]


def _council_state_path(state_dir: Path, ns: str) -> Path:
    return state_dir / "idea_council" / f"{ns}.json"


def load_council_state(state_dir: Path, ns: str) -> dict[str, Any]:
    path = _council_state_path(state_dir, ns)
    if not path.exists():
        return {"completedFingerprints": [], "rejectedFingerprints": [], "iterations": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"completedFingerprints": [], "rejectedFingerprints": [], "iterations": []}


def save_council_state(state_dir: Path, ns: str, payload: dict[str, Any]) -> None:
    path = _council_state_path(state_dir, ns)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────────────
# Council orchestration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CouncilContext:
    originalUserGoal: str
    productType: str
    targetUsers: str
    productRoot: str
    capabilityMap: dict[str, Any]
    currentRoutes: list[dict[str, Any]]
    currentComponents: list[dict[str, Any]]
    currentFeatures: list[dict[str, Any]]
    missingCapabilities: list[str]
    recentBrowserFindings: list[dict[str, Any]]
    failingTests: list[dict[str, Any]]
    consoleErrors: list[str]
    changedFilesFromRecentIterations: list[str]
    completedIdeas: list[dict[str, Any]]
    rejectedIdeas: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_council_context(
    *,
    goal: str,
    capability_map: CapabilityMap,
    iteration_history: list[dict[str, Any]],
    completed_ideas: list[dict[str, Any]],
    rejected_ideas: list[dict[str, Any]],
    recent_browser_findings: list[dict[str, Any]] | None = None,
    failing_tests: list[dict[str, Any]] | None = None,
    console_errors: list[str] | None = None,
) -> CouncilContext:
    missing = [c.name for c in capability_map.capabilities if c.status == "missing"]
    changed: list[str] = []
    for item in (iteration_history or [])[-6:]:
        for f in (item.get("files") or [])[:3]:
            if f and f not in changed:
                changed.append(f)
    return CouncilContext(
        originalUserGoal=goal,
        productType="auto",
        targetUsers="auto",
        productRoot=capability_map.productRoot,
        capabilityMap=capability_map.to_dict(),
        currentRoutes=capability_map.routes,
        currentComponents=capability_map.components[:25],
        currentFeatures=[asdict(c) for c in capability_map.capabilities if c.status != "missing"],
        missingCapabilities=missing,
        recentBrowserFindings=list(recent_browser_findings or []),
        failingTests=list(failing_tests or []),
        consoleErrors=list(console_errors or []),
        changedFilesFromRecentIterations=changed[:15],
        completedIdeas=completed_ideas[-15:],
        rejectedIdeas=rejected_ideas[-15:],
    )


def invoke_council(
    *,
    context: CouncilContext,
    chat: Callable[[list[dict[str, str]], float, bool], str],
    is_scaffold_iteration: bool,
    anti_halt_instruction: str = "",
) -> tuple[list[Proposal], list[Proposal]]:
    """Invoke each agent independently. Returns (kept, rejected) proposals."""
    raw_proposals: list[Proposal] = []
    context_json = context.to_dict()
    for agent in PROPOSER_AGENTS:
        sys_prompt = _proposer_system_prompt(agent, anti_halt_instruction)
        user_payload = _proposer_user_payload(agent, context_json)
        try:
            raw = chat(
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_payload},
                ],
                0.4,
                True,
            )
        except Exception as exc:
            write_debug_event("council.proposer_error", {"agent": agent, "error": str(exc)})
            continue
        parsed = _safe_parse_json(raw) or {}
        ideas = parsed.get("ideas") if isinstance(parsed, dict) else None
        if not isinstance(ideas, list):
            continue
        for raw_idea in ideas[:5]:
            shaped = _ensure_proposal_shape(raw_idea, agent)
            if not shaped:
                continue
            raw_proposals.append(
                Proposal(
                    raw=shaped,
                    proposer=agent,
                    fingerprint=fingerprint_proposal(context.originalUserGoal, shaped),
                )
            )
    return raw_proposals, []


def run_council_round(
    *,
    goal: str,
    session_id: str,
    workspace_path: str,
    state_dir: Path,
    capability_map: CapabilityMap,
    iteration: int,
    iteration_history: list[dict[str, Any]],
    chat: Callable[[list[dict[str, str]], float, bool], str],
    recent_browser_findings: list[dict[str, Any]] | None = None,
    failing_tests: list[dict[str, Any]] | None = None,
    console_errors: list[str] | None = None,
    anti_halt_instruction: str = "",
) -> dict[str, Any]:
    """One full council round: propose → critic → score → arbiter → persist."""
    ns = memory_namespace(goal, session_id, workspace_path)
    persisted = load_council_state(state_dir, ns)
    completed_fp = set(persisted.get("completedFingerprints") or [])
    rejected_fp = set(persisted.get("rejectedFingerprints") or [])

    context = build_council_context(
        goal=goal,
        capability_map=capability_map,
        iteration_history=iteration_history,
        completed_ideas=persisted.get("completedIdeas") or [],
        rejected_ideas=persisted.get("rejectedIdeas") or [],
        recent_browser_findings=recent_browser_findings,
        failing_tests=failing_tests,
        console_errors=console_errors,
    )
    is_scaffold = iteration <= 0 and not capability_map.components and not capability_map.routes

    proposals, _ = invoke_council(
        context=context,
        chat=chat,
        is_scaffold_iteration=is_scaffold,
        anti_halt_instruction=anti_halt_instruction,
    )

    kept: list[Proposal] = []
    rejected_now: list[Proposal] = []
    for prop in proposals:
        reason = critic_reject(
            prop.raw,
            goal=goal,
            capability_map=capability_map,
            completed_fingerprints=completed_fp,
            rejected_fingerprints=rejected_fp,
            is_scaffold_iteration=is_scaffold,
        )
        if reason:
            prop.rejected = True
            prop.rejectionReason = reason
            rejected_now.append(prop)
            continue
        total, breakdown = arbiter_score(prop.raw)
        prop.score = total
        prop.scoreBreakdown = breakdown
        kept.append(prop)

    kept.sort(key=lambda p: p.score, reverse=True)
    winner = kept[0] if kept and kept[0].score >= MIN_ARBITER_SCORE else None

    iteration_record = {
        "iteration": iteration,
        "ts": time.time(),
        "kept": [p.to_dict() for p in kept],
        "rejected": [p.to_dict() for p in rejected_now],
        "winner": winner.to_dict() if winner else None,
        "capabilityMapSummary": {
            "productRoot": capability_map.productRoot,
            "rawFiles": capability_map.rawFiles,
            "capabilityStatuses": {c.name: c.status for c in capability_map.capabilities},
        },
    }
    persisted.setdefault("iterations", []).append(iteration_record)
    if winner:
        persisted.setdefault("completedFingerprints", []).append(winner.fingerprint)
        persisted.setdefault("completedIdeas", []).append({"title": winner.raw.get("title"), "fingerprint": winner.fingerprint})
    for p in rejected_now:
        persisted.setdefault("rejectedFingerprints", []).append(p.fingerprint)
        persisted.setdefault("rejectedIdeas", []).append(
            {"title": p.raw.get("title"), "fingerprint": p.fingerprint, "reason": p.rejectionReason}
        )
    save_council_state(state_dir, ns, persisted)

    write_debug_event(
        "council.round",
        {
            "iteration": iteration,
            "ns": ns,
            "proposed": len(proposals),
            "kept": len(kept),
            "rejected": len(rejected_now),
            "winner": (winner.raw.get("title") if winner else None),
            "winnerScore": (winner.score if winner else None),
        },
    )

    return {
        "ok": True,
        "namespace": ns,
        "capabilityMap": capability_map.to_dict(),
        "proposals": [p.to_dict() for p in kept + rejected_now],
        "winner": winner.to_dict() if winner else None,
        "minScore": MIN_ARBITER_SCORE,
        "isScaffoldIteration": is_scaffold,
    }


def format_winner_task(winner: dict[str, Any], goal: str, product_root: str) -> str:
    raw = winner.get("raw") or {}
    lines = [
        "AUTONOMOUS PRODUCT ITERATION — MUTATION_REQUIRED=true",
        "",
        f"Original goal: {goal}",
        f"Product root: {product_root}",
        f"Selected capability: {raw.get('productCapability') or '(unknown)'}",
        f"User problem: {raw.get('userProblem') or '(unknown)'}",
    ]
    evid = raw.get("repositoryEvidence") or []
    if evid:
        lines.append("Repository evidence:")
        for item in evid[:6]:
            lines.append(f"  - {item.get('file')}::{item.get('symbol') or ''} — {item.get('observation') or ''}")
    if raw.get("expectedUserValue"):
        lines.append(f"Expected user value: {raw['expectedUserValue']}")
    scope = raw.get("implementationScope") or []
    if scope:
        lines.append("Files likely involved:")
        for f in scope[:8]:
            lines.append(f"  - {f}")
    crit = raw.get("acceptanceCriteria") or []
    if crit:
        lines.append("Acceptance criteria:")
        for c in crit[:8]:
            lines.append(f"  - {c}")
    verif = raw.get("verificationPlan") or []
    if verif:
        lines.append("Browser verification:")
        for v in verif[:6]:
            lines.append(f"  - {v}")
    lines.extend([
        "",
        "FULL WRITE ACCESS to productRoot. You MUST edit files. You MUST run commands. You MUST verify with browser.",
        f"Bạn được tự sửa file bên trong productRoot ({product_root}).",
        "MUTATION_REQUIRED=true. Do NOT produce a plan/report. Produce working code changes.",
        "Phạm vi ghi chỉ nằm trong productRoot; agent platform (FlowView, classifier, claude_adapter, autonomy, idea_council) nằm ngoài phạm vi.",
        "Không hỏi lại — đã có default an toàn từ council.",
        "Nếu evidence đã stale, khảo sát lại trước khi sửa.",
        "Phải tạo feature hoạt động, không chỉ viết báo cáo hoặc script smoke test.",
    ])
    return "\n".join(lines)
