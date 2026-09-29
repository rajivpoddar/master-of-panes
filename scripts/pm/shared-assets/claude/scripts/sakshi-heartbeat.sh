#!/usr/bin/env bash
# Sakshi heartbeat entry point.
#
#   sakshi-heartbeat.sh compose [--mode 3h|1h] [composer args]
#       Print the redesigned heartbeat post (docs/pm/heartbeat-redesign-2026-09-29.md):
#       3h = Axiom errors + support + open PRs, always @-mentions CTO;
#       1h = Axiom errors + open PRs, @-mentions CTO only for merge/admission/escalation asks.
#       Never posts; the calling skill posts the output.
#   sakshi-heartbeat.sh [args]
#       Legacy/housekeeping probe: forwards to the canonical .py installed by
#       scripts/pm/install-sakshi-heartbeat.py (writes /tmp/sakshi-heartbeat.json).
REPO="${HEYDONNA_REPO:-/Users/rajiv/Downloads/projects/heydonna-app}"
if [ "${1:-}" = "compose" ]; then
  shift
  export AXIOM_ENV_FILE="${AXIOM_ENV_FILE:-$REPO/.env.local}"
  if [ ! -f "$REPO/scripts/pm/heartbeat/heartbeat-compose.py" ]; then
    echo "HEARTBEAT_COLLECTORS_ABSENT: $REPO/scripts/pm/heartbeat/heartbeat-compose.py not found; the heartbeat collectors land via heydonna-app PR #8481 (merge it and pull main). Compose skipped." >&2
    exit 3
  fi
  exec python3 "$REPO/scripts/pm/heartbeat/heartbeat-compose.py" "$@"
fi
exec python3 /Users/rajiv/.claude/scripts/sakshi-heartbeat.py "$@"
