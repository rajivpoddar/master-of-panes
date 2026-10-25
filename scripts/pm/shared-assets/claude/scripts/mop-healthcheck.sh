#!/bin/bash
# MoP HTTP server healthcheck + auto-restart
#
# Runs every 60s via launchd (com.heydonna.mop-healthcheck.plist).
# - curl http://localhost:3100/health with 5s max timeout
# - On failure: increment counter at /tmp/mop-healthcheck-fail-count.txt
# - When counter >= 3 (~3 min down): trigger restart per mop-restart skill
# - On success: reset counter to 0
# - Logs to /tmp/mop-healthcheck.log (rotated at 1MB)
#
# Companion: ~/Library/LaunchAgents/com.heydonna.mop-healthcheck.plist
# Created per Rajiv directive 2026-05-16 20:10 IST thread `1778941863.642439`
# Diagnosis: /tmp/mop-idle-diagnosis-2026-05-16.md
#
# 2026-05-27: MoP is launchd-owned by com.heydonna.mop-server. The historical
# /tmp/mop-server.pid file is no longer authoritative and must not be the first
# restart target. Use launchctl kickstart for the launchd service. A failed kickstart
# of a still-loaded service is reported, never worked around (no port kill, no
# second server); only proven launchd absence re-bootstraps the plist.

set -u

LOG_FILE="${MOP_HC_LOG_FILE:-/tmp/mop-healthcheck.log}"
COUNTER_FILE="${MOP_HC_COUNTER_FILE:-/tmp/mop-healthcheck-fail-count.txt}"
MOP_CURRENT="${MOP_HC_CURRENT:-$HOME/.local/share/master-of-panes/current}"
MOP_PLIST="${MOP_HC_PLIST:-$HOME/Library/LaunchAgents/com.heydonna.mop-server.plist}"
# Startup grace for the launchd child (seconds). Same default and env name as the
# installer's finite health budget (scripts/install-release.py STARTUP_GRACE_SECONDS).
STARTUP_GRACE="${MOP_STARTUP_GRACE_SECONDS:-150}"
LAUNCHD_LABEL="com.heydonna.mop-server"
THRESHOLD=3
LOG_MAX_BYTES=1048576  # 1 MB

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG_FILE"
}

# Rotate the log when it crosses 1 MB.
if [ -f "$LOG_FILE" ]; then
  SIZE=$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
  if [ "$SIZE" -gt "$LOG_MAX_BYTES" ]; then
    mv "$LOG_FILE" "${LOG_FILE}.1" 2>/dev/null || true
    : > "$LOG_FILE"
    log "ROTATED previous log (>${LOG_MAX_BYTES}B) to ${LOG_FILE}.1"
  fi
fi

# Read current counter (default 0).
COUNTER=0
if [ -f "$COUNTER_FILE" ]; then
  COUNTER=$(cat "$COUNTER_FILE" 2>/dev/null | head -1 | tr -d '[:space:]')
  case "$COUNTER" in
    ''|*[!0-9]*) COUNTER=0 ;;
  esac
fi

# Health probe. -f makes non-2xx an error; --max-time 5s prevents launchd queue pile-up.
#
# Bind to 127.0.0.1 explicitly (Rajiv directive 2026-05-27 21:58 IST DM
# thread `1779899308.449479` spec item 4) so we don't depend on DNS
# resolution of `localhost` (some macOS configurations resolve to ::1
# while the Hono server binds IPv4 by default).
#
# Classify failure shape for log analysis: `connect_refused` (curl exit 7)
# = process gone or not bound. `timeout` (curl exit 28) = process alive
# but event-loop wedged (the symptom this healthcheck refactor targets).
PROBE_URL="http://127.0.0.1:3100/health"
CURL_OUT=$(curl --max-time 5 -fs -o /dev/null -w '%{http_code}' "$PROBE_URL" 2>/dev/null)
CURL_RC=$?
if [ $CURL_RC -eq 0 ] && [ "$CURL_OUT" = "200" ]; then
  # Healthy — reset counter only when we'd previously logged failures.
  if [ "$COUNTER" -gt 0 ]; then
    log "HEALTHY — resetting fail counter (was $COUNTER)"
    echo 0 > "$COUNTER_FILE"
  fi
  exit 0
fi

# etime ([[dd-]hh:]mm:ss) -> seconds.
etime_seconds() {
  echo "$1" | awk -F'[-:]' '{ n=NF; s=$n+60*$(n-1); if (n>=3) s+=3600*$(n-2); if (n>=4) s+=86400*$(n-3); print s }'
}

# Startup grace: a launchd child that was started but has not bound yet is
# "starting", not "down". Do not count it, kickstart it again, or spawn a
# competing server until its grace window has elapsed.
UID_NUM=$(id -u)
PRINT_OUT=$(launchctl print "gui/${UID_NUM}/${LAUNCHD_LABEL}" 2>&1)
PRINT_RC=$?
if [ "$PRINT_RC" -eq 0 ]; then
  PRINT_STATE=loaded
elif [ "$PRINT_RC" -eq 113 ] && echo "$PRINT_OUT" | grep -q "Could not find service"; then
  PRINT_STATE=not_found
else
  PRINT_STATE=unknown
fi
CHILD_PID=""
if [ "$PRINT_STATE" = "loaded" ]; then
  CHILD_PID=$(echo "$PRINT_OUT" | awk '/^[[:space:]]*pid = [0-9]+/ {print $3; exit}')
fi
if [ -n "${CHILD_PID:-}" ]; then
  CHILD_ETIME=$(ps -o etime= -p "$CHILD_PID" 2>/dev/null | tr -d '[:space:]')
  if [ -n "$CHILD_ETIME" ]; then
    CHILD_AGE=$(etime_seconds "$CHILD_ETIME")
    if [ "${CHILD_AGE:-999999}" -lt "$STARTUP_GRACE" ]; then
      log "STARTING launchd child pid=$CHILD_PID age=${CHILD_AGE}s < grace ${STARTUP_GRACE}s — not counting, no restart, no fallback"
      echo 0 > "$COUNTER_FILE"
      exit 0
    fi
  fi
fi

# Unhealthy — classify and increment.
case "$CURL_RC" in
  7)  FAIL_CLASS="connect_refused" ;;
  28) FAIL_CLASS="timeout" ;;
  6)  FAIL_CLASS="resolve_failed" ;;
  *)  FAIL_CLASS="other_curl_rc=${CURL_RC}_http=${CURL_OUT}" ;;
esac
COUNTER=$((COUNTER + 1))
echo "$COUNTER" > "$COUNTER_FILE"

# Timeout means the MoP process may be alive but its Node event loop is
# temporarily starved by slot classifiers/tmux probes. Do not restart-loop on
# transient lag; only restart quickly on connect-refused/process-gone classes.
TIMEOUT_THRESHOLD="${MOP_HEALTHCHECK_TIMEOUT_THRESHOLD:-10}"
EFFECTIVE_THRESHOLD="$THRESHOLD"
if [ "$FAIL_CLASS" = "timeout" ]; then
  EFFECTIVE_THRESHOLD="$TIMEOUT_THRESHOLD"
fi

log "UNHEALTHY (${FAIL_CLASS}) — fail count now $COUNTER/$EFFECTIVE_THRESHOLD"

if [ "$COUNTER" -lt "$EFFECTIVE_THRESHOLD" ]; then
  exit 0
fi

# Threshold breached — restart per launchd. Process liveness is handled by
# KeepAlive; this watchdog catches the "process alive but HTTP wedged" case.
log "restart_triggered THRESHOLD BREACHED ($COUNTER >= $EFFECTIVE_THRESHOLD, last_class=${FAIL_CLASS}) — restarting MoP HTTP server via launchd"

if [ "$PRINT_STATE" = "loaded" ]; then
  if launchctl kickstart -k "gui/${UID_NUM}/${LAUNCHD_LABEL}" >>"$LOG_FILE" 2>&1; then
    # Started successfully: launchd is the single owner. The child gets the full
    # startup grace on the next ticks; no fallback server, no second kickstart.
    log "  launchctl kickstart -k gui/${UID_NUM}/${LAUNCHD_LABEL} succeeded; waiting ${STARTUP_GRACE}s grace, no fallback"
    echo 0 > "$COUNTER_FILE"
    exit 0
  fi
  # launchd still owns the service, so a failed kickstart does NOT prove its
  # child is gone. Preserve the single owner: no port kill, no second server.
  # Report a bounded failure; the counter restarts, so the next attempt is one
  # full threshold window away.
  log "  RECOVERY_FAILED: kickstart failed while launchd still owns ${LAUNCHD_LABEL}; child not proven gone; no port kill, no fallback server"
  echo 0 > "$COUNTER_FILE"
  exit 0
fi

# Ownership/port observations must each be EXPLICIT. Only a recognized
# owner-not-found result (exit 113 + "Could not find service") for BOTH print and
# list, plus a lsof that completed cleanly and saw no listener, authorizes a
# bootstrap. Any other status (permission, unavailable, parse, unexpected exit)
# is UNKNOWN: report RECOVERY_FAILED, take no start action, retry next window.
unknown() {
  log "  RECOVERY_FAILED: $1; ownership/port state UNKNOWN; no start action (no bootstrap, kickstart, kill or server)"
  echo 0 > "$COUNTER_FILE"
  exit 0
}

if [ "$PRINT_STATE" != "not_found" ]; then
  unknown "launchctl print was inconclusive (rc=${PRINT_RC}: $(echo "$PRINT_OUT" | head -1))"
fi
LIST_OUT=$(launchctl list "${LAUNCHD_LABEL}" 2>&1)
LIST_RC=$?
if [ "$LIST_RC" -eq 0 ]; then
  unknown "launchctl list still knows ${LAUNCHD_LABEL} (print says not found)"
elif ! { [ "$LIST_RC" -eq 113 ] && echo "$LIST_OUT" | grep -q "Could not find service"; }; then
  unknown "launchctl list was inconclusive (rc=${LIST_RC}: $(echo "$LIST_OUT" | head -1))"
fi
LSOF_ERR_FILE="${COUNTER_FILE}.lsof.err"
LSOF_OUT=$(lsof -nP -iTCP:3100 -sTCP:LISTEN -t 2>"$LSOF_ERR_FILE")
LSOF_RC=$?
LSOF_ERR=$(cat "$LSOF_ERR_FILE" 2>/dev/null)
rm -f "$LSOF_ERR_FILE"
if [ "$LSOF_RC" -eq 0 ] && [ -n "$LSOF_OUT" ]; then
  unknown "launchd absent but a listener (pid $(echo "$LSOF_OUT" | head -1)) holds :3100; refusing to kill it or start a second server"
elif [ "$LSOF_RC" -ne 0 ] && ! { [ "$LSOF_RC" -eq 1 ] && [ -z "$LSOF_OUT" ] && [ -z "$LSOF_ERR" ]; }; then
  unknown "lsof failed (rc=${LSOF_RC}: $(echo "$LSOF_ERR" | head -1)); an empty read is not a clean port"
elif [ "$LSOF_RC" -eq 0 ]; then
  unknown "lsof exited 0 without output; not a recognized clean-port result"
fi
RELEASE=$(cd "$MOP_CURRENT" 2>/dev/null && pwd -P || true)
if [ -z "$RELEASE" ] || [ ! -f "$RELEASE/dist/server.js" ] || [ ! -f "$MOP_PLIST" ]; then
  unknown "pointer-selected release ($MOP_CURRENT -> ${RELEASE:-unresolved}) or plist ($MOP_PLIST) is unusable"
fi
# Owner-absent fallback = re-establish launchd ownership from the plist, which pins
# the runtime, env (MOP_DB_PATH) and WorkingDirectory=$MOP_CURRENT. Never a bare
# nohup server and never the plugin-cache dist.
log "  launchd absent (recognized not-found), :3100 clean (lsof completed, no listener); bootstrapping ${LAUNCHD_LABEL} for pointer-selected release ${RELEASE}"
if launchctl bootstrap "gui/${UID_NUM}" "$MOP_PLIST" >>"$LOG_FILE" 2>&1; then
  echo 0 > "$COUNTER_FILE"
else
  log "  WARNING: launchctl bootstrap failed; next window retries"
  echo 0 > "$COUNTER_FILE"
fi
exit 0
