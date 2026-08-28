#!/bin/bash
# Canonical slot-side client for MoP-owned Claude Code respawns.

set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo "ERROR: /respawn no longer accepts model switches or intermediate shell commands" >&2
  exit 64
fi

case "$(basename "$PWD")" in
  heydonna-app-3001) SLOT=1 ;;
  heydonna-app-3002) SLOT=2 ;;
  heydonna-app-3003) SLOT=3 ;;
  heydonna-app-3004) SLOT=4 ;;
  heydonna-app-3005) SLOT=5 ;;
  heydonna-app-3006) SLOT=6 ;;
  *)
    echo "ERROR: /respawn is restricted to HeyDonna numbered-slot checkouts" >&2
    exit 64
    ;;
esac

MOP_BASE_URL="${MOP_BASE_URL:-http://127.0.0.1:3100}"
LOCK_DIR="/tmp/mop-slot-respawn-${SLOT}.lock"
LOG_FILE="/tmp/mop-slot-respawn-${SLOT}.log"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "ERROR: slot ${SLOT} already has a pending MoP respawn" >&2
  exit 75
fi

RUNNER="$(mktemp "/tmp/mop-slot-respawn-${SLOT}.XXXXXX.sh")"
cat > "$RUNNER" <<EOF
#!/bin/bash
set -euo pipefail
cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
  rm -f "$RUNNER"
}
trap cleanup EXIT

for _attempt in \$(seq 1 60); do
  state=\$(curl -sS --max-time 2 "$MOP_BASE_URL/slots/$SLOT" 2>/dev/null || true)
  if printf '%s' "\$state" | jq -e '.idle == true and .active_turn_state == "inactive"' >/dev/null 2>&1; then
    receipt=\$(mktemp "/tmp/mop-slot-respawn-${SLOT}.receipt.XXXXXX")
    status=\$(curl -sS --max-time 75 -o "\$receipt" -w '%{http_code}' \
      -X POST "$MOP_BASE_URL/slots/$SLOT/respawn" \
      -H 'content-type: application/json' \
      --data '{"continue_session":true}' 2>/dev/null || true)
    if [[ "\$status" == "200" ]]; then
      printf 'MOP_RESPAWN_ACCEPTED slot=%s receipt=' "$SLOT"
      cat "\$receipt"
      printf '\n'
      rm -f "\$receipt"
      exit 0
    fi
    printf 'MOP_RESPAWN_REFUSED slot=%s status=%s receipt=' "$SLOT" "\$status" >&2
    cat "\$receipt" >&2
    printf '\n' >&2
    rm -f "\$receipt"
    [[ "\$status" == "409" ]] || exit 1
  fi
  sleep 0.5
done

echo "ERROR: slot $SLOT did not become idle for MoP respawn within 30 seconds" >&2
exit 1
EOF
chmod 700 "$RUNNER"

nohup bash "$RUNNER" >"$LOG_FILE" 2>&1 </dev/null &
echo "Respawn queued through MoP for slot ${SLOT}; receipt: ${LOG_FILE}"
