#!/usr/bin/env python3
"""Block background Agent tool calls in dev slots.

PM and dev slots intentionally have opposite agent policies:
  - PM must use background agents so the pane stays event-driven.
  - Dev slots may use foreground agents, but must not launch async/background
    subagents.

This hook is installed only in dev slot project settings, and it also gates on
cwd so nested slot paths are covered without affecting the PM checkout.
"""

import json
import os
import re
import sys


DEV_SLOT_PATH_RE = re.compile(r"/heydonna-app-300[1-8](?:/|$)")


def block(reason: str) -> int:
    print(json.dumps({
        "decision": "block",
        "message": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
    }))
    return 0


def main() -> int:
    if not DEV_SLOT_PATH_RE.search(os.getcwd()):
        return 0

    try:
        data = json.load(sys.stdin)
    except Exception:
        return block(
            "BLOCKED: Dev-slot Agent hook input was not parseable, so "
            "background mode cannot be ruled out. Relaunch as a foreground "
            "Agent with run_in_background:false."
        )

    tool_input = data.get("tool_input") or data.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return block(
            "BLOCKED: Dev-slot Agent hook input was not an object, so "
            "background mode cannot be ruled out. Relaunch as a foreground "
            "Agent with run_in_background:false."
        )

    if tool_input.get("run_in_background") is False:
        return 0

    # Claude 2.1.212 normally backgrounds an omitted field. Normalize both
    # omission and an explicit true to false before the Agent is dispatched.
    # The launcher also sets CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1; this hook
    # makes the foreground policy visible at the tool boundary as well.
    updated_input = dict(tool_input)
    updated_input["run_in_background"] = False
    reason = (
        "Dev slots run Agent subagents in the foreground; normalized "
        "run_in_background to false."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
            "updatedInput": updated_input,
        },
        "systemMessage": reason,
    }))
    return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
