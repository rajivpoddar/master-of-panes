#!/usr/bin/env bash
# Sakshi heartbeat entry point.
#
#   sakshi-heartbeat.sh compose [--mode 3h|1h] [composer args]
#       Print the redesigned heartbeat post (collectors installed by MoP to ~/.claude/scripts/pm/heartbeat):
#       3h = Axiom errors + support + open PRs, always @-mentions CTO;
#       1h = Axiom errors + open PRs, @-mentions CTO only for merge/admission/escalation asks.
#       Never posts; the calling skill posts the output.
#   sakshi-heartbeat.sh [args]
#       Legacy/housekeeping probe: forwards to the canonical .py installed by
#       scripts/pm/install-sakshi-heartbeat.py (writes /tmp/sakshi-heartbeat.json).
REPO="${HEYDONNA_REPO:-/Users/rajiv/Downloads/projects/heydonna-app}"
HEARTBEAT_DIR="${HEARTBEAT_DIR:-/Users/rajiv/.claude/scripts/pm/heartbeat}"  # MoP-installed (manifest)
if [ "${1:-}" = "compose" ]; then
  shift
  export AXIOM_ENV_FILE="${AXIOM_ENV_FILE:-$REPO/.env.local}"
  export HEYDONNA_REPO="$REPO"
  if [ ! -f "$HEARTBEAT_DIR/heartbeat-compose.py" ]; then
    echo "HEARTBEAT_COLLECTORS_ABSENT: $HEARTBEAT_DIR/heartbeat-compose.py not installed; install the MoP release that ships scripts/pm/heartbeat. Compose skipped." >&2
    exit 3
  fi
  exec python3 "$HEARTBEAT_DIR/heartbeat-compose.py" "$@"
fi
exec python3 /Users/rajiv/.claude/scripts/sakshi-heartbeat.py "$@"
