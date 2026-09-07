#!/usr/bin/env bash
# Block raw /clear delivery from PM/control-plane paths.
#
# Session clearing must go through MoP logged clear so every clear attempt has
# MoP event-log evidence. The slash is required: prose such as "clear error"
# is not a lifecycle command.

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
      json_block "BLOCKED: do not send /clear through mop_send_to_slot. Use MoP logged clear instead: mop_clear_slot(slot: \"${target:-N}\") when loadable, or bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh \"${target:-N}\"."
    fi
    ;;
  Bash)
    if is_clear_command "$COMMAND" && printf '%s' "$COMMAND" | grep -Eq '(tmux[[:space:]]+send-keys|send-to-slot\.sh|run-and-wait\.sh)'; then
      json_block "BLOCKED: do not inject /clear with raw tmux or slot scripts. Use MoP logged clear instead: mop_clear_slot(slot: \"N\"|\"pm\"|\"all\") when loadable, or bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh N."
    fi
    ;;
esac

exit 0
