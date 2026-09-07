from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ASSETS = ROOT / "scripts" / "pm" / "shared-assets" / "claude"
PM_CONTEXT = ASSETS / "hooks" / "pm-context-injector.sh"
LEDGER_GUARD = ASSETS / "hooks" / "block-invalid-issue-contract-ledger.sh"
CLEAR_GUARD = ASSETS / "hooks" / "block-raw-clear-outside-mop.sh"


def run_hook(path: Path, payload: dict[str, object], env: dict[str, str] | None = None):
    return subprocess.run(
        ["bash", str(path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


def test_cto_selected_diagnostic_does_not_create_pm_ci_work() -> None:
    with tempfile.TemporaryDirectory(prefix="pm-guard-diagnostic-") as tmp:
        pm_ops = Path(tmp) / "pm-ops"
        pm_ops.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' called >> \"$PM_OPS_CALLS\"\n",
            encoding="utf-8",
        )
        pm_ops.chmod(0o755)
        calls = Path(tmp) / "pm-ops.calls"
        result = run_hook(
            PM_CONTEXT,
            {
                "cwd": "/Users/rajiv/Downloads/projects/heydonna-app",
                "prompt": (
                    "# slack-channel C0ALZJHGE49 in thread 1788777687.037799 | "
                    "Abhijit CTO | selected diagnostic 34112270372 for PR #7648: CI failed"
                ),
            },
            {
                "HOME": tmp,
                "LOG": str(Path(tmp) / "hook.log"),
                "PM_OPS": str(pm_ops),
                "PM_OPS_CALLS": str(calls),
            },
        )
        assert result.returncode == 0
        assert result.stdout == ""
        assert not calls.exists()


def test_required_failure_route_remains_visible() -> None:
    result = run_hook(
        PM_CONTEXT,
        {
            "cwd": "/Users/rajiv/Downloads/projects/heydonna-app",
            "prompt": "# slack-channel C0AEY9CEC4D in thread 1.2 | HeyDonna Alerts | CI failed",
        },
    )
    assert result.returncode == 0
    assert "CI_FAILURE_DETECTED" in result.stdout


def test_internal_issue_skips_product_ledger_noise_but_external_stays_blocked() -> None:
    with tempfile.TemporaryDirectory(prefix="pm-guard-ledger-") as tmp:
        parser = Path(tmp) / "parser.py"
        parser.write_text(
            "import json, os\n"
            "print(json.dumps({'block': True, 'target': 'x', 'errors': "
            "['internal_control_plane_issue_forbidden'] if 'control-plane' in "
            "os.environ.get('CMD_TEXT', '') else ['invalid_ledger']}))\n",
            encoding="utf-8",
        )
        internal = run_hook(
            LEDGER_GUARD,
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "gh issue create --title 'control-plane reimplementation'"
                },
            },
            {"ISSUE_CONTRACT_LEDGER_HOOK_PARSER": str(parser)},
        )
        external = run_hook(
            LEDGER_GUARD,
            {
                "tool_name": "Bash",
                "tool_input": {"command": "gh issue create --title 'customer bug'"},
            },
            {"ISSUE_CONTRACT_LEDGER_HOOK_PARSER": str(parser)},
        )
        assert internal.returncode == 0
        assert internal.stdout == ""
        assert external.returncode == 0
        assert '"decision":"block"' in external.stdout


def test_clear_word_in_message_is_allowed_but_lifecycle_command_is_blocked() -> None:
    prose = run_hook(
        CLEAR_GUARD,
        {
            "tool_name": "mcp__plugin_master-of-panes_mop__mop_send_to_slot",
            "tool_input": {"slot": "4", "command": "handoff: investigate the clear error"},
        },
    )
    lifecycle = run_hook(
        CLEAR_GUARD,
        {
            "tool_name": "mcp__plugin_master-of-panes_mop__mop_send_to_slot",
            "tool_input": {"slot": "4", "command": "/clear"},
        },
    )
    assert prose.returncode == lifecycle.returncode == 0
    assert prose.stdout == ""
    assert '"decision":"block"' in lifecycle.stdout
