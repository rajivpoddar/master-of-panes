#!/usr/bin/env bash
# Block raw /clear delivery from PM/control-plane paths.
#
# Session clearing must go through MoP logged clear so every clear attempt has
# MoP event-log evidence. The slash is required: prose such as "clear error"
# is not a lifecycle command.
#
# Dev slots S1-S6 are NOT cleared on a cadence or by an injected /clear: their
# clearing belongs to the new-issue assignment boundary inside the atomic
# mop-assign-slot operation. This hook therefore points S1-S6 callers at
# mop-assign-slot, and at the distinct operator acknowledgement
# (--operator-confirm-dev-slot-clear) for a genuine manual dev-slot clear. PM
# self-clear is unchanged. This hook governs /clear INJECTION only; direct
# assignment-ownership calls are governed by the separate Slice-B hook.

INPUT=$(cat 2>/dev/null || echo "{}")

TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || echo "")
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null || echo "")
SLOT=$(printf '%s' "$INPUT" | jq -r '.tool_input.slot // empty' 2>/dev/null || echo "")

json_block() {
  jq -cn --arg message "$1" '{decision:"block", message:$message}'
}

is_clear_command() {
  printf '%s' "$1" | grep -Eq '(^|[[:space:];|&'"'"'"`])/clear([[:space:];|&'"'"'"`]|$)'
}

case "$TOOL_NAME" in
  mcp__plugin_master-of-panes_mop__mop_send_to_slot)
    if is_clear_command "$COMMAND"; then
      target="$SLOT"
      [ "$target" = "0" ] && target="pm"
      if [ "$target" = "pm" ]; then
        json_block "BLOCKED: do not send /clear through mop_send_to_slot. PM self-clear uses the MoP-logged path: mop_clear_slot(slot: \"pm\") when loadable, or bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh pm."
      else
        json_block "BLOCKED: do not send /clear through mop_send_to_slot. Dev slots S1-S6 are never cleared by an injected /clear. Clearing belongs to the new-issue assignment boundary: run the mop-assign-slot operation (python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py --slot N --class new_issue ... --task-file <file>). A genuine operator clear of a dev slot requires the distinct explicit acknowledgement: bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh \"${target:-N}\" --operator-confirm-dev-slot-clear."
      fi
    fi
    ;;
  Bash)
    if is_clear_command "$COMMAND" && printf '%s' "$COMMAND" | grep -Eq '(tmux[[:space:]]+send-keys|send-to-slot\.sh|run-and-wait\.sh)'; then
      json_block "BLOCKED: do not inject /clear with raw tmux or slot scripts. Dev slots S1-S6 are never cleared by an injected /clear; clearing belongs to the new-issue assignment boundary via mop-assign-slot (python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py --slot N --class new_issue ... --task-file <file>). PM self-clear uses mop_clear_slot(slot: \"pm\") / mop-clear-slot.sh pm. A genuine operator clear of a dev slot requires the distinct explicit acknowledgement --operator-confirm-dev-slot-clear."
    fi
    ;;
esac

exit 0
