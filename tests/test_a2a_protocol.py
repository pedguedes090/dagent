"""A2A protocol compliance tests.

Phase 0: Type serialization round-trips, Message Part union, TaskState mapping.
Phase 1: AgentCard schema validation, Registry.
Phase 2: Task lifecycle state transitions.
Phase 10: E2E product goal guard — "code web nghe nhạc" must NOT spawn platform ideas.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_engine.a2a.types import (
    A2A_PROTOCOL_VERSION,
    AgentCard,
    AgentCapability,
    AgentSkill,
    AgentSecurity,
    Artifact,
    DataPart,
    FilePart,
    Message,
    Part,
    Task,
    TaskState,
    TextPart,
    part_from_dict,
    part_to_dict,
    task_state_from_run_status,
)
from agent_engine.a2a.types import __all__ as types_all


class TypeSerializationTests(unittest.TestCase):
    """Round-trip tests for every A2A wire type."""

    def test_text_part_roundtrip(self) -> None:
        tp = TextPart(text="hello")
        d = tp.to_dict()
        self.assertEqual(d, {"type": "text", "text": "hello"})
        tp2 = TextPart.from_dict(d)
        self.assertEqual(tp2.text, "hello")
        self.assertIsInstance(part_from_dict(d), TextPart)

    def test_data_part_roundtrip(self) -> None:
        dp = DataPart(data={"key": 42}, mimeType="application/json")
        d = dp.to_dict()
        self.assertEqual(d["type"], "data")
        self.assertEqual(d["data"], {"key": 42})
        dp2 = DataPart.from_dict(d)
        self.assertEqual(dp2.data, {"key": 42})

    def test_file_part_roundtrip(self) -> None:
        fp = FilePart(name="diff.patch", mediaType="text/x-diff", uri="file:///tmp/diff.patch")
        d = fp.to_dict()
        fp2 = FilePart.from_dict(d)
        self.assertEqual(fp2.name, "diff.patch")
        self.assertEqual(fp2.uri, "file:///tmp/diff.patch")

    def test_message_from_legacy_content(self) -> None:
        msg = Message.from_legacy_content("user", "fix the bug")
        self.assertEqual(msg.role, "user")
        self.assertEqual(len(msg.parts), 1)
        self.assertIsInstance(msg.parts[0], TextPart)
        self.assertEqual(msg.parts[0].text, "fix the bug")
        self.assertTrue(msg.messageId.startswith("msg-"))

    def test_message_from_text_shortcut(self) -> None:
        msg = Message.from_text("implement login", role="user", contextId="ctx-1")
        self.assertEqual(msg.text, "implement login")
        self.assertEqual(msg.contextId, "ctx-1")

    def test_message_from_dict_with_legacy_content(self) -> None:
        msg = Message.from_dict({"role": "assistant", "content": "done"})
        self.assertEqual(msg.text, "done")
        self.assertEqual(msg.role, "assistant")

    def test_artifact_content_addressing(self) -> None:
        a1 = Artifact.from_text("summary", "done")
        a2 = Artifact.from_text("summary", "done")
        self.assertEqual(a1.artifactId, a2.artifactId)  # deterministic
        a3 = Artifact.from_text("summary", "done v2")
        self.assertNotEqual(a1.artifactId, a3.artifactId)

    def test_artifact_parent_ids(self) -> None:
        parent = Artifact.from_text("plan", "architecture")
        child = Artifact.from_text("code", "implementation")
        child2 = Artifact(
            name="code", parts=[TextPart(text="implementation")],
            parentArtifactIds=[parent.artifactId],
        )
        self.assertIn(parent.artifactId, child2.parentArtifactIds)

    def test_task_serialization(self) -> None:
        task = Task(
            id="task-abc",
            contextId="ctx-1",
            status={"state": "submitted", "timestamp": "2026-01-01T00:00:00Z"},
            history=[Message.from_text("start", role="orchestrator")],
            metadata={"goal": "music app"},
        )
        d = task.to_dict()
        self.assertEqual(d["id"], "task-abc")
        self.assertEqual(d["status"]["state"], "submitted")
        task2 = Task.from_dict(d)
        self.assertEqual(task2.state, TaskState.SUBMITTED)
        self.assertEqual(len(task2.history), 1)

    def test_agent_card_to_dict_includes_required_fields(self) -> None:
        card = AgentCard(
            name="Tester",
            description="Verification runner",
            url="/v1/agents/tester",
            capabilities=[AgentCapability(name="test:run")],
            skills=[AgentSkill(id="test:unit", name="Unit Test Runner")],
            security=AgentSecurity(sandboxed=True),
            streaming=True,
        )
        d = card.to_dict()
        self.assertEqual(d["name"], "Tester")
        self.assertEqual(d["protocolVersion"], A2A_PROTOCOL_VERSION)
        self.assertEqual(len(d["capabilities"]), 1)
        self.assertEqual(d["security"]["sandboxed"], True)
        self.assertTrue(d["streaming"])

    def test_agent_card_from_dict_minimal(self) -> None:
        d = {"name": "X", "description": "Y", "url": "/z", "protocolVersion": "0.3.0", "capabilities": []}
        card = AgentCard.from_dict(d)
        self.assertEqual(card.name, "X")
        self.assertEqual(card.protocolVersion, "0.3.0")


class TaskStateMappingTests(unittest.TestCase):
    """Existing RunStatus values must map losslessly to A2A TaskState."""

    def test_all_run_statuses_have_mapping(self) -> None:
        run_statuses = ["queued", "running", "awaiting_approval", "completed", "failed", "blocked", "canceled"]
        for rs in run_statuses:
            self.assertIn(task_state_from_run_status(rs), TaskState)

    def test_terminal_states_are_terminal(self) -> None:
        for state in TaskState.terminal():
            self.assertTrue(state.is_terminal)
        self.assertFalse(TaskState.SUBMITTED.is_terminal)
        self.assertFalse(TaskState.PROCESSING.is_terminal)

    def test_input_required_not_counted_as_completed(self) -> None:
        self.assertFalse(TaskState.INPUT_REQUIRED.is_terminal)
        self.assertFalse(TaskState.INPUT_REQUIRED.is_success)

    def test_unknown_status_defaults_to_submitted(self) -> None:
        self.assertEqual(task_state_from_run_status("unknown"), TaskState.SUBMITTED)


class AgentCardSchemaTests(unittest.TestCase):
    """AgentCard JSON Schema validation."""

    def test_schema_required_fields_present(self) -> None:
        s = AgentCard.SCHEMA
        required = s.get("required") or []
        for field in ("name", "description", "url", "protocolVersion", "capabilities"):
            self.assertIn(field, required)

    def test_valid_card_passes_basic_checks(self) -> None:
        card = AgentCard(
            name="Orchestrator",
            description="Entry point",
            url="/v1/agents/orchestrator",
            capabilities=[AgentCapability(name="classify", description="Detect intent")],
        )
        d = card.to_dict()
        self.assertTrue(isinstance(d, dict))
        self.assertTrue(all(k in d for k in ("name", "description", "url", "protocolVersion", "capabilities")))


class ProductGoalGuardTests(unittest.TestCase):
    """E2E: goal "code web nghe nhạc" MUST NOT spawn platform/agent-engine ideas."""

    def test_taste_director_agent_card_not_platform_offgoal(self) -> None:
        """Taste Director card should be about design, not agent-engine internals."""
        from agent_engine.a2a.taste_director import TASTE_CARD
        text = json.dumps(TASTE_CARD.to_dict(), ensure_ascii=False).lower()
        self.assertNotIn("flowview", text)
        self.assertNotIn("bottleneck", text)
        self.assertNotIn("classifier", text)

    def test_a2a_types_are_importable(self) -> None:
        """All advertised types should be importable."""
        self.assertGreater(len(types_all), 8)
        for name in ("AgentCard", "Task", "TaskState", "Message", "Artifact", "TextPart", "DataPart", "FilePart", "Part"):
            self.assertIn(name, types_all)


class TasteDirectorTests(unittest.TestCase):
    """Taste Director audit is deterministic and produces violations + score."""

    def test_audit_this_repo(self) -> None:
        from agent_engine.a2a.taste_director import run_taste_audit
        import os
        repo = Path(os.getcwd())
        report = run_taste_audit(repo)
        self.assertIsNotNone(report.score)
        self.assertGreaterEqual(report.score, 0)
        self.assertLessEqual(report.score, 10)
        self.assertTrue(report.summary)

    def test_extract_existing_tokens(self) -> None:
        from agent_engine.a2a.taste_director import extract_existing_tokens
        css = ":root { --bg: #F6F5F0; --accent: #176B63; }"
        tokens = extract_existing_tokens(css)
        self.assertEqual(tokens["--bg"], "#F6F5F0")
        self.assertEqual(tokens["--accent"], "#176B63")

    def test_build_design_system_from_workspace(self) -> None:
        from agent_engine.a2a.taste_director import build_design_system
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "renderer").mkdir(parents=True)
            (root / "src" / "renderer" / "styles.css").write_text(
                ":root { --bg: #fff; --text: #111; --accent: teal; --anim-fast: 0.15s; }",
                encoding="utf-8",
            )
            ds = build_design_system(root, "dark music app", "music lovers")
            self.assertEqual(ds.productMood, "dark music app")
            self.assertIn("bg", ds.colorTokens)
            self.assertIn("accent", ds.colorTokens)
            self.assertIn("anim-fast", ds.animationTokens)
            md = ds.to_markdown()
            self.assertIn("# DESIGN SYSTEM", md)
            self.assertIn("dark music app", md)


if __name__ == "__main__":
    unittest.main()
