#!/bin/bash
# Shared library for tmux slot management scripts.
#
# Provides: config loading, slot validation, jq dependency check, file locking,
# safe JSON writes, ghost text stripping.
# Source this from other scripts: source "$(dirname "$0")/slot-lib.sh"

SLOT_STATE_DIR="$HOME/.claude/tmux-slots"
_SLOT_LOCK_DIR=""

# Config defaults — overridden by load_config() if config.json exists.
NUM_SLOTS=4
PANE_PREFIX="0:0"
MANAGER_PANE="0:0.0"

# Derived from PANE_PREFIX — set by load_config().
TMUX_SESSION=""
TMUX_WINDOW=""

_CONFIG_LOADED=false

# Load configuration from config.json, or use defaults.
# Reads the file once (atomic snapshot) to prevent mixed reads during writes.
# Validates all values and falls back to defaults on bad data.
load_config() {
  if [ "$_CONFIG_LOADED" = true ]; then
    return 0
  fi
  _CONFIG_LOADED=true

  local config_file="$SLOT_STATE_DIR/config.json"
  if [ -f "$config_file" ]; then
    if command -v jq &>/dev/null; then
      # Read file once into memory — prevents partial-read race with writers
      local config_content
      config_content=$(cat "$config_file" 2>/dev/null) || {
        echo "WARNING: Could not read config.json. Using defaults." >&2
        config_content=""
      }
      if [ -n "$config_content" ]; then
        NUM_SLOTS=$(echo "$config_content" | jq -r '.slots // 4' 2>/dev/null) || NUM_SLOTS=4
        PANE_PREFIX=$(echo "$config_content" | jq -r '.pane_prefix // "0:0"' 2>/dev/null) || PANE_PREFIX="0:0"
        MANAGER_PANE=$(echo "$config_content" | jq -r '.manager_pane // "0:0.0"' 2>/dev/null) || MANAGER_PANE="0:0.0"
        local custom_dir
        custom_dir=$(echo "$config_content" | jq -r '.state_dir // ""' 2>/dev/null) || custom_dir=""
        if [ -n "$custom_dir" ] && [ "$custom_dir" != "null" ]; then
          SLOT_STATE_DIR="${custom_dir/#\~/$HOME}"
        fi
      fi
    fi
  else
    echo "Note: No config found. Using defaults ($NUM_SLOTS slots, pane prefix $PANE_PREFIX)." >&2
    echo "  Run /master-of-panes:setup to configure." >&2
  fi

  # Validate NUM_SLOTS is a positive integer
  if ! [[ "$NUM_SLOTS" =~ ^[0-9]+$ ]] || [ "$NUM_SLOTS" -lt 1 ] || [ "$NUM_SLOTS" -gt 99 ]; then
    echo "WARNING: Invalid slots value '$NUM_SLOTS' in config. Using default: 4" >&2
    NUM_SLOTS=4
  fi

  # Validate PANE_PREFIX contains exactly one colon (session:window format)
  if ! [[ "$PANE_PREFIX" =~ ^[^:]+:[^:]+$ ]]; then
    echo "WARNING: Invalid pane_prefix '$PANE_PREFIX' (must be 'session:window'). Using default: 0:0" >&2
    PANE_PREFIX="0:0"
  fi

  # Derive tmux session and window from pane prefix
  TMUX_SESSION="${PANE_PREFIX%%:*}"
  TMUX_WINDOW="$PANE_PREFIX"
}

# Fail if jq is not installed.
require_jq() {
  if ! command -v jq &>/dev/null; then
    echo "ERROR: jq is required but not installed. Install with: brew install jq" >&2
    exit 1
  fi
}

# Check if slot number is valid (1..NUM_SLOTS). Returns 1 on failure (does not exit).
# Use this when the caller needs to choose its own exit code (e.g., is-active.sh exits 2).
check_slot() {
  local slot="$1"
  if ! [[ "$slot" =~ ^[0-9]+$ ]] || [ "$slot" -lt 1 ] || [ "$slot" -gt "$NUM_SLOTS" ]; then
    echo "ERROR: Slot must be 1-$NUM_SLOTS, got: $slot" >&2
    return 1
  fi
}

# Validate slot number is 1..NUM_SLOTS. Exits 1 on failure.
# For scripts where exit 1 ≠ a meaningful status, use this directly.
validate_slot() {
  check_slot "$1" || exit 1
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

# Build the pane address for a slot: "${PANE_PREFIX}.<slot>"
slot_pane() {
  echo "${PANE_PREFIX}.$1"
}

# Ensure state directory and a slot's state file exist.
# Uses mktemp + mv to prevent torn writes from concurrent startup.
ensure_state_file() {
  local slot="$1"
  mkdir -p "$SLOT_STATE_DIR"
  local state_file="$SLOT_STATE_DIR/slot-${slot}.json"
  if [ ! -f "$state_file" ]; then
    local tmp
    tmp=$(mktemp "${state_file}.XXXXXX") || return 1
    cat > "$tmp" << EOF
{
  "slot": $slot,
  "occupied": false,
  "pane": "${PANE_PREFIX}.$slot",
  "session_id": null,
  "task": null,
  "branch": null,
  "assigned_at": null,
  "last_activity": null
}
EOF
    # Re-check after write — if another process created it, discard ours
    if [ -f "$state_file" ]; then
      rm -f "$tmp"
    else
      mv "$tmp" "$state_file" 2>/dev/null || rm -f "$tmp"
    fi
  fi
}
