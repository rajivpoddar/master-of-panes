#!/usr/bin/env bash
# Direct Claude hooks for MoP. Honor explicit PM admission denials, but fail
# open on transport/protocol failures so recovery tools remain available.
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
    # Admission tracking is best effort when MoP cannot acknowledge. Never
    # turn a service outage into a session-wide lockout of recovery tools.
    pass
' "$HOOK_TYPE" 2>/dev/null
