#!/usr/bin/env python3
"""Focused contract tests for the mapped slot Stop hook."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "scripts/pm/shared-assets/claude/hooks/slot-terminal-message-pm-stop.sh"
MANIFEST = ROOT / "scripts/pm/shared-assets/manifest.json"
HEAD = "a" * 40


def run_stop(completion: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        slot_cwd = root / "heydonna-app-3001"
        slot_cwd.mkdir()
        transcript = root / "turn.jsonl"
        fake_bin = root / "bin"
        fake_bin.mkdir()
        (fake_bin / "git").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  'branch --show-current') printf '%s\\n' 'fix/7629-test-only' ;;\n"
            f"  'rev-parse HEAD') printf '%s\\n' '{HEAD}' ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        (fake_bin / "git").chmod(0o755)
        events = [
            {
                "type": "user",
                "message": {"role": "user", "content": "Finish the authorized work."},
                "timestamp": "2026-09-06T00:00:00Z",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__plugin_master-of-panes_mop__mop_send_to_slot",
                            "input": {"slot": 0, "command": "slot 0 status"},
                        }
                    ],
                },
                "timestamp": "2026-09-06T00:00:01Z",
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": completion},
                "timestamp": "2026-09-06T00:00:02Z",
            },
        ]
        transcript.write_text("".join(json.dumps(event) + "\n" for event in events))
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env.get('PATH', '')}",
                "PM_OPS_DB": str(root / "missing-pm-ops.db"),
                "SLOT_READY_EVENT_DIR": str(root / "no-ready-packets"),
                "SLOT_PM_NOTIFY_TEST_SLOTS_JSON": json.dumps(
                    {"slots": [{"slot": 1, "occupied": True, "dnd": False, "pr": "7629"}]}
                ),
            }
        )
        return subprocess.run(
            ["bash", str(HOOK)],
            cwd=slot_cwd,
            input=json.dumps({"transcript_path": str(transcript), "stop_hook_active": False}),
            text=True,
            capture_output=True,
            env=env,
        )


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    entry = next(
        item for item in manifest["entries"]
        if item["source_path"] == "claude/hooks/slot-terminal-message-pm-stop.sh"
    )
    assert entry["canonical_target"] == "/Users/rajiv/.claude/hooks/slot-terminal-message-pm-stop.sh"
    assert entry["mode"] == 0o755
    assert hashlib.sha256(HOOK.read_bytes()).hexdigest() == entry["sha256"]
    assert stat.S_IMODE(HOOK.stat().st_mode) == 0o755

    allowed = run_stop(
        "Task complete: the test-only local commit is complete; no-push was required."
    )
    assert allowed.returncode == 0, allowed
    assert "slot-submit-ready.sh" not in allowed.stdout

    for counterexample in (
        "Task complete: this is not test-only work; no-push was not requested. PR ready for PM review.",
        "All tests passed for the test-only local-only checks; implementation is still unfinished.",
        "Task complete: test-only local-only checks are pending; no-push was requested.",
        "Task complete: test-only local-only checks completed; implementation is unfinished.",
    ):
        refused = run_stop(counterexample)
        assert refused.returncode == 2, (counterexample, refused)
        assert "SLOT_READY_PACKET_UNAVAILABLE" in refused.stdout, refused.stdout
        assert "slot-submit-ready.sh" not in refused.stdout, refused.stdout

    incomplete = run_stop("Implementation complete and ready for PM review.")
    assert incomplete.returncode == 2, incomplete
    assert "SLOT_READY_PACKET_UNAVAILABLE" in incomplete.stdout, incomplete.stdout
    assert "slot-submit-ready.sh" not in incomplete.stdout, incomplete.stdout
    assert "--issue" not in incomplete.stdout, incomplete.stdout

    print("PASS: Stop hook retires absent slot-submit-ready demand safely")


if __name__ == "__main__":
    main()
