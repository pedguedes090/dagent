from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_engine.autonomy import select_next_task
from agent_engine.idea_council import (
    CapabilityMap,
    CapabilityMapEntry,
    PROPOSAL_KEYS,
    arbiter_score,
    critic_reject,
    fingerprint_proposal,
    invoke_council,
    load_council_state,
    memory_namespace,
    run_council_round,
    save_council_state,
    scan_capabilities,
)


def _fixture_music_workspace(root: Path) -> Path:
    """Create a minimal music app workspace mimicking what the worker scaffolds.
    NO catalog/search/player yet — capability map should report them missing."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        "<!doctype html><html><body><div id='app'></div><script type=module src='./src/main.js'></script></body></html>",
        encoding="utf-8",
    )
    (root / "package.json").write_text(json.dumps({"name": "music-app"}), encoding="utf-8")
    src = root / "src"
    src.mkdir()
    (src / "main.js").write_text(
        "import { App } from './App.js';\n"
        "function App(){ return '<h1>Music</h1>'; }\n"
        "document.getElementById('app').innerHTML = App();\n",
        encoding="utf-8",
    )
    (src / "App.js").write_text(
        "export function App(){ return '<h1>Music</h1>'; }\n",
        encoding="utf-8",
    )
    return root


def _valid_idea(**overrides):
    base = {
        "title": "Audio player with play/pause/seek",
        "proposer": "product",
        "productCapability": "audio_playback",
        "userProblem": "Users cannot listen to tracks.",
        "goalConnection": "Core to a music web app.",
        "repositoryEvidence": [
            {"file": "src/App.js", "symbol": "App", "observation": "No <audio> element rendered yet."}
        ],
        "browserEvidence": [],
        "expectedUserValue": "User can play the first sample track.",
        "implementationScope": ["src/Player.js", "src/App.js"],
        "acceptanceCriteria": ["Click play starts audio", "Click pause stops audio"],
        "verificationPlan": ["Open in browser, click play, hear sound"],
        "risks": [],
        "dependencies": [],
        "estimatedEffort": 2,
        "goalRelevance": 95,
        "userValue": 90,
        "evidenceStrength": 80,
        "productCompleteness": 80,
        "feasibility": 90,
    }
    base.update(overrides)
    return base


class CapabilityScannerTests(unittest.TestCase):
    def test_scan_detects_missing_audio_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture_music_workspace(Path(tmp))
            cap = scan_capabilities(str(root), "code web nghe nhạc")
            audio = next(c for c in cap.capabilities if c.name == "audio_playback")
            self.assertEqual(audio.status, "missing")
            ui = next(c for c in cap.capabilities if c.name == "ui_entrypoint")
            self.assertNotEqual(ui.status, "missing")

    def test_scan_resolves_product_root_when_workspace_is_repo_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            inner = parent / "nghe-nhac-app"
            inner.mkdir()
            _fixture_music_workspace(inner)
            cap = scan_capabilities(str(parent), "code web nghe nhạc")
            self.assertTrue(cap.productRoot.endswith("nghe-nhac-app"))


class CriticTests(unittest.TestCase):
    def _cap_map(self) -> CapabilityMap:
        return CapabilityMap(
            productRoot="/x",
            capabilities=[
                CapabilityMapEntry(name="audio_playback", status="missing"),
                CapabilityMapEntry(name="search", status="verified"),
            ],
        )

    def test_rejects_offgoal_agent_platform_idea(self) -> None:
        idea = _valid_idea(title="Improve FlowView bottleneck export panel", productCapability="agent_observability")
        reason = critic_reject(
            idea, goal="code web nghe nhạc", capability_map=self._cap_map(),
            completed_fingerprints=set(), rejected_fingerprints=set(), is_scaffold_iteration=False,
        )
        self.assertIsNotNone(reason)
        self.assertIn("off-goal", reason)

    def test_rejects_smoke_test_only_idea(self) -> None:
        idea = _valid_idea(title="Add classifier smoke test script", productCapability="testing")
        reason = critic_reject(
            idea, goal="code web nghe nhạc", capability_map=self._cap_map(),
            completed_fingerprints=set(), rejected_fingerprints=set(), is_scaffold_iteration=False,
        )
        self.assertIsNotNone(reason)

    def test_rejects_no_evidence_non_scaffold(self) -> None:
        idea = _valid_idea(repositoryEvidence=[], browserEvidence=[])
        reason = critic_reject(
            idea, goal="code web nghe nhạc", capability_map=self._cap_map(),
            completed_fingerprints=set(), rejected_fingerprints=set(), is_scaffold_iteration=False,
        )
        self.assertIsNotNone(reason)

    def test_accepts_no_evidence_when_scaffold(self) -> None:
        idea = _valid_idea(repositoryEvidence=[], browserEvidence=[])
        reason = critic_reject(
            idea, goal="code web nghe nhạc", capability_map=self._cap_map(),
            completed_fingerprints=set(), rejected_fingerprints=set(), is_scaffold_iteration=True,
        )
        self.assertIsNone(reason)

    def test_rejects_verified_capability(self) -> None:
        idea = _valid_idea(productCapability="search")
        reason = critic_reject(
            idea, goal="code web nghe nhạc", capability_map=self._cap_map(),
            completed_fingerprints=set(), rejected_fingerprints=set(), is_scaffold_iteration=False,
        )
        self.assertIsNotNone(reason)
        self.assertIn("already verified", reason)

    def test_rejects_duplicate_completed(self) -> None:
        idea = _valid_idea()
        fp = fingerprint_proposal("code web nghe nhạc", idea)
        reason = critic_reject(
            idea, goal="code web nghe nhạc", capability_map=self._cap_map(),
            completed_fingerprints={fp}, rejected_fingerprints=set(), is_scaffold_iteration=False,
        )
        self.assertIsNotNone(reason)


class ArbiterTests(unittest.TestCase):
    def test_score_in_range(self) -> None:
        idea = _valid_idea()
        score, breakdown = arbiter_score(idea)
        self.assertGreaterEqual(score, 75)
        self.assertLessEqual(score, 100)
        self.assertEqual(sum(breakdown.values()), score)

    def test_score_penalizes_risks(self) -> None:
        baseline_score, _ = arbiter_score(_valid_idea(risks=[]))
        risky_score, _ = arbiter_score(_valid_idea(risks=["a", "b", "c", "d", "e"]))
        self.assertGreater(baseline_score, risky_score)


class CouncilOrchestrationTests(unittest.TestCase):
    def test_invoke_council_each_agent_isolated(self) -> None:
        seen_agents: list[str] = []

        def fake_chat(messages, temperature, json_mode):
            sys = next((m["content"] for m in messages if m["role"] == "system"), "")
            agent = "product"
            for candidate in ("PRODUCT", "UX", "FRONTEND", "ARCHITECT", "QA", "SECURITY_PERFORMANCE"):
                if candidate in sys:
                    agent = candidate.lower()
                    if candidate == "SECURITY_PERFORMANCE":
                        agent = "security_performance"
                    break
            seen_agents.append(agent)
            return json.dumps({"ideas": [_valid_idea(proposer=agent, title=f"{agent}-idea")]})

        cap = CapabilityMap(
            productRoot="/x",
            components=[{"name": "App", "file": "src/App.js"}],
            capabilities=[CapabilityMapEntry(name="audio_playback", status="missing")],
        )
        from agent_engine.idea_council import build_council_context, PROPOSER_AGENTS
        ctx = build_council_context(
            goal="code web nghe nhạc", capability_map=cap,
            iteration_history=[], completed_ideas=[], rejected_ideas=[],
        )
        kept, _ = invoke_council(context=ctx, chat=fake_chat, is_scaffold_iteration=False)
        self.assertEqual(set(seen_agents), set(PROPOSER_AGENTS))
        self.assertEqual(len(kept), len(PROPOSER_AGENTS))

    def test_run_council_round_selects_highest_score_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)

            def fake_chat(messages, temperature, json_mode):
                sys = next((m["content"] for m in messages if m["role"] == "system"), "")
                if "PRODUCT" in sys:
                    return json.dumps({"ideas": [_valid_idea(title="High-value player", goalRelevance=100, userValue=100)]})
                return json.dumps({"ideas": []})

            cap = scan_capabilities(str(_fixture_music_workspace(sd / "ws")), "code web nghe nhạc")
            result = run_council_round(
                goal="code web nghe nhạc", session_id="s1", workspace_path=str(sd / "ws"),
                state_dir=sd, capability_map=cap, iteration=1, iteration_history=[], chat=fake_chat,
            )
            self.assertIsNotNone(result["winner"])
            self.assertGreaterEqual(result["winner"]["score"], 75)

    def test_run_council_round_returns_no_winner_when_all_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)

            def fake_chat(messages, temperature, json_mode):
                return json.dumps({"ideas": [_valid_idea(title="Improve FlowView", productCapability="flow_view")]})

            cap = scan_capabilities(str(_fixture_music_workspace(sd / "ws")), "code web nghe nhạc")
            result = run_council_round(
                goal="code web nghe nhạc", session_id="s2", workspace_path=str(sd / "ws"),
                state_dir=sd, capability_map=cap, iteration=1, iteration_history=[], chat=fake_chat,
            )
            self.assertIsNone(result["winner"])
            self.assertTrue(any(p["rejected"] for p in result["proposals"]))


class MemoryNamespaceTests(unittest.TestCase):
    def test_namespace_differs_per_goal_session_workspace(self) -> None:
        a = memory_namespace("code web nghe nhạc", "sess-1", "/ws/a")
        b = memory_namespace("code web nghe nhạc", "sess-2", "/ws/a")
        c = memory_namespace("code web xem phim", "sess-1", "/ws/a")
        d = memory_namespace("code web nghe nhạc", "sess-1", "/ws/b")
        self.assertEqual(len({a, b, c, d}), 4)

    def test_load_save_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            ns = memory_namespace("g", "s", "/w")
            save_council_state(sd, ns, {"completedFingerprints": ["fp1"], "rejectedFingerprints": []})
            loaded = load_council_state(sd, ns)
            self.assertIn("fp1", loaded["completedFingerprints"])

    def test_namespace_isolation_does_not_leak_completed_ideas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            ns1 = memory_namespace("g1", "s1", "/w")
            ns2 = memory_namespace("g2", "s1", "/w")
            save_council_state(sd, ns1, {"completedFingerprints": ["a", "b"]})
            self.assertEqual(load_council_state(sd, ns2).get("completedFingerprints"), [])


class SelectNextTaskCouncilTests(unittest.TestCase):
    def test_routes_to_council_when_goal_and_workspace_not_agent_engine(self) -> None:
        called = {"flag": False}

        def fake_council(*, goal, iteration, iteration_history, session_id, workspace_path, anti_halt_instruction="", **__):
            called["flag"] = True
            return {
                "winner": {
                    "raw": {
                        "title": "Audio player",
                        "productCapability": "audio_playback",
                        "formattedTask": "AUTONOMOUS PRODUCT ITERATION\nimplement audio",
                    },
                    "fingerprint": "abc123",
                    "score": 90,
                    "scoreBreakdown": {},
                }
            }

        task = select_next_task(
            {"workspacePath": "/tmp/nghe-nhac-app"}, completed_ids=set(),
            product_goal="code web nghe nhạc", iteration=1,
            council_round=fake_council, session_id="s", workspace_path="/tmp/nghe-nhac-app",
        )
        self.assertTrue(called["flag"])
        self.assertIsNotNone(task)
        self.assertEqual(task["kind"], "council_idea")
        self.assertIn("AUTONOMOUS PRODUCT ITERATION", task["task"])

    def test_iter_zero_returns_user_goal_verbatim(self) -> None:
        task = select_next_task(
            {"workspacePath": "/tmp/x"}, completed_ids=set(),
            product_goal="code web nghe nhạc", iteration=0,
            council_round=lambda **_: None, session_id="s", workspace_path="/tmp/x",
        )
        self.assertEqual(task["kind"], "user_goal")
        self.assertEqual(task["task"], "code web nghe nhạc")

    def test_no_winner_returns_none_no_hardcoded_fallback(self) -> None:
        task = select_next_task(
            {"workspacePath": "/tmp/x"}, completed_ids=set(),
            product_goal="code web nghe nhạc", iteration=1,
            council_round=lambda **_: {"winner": None, "proposals": []},
            session_id="s", workspace_path="/tmp/x",
        )
        self.assertIsNone(task)

    def test_product_goal_invokes_council_even_inside_agent_workspace(self) -> None:
        called = {"flag": False}

        def council(**_):
            called["flag"] = True
            return {"winner": None}

        task = select_next_task(
            {"workspacePath": "/repos/fractal-agent-system", "findings": [
                {"id": "f1", "category": "security", "title": "x", "source": "s", "priorityScore": 1.0},
            ]},
            completed_ids=set(), product_goal="any goal", iteration=1,
            council_round=council, session_id="s", workspace_path="/repos/fractal-agent-system",
        )
        self.assertTrue(called["flag"])
        self.assertIsNone(task)


class HardcodedBacklogAuditTests(unittest.TestCase):
    """Regression guard: the hard-coded enhancement pool must not survive."""

    def test_no_enhancement_ideas_constant(self) -> None:
        import agent_engine.autonomy as autonomy_mod
        self.assertFalse(hasattr(autonomy_mod, "_ENHANCEMENT_IDEAS"))
        self.assertFalse(hasattr(autonomy_mod, "_build_typed_pool"))
        self.assertFalse(hasattr(autonomy_mod, "_build_product_enhancement_pool"))

    def test_proposal_keys_are_complete(self) -> None:
        for required in (
            "title", "proposer", "productCapability", "userProblem",
            "goalConnection", "repositoryEvidence", "expectedUserValue",
            "acceptanceCriteria", "verificationPlan",
        ):
            self.assertIn(required, PROPOSAL_KEYS)


if __name__ == "__main__":
    unittest.main()
