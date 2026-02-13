#!/bin/bash
# Shared library for tmux pane management scripts.
#
# Provides: config loading, pane validation, jq dependency check, file locking,
# safe JSON writes, ghost text stripping.
# Source this from other scripts: source "$(dirname "$0")/pane-lib.sh"

PANE_STATE_DIR="$HOME/.claude/tmux-panes"
_PANE_LOCK_DIR=""

# Config defaults — overridden by load_config() if config.json exists.
NUM_DEV_PANES=4
MANAGER_PANE="0:0.0"

# Newline-separated list of dev pane addresses (bash 3.x safe, no arrays).
_DEV_PANE_LIST=""

_CONFIG_LOADED=false

# Load configuration from config.json, or use defaults.
# Reads the file once (atomic snapshot) to prevent mixed reads during writes.
# Validates all values and falls back to defaults on bad data.
load_config() {
  if [ "$_CONFIG_LOADED" = true ]; then
    return 0
  fi
  _CONFIG_LOADED=true

  local config_file="$PANE_STATE_DIR/config.json"
  if [ -f "$config_file" ]; then
    if command -v jq &>/dev/null; then
      # Read file once into memory — prevents partial-read race with writers
      local config_content
      config_content=$(cat "$config_file" 2>/dev/null) || {
        echo "WARNING: Could not read config.json. Using defaults." >&2
        config_content=""
      }
      if [ -n "$config_content" ]; then
        MANAGER_PANE=$(echo "$config_content" | jq -r '.panes.manager // "0:0.0"' 2>/dev/null) || MANAGER_PANE="0:0.0"
        _DEV_PANE_LIST=$(echo "$config_content" | jq -r '.panes.dev[]' 2>/dev/null) || _DEV_PANE_LIST=""
        NUM_DEV_PANES=$(echo "$_DEV_PANE_LIST" | grep -c .) || NUM_DEV_PANES=0

        local custom_dir
        custom_dir=$(echo "$config_content" | jq -r '.state_dir // ""' 2>/dev/null) || custom_dir=""
        if [ -n "$custom_dir" ] && [ "$custom_dir" != "null" ]; then
          PANE_STATE_DIR="${custom_dir/#\~/$HOME}"
        fi
      fi
    fi
  else
    echo "Note: No config found. Using defaults ($NUM_DEV_PANES dev panes, manager $MANAGER_PANE)." >&2
    echo "  Run /master-of-panes:pane-setup to configure." >&2
  fi

  # Build default dev pane list if config didn't provide one
  if [ -z "$_DEV_PANE_LIST" ] || [ "$NUM_DEV_PANES" -eq 0 ]; then
    NUM_DEV_PANES=4
    _DEV_PANE_LIST="0:0.1
0:0.2
0:0.3
0:0.4"
  fi

  # Validate NUM_DEV_PANES
  if ! [[ "$NUM_DEV_PANES" =~ ^[0-9]+$ ]] || [ "$NUM_DEV_PANES" -lt 1 ] || [ "$NUM_DEV_PANES" -gt 99 ]; then
    echo "WARNING: Invalid dev pane count '$NUM_DEV_PANES'. Using default: 4" >&2
    NUM_DEV_PANES=4
    _DEV_PANE_LIST="0:0.1
0:0.2
0:0.3
0:0.4"
  fi

  # Validate each dev pane address contains a dot (session:window.pane format)
  local addr
  while IFS= read -r addr; do
    if [[ -n "$addr" ]] && ! [[ "$addr" == *.* ]]; then
      echo "WARNING: Dev pane address '$addr' missing dot separator (expected session:window.pane)" >&2
    fi
  done <<< "$_DEV_PANE_LIST"
}

# Fail if jq is not installed.
require_jq() {
  if ! command -v jq &>/dev/null; then
    echo "ERROR: jq is required but not installed. Install with: brew install jq" >&2
    exit 1
  fi
}

# Check if pane number is valid (1..NUM_DEV_PANES). Returns 1 on failure (does not exit).
# Use this when the caller needs to choose its own exit code (e.g., is-active.sh exits 2).
check_pane() {
  local pane_num="$1"
  if ! [[ "$pane_num" =~ ^[0-9]+$ ]] || [ "$pane_num" -lt 1 ] || [ "$pane_num" -gt "$NUM_DEV_PANES" ]; then
    echo "ERROR: Pane must be 1-$NUM_DEV_PANES, got: $pane_num" >&2
    return 1
  fi
}

# Validate pane number is 1..NUM_DEV_PANES. Exits 1 on failure.
# For scripts where exit 1 ≠ a meaningful status, use this directly.
validate_pane() {
  check_pane "$1" || exit 1
}

# Acquire per-pane exclusive lock using mkdir (atomic on macOS + Linux).
# Sets EXIT trap to auto-release. Only one lock can be held per process.
acquire_pane_lock() {
  local pane_num="$1"
  _PANE_LOCK_DIR="$PANE_STATE_DIR/.pane-${pane_num}.lock"
  if ! mkdir "$_PANE_LOCK_DIR" 2>/dev/null; then
    echo "ERROR: Pane $pane_num is locked by another process" >&2
    exit 1
  fi
  trap 'rmdir "$_PANE_LOCK_DIR" 2>/dev/null' EXIT
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
# Usage: tmux capture-pane -t "$PANE_ADDR" -p | strip_prompt_line
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

# Look up the tmux address for dev pane N from _DEV_PANE_LIST.
pane_address() {
  echo "$_DEV_PANE_LIST" | sed -n "${1}p"
}

# Validate that a tmux pane exists at the given address.
# Uses tmux display-message which is a single-command check.
# Returns 0 if pane exists, 1 if not.
pane_exists() {
  tmux display-message -p -t "$1" '#{pane_id}' &>/dev/null
}

# Ensure state directory and a pane's state file exist.
# Uses mktemp + mv to prevent torn writes from concurrent startup.
ensure_pane_state() {
  local pane_num="$1"
  mkdir -p "$PANE_STATE_DIR"
  local state_file="$PANE_STATE_DIR/pane-${pane_num}.json"
  if [ ! -f "$state_file" ]; then
    local addr
    addr=$(pane_address "$pane_num")
    local tmp
    tmp=$(mktemp "${state_file}.XXXXXX") || return 1
    cat > "$tmp" << EOF
{
  "pane": $pane_num,
  "address": "$addr",
  "occupied": false,
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
