from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
CONTINUITY = SHARED / "claude" / "scripts" / "pm-terminal-continuity.py"
AUTOMATION = SHARED / "claude" / "scripts" / "pm-merges-automation-update.py"
PROMPT = SHARED / "codex" / "automations" / "pr-merges-residency-heartbeat" / "prompt.template"
MONITOR = SHARED / "codex" / "monitors" / "heydonna-pm-chat" / "MONITOR.md"
SOP = SHARED / "codex" / "monitors" / "heydonna-pm-chat" / "WAKE_SOP.md"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load(CONTINUITY, "pm_terminal_continuity")
AUTOMATION_MODULE = load(AUTOMATION, "pm_merges_automation_update")


def envelope(terminal_type: str = "FAILED_RUN_INVESTIGATION", head: str = "a" * 40, receipt: str = "source-1"):
    return {
        "terminal_type": terminal_type,
        "pr": 7599,
        "head": head,
        "run_or_capture": "run-1",
        "owner": "pm",
        "evidence_summary": "bounded evidence",
        "next_action": "route causal investigation",
        "next_owner": "CTO_DECISIONS",
        "wake": "immediate_cto_once",
        "source_receipt": receipt,
    }


class PMTerminalContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "continuity.json"
        self.env = {**os.environ, "PM_TERMINAL_CONTINUITY_STATE": str(self.state)}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, command: str, value: dict, *extra: str) -> dict:
        result = subprocess.run(
            [sys.executable, str(CONTINUITY), command, *extra],
            input=json.dumps(value), text=True, capture_output=True, env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_all_six_terminal_types_emit_and_route_once(self) -> None:
        for index, terminal_type in enumerate(sorted(MODULE.TERMINAL_TYPES)):
            result = self.call("route", envelope(terminal_type=terminal_type, receipt=f"source-{index}"))
            self.assertEqual(result["status"], "EMITTED")
            self.assertEqual(result["route"], "CTO_DECISIONS")
            duplicate = self.call("route", envelope(terminal_type=terminal_type, receipt=f"source-{index}"))
            self.assertEqual(duplicate["status"], "DUPLICATE_SUPPRESSED")

    def test_response_loss_is_durable_and_changed_key_is_distinct(self) -> None:
        first = envelope()
        result = subprocess.run(
            [sys.executable, str(CONTINUITY), "emit", "--response-lost"],
            input=json.dumps(first), text=True, capture_output=True, env=self.env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "UNCERTAIN")
        replay = self.call("emit", first)
        self.assertEqual(replay["status"], "DUPLICATE_SUPPRESSED")
        repair = self.call("hourly-repair", first)
        self.assertEqual(repair["status"], "UNCERTAIN_SUPPRESSED")
        changed_head = self.call("emit", envelope(head="b" * 40))
        self.assertEqual(changed_head["status"], "EMITTED")

    def test_route_parses_and_wakes_cto_once(self) -> None:
        value = envelope(receipt="route-1")
        routed = self.call("route", value)
        self.assertEqual(routed["status"], "EMITTED")
        self.assertEqual(routed["route"], "CTO_DECISIONS")
        duplicate = self.call("route", value)
        self.assertEqual(duplicate["status"], "DUPLICATE_SUPPRESSED")

    def test_consumption_and_next_edge_bind_exact_key(self) -> None:
        value = envelope()
        self.call("emit", value)
        consumed = self.call("consume", value, "--receipt", "cto-1")
        self.assertEqual(consumed["status"], "BOUND")
        edge = self.call("edge", value, "--receipt", "edge-1")
        self.assertEqual(edge["status"], "BOUND")

    def test_hourly_repair_is_exactly_once_only_without_continuation(self) -> None:
        value = envelope()
        self.call("emit", value)
        first = self.call("hourly-repair", value)
        self.assertEqual(first["status"], "HOURLY_REPAIR")
        second = self.call("hourly-repair", value)
        self.assertEqual(second["status"], "REPAIR_ALREADY_USED")

    def test_invalid_envelope_refuses_without_state(self) -> None:
        bad = envelope(head="not-a-head")
        result = subprocess.run(
            [sys.executable, str(CONTINUITY), "emit"], input=json.dumps(bad), text=True,
            capture_output=True, env=self.env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "REFUSED")
        self.assertFalse(self.state.exists())

    def test_automation_adapter_preserves_scheduler_metadata(self) -> None:
        config = Path(self.temp.name) / "automation.toml"
        config.write_text(
            'id = "pr-merges-residency-heartbeat"\nkind = "heartbeat"\n'
            'name = "PR Merges hourly open-PR audit"\nstatus = "ACTIVE"\n'
            'rrule = "FREQ=HOURLY;INTERVAL=1;BYMINUTE=12"\n'
            'target_thread_id = "01a0324b-68e0-7491-988f-e7e1549f16f7"\n'
            'created_at = 1\nupdated_at = 2\n', encoding="utf-8"
        )
        payload = AUTOMATION_MODULE.render(config, PROMPT)
        self.assertEqual(payload["mode"], "update")
        self.assertIn("terminal continuity", payload["prompt"].lower())
        self.assertEqual(payload["preserve"][:6], ["id", "kind", "name", "status", "rrule", "targetThreadId"])
        self.assertEqual(tomllib.loads(config.read_text())["created_at"], 1)

    def test_docs_bind_real_executable_boundary(self) -> None:
        for text in (MONITOR.read_text(), SOP.read_text(), PROMPT.read_text()):
            self.assertIn("pm-terminal-continuity.py", text)
        self.assertIn("pm-merges-automation-update.py", PROMPT.read_text())


if __name__ == "__main__":
    unittest.main()
