#!/usr/bin/env bash
# Wrapper-exit completion relay. Fail open; preserve a local delivery receipt.
set -u
[[ $# == 4 ]] || exit 0
name=$1 rc=$2 out=$3 receipt=$4
result="$(python3 -I - "$out" <<'PY'
import os,pathlib,re,sys
try:
    lines=pathlib.Path(sys.argv[1]).read_text(errors='replace').splitlines()
    line=next((s.strip() for s in reversed(lines) if re.match(r'^\s*\**\s*(?:RESULT\b|DONE\s*:|BLOCKED\s*:|VERDICT\s*:)', s, re.I)), None)
    if line:
        for key, value in os.environ.items():
            if len(value) >= 6 and re.search(r'token|secret|password|api.?key', key, re.I):
                line = line.replace(value, '[REDACTED]')
        line = re.sub(r'(?i)(bearer\s+)[\w.~/+\-=]+', r'\1[REDACTED]', line)
        line = re.sub(r'(?i)((?:[\w-]*(?:token|secret|password|api[_-]?key)[\w-]*)["\x27]?\s*[:=]\s*)["\x27]?[^\s,;"\x27]+', r'\1[REDACTED]', line)
        line = re.sub(r'\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]+', '[REDACTED]', line)
        line = ''.join(c if c >= ' ' and c != '\x7f' else ' ' for c in line)[:400]
    print(('RESULT ' + line) if line and not line.upper().startswith('RESULT') else (line or 'RESULT UNAVAILABLE reason=no_final_result_line'))
except OSError:
    print('RESULT UNAVAILABLE reason=output_missing')
PY
)"
# Structured exit result (RESULT_BEGIN..RESULT_END JSON block before the final marker line). Fail open: any helper problem leaves the legacy
# result line untouched. A missing/invalid block is marked UNSTRUCTURED and its head is derived from git ls-remote (proxy-agent-notify.py result).
RESULT_STATUS=''; RESULT_HEAD=''; RESULT_VERDICT=''; RESULT_REASON=''; RESULT_JSON=''; RESULT_MSGJSON=''
if [[ -f "$HOME/.claude/scripts/proxy-agent-notify.py" && "$result" != 'RESULT UNAVAILABLE reason=output_missing' ]]; then
  structured="$(python3 -I "$HOME/.claude/scripts/proxy-agent-notify.py" result --name "${PROXY_AGENT_RUN_NAME:-$name}" --type "${PROXY_AGENT_STOP_TYPE:-}" \
    --brief "${PROXY_AGENT_STOP_BRIEF:-}" --out "$out" --jsonl "${PROXY_AGENT_STOP_JSONL:-}" --format shell 2>/dev/null)" || structured=''
  # The helper prints only shlex-quoted RESULT_* assignments.
  if [[ -n "$structured" && "$(printf '%s\n' "$structured" | grep -cvE '^RESULT_[A-Z]+=')" == 0 ]]; then eval "$structured"; fi
fi
display="$result"; frag=''
case "$RESULT_STATUS" in
  STRUCTURED)
    frag="result=STRUCTURED verdict=$RESULT_VERDICT head=$RESULT_HEAD RESULT_JSON $RESULT_MSGJSON"
    [[ "$result" != 'RESULT UNAVAILABLE reason=no_final_result_line' ]] || display='RESULT STRUCTURED';;
  UNSTRUCTURED)
    frag="head=${RESULT_HEAD:-UNKNOWN} reason=$RESULT_REASON"
    if [[ "$result" == 'RESULT UNAVAILABLE reason=no_final_result_line' ]]; then display='RESULT UNSTRUCTURED'; else frag="result=UNSTRUCTURED $frag"; fi;;
esac
reason=""
if python3 -I "$HOME/.claude/scripts/proxy-run-state.py" quota --out "$out" --log "${PROXY_AGENT_STOP_LOG:-}" --jsonl "${PROXY_AGENT_STOP_JSONL:-}"; then
  reason=quota
fi
# Disk-watchdog stop (Rajiv C0ALZJHGE49 thread 1795448835.183169). The watchdog writes a tiny marker file next to the lane log;
# read THAT, never grep a multi-GB log. Only set when quota did not fire, so an existing reason wording is untouched.
stop_why=""
if [[ -n "${PROXY_AGENT_STOP_LOG:-}" && -s "${PROXY_AGENT_STOP_LOG}.disk_low" ]]; then
  # Marker body is "disk_low free_kib=<n> low_kib=<n>"; fields 2-3 carry their own keys.
  stop_why="$(cut -d' ' -f2,3 "${PROXY_AGENT_STOP_LOG}.disk_low" 2>/dev/null) action=stopped_wip_preserved"
  [[ -n "$reason" ]] || reason=disk_low
fi
details="$(python3 -I "$HOME/.claude/scripts/proxy-observe.py" --stop-info "${PROXY_AGENT_STOP_JSONL:-}" 2>/dev/null)" || details='elapsed_seconds=UNKNOWN last_pushed_head=UNKNOWN'
message="PROXY_STOP name=$name rc=$rc out=$out $details $display${frag:+ $frag}"
# Surface every proxy end in the combined pane log (Rajiv D0AMF0XE6TS ts 1794638003.427449).
mkdir -p "$(dirname "${PROXY_AGENT_COMBINED_LOG:-/tmp/proxy-agent/all.log}")" 2>/dev/null || true
{ printf '%s %s %s [STOPPED] rc=%s %s\n' "$(date +%H:%M:%S)" "$name" "${PROXY_AGENT_STOP_MODEL:-?}" "$rc" "$(printf '%s' "$result" | tr '\n' ' ' | cut -c1-300)" >> "${PROXY_AGENT_COMBINED_LOG:-/tmp/proxy-agent/all.log}"; } 2>/dev/null || true
if [[ -n "$reason" ]]; then
  message="$message reason=$reason model=${PROXY_AGENT_STOP_MODEL:-UNAVAILABLE}"
fi
[[ -z "$stop_why" ]] || message="$message $stop_why"
# Automatic rung escalation (Rajiv D0AMF0XE6TS ts 1794641763.847759). Fail open: a helper failure leaves the stop message unchanged.
# Runs before `finish`, so the writer fence sees this run as still live and excludes its own name. A disk_low stop is excluded:
# relaunching a lane on a higher rung while / is still below the threshold would immediately refill the disk.
esc=''
if [[ "$reason" != quota && "$reason" != disk_low && -f "$HOME/.claude/scripts/proxy-escalate.py" ]]; then
  esc="$(python3 -I "$HOME/.claude/scripts/proxy-escalate.py" --name "${PROXY_AGENT_RUN_NAME:-$name}" --rc "$rc" --out "$out" \
    --jsonl "${PROXY_AGENT_STOP_JSONL:-}" --model "${PROXY_AGENT_STOP_MODEL:-}" --details "$details" --result "$result" 2>/dev/null)" || esc=''
fi
if [[ -n "$esc" ]]; then message="$message $esc"; fi
if [[ -n "${PROXY_AGENT_STOP_LOG:-}" ]]; then printf '\n[%s] %s\n' "$(date -u +%FT%TZ)" "$message" >> "$PROXY_AGENT_STOP_LOG" 2>/dev/null || true; fi
if [[ -x "$HOME/.claude/scripts/message-pm.sh" ]]; then
  "$HOME/.claude/scripts/message-pm.sh" -- "$message" > "$receipt" 2>&1 || printf 'PROXY_STOP_DELIVERY_UNAVAILABLE\n' >> "$receipt"
else
  printf 'PROXY_STOP_DELIVERY_UNAVAILABLE reason=message_pm_missing\n' > "$receipt"
fi
if [[ -n "${PROXY_AGENT_RUN_TOKEN:-}" && -n "${PROXY_AGENT_RUN_NAME:-}" ]]; then
  status=unavailable
  if [[ "$result" != 'RESULT UNAVAILABLE reason='* || "$RESULT_STATUS" == STRUCTURED ]]; then
    status=result
  elif [[ "$reason" == quota ]]; then
    status=quota
  elif [[ "$rc" != 0 ]]; then
    status=failed
  fi
  python3 -I "$HOME/.claude/scripts/proxy-run-state.py" finish --name "$PROXY_AGENT_RUN_NAME" --token "$PROXY_AGENT_RUN_TOKEN" --rc "$rc" --reason "$status" --result "$display" \
    --result-status "$RESULT_STATUS" --result-json "$RESULT_JSON" --head "$RESULT_HEAD" \
    || printf 'PROXY_AGENT_WARN reason=run_state_finish_failed\n' >&2
fi
exit 0
