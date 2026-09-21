#!/usr/bin/env python3
"""Focused proof for the Slice-B direct-assignment guard.

The hook must refuse every direct reach into MoP's assignment-ownership routes,
leave ordinary delivery and the explicit operator release usable, and pass the
sanctioned atomic operation through untouched using a narrow program-word allow
rather than a caller-controlled prose marker.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
HOOK = SHARED / "claude" / "hooks" / "block-direct-mop-assign.sh"
MANIFEST = SHARED / "manifest.json"

SANCTIONED = (
    "python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py --slot 2 --class new_issue "
    "--repo-id github:heydonna-app/heydonna-app --issue 8110 --branch fix/8110-x "
    f"--head {'a'*40} --work-kind implementation --handoff handoff-8110 --task-file /tmp/t.md"
)


def invoke(tool_name: str, command: str) -> dict:
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    completed = subprocess.run(
        ["bash", str(HOOK)], input=payload, capture_output=True, text=True, check=False,
    )
    self_rc = completed.returncode
    assert self_rc == 0, f"hook must always exit 0, got {self_rc}: {completed.stderr}"
    out = completed.stdout.strip()
    return json.loads(out) if out else {}


def blocked(tool_name: str, command: str) -> bool:
    return invoke(tool_name, command).get("decision") == "block"



def route_token_engaged(command: str) -> bool:
    """True when the hook's recognition layer fires for this command.

    Mirrors reaches_assign_ownership so the ALLOW cases can prove they are not no-ops.
    """
    import re
    return re.search(r"/slots/[^\s\"']*/(assign|adopt-issue-claim)([^A-Za-z0-9-]|$)", command) is not None

class BlockDirectMopAssignTests(unittest.TestCase):
    def test_hand_rolled_direct_assign_is_refused(self) -> None:
        self.assertTrue(blocked("Bash", 'curl -sS -X POST http://127.0.0.1:3100/slots/2/assign -d \'{"issue":8110}\''))
        self.assertTrue(blocked("Bash", 'curl -X POST "http://127.0.0.1:3100/slots/2/assign"'))

    def test_direct_adopt_issue_claim_is_refused(self) -> None:
        self.assertTrue(blocked("Bash", 'curl -X POST http://127.0.0.1:3100/slots/2/adopt-issue-claim -d @claim.json'))

    def test_refusal_names_the_sanctioned_path(self) -> None:
        payload = invoke("Bash", 'curl -X POST http://127.0.0.1:3100/slots/2/assign')
        self.assertIn("mop-assign-slot", payload.get("message", ""))

    def test_direct_ownership_mcp_wrapper_is_refused(self) -> None:
        for name in ("mcp__plugin_master-of-panes_mop__mop_assign_slot", "mcp__x__mop_adopt_issue_claim"):
            self.assertTrue(blocked(name, ""), f"{name} must be refused")

    def test_ordinary_non_ownership_tools_are_unaffected(self) -> None:
        for tok in ("Bash", "mcp__plugin_master-of-panes_mop__mop_send_to_slot", "Edit", "Read"):
            self.assertFalse(blocked(tok, "echo hello"), f"{tok} must not be blocked")

    def test_release_remains_usable(self) -> None:
        self.assertFalse(blocked("Bash", 'curl -X POST http://127.0.0.1:3100/slots/2/release -d @release.json'))
        self.assertFalse(blocked("mcp__plugin_master-of-panes_mop__mop_release_slot", ""))

    def test_sanctioned_operation_passes_through_untouched(self) -> None:
        self.assertFalse(blocked("Bash", SANCTIONED))

    def test_prose_mentioning_the_script_cannot_smuggle_a_direct_call(self) -> None:
        # A caller-controlled prose marker must not open the gate.
        self.assertTrue(blocked("Bash", 'curl -X POST http://127.0.0.1:3100/slots/2/assign  # use mop-assign-slot instead'))
        self.assertTrue(blocked("Bash", f'{SANCTIONED} && curl -X POST http://127.0.0.1:3100/slots/3/assign'))
        self.assertTrue(blocked("Bash", 'echo "mop-assign-slot" ; curl -X POST http://127.0.0.1:3100/slots/2/assign'))

    def test_assign_effect_endpoint_is_not_mistaken_for_the_blocked_route(self) -> None:
        self.assertFalse(blocked("Bash", 'curl -X POST http://127.0.0.1:3100/slots/2/assign-effect -d @effect.json'))

    def test_interior_newline_cannot_smuggle_a_second_call(self) -> None:
        # THE BYPASS: grep -Eq is line-oriented, so before the fix the anchored
        # match was satisfied by line 1 and the second line rode through.
        self.assertTrue(blocked("Bash", SANCTIONED + "\ncurl -X POST http://127.0.0.1:3100/slots/2/assign"))
        self.assertTrue(blocked("Bash", SANCTIONED + "\n" + 'curl -X POST http://127.0.0.1:3100/slots/2/assign'))

    def test_backslash_newline_continuation_is_also_refused(self) -> None:
        self.assertTrue(blocked("Bash", SANCTIONED + " \\\ncurl -X POST http://127.0.0.1:3100/slots/2/assign"))

    def test_trailing_newline_and_whitespace_still_allow_the_sanctioned_path(self) -> None:
        # Guards the other direction: the fix must not become a false-block.
        self.assertFalse(blocked("Bash", SANCTIONED + "\n"))
        self.assertFalse(blocked("Bash", SANCTIONED + "   \n"))
        self.assertFalse(blocked("Bash", SANCTIONED + "\t"))

    def test_recognition_layer_metacharacter_terminators(self) -> None:
        # LAYER 2: the terminator class previously excluded every unattached
        # shell metacharacter, so the branch never fired and the hook exited 0
        # before the pure predicate ran. `curl .../assign|jq .` was idiomatic.
        for shape in (
            "curl -X POST http://127.0.0.1:3100/slots/2/assign; echo done",
            "curl -sS -X POST http://127.0.0.1:3100/slots/2/assign|jq .",
            "curl -X POST http://127.0.0.1:3100/slots/2/assign&",
            "curl -X POST http://127.0.0.1:3100/slots/2/assign>/tmp/log",
            "curl -sS -X POST http://127.0.0.1:3100/slots/2/adopt-issue-claim|jq .",
        ):
            self.assertTrue(blocked("Bash", shape), shape)

    def test_recognition_controls_still_hold(self) -> None:
        self.assertTrue(blocked("Bash", "curl -X POST http://127.0.0.1:3100/slots/2/assign"))
        self.assertFalse(blocked("Bash", "curl -X POST http://127.0.0.1:3100/slots/2/assign-effect"))
        self.assertFalse(blocked("Bash", "echo check assignee report"))

    def test_substitution_cannot_carry_a_hidden_ownership_call(self) -> None:
        # LAYER 3: once recognition works, substitution must be refused too.
        self.assertTrue(blocked("Bash", SANCTIONED + " --task-file $(curl -X POST http://127.0.0.1:3100/slots/2/assign)"))
        self.assertTrue(blocked("Bash", "curl -X POST `echo http://127.0.0.1:3100/slots/2/assign`"))
        self.assertTrue(blocked("Bash", SANCTIONED + " --task-file <(curl -X POST http://127.0.0.1:3100/slots/2/assign)"))

    def test_legitimate_task_file_forms_still_allow(self) -> None:
        # The path must CONTAIN a route-recognizing token, or reaches_assign_ownership
        # returns false, the hook exits 0 and the predicate is never consulted - the
        # assertion would pass trivially and prove nothing. These paths deliberately
        # carry `/slots/2/assign` inside a spaced directory name, so the hook engages
        # and the predicate is the thing under test. This is the pair that guards the
        # bare-backslash decision: a backslash rejection would false-block the second.
        self.assertFalse(blocked(
            "Bash",
            'python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py --slot 2 '
            '--task-file "/path with /slots/2/assign space/t.md"',
        ))
        self.assertFalse(blocked(
            "Bash",
            "python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py --slot 2 "
            "--task-file /path\\ with\\ /slots/2/assign\\ space/t.md",
        ))

    def test_the_allow_pair_actually_engages_the_hook(self) -> None:
        # Anti-no-op guard: prove the two cases above reach the predicate rather than
        # exiting early. If reaches_assign_ownership stopped matching the in-path route
        # token, these would silently degrade to no-ops again.
        for cmd in (
            'python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py --slot 2 '
            '--task-file "/path with /slots/2/assign space/t.md"',
            "python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py --slot 2 "
            "--task-file /path\\ with\\ /slots/2/assign\\ space/t.md",
        ):
            self.assertTrue(route_token_engaged(cmd), f"no-op case: hook not engaged for {cmd!r}")

    def test_manifest_row_is_a_home_hook_with_matching_digest_and_mode(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        rows = [r for r in manifest["entries"] if r["source_path"] == "claude/hooks/block-direct-mop-assign.sh"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["canonical_target"], "/Users/rajiv/.claude/hooks/block-direct-mop-assign.sh")
        self.assertEqual(row["mode"], 493)
        self.assertEqual(row["dependencies"], [])
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["sha256"], hashlib.sha256(HOOK.read_bytes()).hexdigest())
        self.assertEqual(manifest["entries"], sorted(manifest["entries"], key=lambda i: i["source_path"]))
        self.assertEqual(manifest["inventory"]["selected_count"], len(manifest["entries"]))

    def test_sibling_registration_shape_is_the_shared_wrapper(self) -> None:
        settings = Path("/Users/rajiv/.claude/settings.json")
        if not settings.is_file():
            self.skipTest("home settings.json unavailable")
        data = json.loads(settings.read_text(encoding="utf-8"))
        commands = [
            h.get("command", "")
            for e in (data.get("hooks") or {}).get("PreToolUse", [])
            for h in (e.get("hooks") or [])
        ]
        self.assertTrue(
            any("pretooluse-reason-wrapper.sh" in c and "block-ci-rerun-without-local-proof.sh" in c for c in commands),
            "the sibling home-hook registration mechanism must exist",
        )


if __name__ == "__main__":
    unittest.main()
