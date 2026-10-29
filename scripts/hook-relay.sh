#!/usr/bin/env bash
# Direct Claude hooks for MoP. PM PreToolUse is an admission handshake, not
# fire-and-forget: no acknowledged tracking means no admitted PM tool.
set -euo pipefail
HOOK_TYPE="${1:-}"
[ -z "$HOOK_TYPE" ] && exit 0
python3 -c '
import json, sys, re, urllib.request
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
data["hook_event_name"] = hook_type
if slot == "0":
    data["mop_tool_tracking"] = 1
try:
    req = urllib.request.Request(
        f"http://localhost:3100/hooks/slot/{slot}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as response:
        result = json.load(response)
    if slot == "0" and hook_type == "PreToolUse":
        if result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
            print(json.dumps(result))
        elif result.pop("mop_tool_admitted", False) is True:
            print(json.dumps(result))
        else:
            raise RuntimeError("MoP did not acknowledge admission")
    elif slot == "0" and hook_type == "UserPromptSubmit":
        print(json.dumps(result))
except Exception:
    if slot == "0" and hook_type == "PreToolUse":
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "MoP could not acknowledge PM tool admission; retry after service recovery."
        }}))
' "$HOOK_TYPE" 2>/dev/null
