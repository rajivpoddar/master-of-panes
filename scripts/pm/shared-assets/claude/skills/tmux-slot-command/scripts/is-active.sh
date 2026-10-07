#!/bin/bash
# Read-only activity probe. 0=active, 1=proven idle, 2=unknown/error.
# The shared slot-pane resolver verifies checkout and immutable pane identity.
# Usage: is-active.sh <0-8> [-v|--debug|--fast] [--pane-id %N]
SLOT="${1:-}"
[ -n "$SLOT" ] || exit 2
shift
FLAG=""
PIN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --pane-id) PIN="${2:-}"; [ -n "$PIN" ] || exit 2; shift ;;
    -v|--debug|--fast) FLAG="$1" ;;
    *) exit 2 ;;
  esac
  shift
done
[ -z "$PIN" ] || [[ "$PIN" =~ ^%[0-9]+$ ]] || exit 2
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
resolve_pane() {
  local got
  got=$("$SCRIPT_DIR/slot-pane.sh" "$SLOT" 2>/dev/null) || return 2
  [ -z "$PIN" ] || [ "$got" = "$PIN" ] || return 2
  printf '%s\n' "$got"
}
PANE=$(resolve_pane) || exit 2
same_pane() { local current; current=$(resolve_pane) && [ "$current" = "$PANE" ]; }
output=$(tmux capture-pane -e -t "$PANE" -p 2>/dev/null) || exit 2
[ -n "$output" ] && same_pane || exit 2
chevron_line=$(printf '%s\n' "$output" | grep '❯' | tail -1)
[ -n "$chevron_line" ] || exit 2
if [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ]; then
  printf 'pane=%s\n%s\n' "$PANE" "$chevron_line"
fi
printf '%s\n' "$output" | python3 -c '
import re,sys
text=sys.stdin.read(); sgr=re.compile(r"\x1b\[([0-9;]*)m")
lines=text.splitlines(); line=next((l for l in reversed(lines) if re.match(r"^\s*❯(?:\s|$)",sgr.sub("",l))),None)
if line is None: sys.exit(2)
color="default"
# SGR foreground may be inherited from a preceding line.
prefix=text[:text.rfind(line)+line.index("❯")]
for match in sgr.finditer(prefix):
    codes=[int(c) for c in (match[1] or "0").split(";")]; i=0
    while i<len(codes):
        c=codes[i]
        if c in (0,39): color="default"
        elif c==38 and i+1<len(codes) and codes[i+1]==2:
            color=";".join(map(str,codes[i+2:i+5])); i+=4
        elif c==38 and i+1<len(codes) and codes[i+1]==5:
            color="indexed:"+str(codes[i+2]) if i+2<len(codes) else "unknown"; i+=2
        elif 30<=c<=37 or 90<=c<=97: color=str(c)
        i+=1
sys.exit(0 if color=="153;153;153" else 1 if color in ("default","37","97","255;255;255") else 2)
'
activity=$?
same_pane || exit 2
[ "$activity" -eq 1 ] || exit "$activity"
[ "$FLAG" = "--fast" ] && exit 1

# Retain content-change fallback, pinned to the same pane throughout.
plain1=$(tmux capture-pane -t "$PANE" -p 2>/dev/null) || exit 2
[ -n "$plain1" ] && same_pane || exit 2
sleep 1.5
plain2=$(tmux capture-pane -t "$PANE" -p 2>/dev/null) || exit 2
[ -n "$plain2" ] && same_pane || exit 2
content() {
  local count
  count=$(printf '%s\n' "$1" | wc -l | tr -d ' ')
  if [ "$count" -gt 6 ]; then printf '%s\n' "$1" | head -n "$((count - 6))"; else printf '%s\n' "$1"; fi
}
if [ "$(content "$plain1")" != "$(content "$plain2")" ]; then exit 0; fi
exit 1
