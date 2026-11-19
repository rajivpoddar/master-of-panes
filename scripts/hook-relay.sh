#!/usr/bin/env bash
# Direct Claude hooks for MoP. Honor explicit PM admission denials, but fail
# open on transport/protocol failures so recovery tools remain available.
set -euo pipefail
HOOK_TYPE="${1:-}"
[ -z "$HOOK_TYPE" ] && exit 0

# Dev slots set HEYDONNA_SLOT_STOP_GATED=1 (Rajiv ts 1794273343.877539). The
# plugin hooks.json still runs this script on every Stop, including a blocked
# turn. The only allowed Stop is an explicit MOP_SLOT_STOP_ALLOWED=1 set by
# the caller (slot-terminal-message-pm-stop.sh). A parent command that merely
# contains that filename is not allowed. PM leaves the env unset and still
# posts. Every other hook type posts either way.
if [ "$HOOK_TYPE" = "Stop" ] && [ "${HEYDONNA_SLOT_STOP_GATED:-}" = "1" ]; then
  if [ "${MOP_SLOT_STOP_ALLOWED:-}" != "1" ]; then
    exit 0
  fi
  export MOP_STOP_MARK_ALLOWED=1
fi

python3 -c '
import json, sys, re, urllib.request, os, runpy
from pathlib import Path
hook_type = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
cwd = data.get("cwd", "")
m = re.search(r"heydonna-app-300([1-8])", cwd)
if m:
    slot = m.group(1)
elif re.search(r"heydonna-app(?!-300)", cwd):
    slot = "0"
else:
    sys.exit(0)
# This hook is already registered for PM PreToolUse. Do not put this on
# UserPromptSubmit or make MoP transport health a prerequisite for review.
review_note = ""
review_input = None
review_checked = False
if slot == "0" and hook_type == "PreToolUse" and data.get("tool_name") == "Agent":
    tool_input = data.get("tool_input") or {}
    agent_type = tool_input.get("subagent_type")
    if agent_type in ("pm-admission-reviewer", "hd-proof-qa", "pm-proof-qa"):
        review_checked = True
        try:
            helpers = Path(sys.argv[2]).parent / "pm/shared-assets/claude/scripts/review-launch-cap.py"
            cap = runpy.run_path(str(helpers))
            prompt = tool_input.get("prompt", "")
            rc, review_note = cap["check"](agent_type, prompt, timeout=2)
            if rc == 76:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                    "permissionDecision": "deny", "permissionDecisionReason": review_note}}))
                sys.exit(0)  # Claude Code requires hook JSON, not launcher exit 76.
            if cap["STAGES"][agent_type] == "qa":
                review_input = dict(tool_input, prompt=prompt + "\n\n" + cap["QA_RECEIPT"])
        except Exception:
            review_note = "warning: review cap skipped: cap helper unavailable"

def emit_pretool(result):
    if review_note:
        result["systemMessage"] = "review-launch: " + review_note
    if review_input is not None:
        output = result.setdefault("hookSpecificOutput", {"hookEventName": "PreToolUse"})
        if output.get("permissionDecision") != "deny":
            output.setdefault("updatedInput", review_input)
    print(json.dumps(result))

data["hook_event_name"] = hook_type
if slot == "0":
    data["mop_tool_tracking"] = 1
if hook_type == "Stop" and os.environ.get("MOP_STOP_MARK_ALLOWED") == "1":
    data["mop_stop_allowed"] = 1
try:
    base = os.environ.get("MOP_HOOK_BASE", "http://localhost:3100").rstrip("/")
    req = urllib.request.Request(
        f"{base}/hooks/slot/{slot}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=1 if review_checked else 3) as response:
        result = json.load(response)
    if slot == "0" and hook_type == "PreToolUse":
        if result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
            emit_pretool(result)
        elif result.pop("mop_tool_admitted", False) is True:
            emit_pretool(result)
        else:
            raise RuntimeError("MoP did not acknowledge admission")
    elif slot == "0" and hook_type == "UserPromptSubmit":
        print(json.dumps(result))
except Exception:
    # Admission tracking is best effort when MoP cannot acknowledge. Never
    # turn a service outage into a session-wide lockout of recovery tools.
    if review_note or review_input is not None:
        emit_pretool({})
' "$HOOK_TYPE" "$0" 2>/dev/null
