# TasteSkill — Design Quality Enforcement

**Source:** https://github.com/Leonxlnx/taste-skill (pinned at main, 2026)
**Agent:** Taste Director (`engine/agent_engine/a2a/taste_director.py`)

## Purpose

Every project needs a single, coherent design direction. TasteSkill prevents
AI-generic interfaces (purple/blue gradients, excessive glassmorphism,
random border-radii, missing dark mode, broken focus indicators) by enforcing
a token-based design system with automated auditing.

## When to Use

| Scenario | Skill |
|---|---|
| New project, no existing CSS | `design-taste-frontend` |
| Existing project with live styles | `redesign-existing-projects` |
| Agent outputs incomplete UI | `full-output-enforcement` |
| Strict token validation needed | `gpt-taste` |

## How It Works

1. **Audit** — scans all CSS/JS/HTML files for:
   - Hardcoded hex/rgb() values
   - Missing :root custom properties
   - Missing [data-theme=dark] rules
   - Missing :focus-visible
   - Missing prefers-reduced-motion
   - AI-generic patterns (gradients, excessive blur, neon shadows)

2. **Report** — produces `TasteViolationReport` with:
   - Each violation: file, line, severity (P0/P1/P2), category, remediation
   - Score 0-10

3. **Design System** — generates `DESIGN_SYSTEM.md` with:
   - Product mood + audience
   - Color token table (token → hex)
   - Typography hierarchy
   - Spacing scale
   - Breakpoints
   - Animation tokens
   - Anti-pattern blacklist
   - Accessibility requirements

4. **Fix Plan** — actionable CSS replacements:
   - `#xxxxxx` → `var(--semantic-token)`
   - Add missing `:focus-visible` rules
   - Add missing `[data-theme=dark]` block
   - Add `prefers-reduced-motion` query

## Anti-Patterns Enforced

- ❌ No purple/blue gradients (AI-generic)
- ❌ No oversized border-radius (>12px without purpose)
- ❌ No neon glow box-shadows
- ❌ No backdrop-filter: blur() > 12px
- ❌ No gratuitous animations (float/pulse/glow/shimmer)
- ❌ No hardcoded hex values outside :root token definitions
- ❌ No missing loading/empty/error states
- ❌ No placeholder buttons or mock data in production paths
- ❌ No copy-cat Spotify/Netflix UI without adaptation
- ❌ No random font stacks or inconsistent hierarchy

## Integration

The Taste Director is an A2A agent with AgentCard at `/v1/agents/taste-director`.
It can be invoked:
- Manually via `POST /v1/tasks` with `skillId: "taste:audit"`
- Automatically after each Auto Loop iteration (if `tasteReviewEnabled` flag)
- As part of the Definition of Done gate (score ≥ 8.5 required)

## Output Artifacts

- `DESIGN_SYSTEM.md` (TextPart artifact)
- `TasteViolationReport` (DataPart artifact)
- `FixPlan` (DataPart artifact with file:line:replacement entries)
