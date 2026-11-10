#!/usr/bin/env bash
# Direct Claude hooks for MoP. Honor explicit PM admission denials, but fail
# open on transport/protocol failures so recovery tools remain available.
set -euo pipefail
HOOK_TYPE="${1:-}"
[ -z "$HOOK_TYPE" ] && exit 0

# Dev slots set HEYDONNA_SLOT_STOP_GATED=1 (Rajiv ts 1794273343.877539). The
# plugin hooks.json still runs this script on every Stop, including a blocked
# turn. The allowed Stop is the child of slot-terminal-message-pm-stop.sh, or
# an explicit MOP_SLOT_STOP_ALLOWED=1. PM leaves the env unset and still posts.
# Every other hook type posts either way.
if [ "$HOOK_TYPE" = "Stop" ] && [ "${HEYDONNA_SLOT_STOP_GATED:-}" = "1" ]; then
  allowed=0
  if [ "${MOP_SLOT_STOP_ALLOWED:-}" = "1" ]; then
    allowed=1
  else
    parent_cmd=$(ps -o command= -p "$PPID" 2>/dev/null || true)
    case "$parent_cmd" in
      *slot-terminal-message-pm-stop.sh*) allowed=1 ;;
    esac
  fi
  if [ "$allowed" != "1" ]; then
    exit 0
  fi
  export MOP_STOP_MARK_ALLOWED=1
fi

python3 -c '
import json, sys, re, urllib.request, os
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
if hook_type == "Stop" and os.environ.get("MOP_STOP_MARK_ALLOWED") == "1":
    data["mop_stop_allowed"] = 1
try:
    base = os.environ.get("MOP_HOOK_BASE", "http://localhost:3100").rstrip("/")
    req = urllib.request.Request(
        f"{base}/hooks/slot/{slot}",
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
