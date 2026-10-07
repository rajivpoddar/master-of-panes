#!/bin/bash
# Send command to Claude Code session in tmux slot via MoP gateway.
# ALL slot communication goes through MoP HTTP endpoint.
#
# Usage: send-to-slot.sh <slot> <command> [--wait] [--force] [--raw]
#        send-to-slot.sh <slot> --file <filepath> [--force]
#
# Examples:
#   send-to-slot.sh 1 '/review-and-pr'
#   send-to-slot.sh 1 '/git-sync-main' --wait
#   send-to-slot.sh 3 'correction: keep sidebar' --force
#   send-to-slot.sh 4 --file /tmp/slot-delivery-2898.md
#   send-to-slot.sh 2 --file /tmp/clerk-jwt-fix.md --force
#
# Architecture (Rajiv directive 2026-03-20):
#   send-to-slot.sh → MoP POST /slots/N/send → tmux
#   If command is "2" and slot is awaiting plan approval:
#     MoP internally routes to approve-plan (Codex session gate enforced)
#
# --raw mode: still uses tmux directly (for key sequences like Escape, BTab)
#   These are UI control sequences, not slot communication.

usage() {
  cat <<'USAGE'
Usage: send-to-slot.sh <slot> <command> [--wait] [--force] [--raw]
       send-to-slot.sh <slot> --file <filepath> [--force]
       send-to-slot.sh -h | --help          (print this help; sends nothing)

  <slot>      1-8 (dev slot number)
  <command>   text or slash command delivered via MoP POST /slots/N/send
  --wait      wait for the slot to return idle after sending
  --force     send immediately even if the slot is active
  --raw       send tmux key names directly (Enter, Escape, BTab, C-c); UI control only
  --file F    deliver the contents of file F

Unknown --flags are rejected (never sent as message text).
USAGE
}

# Help mode: any -h/--help anywhere prints usage and exits without sending.
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
  esac
done

SLOT="$1"
shift 1 2>/dev/null

WAIT=""
FORCE=""
RAW=""
FILE=""
COMMAND=""

# Parse arguments
while [ $# -gt 0 ]; do
  case "$1" in
    --wait) WAIT="--wait" ;;
    --force) FORCE="--force" ;;
    --raw) RAW="--raw" ;;
    --file) FILE="$2"; shift ;;
    --*)
      if [ -z "$COMMAND" ]; then
        echo "ERROR: unknown option '$1' (not sent). See send-to-slot.sh --help" >&2
        exit 2
      fi
      ;;
    *) [ -z "$COMMAND" ] && COMMAND="$1" ;;
  esac
  shift
done

if [ -z "$SLOT" ]; then
  echo "Usage: send-to-slot.sh <slot> <command> [--wait] [--force]" >&2
  echo "       send-to-slot.sh <slot> --file <filepath> [--force]" >&2
  exit 1
fi

if [ -z "$COMMAND" ] && [ -z "$FILE" ]; then
  echo "Usage: send-to-slot.sh <slot> <command> [--wait] [--force]" >&2
  echo "       send-to-slot.sh <slot> --file <filepath> [--force]" >&2
  exit 1
fi

# Panes are numbered by screen position: S6 is index 7, S7 is index 6 (Rajiv 2026-10-07).
case "$SLOT" in
  6) PANE="0:0.7" ;;
  7) PANE="0:0.6" ;;
  *) PANE="0:0.$SLOT" ;;
esac
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MOP_PORT="${MOP_PORT:-3100}"

# Raw mode: send tmux key sequences directly (Escape, BTab, C-c, etc.)
# These are UI control sequences, not slot communication — bypass MoP.
if [ "$RAW" = "--raw" ]; then
  # shellcheck disable=SC2086
  tmux send-keys -t "$PANE" $COMMAND
  echo "✓ Sent raw keys to slot $SLOT: $COMMAND"
  exit 0
fi

# Delegate activity check to is-active.sh (color-only detection, no word matching)
is_claude_active() {
  "$SCRIPT_DIR/is-active.sh" "$SLOT" 2>/dev/null
}

# Function to wait for Claude to become idle
wait_for_idle() {
  local max_wait=${1:-10}
  local count=0
  echo "⏳ Waiting for slot $SLOT to become idle..."
  while [ $count -lt $max_wait ]; do
    if ! is_claude_active; then
      echo "✓ Slot $SLOT is idle"
      return 0
    fi
    sleep 2
    count=$((count + 2))
    if [ $((count % 10)) -eq 0 ]; then
      echo "  Still waiting... (${count}s)"
    fi
  done
  echo "⚠️ Timeout waiting for slot $SLOT to become idle" >&2
  return 1
}

# Function to wait for prompt (command completed)
wait_for_prompt() {
  local max_wait=${1:-60}
  local count=0
  while [ $count -lt $max_wait ]; do
    if tmux capture-pane -t "$PANE" -p | tail -5 | grep -q '^❯' && \
       tmux capture-pane -t "$PANE" -p | tail -3 | grep -q 'INSERT'; then
      return 0
    fi
    sleep 1
    count=$((count + 1))
  done
  echo "⚠️ Timeout waiting for prompt in slot $SLOT" >&2
  return 1
}

# Wait for idle (unless --force)
if [ "$FORCE" != "--force" ] && is_claude_active; then
  wait_for_idle 10 || exit 1
elif [ "$FORCE" = "--force" ]; then
  echo "⚡ Force mode — sending immediately (skipping idle wait)"
fi

# ── Route through MoP gateway ──────────────────────────
# All commands and file pastes go through MoP POST /slots/N/send
# MoP handles: INSERT mode detection, tmux send-keys, load-buffer/paste-buffer,
# and plan approval gate (command "2" → approve-plan with Codex check).

FORCE_JSON="false"
[ "$FORCE" = "--force" ] && FORCE_JSON="true"

if [ -n "$FILE" ]; then
  # File mode
  if [ ! -f "$FILE" ]; then
    echo "ERROR: File not found: $FILE" >&2
    exit 1
  fi

  # Pre-delivery sanity check: reject any --file payload whose first non-blank
  # line is a slash-command literal. Slot Claude Code instances cannot interpret
  # PM-only slash commands (/dev-handoff, /rework-handoff, /handoff, /cleanup-pr,
  # /slot-idle, /slot-active, /check-slot, /escalate). Body files that begin with
  # such a literal cause the slot to render "Unknown command: <cmd>" and the
  # remaining lines paste as a user message that never triggers a Claude response.
  # Reference: ~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/feedback_bg_agent_handoff_body_file_must_not_prepend_slash_command_literal.md
  FIRST_NON_BLANK_LINE=$(awk 'NF{print; exit}' "$FILE")
  if echo "$FIRST_NON_BLANK_LINE" | grep -qE '^/[a-z]+(-[a-z]+)*([[:space:]]|$)'; then
    echo "ERROR: send-to-slot.sh refusing to deliver $FILE — first non-blank line is a slash-command literal." >&2
    echo "  Offending line: $FIRST_NON_BLANK_LINE" >&2
    echo "  Slot Claude Code does not have PM-only slash commands registered." >&2
    echo "  Strip the literal from line 1 of the body file and retry." >&2
    echo "  See feedback_bg_agent_handoff_body_file_must_not_prepend_slash_command_literal." >&2
    exit 2
  fi

  FILESIZE=$(wc -c < "$FILE" | tr -d ' ')

  RESULT=$(curl -s -X POST "http://localhost:${MOP_PORT}/slots/${SLOT}/send" \
    -H "Content-Type: application/json" \
    -d "{\"file\": \"${FILE}\", \"force\": ${FORCE_JSON}}")

  OK=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print('OK' if d.get('success') else d.get('error','unknown'))" 2>/dev/null)

  if [ "$OK" = "OK" ]; then
    echo "✓ Pasted file to slot $SLOT: $FILE ($FILESIZE bytes)"
  else
    echo "ERROR: $OK" >&2
    echo "Full response: $RESULT" >&2
    exit 1
  fi
else
  # Command mode — escape for JSON
  COMMAND_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$COMMAND" 2>/dev/null)

  RESULT=$(curl -s -X POST "http://localhost:${MOP_PORT}/slots/${SLOT}/send" \
    -H "Content-Type: application/json" \
    -d "{\"command\": ${COMMAND_JSON}, \"force\": ${FORCE_JSON}}")

  OK=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print('OK' if d.get('success') else d.get('error','unknown'))" 2>/dev/null)

  if [ "$OK" = "OK" ]; then
    echo "✓ Sent to slot $SLOT: $COMMAND"
  else
    echo "ERROR: $OK" >&2
    echo "Full response: $RESULT" >&2
    exit 1
  fi
fi

# If --wait flag, wait for command to complete AND slot to become idle
if [ "$WAIT" = "--wait" ]; then
  echo "⏳ Waiting for completion..."
  if wait_for_prompt 60; then
    sleep 1
    if is_claude_active; then
      echo "  Slot became active again, waiting for idle..."
      wait_for_idle 10
    fi
    echo "✓ Command completed"
  fi
fi
