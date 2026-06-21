"""A2A Taste Director Agent.

Reads the codebase frontend (CSS, JS inline styles, HTML) and produces:
  1. DESIGN_SYSTEM.md — token architecture (colors, typography, spacing, etc.)
  2. TasteViolationReport — anti-pattern inventory with line references
  3. FixPlan — actionable CSS variable replacements to heal violations

Implements the TasteSkill methodology (tasteskill.dev / github.com/Leonxlnx/taste-skill):
  - design-taste-frontend: greenfield projects
  - redesign-existing-projects: legacy codebase with live styles
  - full-output-enforcement: prevent placeholder/incomplete UI output

No external dependency. Uses deterministic scanning for hardcoded values;
LLM for qualitative design judgment (mood, hierarchy, accessibility audit).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .types import AgentCard, AgentCapability, AgentSkill, AgentSecurity
from ..debug_log import write_debug_event
from ..workspace import IGNORED_DIRS, relpath

TASTE_DIRECTOR_AGENT_ID = "taste-director"

TASTE_CARD = AgentCard(
    name="Taste Director",
    description="Design quality enforcement agent. Audits UI for anti-patterns (AI-generic gradients, "
    "hardcoded hex colors, missing dark mode, no focus-visible, wrong hierarchy), "
    "produces DESIGN_SYSTEM.md token architecture, and generates CSS-variable fix plans.",
    url="/v1/agents/taste-director",
    version="1.0.0",
    capabilities=[
        AgentCapability(name="design:audit", description="Scan frontend files for design anti-patterns", tools=["greppy", "read_file"]),
        AgentCapability(name="design:system", description="Generate DESIGN_SYSTEM.md token architecture artifact", tools=["read_file"]),
        AgentCapability(name="design:review", description="Taste review with evidence-based scoring (0-10)", tools=["read_file", "llm_judge"]),
        AgentCapability(name="design:fix", description="Generate CSS variable replacement plan for hardcoded values", tools=["read_file", "edit_file"]),
    ],
    skills=[
        AgentSkill(
            id="taste:audit",
            name="Taste Audit",
            description="Scan workspace for hardcoded colors, missing states, AI-generic patterns",
            tags=["design", "audit", "taste"],
            examples=["Scan the workspace and list every hardcoded hex color with file:line references"],
        ),
        AgentSkill(
            id="taste:design-system",
            name="Design System Generator",
            description="Generate DESIGN_SYSTEM.md with tokens, hierarchy, breakpoints",
            tags=["design", "system", "tokens"],
            examples=["Generate a DESIGN_SYSTEM.md for a dark music player app"],
        ),
        AgentSkill(
            id="taste:review",
            name="Taste Review",
            description="Review UI against taste rules; score 0-10 with evidence",
            tags=["design", "review", "taste"],
            examples=["Review the current UI and score it on design quality"],
        ),
    ],
    security=AgentSecurity(
        sandboxed=False,
        networkAccess=False,
        allowedCommands=[],
        allowedPaths=["**/*.css", "**/*.js", "**/*.html", "**/*.md"],
    ),
    streaming=False,
    pushNotifications=False,
)

# ── Patterns for scanning ─────────────────────────────────────────────────────

_COLOR_HEX = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})(?![0-9a-fA-F])")
_COLOR_RGB = re.compile(r"rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
_GRADIENT_RE = re.compile(r"(linear|radial|conic)-gradient\s*\(", re.IGNORECASE)
_INLINE_STYLE = re.compile(r"style\s*=\s*['\"]")
_FONT_FAMILY = re.compile(r"font-family\s*:\s*([^;]+);?", re.IGNORECASE)
_NO_FOCUS = re.compile(r":focus\b(?!-visible|-within)")

_AI_ANTI_PATTERNS = [
    (re.compile(r"linear-gradient\s*\(\s*135deg\s*,\s*#(?:6[0-9a-f]|7[0-9a-f]|8[0-9a-f]|9[0-9a-f]|a[0-9a-f]|b[0-9a-f]|c[0-9a-f]|d[0-9a-f]|e[0-9a-f])", re.IGNORECASE), "purple-blue gradient — AI-generic"),
    (re.compile(r"backdrop-filter\s*:\s*blur\s*\(\s*(?:15|20|25|30)px\s*\)", re.IGNORECASE), "excessive backdrop-blur — glassmorphism overuse"),
    (re.compile(r"box-shadow\s*:\s*0\s+0\s+(?:30|40|50|60)px", re.IGNORECASE), "oversized glow shadow — AI-generic neon"),
    (re.compile(r"border-radius\s*:\s*(?:20|24|28|32)px", re.IGNORECASE), "oversized border-radius — card-itis"),
    (re.compile(r"animation\s*:\s*(?:float|pulse|glow|shimmer)", re.IGNORECASE), "gratuitous animation — prefers-reduced-motion not respected"),
]


@dataclass
class ColorFinding:
    file: str
    line: int
    value: str
    context: str  # surrounding CSS property or JS expression
    category: str  # "hex", "rgb", "gradient", "hardcoded"


@dataclass
class TasteViolation:
    file: str
    line: int | None
    severity: str  # P0/P1/P2
    category: str  # "color_token", "dark_mode", "focus_visible", "motion", "hierarchy", "ai_generic"
    description: str
    remediation: str


@dataclass
class TasteViolationReport:
    violations: list[TasteViolation] = field(default_factory=list)
    colorFindings: list[ColorFinding] = field(default_factory=list)
    summary: str = ""
    score: float = 0.0  # 0-10

    def to_dict(self) -> dict[str, Any]:
        return {
            "violations": [asdict(v) for v in self.violations],
            "colorFindings": [asdict(c) for c in self.colorFindings],
            "summary": self.summary,
            "score": self.score,
            "violationCount": len(self.violations),
            "bySeverity": {
                "P0": sum(1 for v in self.violations if v.severity == "P0"),
                "P1": sum(1 for v in self.violations if v.severity == "P1"),
                "P2": sum(1 for v in self.violations if v.severity == "P2"),
            },
        }


@dataclass
class DesignSystem:
    """Generated DESIGN_SYSTEM.md artifact."""
    productMood: str = ""
    targetAudience: str = ""
    colorTokens: dict[str, str] = field(default_factory=dict)  # {token-name: hex-value}
    typography: dict[str, str] = field(default_factory=dict)    # {role: "font-family font-size font-weight line-height"}
    spacing: dict[str, str] = field(default_factory=dict)       # {token: value}
    breakpoints: dict[str, str] = field(default_factory=dict)    # {name: "min-width"}
    radii: dict[str, str] = field(default_factory=dict)
    shadows: dict[str, str] = field(default_factory=dict)
    animationTokens: dict[str, str] = field(default_factory=dict)
    antiPatterns: list[str] = field(default_factory=list)
    accessibilityRequirements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        lines = [
            "# DESIGN SYSTEM",
            "",
            f"**Product Mood:** {self.productMood}",
            f"**Target Audience:** {self.targetAudience}",
            "",
            "## Color Tokens",
            "",
            "| Token | Value |",
            "|-------|-------|",
        ]
        for token, val in sorted(self.colorTokens.items()):
            lines.append(f"| `--{token}` | `{val}` |")
        lines.append("")
        lines.append("## Typography")
        lines.append("")
        for role, spec in sorted(self.typography.items()):
            lines.append(f"- **{role}**: {spec}")
        lines.append("")
        lines.append("## Spacing")
        lines.append("")
        for token, val in sorted(self.spacing.items()):
            lines.append(f"- `--{token}`: {val}")
        lines.append("")
        lines.append("## Breakpoints")
        lines.append("")
        for name, bp in sorted(self.breakpoints.items()):
            lines.append(f"- **{name}**: {bp}")
        lines.append("")
        if self.radii:
            lines.append("## Border Radius")
            lines.append("")
            for token, val in sorted(self.radii.items()):
                lines.append(f"- `--{token}`: {val}")
            lines.append("")
        if self.shadows:
            lines.append("## Shadows")
            lines.append("")
            for token, val in sorted(self.shadows.items()):
                lines.append(f"- `--{token}`: {val}")
            lines.append("")
        if self.animationTokens:
            lines.append("## Animation Tokens")
            lines.append("")
            for token, val in sorted(self.animationTokens.items()):
                lines.append(f"- `--{token}`: {val}")
            lines.append("")
        if self.antiPatterns:
            lines.append("## Forbidden Anti-Patterns")
            lines.append("")
            for ap in self.antiPatterns:
                lines.append(f"- ❌ {ap}")
            lines.append("")
        if self.accessibilityRequirements:
            lines.append("## Accessibility Requirements")
            lines.append("")
            for item in self.accessibilityRequirements:
                lines.append(f"- ✅ {item}")
            lines.append("")
        return "\n".join(lines)


# ── Scanner ────────────────────────────────────────────────────────────────────

_CSS_FILES = {".css", ".scss", ".less"}
_JS_FILES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_HTML_FILES = {".html", ".htm", ".vue", ".svelte"}
_FRONTEND_EXTS = _CSS_FILES | _JS_FILES | _HTML_FILES


def scan_frontend_colors(root: Path, max_files: int = 80) -> list[ColorFinding]:
    findings: list[ColorFinding] = []
    try:
        files = sorted(
            [p for p in root.rglob("*") if p.suffix.lower() in _FRONTEND_EXTS and p.is_file()],
            key=lambda p: p.name,
        )[:max_files]
    except OSError:
        return findings
    for path in files:
        if any(ignored in path.parts for ignored in IGNORED_DIRS.union({"node_modules", "__pycache__", ".agent-state"})):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = relpath(path, root)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _COLOR_HEX.finditer(line):
                findings.append(ColorFinding(
                    file=rel, line=lineno, value=match.group(0),
                    context=line.strip()[:200], category="hex",
                ))
            for match in _COLOR_RGB.finditer(line):
                findings.append(ColorFinding(
                    file=rel, line=lineno,
                    value=f"rgb({match.group(1)},{match.group(2)},{match.group(3)})",
                    context=line.strip()[:200], category="rgb",
                ))
    return findings


def scan_taste_violations(root: Path) -> list[TasteViolation]:
    violations: list[TasteViolation] = []

    # Existing CSS token audit — check styles.css for theme completeness
    css_file = root / "src" / "renderer" / "styles.css"
    if not css_file.exists():
        violations.append(TasteViolation(
            file="src/renderer/styles.css", line=None, severity="P0",
            category="design_system", description="No styles.css found — missing design tokens",
            remediation="Create styles.css with :root { --bg, --surface, --text, --accent, ... } tokens",
        ))
        return violations

    try:
        css = css_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return violations

    # Check for dark-mode rules
    if "[data-theme=\"dark\"]" not in css and "[data-theme=dark]" not in css:
        violations.append(TasteViolation(
            file="src/renderer/styles.css", line=None, severity="P0",
            category="dark_mode", description="Missing [data-theme=dark] CSS rules — no dark mode support",
            remediation="Add [data-theme=dark] block mapping :root light tokens to dark equivalents",
        ))

    # Check for :focus-visible
    if ":focus-visible" not in css:
        violations.append(TasteViolation(
            file="src/renderer/styles.css", line=None, severity="P1",
            category="focus_visible", description="Missing :focus-visible styles — no visible focus indicator",
            remediation="Add :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }",
        ))

    # Check prefers-reduced-motion
    if "prefers-reduced-motion" not in css:
        violations.append(TasteViolation(
            file="src/renderer/styles.css", line=None, severity="P1",
            category="motion", description="No prefers-reduced-motion media query — animations may harm vestibular users",
            remediation="Add @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; } }",
        ))

    # Check animation tokens
    if "--anim-" not in css:
        violations.append(TasteViolation(
            file="src/renderer/styles.css", line=None, severity="P2",
            category="animation_tokens", description="No animation duration tokens (--anim-fast, --anim-normal, --anim-slow)",
            remediation="Add --anim-fast: 0.15s; --anim-normal: 0.22s; --anim-slow: 0.7s; in :root",
        ))

    # Scan for AI-generic anti-patterns in CSS
    css_lines = css.splitlines()
    for lineno, line in enumerate(css_lines, start=1):
        for pattern, desc in _AI_ANTI_PATTERNS:
            if pattern.search(line):
                violations.append(TasteViolation(
                    file="src/renderer/styles.css", line=lineno, severity="P1",
                    category="ai_generic", description=desc,
                    remediation="Replace with semantic design token; prefer subtle, professional styling",
                ))

    return violations


def extract_existing_tokens(css: str) -> dict[str, str]:
    """Extract all --custom-property: value; declarations from :root block."""
    root_match = re.search(r":root\s*\{([^}]+)\}", css, re.DOTALL)
    if not root_match:
        return {}
    tokens: dict[str, str] = {}
    for match in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", root_match.group(1)):
        tokens[match.group(1)] = match.group(2).strip()
    return tokens


def taste_score(violations: list[TasteViolation]) -> float:
    """Score from 10.0 down based on violation severity."""
    score = 10.0
    for v in violations:
        if v.severity == "P0":
            score -= 1.5
        elif v.severity == "P1":
            score -= 0.75
        else:
            score -= 0.3
    return max(0.0, round(score, 1))


def run_taste_audit(workspace: str | Path) -> TasteViolationReport:
    root = Path(workspace).resolve()
    violations = scan_taste_violations(root)
    color_findings = scan_frontend_colors(root)
    score = taste_score(violations)
    has_dark = any(v.category == "dark_mode" for v in violations)
    has_focus = any(v.category == "focus_visible" for v in violations)
    has_motion = any(v.category == "motion" for v in violations)
    lines: list[str] = [f"Taste Audit: {len(violations)} violations, {len(color_findings)} hardcoded colors, score {score}/10"]
    if has_dark:
        lines.append("P0: Missing dark mode — add [data-theme=dark] block")
    if has_focus:
        lines.append("P1: Missing :focus-visible styles")
    if has_motion:
        lines.append("P1: Missing prefers-reduced-motion support")
    if color_findings:
        lines.append(f"P1: {len(color_findings)} hardcoded color values — replace with CSS variables")
    report = TasteViolationReport(
        violations=violations,
        colorFindings=color_findings,
        summary="\n".join(lines),
        score=score,
    )
    write_debug_event("taste.audit", {
        "violationCount": len(violations),
        "colorCount": len(color_findings),
        "score": score,
    })
    return report


def build_design_system(
    workspace: str | Path,
    product_mood: str = "",
    target_audience: str = "",
) -> DesignSystem:
    """Generate DESIGN_SYSTEM.md from existing CSS tokens + LLM assistance (if provided)."""
    root = Path(workspace).resolve()
    css_file = root / "src" / "renderer" / "styles.css"
    tokens: dict[str, str] = {}
    if css_file.exists():
        try:
            tokens = extract_existing_tokens(css_file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass

    ds = DesignSystem(
        productMood=product_mood or "Professional, modern, calm — dev-tool aesthetic",
        targetAudience=target_audience or "Developers and power users",
    )

    # Extract color tokens
    for name in ("--bg", "--surface", "--surface-subtle", "--text", "--text-muted",
                  "--border", "--accent", "--accent-soft", "--shadow",
                  "--status-ok", "--status-warn", "--status-error", "--status-info"):
        if name in tokens:
            ds.colorTokens[name.lstrip("-")] = tokens[name]

    # Typography
    for name in ("--font-family", "--font-size-base", "--font-size-sm", "--font-size-lg",
                  "--font-size-xl", "--line-height-base"):
        if name in tokens:
            ds.typography[name.lstrip("-")] = tokens[name]

    # Spacing
    for name in ("--spacing-xs", "--spacing-sm", "--spacing-md", "--spacing-lg", "--spacing-xl",
                  "--section-gap", "--panel-padding"):
        if name in tokens:
            ds.spacing[name.lstrip("-")] = tokens[name]

    # Animation
    for name in ("--anim-fast", "--anim-normal", "--anim-slow"):
        if name in tokens:
            ds.animationTokens[name.lstrip("-")] = tokens[name]

    # Breakpoints (from CLAUDE.md or hardcoded defaults)
    ds.breakpoints = {
        "mobile": "390px",
        "tablet": "768px",
        "desktop": "1440px",
    }

    # Anti-patterns inventory
    ds.antiPatterns = [
        "No purple/blue gradient abuse — use single accent color",
        "No oversized border-radius — max 8px for cards",
        "No neon glow box-shadows",
        "No gratuitous animations — every animation must have purpose",
        "No hardcoded hex values — use CSS variables",
    ]

    ds.accessibilityRequirements = [
        "WCAG 2.1 AA contrast ratio ≥ 4.5:1 for text",
        "Visible :focus-visible on all interactive elements",
        "@media (prefers-reduced-motion: reduce) respected",
        "Keyboard navigation for all controls",
        "ARIA labels on SVG elements and custom controls",
    ]

    return ds
