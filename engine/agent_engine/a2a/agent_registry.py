"""A2A Agent Card Builder + Registry.

Builds AgentCards from the existing agent_contracts.py role definitions
and provides a registry for discovery (by name, capability, or skill match).
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from ..debug_log import write_debug_event
from .taste_director import TASTE_CARD, TASTE_DIRECTOR_AGENT_ID
from .types import A2A_PROTOCOL_VERSION, AgentCapability, AgentCard, AgentSecurity, AgentSkill

# ── Card definitions for all pipeline agents ──────────────────────────────────

_BUILTIN_CARDS: list[AgentCard] = [
    TASTE_CARD,

    AgentCard(
        name="Orchestrator",
        description="Pipeline entry point — classifies execution lane, detects task intent, loads long-term memory.",
        url="/v1/agents/orchestrator",
        capabilities=[
            AgentCapability(name="classify", description="Classify execution lane (read_only vs write) from task text", tools=["_detect_task_intent", "classify_execution"]),
            AgentCapability(name="snapshot", description="Read workspace filesystem snapshot before execution", tools=["run_preflight"]),
        ],
        skills=[
            AgentSkill(id="orchestrator:classify", name="Task Classification", description="Classify execution intent from natural language task", tags=["classify", "intake"]),
        ],
        streaming=True,
    ),

    AgentCard(
        name="Intake Synthesizer",
        description="Synthesizes problem statement from fan-out intake agents with autonomous defaults.",
        url="/v1/agents/intake-synthesizer",
        capabilities=[
            AgentCapability(name="synthesize", description="Merge intake agent findings into problem statement", tools=["_json"]),
        ],
        skills=[
            AgentSkill(id="intake:synthesize", name="Intake Synthesis", description="Merge user_intent + ambiguity + repo_context into spec", tags=["intake", "synthesize"]),
        ],
    ),

    AgentCard(
        name="Plan Arbiter",
        description="Selects final plan from parallel planners + critics, produces workerTaskSpec.",
        url="/v1/agents/plan-arbiter",
        capabilities=[
            AgentCapability(name="arbitrate", description="Judge + select the best plan from candidates", tools=["_json", "_normalize_worker_task_spec"]),
        ],
        skills=[
            AgentSkill(id="plan:arbitrate", name="Plan Arbitration", description="Score + select final plan, produce workerTaskSpec", tags=["planning", "arbitration"]),
        ],
    ),

    AgentCard(
        name="Coder",
        description="Primary coding agent — edits files, runs commands, verifies results. Uses claude-agent-sdk.",
        url="/v1/agents/coder",
        capabilities=[
            AgentCapability(name="code:edit", description="Read/Edit/Write file operations", tools=["Read", "Edit", "Write"]),
            AgentCapability(name="code:search", description="Glob/Grep file and content search", tools=["Glob", "Grep"]),
            AgentCapability(name="code:exec", description="Bash command execution in workspace", tools=["Bash"]),
            AgentCapability(name="code:memory", description="Codebase-Memory MCP tools", tools=["search_graph", "trace_path", "get_architecture"]),
        ],
        skills=[
            AgentSkill(id="code:implement", name="Implement Feature", description="Edit files + install deps + verify", tags=["code", "implement"]),
            AgentSkill(id="code:fix", name="Fix Bug", description="Root-cause debug + edit + test", tags=["code", "fix", "debug"]),
            AgentSkill(id="code:scaffold", name="Scaffold Project", description="Create new project from scratch", tags=["code", "create", "scaffold"]),
        ],
        security=AgentSecurity(
            allowedPaths=["music-app/**", "todo-app/**", "app/**", "src/**", "package.json"],
            forbiddenPaths=[".git/**", ".env*", "*.secret*"],
            allowedCommands=["npm", "npx", "pnpm", "python", "pip", "uv", "cargo", "go", "node", "git"],
        ),
        streaming=True,
    ),

    AgentCard(
        name="Tester",
        description="Runs verification commands, interprets results, classifies failures.",
        url="/v1/agents/tester",
        capabilities=[
            AgentCapability(name="test:run", description="Execute verification commands", tools=["run_command"]),
            AgentCapability(name="test:classify", description="Classify failures as blockers vs warnings", tools=["_json"]),
        ],
        skills=[
            AgentSkill(id="test:verify", name="Verification Runner", description="Run build/test/lint and report results", tags=["test", "verify"]),
        ],
    ),

    AgentCard(
        name="Code Reviewer",
        description="Review code changes for correctness + merge readiness.",
        url="/v1/agents/code-reviewer",
        capabilities=[
            AgentCapability(name="review:code", description="Correctness + regression risk review", tools=["_json"]),
            AgentCapability(name="review:sanitize", description="Downgrade spurious blocker claims", tools=["_sanitize_review_claims"]),
        ],
        skills=[
            AgentSkill(id="review:code", name="Code Review", description="Review diff for correctness + risk", tags=["review", "code"]),
        ],
    ),

    AgentCard(
        name="Security Reviewer",
        description="Security policy audit — auth, secrets, injection, sandbox violations.",
        url="/v1/agents/security-reviewer",
        capabilities=[
            AgentCapability(name="review:security", description="Security + policy audit", tools=["_json"]),
        ],
        skills=[
            AgentSkill(id="review:security", name="Security Review", description="Audit for secrets, injection, permissions", tags=["review", "security"]),
        ],
    ),

    AgentCard(
        name="Project Doctor",
        description="Autonomous scan→plan→patch→verify pipeline for hygiene fixes.",
        url="/v1/agents/project-doctor",
        capabilities=[
            AgentCapability(name="doctor:scan", description="Find secrets, syntax errors, drift", tools=["_scan"]),
            AgentCapability(name="doctor:plan", description="Order findings by severity", tools=["_plan"]),
            AgentCapability(name="doctor:patch", description="Apply deterministic or LLM fix", tools=["_patch"]),
            AgentCapability(name="doctor:verify", description="Re-run project checks", tools=["_verify"]),
        ],
        skills=[
            AgentSkill(id="doctor:full", name="Full Health Check", description="Scan→plan→patch→verify pipeline", tags=["doctor", "fix", "verify"]),
        ],
    ),

    AgentCard(
        name="Repo Intelligence",
        description="8-stage codebase analysis: graph retrieval, source verification, architecture reconstruction.",
        url="/v1/agents/repo-intelligence",
        capabilities=[
            AgentCapability(name="intelligence:analyze", description="Full ContextPack analysis", tools=["analyze"]),
            AgentCapability(name="intelligence:codegraph", description="CodeGraphAdapter queries", tools=["_query_graph"]),
        ],
        skills=[
            AgentSkill(id="intelligence:situate", name="Codebase Situate", description="Analyze repo context for a task", tags=["intelligence", "context"]),
        ],
    ),

    AgentCard(
        name="Idea Council - Product",
        description="Propose product ideas advancing the core user journey.",
        url="/v1/agents/idea-council-product",
        capabilities=[
            AgentCapability(name="idea:product", description="Product idea generation", tools=["_proposer_system_prompt"]),
        ],
        skills=[
            AgentSkill(id="idea:product", name="Product Proposer", description="Generate product ideas from goal + code evidence", tags=["idea", "product"]),
        ],
    ),

    AgentCard(
        name="Idea Council - UX",
        description="Propose UX improvements from usability gaps.",
        url="/v1/agents/idea-council-ux",
        capabilities=[
            AgentCapability(name="idea:ux", description="UX idea generation", tools=["_proposer_system_prompt"]),
        ],
        skills=[
            AgentSkill(id="idea:ux", name="UX Proposer", description="Generate UX ideas from usability gaps", tags=["idea", "ux"]),
        ],
    ),

    AgentCard(
        name="Idea Council - Frontend",
        description="Propose component architecture and state improvements.",
        url="/v1/agents/idea-council-frontend",
        capabilities=[
            AgentCapability(name="idea:frontend", description="Frontend idea generation", tools=["_proposer_system_prompt"]),
        ],
        skills=[
            AgentSkill(id="idea:frontend", name="Frontend Proposer", description="Generate frontend architecture ideas", tags=["idea", "frontend"]),
        ],
    ),

    AgentCard(
        name="Idea Council - Architect",
        description="Propose data flow/architecture ideas only when they unblock product.",
        url="/v1/agents/idea-council-architect",
        capabilities=[
            AgentCapability(name="idea:architect", description="Architecture idea generation", tools=["_proposer_system_prompt"]),
        ],
        skills=[
            AgentSkill(id="idea:architect", name="Architect Proposer", description="Generate architecture ideas", tags=["idea", "architect"]),
        ],
    ),

    AgentCard(
        name="Idea Council - QA",
        description="Propose ideas from concrete test/console failures.",
        url="/v1/agents/idea-council-qa",
        capabilities=[
            AgentCapability(name="idea:qa", description="QA idea generation", tools=["_proposer_system_prompt"]),
        ],
        skills=[
            AgentSkill(id="idea:qa", name="QA Proposer", description="Generate QA ideas from failures", tags=["idea", "qa"]),
        ],
    ),

    AgentCard(
        name="Idea Council - Security/Performance",
        description="Propose only with measurable security/perf evidence.",
        url="/v1/agents/idea-council-security-performance",
        capabilities=[
            AgentCapability(name="idea:security-perf", description="Security/Perf idea generation", tools=["_proposer_system_prompt"]),
        ],
        skills=[
            AgentSkill(id="idea:sec-perf", name="Security/Perf Proposer", description="Generate security/perf ideas from evidence", tags=["idea", "security", "performance"]),
        ],
    ),
]


# ── Registry ──────────────────────────────────────────────────────────────────


@dataclass
class A2AAgentRegistry:
    cards: dict[str, AgentCard] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.cards:
            self.load_builtins()

    def load_builtins(self) -> None:
        for card in _BUILTIN_CARDS:
            slug = card.name.lower().replace(" ", "-")
            with self._lock:
                self.cards[slug] = card

    def register(self, agent_id: str, card: AgentCard) -> None:
        with self._lock:
            self.cards[agent_id] = card
        write_debug_event("a2a.registry.register", {"id": agent_id, "name": card.name})

    def get(self, agent_id: str) -> AgentCard | None:
        with self._lock:
            return self.cards.get(agent_id)

    def list_all(self) -> list[AgentCard]:
        with self._lock:
            return list(self.cards.values())

    def list_ids(self) -> list[str]:
        with self._lock:
            return sorted(self.cards.keys())

    def list_by_capability(self, capability: str) -> list[AgentCard]:
        found: list[AgentCard] = []
        with self._lock:
            for card in self.cards.values():
                for cap in card.capabilities:
                    if capability.lower() in cap.name.lower() or capability.lower() in cap.description.lower():
                        found.append(card)
                        break
        return found

    def match_skill(self, task_description: str) -> list[tuple[AgentCard, AgentSkill, int]]:
        """Crude keyword match: rank cards by skill tag overlap with task text.
        Returns sorted list of (card, skill, score)."""
        task_lower = task_description.lower()
        results: list[tuple[AgentCard, AgentSkill, int]] = []
        with self._lock:
            for card in self.cards.values():
                for skill in card.skills:
                    score = sum(1 for tag in skill.tags if tag.lower() in task_lower)
                    for example in skill.examples:
                        for word in example.lower().split():
                            if word in task_lower:
                                score += 1
                    if score > 0:
                        results.append((card, skill, score))
        results.sort(key=lambda x: x[2], reverse=True)
        return results


# ── Module-level singleton ────────────────────────────────────────────────────

_REGISTRY: A2AAgentRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_agent_registry() -> A2AAgentRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = A2AAgentRegistry()
    return _REGISTRY
