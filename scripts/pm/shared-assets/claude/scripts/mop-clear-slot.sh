#!/bin/sh
set -eu

# Canonical PM/slot session-age caller. It deliberately carries the exact
# Sakshi-emitted tuple to the authenticated Python client; it never derives a
# session, clears a pane through /send, or falls back to a legacy endpoint.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "${1:-}" = "pm" ]; then
  shift
  set -- --slot pm "$@"
fi

exec python3 "$SCRIPT_DIR/heartbeat-session-age-clear.py" "$@"
