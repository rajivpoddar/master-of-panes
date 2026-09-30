from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[3]
HOOK = ROOT / "scripts/pm/shared-assets/claude/hooks/validate-codex-review-agent-result.py"
AUDIT = ROOT / "scripts/pm/shared-assets/claude/hooks/pre-issue-create-audit.sh"
SPEC = importlib.util.spec_from_file_location("pm_posttooluse", HOOK)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class PmPostToolUseProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.marker_root = Path("/tmp/codex-review-companion")
        self.marker_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.now = int(time.time())
        self.issue = "pmf-shortcut-ctrl-k"
        self.baseline = "9e90b464c7e7e9f561aefeb4ec00accb8d35eff3"
        self.marker = self.marker_root / f"arch-issue-{self.issue}-{self.now}.md"
        self.marker.write_text(
            "\n".join(
                (
                    "VERDICT: NEEDS_DEEPER_INVESTIGATION",
                    "MARKER_PROVENANCE: codex-review-companion",
                    "TYPE: arch-review",
                    f"TIMESTAMP: {self.now}",
                    f"ISSUE: #{self.issue}",
                    f"BASELINE_SHA: {self.baseline}",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        self.payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "pm-parent-session-1",
            "cwd": MODULE.PM_PANE_CWD,
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": "codex-arch-reviewer",
                "prompt": f"Review issue {self.issue} against baseline SHA {self.baseline}.",
            },
            "tool_response": {
                "agentType": "codex-arch-reviewer",
                "output": f"Review complete. CANONICAL_MARKER: {self.marker}",
            },
        }

    def tearDown(self) -> None:
        self.marker.unlink(missing_ok=True)

    def db_row(self, event: dict, *, slot: int = 0) -> dict:
        normalized = {key: value for key, value in event.items() if key != "hook_event_name"}
        normalized["type"] = "PostToolUse"
        return {
            "slot": slot,
            "event_type": "PostToolUse",
            # MoP SQLite stores UTC without the trailing Z; the audit hook adds it.
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "payload": json.dumps(normalized),
        }

    def audit_count(self, rows: list[dict]) -> int:
        source = AUDIT.read_text(encoding="utf-8")
        opener = 'jq --arg ws "$WINDOW_START"'
        self.assertIn(opener, source, "locate the actual audit-reader jq predicate")
        start = source.index("'", source.index(opener) + len(opener)) + 1
        end = source.index("' 2>/dev/null", start)
        predicate = source[start:end]
        result = subprocess.run(
            ["jq", "--arg", "ws", str(int(time.time()) - MODULE.PM_REVIEW_WINDOW_SECONDS), predicate],
            input=json.dumps({"events": rows}),
            text=True,
            capture_output=True,
            check=True,
        )
        return int(result.stdout.strip())

    def test_valid_pm_completion_posts_the_shape_read_by_the_existing_audit_gate(self) -> None:
        event = MODULE.pm_arch_review_event(self.payload, now=self.now)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["hook_event_name"], "PostToolUse")
        self.assertEqual(event["session_id"], "pm-parent-session-1")
        self.assertEqual(event["cwd"], MODULE.PM_PANE_CWD)
        self.assertEqual(event["tool_name"], "Agent")
        self.assertEqual(event["tool_input"]["subagent_type"], "codex-arch-reviewer")
        self.assertIn(self.issue, event["tool_input"]["prompt"])
        self.assertIn(self.baseline, event["tool_input"]["prompt"])
        # Deliberately omit tool_output: MoP's normal Agent completion handler
        # would otherwise inject a duplicate subagent-completed notice to PM.
        self.assertNotIn("tool_output", event)

        sent = []

        def capture(request, timeout):
            sent.append((request, timeout))
            return Response()

        with patch.object(MODULE.urllib.request, "urlopen", capture):
            self.assertTrue(MODULE.post_pm_arch_review(self.payload))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], 3)
        transmitted = json.loads(sent[0][0].data)
        self.assertEqual(transmitted, event)
        self.assertEqual(self.audit_count([self.db_row(transmitted)]), 1)

    def test_unrelated_or_unbound_completion_never_posts_or_satisfies_the_gate(self) -> None:
        invalid_payloads = []
        for update in (
            {"cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3005"},
            {"tool_name": "Bash"},
            {"session_id": ""},
            {"tool_input": {"subagent_type": "general-purpose", "prompt": self.payload["tool_input"]["prompt"]}},
            {"tool_response": {"agentType": "general-purpose", "output": f"CANONICAL_MARKER: {self.marker}"}},
            {"tool_response": {"agentType": "codex-arch-reviewer", "output": "no marker path returned"}},
            {"tool_input": {"subagent_type": "codex-arch-reviewer", "prompt": "Review a different issue."}},
            {"tool_input": {"subagent_type": "codex-arch-reviewer", "prompt": f"{self.issue} baseline SHA {'a' * 40}"}},
        ):
            invalid_payloads.append({**self.payload, **update})

        stale_marker = self.marker.read_text(encoding="utf-8").replace(
            f"TIMESTAMP: {self.now}", f"TIMESTAMP: {self.now - MODULE.PM_REVIEW_WINDOW_SECONDS - 1}"
        )
        self.marker.write_text(stale_marker, encoding="utf-8")
        invalid_payloads.append(self.payload)
        calls = []
        with patch.object(MODULE.urllib.request, "urlopen", lambda *args, **kwargs: calls.append(args)):
            for payload in invalid_payloads:
                self.assertIsNone(MODULE.pm_arch_review_event(payload, now=self.now))
                self.assertFalse(MODULE.post_pm_arch_review(payload))
        self.assertEqual(calls, [])
        self.assertEqual(self.audit_count([]), 0)

    def test_duplicate_observations_do_not_amplify_the_audit_decision(self) -> None:
        event = MODULE.pm_arch_review_event(self.payload, now=self.now)
        assert event is not None
        row = self.db_row(event)
        once = self.audit_count([row])
        twice = self.audit_count([row, row])
        self.assertEqual(once, 1)
        self.assertEqual(twice, 2)
        self.assertEqual(once > 0, twice > 0, "the existing zero/nonzero gate decision is idempotent")


if __name__ == "__main__":
    unittest.main()
