#!/usr/bin/env bash
# Block direct MoP assignment-ownership calls.
#
# Rajiv ruling: PM must never call the MoP assign boundary directly. Assignment
# goes through the sanctioned atomic operation, which owns the session clear,
# the ownership transition, the literal delivery, and the dual readback:
#
#     python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py \
#         --slot N --class new_issue|repro|rework --repo-id <id> --issue <n> \
#         [tuple flags] --task-file <file>
#
# This hook governs ASSIGNMENT OWNERSHIP only. It does not police /clear
# injection (block-raw-clear-outside-mop.sh owns that class) and it never blocks
# the explicit operator release transition mop_release_slot.

INPUT=$(cat 2>/dev/null || echo "{}")

TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || echo "")
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null || echo "")

json_block() {
  jq -cn --arg message "$1" '{decision:"block", message:$message}'
}

refusal() {
  json_block "BLOCKED: do not call the MoP assignment-ownership boundary directly. Use the sanctioned atomic operation instead: python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py --slot N --class new_issue|repro|rework --repo-id <id> --issue <n> [--pr] [--branch] [--head] [--work-kind] [--handoff] [--claimed-at] --task-file <file>. Reaching POST /slots/N/assign or /slots/N/adopt-issue-claim directly (curl, raw HTTP, or an MCP assignment wrapper) is refused. The explicit operator release transition mop_release_slot is unaffected."
}

# Routes that reach assignment ownership. `assign-effect` is deliberately NOT
# matched: that is the sanctioned operation's own endpoint.
reaches_assign_ownership() {
  # Terminator is NEGATIVE: anything that is not an identifier continuation ends
  # the route token. `-` keeps `assign-effect` excluded, alnum keeps `assignee`
  # excluded, and every unattached shell metacharacter (`;` `|` `&` `>` `)` ...)
  # now matches. Enumerating metacharacters is how the previous hole was created.
  printf '%s' "$1" | grep -Eq "/slots/[^[:space:]\"']*/(assign|adopt-issue-claim)([^[:alnum:]-]|$)"
}

# Narrow allow: the command is EXACTLY one invocation of the sanctioned script
# (optional interpreter first), with nothing after it, so nothing can borrow this
# program word to smuggle a second, direct ownership call.
#
# The property is "exactly one invocation, nothing after it" - NOT "no newline
# characters anywhere". An INTERIOR newline or a backslash-continuation means the
# input is more than one invocation and must be refused; trailing whitespace and a
# trailing newline are legitimate (agents emit them constantly) and must still
# pass. Without the interior-newline test the anchored grep match would be
# satisfied by line 1 alone and a second line could ride through.
is_pure_sanctioned_invocation() {
  local trimmed
  # Strip TRAILING whitespace/newlines only, so a legitimate trailing newline
  # still passes while an interior one survives for the test below.
  trimmed="${1%"${1##*[![:space:]]}"}"
  case "$trimmed" in
    *$'\n'*) return 1 ;;
  esac
  # Command substitution and brace expansion can carry a hidden ownership call
  # inside an otherwise single invocation, e.g. --task-file $(curl .../assign).
  # NOTE the class is written WITHOUT a backslash: inside a POSIX bracket expression
  # `\$` is a literal BACKSLASH, not an escaped dollar, so `[\$`(){}]` would have
  # rejected bare backslashes and false-blocked the legitimate escaped-space path. The
  # ALLOW case below caught exactly that.
  # A BARE backslash stays legal (`/path with\ space/t.md`); only a backslash
  # introducing a line continuation is refused, which the newline test above
  # already catches because the continuation becomes an interior newline.
  printf '%s' "$trimmed" | grep -Eq '[$`(){}]' && return 1
  printf '%s' "$trimmed" | grep -Eq '^[[:space:]]*(python3(\.[0-9]+)?[[:space:]]+)?[^[:space:];&|<>]*/mop-assign-slot(\.py)?([[:space:]]|$)' \
    && ! printf '%s' "$trimmed" | grep -Eq '[;&|]|>>?|<'
}

case "$TOOL_NAME" in
  Bash)
    if reaches_assign_ownership "$COMMAND"; then
      if is_pure_sanctioned_invocation "$COMMAND"; then
        exit 0
      fi
      refusal
    fi
    ;;
  *mop_assign*|*mop_adopt_issue*|*mop_adopt*)
    refusal
    ;;
esac

exit 0
