#!/bin/bash
# Shared library for tmux slot management scripts.
#
# Provides: slot validation, jq dependency check, file locking, safe JSON writes.
# Source this from other scripts: source "$(dirname "$0")/slot-lib.sh"

SLOT_STATE_DIR="$HOME/.claude/tmux-slots"
_SLOT_LOCK_DIR=""

# Fail if jq is not installed.
require_jq() {
  if ! command -v jq &>/dev/null; then
    echo "ERROR: jq is required but not installed. Install with: brew install jq" >&2
    exit 1
  fi
}

# Validate slot number is 1-4. Prevents path traversal and malformed JSON.
validate_slot() {
  local slot="$1"
  if ! [[ "$slot" =~ ^[1-4]$ ]]; then
    echo "ERROR: Slot must be 1-4, got: $slot" >&2
    exit 1
  fi
}

# Acquire per-slot exclusive lock using mkdir (atomic on macOS + Linux).
# Sets EXIT trap to auto-release. Only one lock can be held per process.
acquire_slot_lock() {
  local slot="$1"
  _SLOT_LOCK_DIR="$SLOT_STATE_DIR/.slot-${slot}.lock"
  if ! mkdir "$_SLOT_LOCK_DIR" 2>/dev/null; then
    echo "ERROR: Slot $slot is locked by another process" >&2
    exit 1
  fi
  trap 'rmdir "$_SLOT_LOCK_DIR" 2>/dev/null' EXIT
}

# Atomically update a JSON state file using jq.
# Uses mktemp for unique temp files and checks all exit codes.
#
# Usage: safe_jq_update <state_file> [jq_args...] <jq_filter>
#   safe_jq_update "$FILE" --arg foo bar '.key = $foo'
safe_jq_update() {
  local state_file="$1"
  shift
  local tmp
  tmp=$(mktemp "${state_file}.XXXXXX") || {
    echo "ERROR: Failed to create temp file for $state_file" >&2
    return 1
  }
  if ! jq "$@" "$state_file" > "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    echo "ERROR: Failed to process $state_file (jq error)" >&2
    return 1
  fi
  if ! mv "$tmp" "$state_file"; then
    rm -f "$tmp"
    echo "ERROR: Failed to update $state_file (mv error)" >&2
    return 1
  fi
}

# Strip the prompt line and everything after it from tmux capture-pane output.
#
# Ghost text safety: Claude Code's autocomplete predictions appear on the ❯
# prompt line and are INDISTINGUISHABLE from real input in plain capture-pane
# output. This function removes the last ❯ line and everything below it,
# returning only trusted output (text above the prompt).
#
# Usage: tmux capture-pane -t "$PANE" -p | strip_prompt_line
strip_prompt_line() {
  local input
  input=$(cat)

  # Find the last line number containing ❯
  local last_chevron
  last_chevron=$(echo "$input" | grep -n '❯' | tail -1 | cut -d: -f1)

  if [ -z "$last_chevron" ]; then
    # No ❯ found — output everything (not a standard Claude Code prompt)
    echo "$input"
  elif [ "$last_chevron" -le 1 ]; then
    # ❯ is on the first line — nothing safe above it
    :
  else
    # Output everything before the last ❯ line
    echo "$input" | head -n $((last_chevron - 1))
  fi
}

# Ensure state directory and a slot's state file exist.
ensure_state_file() {
  local slot="$1"
  mkdir -p "$SLOT_STATE_DIR"
  local state_file="$SLOT_STATE_DIR/slot-${slot}.json"
  if [ ! -f "$state_file" ]; then
    cat > "$state_file" << EOF
{
  "slot": $slot,
  "occupied": false,
  "pane": "0:0.$slot",
  "session_id": null,
  "task": null,
  "branch": null,
  "assigned_at": null,
  "last_activity": null
}
EOF
  fi
}
