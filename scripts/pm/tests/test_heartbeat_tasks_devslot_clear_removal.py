#!/usr/bin/env python3
"""Contract tests for the heartbeat-tasks dev-slot clear removal.

The 3h heartbeat must never clear a numbered slot. Dev-slot clearing belongs to
the new-issue assignment boundary (the atomic `mop-assign-slot` operation), and
only the PM session keeps a cadence clear path.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import re
import stat
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SKILL_PATH = SHARED / "claude" / "skills" / "heartbeat-tasks" / "SKILL.md"
MANIFEST_PATH = SHARED / "manifest.json"
RESOLVER_PATH = SHARED / "claude" / "scripts" / "pm" / "control-plane" / "sakshi-heartbeat.py"

TARGET = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/heartbeat-tasks/SKILL.md"
PRODUCER_PATH = SHARED / "claude" / "scripts" / "pm" / "control-plane" / "sakshi-heartbeat.py"
SESSION_AGE_SKILL_PATH = SHARED / "claude" / "skills" / "session-age-clear" / "SKILL.md"
SESSION_AGE_TARGET = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/session-age-clear/SKILL.md"
HEARTBEAT_CLEAR_PY_PATH = SHARED / "claude" / "scripts" / "heartbeat-session-age-clear.py"
HEARTBEAT_CLEAR_TARGET = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/heartbeat-session-age-clear.py"
DEV_SLOT_IDS = ("1", "2", "3", "4", "5", "6")


def _load_producer():
    """Import the real producer with the runtime-observation shim stubbed out."""
    spec = importlib.util.spec_from_file_location("sakshi_heartbeat_cadence_test", PRODUCER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    stub = types.ModuleType("control_plane.runtime_observation")
    stub.RuntimeObservationAdapter = object
    stub.parse_timestamp = lambda value: value
    package = types.ModuleType("control_plane")
    package.runtime_observation = stub
    sys.modules.setdefault("control_plane", package)
    sys.modules["control_plane.runtime_observation"] = stub
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PRODUCER = _load_producer()


def _session_row(slot_id: str, label: str, age: str = "4h 00m") -> dict:
    return {
        "id": slot_id,
        "label": label,
        "clear_due": True,
        "clear_already_requested": False,
        "age": age,
        "present": True,
        "severity": "warning",
    }


def _minimal_report_data(sessions: list[dict]) -> dict:
    return {
        "now_ist": "2026-09-21 23:00 IST",
        "axiom": {},
        "sessions": sessions,
        "tmux": {},
        "process_sweep": {},
        "post_issue_latches": {"count": 0},
        "pr_drift": {"ok": False},
        "queue": {},
        "mop": {},
    }


def _code_skeleton(tree: ast.Module) -> str:
    """AST dump with every docstring removed, to prove a docstring-only fold."""
    clone = copy.deepcopy(tree)
    for node in ast.walk(clone):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.dump(clone)


class HeartbeatTasksDevSlotClearRemovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = SKILL_PATH.read_text(encoding="utf-8")
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_dev_slot_clearing_instructions_are_gone(self) -> None:
        for banned in (
            "--require-terminal",
            "dev-slots-first",
            "idle/free dev slot \u2192 heartbeat clears",
            "creates/updates\n  `SESSION_AGE_CLEAR_PENDING slot:N`",
        ):
            self.assertNotIn(banned, self.skill, f"dev-slot clearing text survived: {banned!r}")
        self.assertNotIn("idle-slot clears", self.skill)

    def test_no_instruction_produces_a_dev_slot_pending_row(self) -> None:
        # The token may appear only inside an explicit prohibition; never as an
        # instruction to create or act on a per-slot pending row.
        for match in re.finditer(r".{0,120}SESSION_AGE_CLEAR_PENDING slot:N.{0,60}", self.skill, re.DOTALL):
            window = match.group(0).lower()
            self.assertTrue(
                ("must not" in window) or ("do not" in window) or ("not be" in window) or ("exists for a dev slot" in window),
                f"non-prohibitive dev-slot pending-row text: {match.group(0)!r}",
            )

    def test_dev_slots_are_cleared_only_at_the_assignment_boundary(self) -> None:
        self.assertIn("new-issue assignment boundary", self.skill)
        self.assertIn("mop-assign-slot", self.skill)
        self.assertIn("NEVER cleared on a cadence", self.skill)

    def test_pm_self_clear_path_is_preserved(self) -> None:
        self.assertIn("SESSION_AGE_CLEAR_PENDING PM", self.skill)
        self.assertIn("mop-clear-slot.sh pm", self.skill)
        self.assertIn("pm-self-clear-stop", self.skill)
        self.assertIn("Due PM becomes a PM pending-clear row", self.skill)

    def test_three_hour_cadence_still_invokes_the_skill(self) -> None:
        cadence = (ROOT / "src" / "pmCadence.ts").read_text(encoding="utf-8")
        self.assertIn("Invoke Skill(heartbeat-tasks)", cadence)

    def test_canonical_source_is_manifest_mapped_to_the_resolver_target(self) -> None:
        rows = [r for r in self.manifest["entries"] if r["source_path"] == "claude/skills/heartbeat-tasks/SKILL.md"]
        self.assertEqual(len(rows), 1, "exactly one canonical heartbeat-tasks row")
        row = rows[0]
        self.assertEqual(row["canonical_target"], TARGET)
        self.assertEqual(row["mode"], 420)
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["dependencies"], [])
        self.assertEqual(
            row["sha256"],
            hashlib.sha256(SKILL_PATH.read_bytes()).hexdigest(),
            "manifest digest must match the canonical source bytes",
        )

    def test_resolver_prefers_exactly_this_target(self) -> None:
        resolver = RESOLVER_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'PROJECT_ROOT / ".claude/skills/heartbeat-tasks/SKILL.md"',
            resolver,
            "the resolver's preferred path must be the manifest target",
        )
        self.assertIn('PROJECT_ROOT = Path("/Users/rajiv/Downloads/projects/heydonna-app")', resolver)
        self.assertEqual(TARGET, "/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/heartbeat-tasks/SKILL.md")

    def test_manifest_count_and_order_are_deterministic(self) -> None:
        self.assertEqual(
            self.manifest["entries"],
            sorted(self.manifest["entries"], key=lambda item: item["source_path"]),
        )
        self.assertEqual(self.manifest["inventory"]["selected_count"], len(self.manifest["entries"]))
        self.assertEqual(self.manifest["inventory"]["ambiguous"], [])


class HeartbeatProducerCadenceEmissionTests(unittest.TestCase):
    """Behavioural proof that the producer no longer emits a dev-slot clear."""

    def _apply(self, sessions: list[dict]) -> list[dict]:
        rows = [dict(row) for row in sessions]
        PRODUCER.apply_clear_policy(rows, {})
        return rows

    def test_dev_slot_age_read_is_observation_only(self) -> None:
        dev, pm = self._apply([_session_row("1", "S1"), _session_row("pm", "PM", "7h 30m")])
        self.assertTrue(dev["clear_due"], "the dev-slot age read is retained")
        self.assertNotIn("clear_reason", dev, "no dev-slot clear reason text")
        self.assertFalse(dev["pm_clear_candidate"], "no dev-slot clear candidate flag")
        self.assertTrue(pm["pm_clear_candidate"], "PM keeps the clear candidate flag")
        self.assertIn("session-age-clear", pm["clear_reason"])

    def test_session_age_handoff_names_pm_only(self) -> None:
        rows = self._apply([_session_row("1", "S1"), _session_row("pm", "PM", "7h 30m")])
        handoff = [line for line in PRODUCER.format_session_age(rows) if "clear handoff" in line]
        self.assertEqual(len(handoff), 1, "exactly one clear-handoff line")
        self.assertIn("PM", handoff[0])
        self.assertNotIn("S1", handoff[0])

    def test_report_emits_no_dev_slot_clear_directive(self) -> None:
        for slot in DEV_SLOT_IDS + ("all",):
            with self.subTest(slot=slot):
                rows = self._apply([_session_row(slot, f"S{slot}")])
                actions = PRODUCER.build_report(_minimal_report_data(rows)).split("*Actions needed:*")[-1]
                self.assertNotIn("Session-age clear due", actions)
                self.assertNotIn("Skill(session-age-clear)", actions)
                self.assertNotIn("mop-clear-slot", actions)
                self.assertNotIn(f'for slot "{slot}"', actions)

    def test_report_still_renders_the_pm_due_path(self) -> None:
        rows = self._apply([_session_row("1", "S1"), _session_row("pm", "PM", "7h 30m")])
        actions = PRODUCER.build_report(_minimal_report_data(rows)).split("*Actions needed:*")[-1]
        self.assertIn("Session-age clear due for PM", actions)
        self.assertIn('Skill(session-age-clear) for slot "pm"', actions)
        self.assertNotIn('for slot "1"', actions)


class SessionAgeClearSkillFenceTests(unittest.TestCase):
    """The session-age-clear skill keeps the PM path and drops every dev-slot clear."""

    def setUp(self) -> None:
        self.skill = SESSION_AGE_SKILL_PATH.read_text(encoding="utf-8")
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_skill_no_longer_clears_or_queues_dev_slots(self) -> None:
        for banned in (
            'mop_clear_slot(slot: "1")',
            'mop_clear_slot(slot: "2")',
            "mop-clear-slot.sh 1",
            "mop-clear-slot.sh --require-terminal",
            "SESSION_AGE_CLEAR_PENDING slot",
            "clear_now",
            "dev-slots-first",
        ):
            self.assertNotIn(banned, self.skill, f"dev-slot clear construct survived: {banned!r}")

    def test_every_pending_row_mention_is_the_pm_row(self) -> None:
        for match in re.finditer(r".{0,120}SESSION_AGE_CLEAR_PENDING.{0,60}", self.skill, re.DOTALL):
            self.assertIn("PM", match.group(0), f"non-PM pending-row text: {match.group(0)!r}")

    def test_pm_self_clear_path_is_preserved(self) -> None:
        for needle in (
            "SESSION_AGE_CLEAR_PENDING PM",
            "mop-clear-slot.sh pm",
            "pm-self-clear-stop.sh",
            "pending_pm_todo",
        ):
            self.assertIn(needle, self.skill)

    def test_skill_names_the_sanctioned_assignment_boundary(self) -> None:
        self.assertIn("mop-assign-slot", self.skill)
        self.assertIn("new_issue", self.skill)

    def test_manifest_row_matches_the_adopted_and_fenced_bytes(self) -> None:
        rows = [
            row
            for row in self.manifest["entries"]
            if row["source_path"] == "claude/skills/session-age-clear/SKILL.md"
        ]
        self.assertEqual(len(rows), 1, "exactly one canonical session-age-clear row")
        row = rows[0]
        self.assertEqual(row["canonical_target"], SESSION_AGE_TARGET)
        self.assertEqual(row["mode"], 420)
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["dependencies"], [])
        self.assertEqual(row["sha256"], hashlib.sha256(SESSION_AGE_SKILL_PATH.read_bytes()).hexdigest())


class HeartbeatSessionAgeClearFoldTests(unittest.TestCase):
    """The materializer keeps its behaviour and loses only the stale docstring."""

    def setUp(self) -> None:
        self.source = HEARTBEAT_CLEAR_PY_PATH.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source, filename=str(HEARTBEAT_CLEAR_PY_PATH))
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_stale_cadence_clear_advertisement_is_gone(self) -> None:
        docstring = ast.get_docstring(self.tree) or ""
        self.assertNotIn("mop-clear-slot.sh --require-terminal", docstring)
        self.assertNotIn("mop-clear-slot.sh --require-terminal", self.source)
        self.assertIn("mop-assign-slot", docstring)

    def test_numbered_panes_remain_materialize_only(self) -> None:
        self.assertIn('"can_clear_now": False', self.source)
        self.assertIn('"materialized_only": True', self.source)

    def test_fold_is_docstring_only(self) -> None:
        live = Path(HEARTBEAT_CLEAR_TARGET)
        if not live.is_file():
            self.skipTest("installed target absent; preimage comparison unavailable")
        live_tree = ast.parse(live.read_text(encoding="utf-8"), filename=str(live))
        self.assertEqual(
            _code_skeleton(self.tree),
            _code_skeleton(live_tree),
            "the fold must not change any non-docstring statement",
        )

    def test_manifest_row_matches_the_folded_bytes(self) -> None:
        rows = [
            row
            for row in self.manifest["entries"]
            if row["source_path"] == "claude/scripts/heartbeat-session-age-clear.py"
        ]
        self.assertEqual(len(rows), 1, "exactly one canonical heartbeat-session-age-clear row")
        row = rows[0]
        self.assertEqual(row["canonical_target"], HEARTBEAT_CLEAR_TARGET)
        self.assertEqual(row["mode"], 493)
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["sha256"], hashlib.sha256(HEARTBEAT_CLEAR_PY_PATH.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(HEARTBEAT_CLEAR_PY_PATH.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
