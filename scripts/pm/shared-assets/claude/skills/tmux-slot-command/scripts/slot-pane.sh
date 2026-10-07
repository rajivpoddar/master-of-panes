#!/bin/bash
# slot-pane.sh v1 (MoP 2026-10-07): resolve a MoP slot to its VERIFIED
# immutable tmux pane id (%N). Same contract as MoP src/paneIdentity.ts
# verifyPaneIdentity(): the slot's explicit pane address is only a hint; the
# pane is accepted only when its checkout (git toplevel of pane_current_path)
# equals the slot's checkout. Otherwise exactly one pane in window 0:0 must
# match; zero or several matches fail closed.
#
# Usage:
#   slot-pane.sh <slot>                 # print %N, exit 0
#   slot-pane.sh <slot> --pane-id %N    # verify a pinned pane id, print it
# Exit 2 = identity unknown, missing or rebound (fail closed; nothing printed).
#
# Panes are numbered by screen position (Rajiv 2026-10-07: S6 below S5 is
# index 7, S7 is index 6), so never derive a pane from the slot number.

SLOT="${1:-}"
PIN=""
if [ "${2:-}" = "--pane-id" ]; then PIN="${3:-}"; fi
TMUX_BIN="${MOP_TMUX_BIN:-tmux}"
WINDOW="0:0"

case "$SLOT" in
  0) ADDR="0:0.0"; EXPECTED="/Users/rajiv/Downloads/projects/heydonna-app" ;;
  1) ADDR="0:0.1"; EXPECTED="/Users/rajiv/Downloads/projects/heydonna-app-3001" ;;
  2) ADDR="0:0.2"; EXPECTED="/Users/rajiv/Downloads/projects/heydonna-app-3002" ;;
  3) ADDR="0:0.3"; EXPECTED="/Users/rajiv/Downloads/projects/heydonna-app-3003" ;;
  4) ADDR="0:0.4"; EXPECTED="/Users/rajiv/Downloads/projects/heydonna-app-3004" ;;
  5) ADDR="0:0.5"; EXPECTED="/Users/rajiv/Downloads/projects/heydonna-app-3005" ;;
  6) ADDR="0:0.7"; EXPECTED="/Users/rajiv/Downloads/projects/heydonna-app-3006" ;;
  7) ADDR="0:0.6"; EXPECTED="/Users/rajiv/Downloads/projects/heydonna-app-3007" ;;
  8) ADDR="0:0.8"; EXPECTED="/Users/rajiv/Downloads/projects/heydonna-app-3008" ;;
  *) echo "slot-pane: unknown slot '$SLOT'" >&2; exit 2 ;;
esac

# Prints "%N" when the "%N|path" line is that slot's checkout.
match_line() {
  local line="$1" id path top
  id="${line%%|*}"
  path="${line#*|}"
  [[ "$id" =~ ^%[0-9]+$ ]] || return 1
  [ -n "$path" ] && [ "$path" != "$line" ] || return 1
  top=$(git -C "$path" rev-parse --show-toplevel 2>/dev/null) || return 1
  [ "$top" = "$EXPECTED" ] || return 1
  printf '%s\n' "$id"
}

if [ -n "$PIN" ]; then
  [[ "$PIN" =~ ^%[0-9]+$ ]] || { echo "slot-pane: bad pinned pane id '$PIN'" >&2; exit 2; }
  line=$("$TMUX_BIN" display-message -t "$PIN" -p '#{pane_id}|#{pane_current_path}' 2>/dev/null | head -1)
  got=$(match_line "$line") || { echo "slot-pane: pinned $PIN is not slot $SLOT ($EXPECTED)" >&2; exit 2; }
  [ "$got" = "$PIN" ] || { echo "slot-pane: pinned $PIN rebound to $got" >&2; exit 2; }
  printf '%s\n' "$PIN"
  exit 0
fi

line=$("$TMUX_BIN" display-message -t "$ADDR" -p '#{pane_id}|#{pane_current_path}' 2>/dev/null | head -1)
if got=$(match_line "$line"); then
  printf '%s\n' "$got"
  exit 0
fi

listed=$("$TMUX_BIN" list-panes -t "$WINDOW" -F '#{pane_id}|#{pane_current_path}' 2>/dev/null)
matches=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  if got=$(match_line "$line"); then matches+=("$got"); fi
done <<< "$listed"
if [ "${#matches[@]}" -eq 1 ]; then
  printf '%s\n' "${matches[0]}"
  exit 0
fi
echo "slot-pane: slot $SLOT expected exactly one pane on $EXPECTED, found ${#matches[@]}" >&2
exit 2
