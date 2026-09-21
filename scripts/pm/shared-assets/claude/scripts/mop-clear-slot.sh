#!/usr/bin/env bash
# Deterministic MoP-logged clear wrapper for PM, bg agents, and hooks.
#
# Prefer the MCP tool when it is available in the current agent surface. This
# script is the canonical fallback for contexts where mop_clear_slot is not
# loadable. It uses the MoP HTTP clear endpoint, never raw tmux /clear.
#
# OPERATOR-ONLY FOR DEV SLOTS. A clear targeting S1-S6 is refused by default.
# Dev slots are cleared only at the new-issue assignment boundary, inside the
# atomic assignment operation:
#     python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py --slot N --class new_issue ...
# A genuine operator clear of a dev slot requires the explicit acknowledgement
# flag --operator-confirm-dev-slot-clear. No periodic caller emits it, and
# --require-terminal does NOT satisfy it; the no-acknowledgement path is always
# refusal. PM self-clear (pm|0) is unaffected.

set -euo pipefail

BASE_URL="${MOP_BASE_URL:-http://127.0.0.1:3100}"
DB_PATH="${MOP_DB_PATH:-$HOME/.claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/data/mop.db}"
SOURCE="${MOP_CLEAR_SOURCE:-mop_clear_slot_http_fallback}"
STALE_MINUTES="${MOP_PM_CLEAR_STALE_REPAIR_MINUTES:-10}"
REPAIR_PM_PENDING=0
REPAIR_ONLY=0
REQUIRE_TERMINAL=0
OPERATOR_CONFIRM_DEV_SLOT_CLEAR=0
CLEAR_EXISTING_PENDING=0
SLOT=""

usage() {
  cat <<'EOF'
Usage: mop-clear-slot.sh [options] <slot>

Slots:
  1|2|3|4|5|6   Clear one dev slot through MoP logging
  pm|0      Clear PM through MoP logging
  all       Clear PM and all dev slots through MoP logging

Options:
  --source NAME                    MoP event source tag
  --base-url URL                   MoP HTTP base URL (default: http://127.0.0.1:3100)
  --clear-existing-pending         Clear existing pending latch before requesting
  --require-terminal               Clear only if terminal; exit 20 if active, without queuing
  --operator-confirm-dev-slot-clear
                                   Required acknowledgement to clear a dev slot
                                   (S1-S6); does not affect pm|0. Refusal without
                                   it names mop-assign-slot as the sanctioned path
  --repair-stale-pm-pending        Repair stale PM clear latch before requesting PM/all
  --repair-stale-pm-pending-only   Only run the stale PM latch repair

Proof:
  Writes /tmp/mop-clear-slot-latest.json and prints the same response.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source)
      SOURCE="${2:?missing --source value}"
      shift 2
      ;;
    --base-url)
      BASE_URL="${2:?missing --base-url value}"
      shift 2
      ;;
    --clear-existing-pending)
      CLEAR_EXISTING_PENDING=1
      shift
      ;;
    --require-terminal)
      REQUIRE_TERMINAL=1
      shift
      ;;
    --operator-confirm-dev-slot-clear)
      OPERATOR_CONFIRM_DEV_SLOT_CLEAR=1
      shift
      ;;
    --repair-stale-pm-pending)
      REPAIR_PM_PENDING=1
      shift
      ;;
    --repair-stale-pm-pending-only)
      REPAIR_PM_PENDING=1
      REPAIR_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [ -n "$SLOT" ]; then
        echo "Unexpected extra argument: $1" >&2
        usage >&2
        exit 2
      fi
      SLOT="$1"
      shift
      ;;
  esac
done

normalize_slot() {
  case "$1" in
    0|pm|PM) printf 'pm' ;;
    all|ALL) printf 'all' ;;
    1|2|3|4|5|6) printf '%s' "$1" ;;
    *) return 1 ;;
  esac
}

repair_stale_pm_pending() {
  python3 - "$DB_PATH" "$STALE_MINUTES" "$SOURCE" <<'PY'
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

db_path, stale_minutes_raw, source = sys.argv[1:4]
stale_minutes = int(stale_minutes_raw)

def parse_ts(value):
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

def config_value(key):
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None

pending = config_value("clear_pending_0") == "true"
requested_at = config_value("pm_clear_requested_at")
requested_dt = parse_ts(requested_at)
now = datetime.now(timezone.utc)

result = {
    "repair": "not_needed",
    "pending": pending,
    "requested_at": requested_at,
    "stale_minutes": stale_minutes,
}

if pending and requested_dt:
    age = now - requested_dt
    row = conn.execute(
        """
        SELECT id, timestamp, payload
        FROM events
        WHERE slot = 0
          AND event_type = 'SessionStart'
          AND timestamp > ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (requested_at.replace("Z", ""),),
    ).fetchone()
    if age >= timedelta(minutes=stale_minutes) and row:
        payload = {
            "name": "PM",
            "requested_at": requested_at,
            "repaired_at": now.isoformat(),
            "later_session_start_event_id": row["id"],
            "later_session_start_timestamp": row["timestamp"],
            "source": source,
            "reason": "stale PM clear request had no SessionStart:clear ack; clearing latch so PM can request a fresh natural-boundary clear",
        }
        conn.execute(
            """
            INSERT INTO events (slot, event_type, hook_type, tool_name, payload)
            VALUES (0, 'clear_pending_stale_repaired', NULL, NULL, ?)
            """,
            (json.dumps(payload, separators=(",", ":")),),
        )
        conn.execute(
            """
            INSERT INTO config (key, value, updated_at)
            VALUES ('clear_pending_0', 'false', strftime('%Y-%m-%dT%H:%M:%f', 'now'))
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """
        )
        conn.commit()
        result.update({
            "repair": "stale_pm_pending_repaired",
            "later_session_start_event_id": row["id"],
            "later_session_start_timestamp": row["timestamp"],
        })
    else:
        result.update({
            "repair": "left_open",
            "age_seconds": int(age.total_seconds()),
            "has_later_session_start": bool(row),
        })

print(json.dumps(result, indent=2, sort_keys=True))
PY
}

if [ "$REPAIR_PM_PENDING" -eq 1 ]; then
  if [ ! -f "$DB_PATH" ]; then
    echo "MoP DB not found: $DB_PATH" >&2
    exit 3
  fi
  repair_stale_pm_pending
  if [ "$REPAIR_ONLY" -eq 1 ]; then
    exit 0
  fi
fi

if [ -z "$SLOT" ]; then
  echo "Missing slot." >&2
  usage >&2
  exit 2
fi

NORMALIZED_SLOT="$(normalize_slot "$SLOT" || true)"
if [ -z "$NORMALIZED_SLOT" ]; then
  echo "Invalid slot: $SLOT" >&2
  usage >&2
  exit 2
fi

# Dev-slot guard: S1-S6 are never cleared by a cadence, a fallback, or a plain
# operator invocation. Clearing belongs to the new-issue assignment boundary.
# --require-terminal is explicitly NOT an acknowledgement.
case "$NORMALIZED_SLOT" in
  1|2|3|4|5|6|all)
    if [ "$OPERATOR_CONFIRM_DEV_SLOT_CLEAR" -ne 1 ]; then
      cat >&2 <<'REFUSED'
REFUSED: dev-slot clearing is not a cadence or fallback action.

Slots S1-S6 are cleared only at the new-issue assignment boundary, which owns
that step inside the atomic assignment operation:
    python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py --slot N --class new_issue ...

A genuine operator clear of a dev slot requires the explicit acknowledgement flag:
    --operator-confirm-dev-slot-clear

--require-terminal does NOT satisfy that acknowledgement.
PM self-clear (mop-clear-slot.sh pm) is unaffected.
REFUSED
      exit 2
    fi
    ;;
esac

if [ "$NORMALIZED_SLOT" = "pm" ] || [ "$NORMALIZED_SLOT" = "all" ]; then
  # Avoid a stale PM latch suppressing a legitimate new natural-boundary clear.
  if [ "$REPAIR_PM_PENDING" -eq 0 ] && [ -f "$DB_PATH" ]; then
    repair_stale_pm_pending >/tmp/mop-clear-slot-pm-repair-latest.json || true
  fi
fi

if ! curl -fsS "$BASE_URL/health" >/dev/null; then
  echo "MoP HTTP is not reachable at $BASE_URL" >&2
  exit 30
fi

BODY="$(python3 - "$SOURCE" "$CLEAR_EXISTING_PENDING" "$REQUIRE_TERMINAL" <<'PY'
import json
import sys

source = sys.argv[1]
clear_existing = sys.argv[2] == "1"
terminal_only = sys.argv[3] == "1"
print(json.dumps({
    "source": source,
    "clear_existing_pending": clear_existing,
    "terminal_only": terminal_only,
}))
PY
)"

# macOS/BSD mktemp requires the X's to be TRAILING; a ".json" suffix after
# XXXXXX makes BSD mktemp treat the whole template literally → repeated runs
# collide on a literal "mop-clear-slot.XXXXXX.json" → "mkstemp failed: File
# exists" → the PM self-clear could never terminalize. Keep X's trailing (the
# extension is not load-bearing; the file is only a transient curl-response read
# back by python below).
TMP_RESPONSE="$(mktemp "${TMPDIR:-/tmp}/mop-clear-slot.XXXXXX")"
if ! curl -fsS \
  -X POST \
  -H "Content-Type: application/json" \
  --data "$BODY" \
  "$BASE_URL/slots/$NORMALIZED_SLOT/clear" \
  >"$TMP_RESPONSE"; then
  cat "$TMP_RESPONSE" >&2 || true
  exit 30
fi

python3 - "$TMP_RESPONSE" "$REQUIRE_TERMINAL" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
require_terminal = sys.argv[2] == "1"
data = json.loads(path.read_text())
Path("/tmp/mop-clear-slot-latest.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
print(json.dumps(data, indent=2, sort_keys=True))

statuses = [str(row.get("status", "")) for row in data.get("results", [])]
if any(status.startswith("failed") or "failed:" in status for status in statuses):
    raise SystemExit(1)
if require_terminal and any("queued" in status or "not terminal" in status for status in statuses):
    raise SystemExit(20)
PY

# A terminal dev-slot clear creates capacity, but it does not assign work. Keep
# that boundary explicit: require PM to run the canonical reconciler and leave
# the obligation pending until live assignment/active-turn proof closes it.
PM_REQUIRED_SLOT_DISPATCH_SENTINEL="${PM_REQUIRED_SLOT_DISPATCH_SENTINEL:-/tmp/pm-required-slot-dispatch.json}" \
python3 - "$TMP_RESPONSE" "$NORMALIZED_SLOT" "$SOURCE" <<'PY'
import datetime
import json
import os
import sys
from pathlib import Path

response_path, requested_slot, source = sys.argv[1:4]
data = json.loads(Path(response_path).read_text())
cleared_slots = sorted({
    int(row.get("slot"))
    for row in data.get("results", [])
    if str(row.get("slot")) in {"1", "2", "3", "4"}
    and str(row.get("status", "")).lower() == "cleared"
})
if not cleared_slots:
    raise SystemExit(0)

path = Path(os.environ["PM_REQUIRED_SLOT_DISPATCH_SENTINEL"])
try:
    existing = json.loads(path.read_text())
except Exception:
    existing = {}

if existing.get("status") != "pending":
    existing = {
        "schema_version": 1,
        "source": "mop-clear-slot",
        "status": "pending",
        "created_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "issues": [],
        "slots": [],
        "recommended_commands": [],
    }

existing["slots"] = sorted({int(slot) for slot in existing.get("slots", []) if str(slot).isdigit()} | set(cleared_slots))
command = "/Users/rajiv/.claude/scripts/pm-transition.sh reconcile-capacity"
commands = [str(item) for item in existing.get("recommended_commands", []) if item]
if command not in commands:
    commands.append(command)
existing["recommended_commands"] = commands
existing["required_command"] = command
existing["reason"] = "terminal_dev_slot_clear_requires_capacity_reconcile"
existing["required_skill"] = "slot-state-machine"
existing["updated_at"] = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
existing["clear_proof"] = {
    "source": source,
    "requested_slot": requested_slot,
    "cleared_slots": cleared_slots,
    "response": "/tmp/mop-clear-slot-latest.json",
}

path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
os.replace(tmp, path)
PY
