#!/usr/bin/env bash
set -u

SEND_SCRIPT="${SEND_SCRIPT:-$HOME/.claude/skills/tmux-slot-command/scripts/send-to-slot.sh}"
ARCHIVE_DIR="${MESSAGE_SLOT_ARCHIVE_DIR:-/tmp/pm-delivered-archive}"
VERIFICATION_LEASE_DIR="${PM_VERIFICATION_LEASE_DIR:-/tmp/pm-verification-leases}"
MOP_BASE="${MOP_BASE:-http://127.0.0.1:3100}"

usage() {
  cat >&2 <<'EOF'
Usage:
  message-slot.sh <slot> '<message>' [--force] [--wait] [--from '<sender>']
  message-slot.sh --slot <slot> '<message>' [--force] [--wait] [--from '<sender>']
  message-slot.sh <slot> --file <path> [--force] [--wait] [--from '<sender>']
  cat <path> | message-slot.sh --slot <slot> -f [--force] [--wait] [--from '<sender>']

Flags:
  --force           Send immediately.
  --wait            Wait for the target slot to return to prompt after send.
  --from <sender>   Override sender label.
  --file <path>     Archive file content and send a FILE_PACKET read instruction.
  -f                Read stdin, archive it, and send a FILE_PACKET read instruction.
  --allow-command   Permit messages beginning with '/'.
  --dry-run         Print proof line without sending.
EOF
}

TARGET_SLOT=""
MESSAGE=""
FILE=""
STDIN_FILE=0
STDIN_TMP=""
FROM_LABEL=""
FORCE=0
WAIT=0
ALLOW_COMMAND=0
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --slot|--to)
      TARGET_SLOT="${2:-}"
      shift 2
      ;;
    --from)
      FROM_LABEL="${2:-}"
      shift 2
      ;;
    --file)
      if [ "$#" -lt 2 ]; then
        echo "MESSAGE_SLOT_FAILED slot:${TARGET_SLOT:-unknown} from=\"${FROM_LABEL:-unknown}\" exit=2 reason=missing_value_for_--file ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
        usage
        exit 2
      fi
      if [ "${2:-}" = "-" ]; then
        STDIN_FILE=1
      else
        FILE="${2:-}"
      fi
      shift 2
      ;;
    -f|--stdin-file|--file-stdin)
      STDIN_FILE=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --wait)
      WAIT=1
      shift
      ;;
    --allow-command)
      ALLOW_COMMAND=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      echo "MESSAGE_SLOT_FAILED slot:${TARGET_SLOT:-unknown} from=\"${FROM_LABEL:-unknown}\" exit=2 reason=unknown_flag_$1 ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
      usage
      exit 2
      ;;
    -*)
      echo "MESSAGE_SLOT_FAILED slot:${TARGET_SLOT:-unknown} from=\"${FROM_LABEL:-unknown}\" exit=2 reason=unknown_flag_$1 ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
      usage
      exit 2
      ;;
    *)
      if [ -z "$TARGET_SLOT" ]; then
        TARGET_SLOT="$1"
      elif [ -z "$MESSAGE" ]; then
        MESSAGE="$1"
      else
        MESSAGE="$MESSAGE $1"
      fi
      shift
      ;;
  esac
done

now_ts() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

archive_ts() {
  date -u +%Y%m%dT%H%M%SZ
}

message_prefix_ts() {
  TZ=Asia/Kolkata date '+%I:%M %p'
}

fail() {
  local exit_code="$1"
  local reason="$2"
  echo "MESSAGE_SLOT_FAILED slot:${TARGET_SLOT:-unknown} from=\"${FROM_LABEL:-unknown}\" exit=${exit_code} reason=${reason} ts=$(now_ts)" >&2
  exit "$exit_code"
}

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    LC_ALL=C shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    LC_ALL=C sha256sum "$1" | awk '{print $1}'
  else
    return 1
  fi
}

file_preview() {
  python3 - "$1" "${MESSAGE_SLOT_PREVIEW_CHARS:-500}" <<'PY'
import re
import sys

path = sys.argv[1]
try:
    limit = int(sys.argv[2])
except Exception:
    limit = 500
limit = max(80, min(limit, 1200))

with open(path, "rb") as f:
    raw = f.read(max(limit * 8, 2048))

text = raw.decode("utf-8", errors="replace")
text = "".join(ch if ch.isprintable() else " " for ch in text)
text = re.sub(r"\s+", " ", text).strip()
if len(text) > limit:
    text = text[: max(0, limit - 3)].rstrip() + "..."
print(text)
PY
}

case "$TARGET_SLOT" in
  1|2|3|4|5|6) ;;
  *) fail 2 "target_slot_must_be_1_2_3_4_5_or_6" ;;
esac

if [ -n "$FILE" ] && [ -n "$MESSAGE" ]; then
  fail 2 "choose_message_or_file_not_both"
fi

if [ "$STDIN_FILE" = "1" ] && { [ -n "$FILE" ] || [ -n "$MESSAGE" ]; }; then
  fail 2 "choose_message_or_file_not_both"
fi

if [ "$STDIN_FILE" != "1" ] && [ -z "$FILE" ] && [ -z "$MESSAGE" ]; then
  fail 2 "missing_message_or_file"
fi

if [ ! -x "$SEND_SCRIPT" ]; then
  fail 3 "send_script_not_executable:$SEND_SCRIPT"
fi

cleanup_stdin_tmp() {
  if [ -n "$STDIN_TMP" ]; then
    rm -f "$STDIN_TMP"
  fi
}

literal_cat_file() {
  python3 - "$1" <<'PY'
import re
import shlex
import sys

message = sys.argv[1].strip()
match = re.fullmatch(r"\$\(\s*cat\s+(.+?)\s*\)", message)
if not match:
    match = re.fullmatch(r"\$\(\s*<\s*(.+?)\s*\)", message)
if not match:
    sys.exit(0)
try:
    parts = shlex.split(match.group(1))
except ValueError:
    sys.exit(0)
if len(parts) == 1:
    print(parts[0])
PY
}

if [ -z "$FILE" ] && [ -n "$MESSAGE" ]; then
  literal_file="$(literal_cat_file "$MESSAGE" 2>/dev/null || true)"
  if [ -n "$literal_file" ]; then
    FILE="$literal_file"
    MESSAGE=""
  fi
fi

if [ "$STDIN_FILE" = "1" ]; then
  if [ -n "$FILE" ] || [ -n "$MESSAGE" ]; then
    fail 2 "choose_message_or_file_not_both"
  fi
  STDIN_TMP=$(mktemp "${TMPDIR:-/tmp}/message-slot-stdin.XXXXXX") || fail 1 "stdin_tmp_mktemp_failed"
  if ! cat > "$STDIN_TMP"; then
    cleanup_stdin_tmp
    fail 1 "stdin_read_failed"
  fi
  FILE="$STDIN_TMP"
fi

assert_verification_lease_allows_delivery() {
  local lease="$VERIFICATION_LEASE_DIR/slot-${TARGET_SLOT}.json"
  [ -f "$lease" ] || return 0
  [ "${PM_VERIFICATION_LEASE_BYPASS:-0}" = "1" ] && return 0
  python3 - "$lease" "${FILE:-}" <<'PY'
import json
import sys
from pathlib import Path

lease_path, candidate = Path(sys.argv[1]), sys.argv[2]
try:
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
if lease.get("status") != "initial_delivery" or not candidate:
    raise SystemExit(1)
try:
    allowed = Path(lease["initial_handoff"]).resolve()
    actual = Path(candidate).resolve()
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if actual == allowed else 1)
PY
}

assert_verification_lease_allows_delivery \
  || fail 45 "verification_lease_active_requires_new_epoch"

read_env_value() {
  local key="$1"
  local env_file="$2"
  [ -f "$env_file" ] || return 1
  sed -n "s/^${key}=//p" "$env_file" | tail -1 | sed 's/^["'\'']//; s/["'\'']$//'
}

infer_sender() {
  if [ -n "$FROM_LABEL" ]; then
    printf '%s' "$FROM_LABEL"
    return 0
  fi

  local cwd base env_file slot_num slot_name
  cwd="$(pwd)"
  base="$(basename "$cwd")"
  env_file="$cwd/.env.local"

  slot_num="$(read_env_value SLOT_NUMBER "$env_file" 2>/dev/null || true)"
  slot_name="$(read_env_value SLOT_NAME "$env_file" 2>/dev/null || true)"

  if [ -z "$slot_num" ]; then
    case "$base" in
      heydonna-app-3001) slot_num="1" ;;
      heydonna-app-3002) slot_num="2" ;;
      heydonna-app-3003) slot_num="3" ;;
      heydonna-app-3004) slot_num="4" ;;
    esac
  fi

  if [ "$slot_num" = "0" ]; then
    printf 'PM'
    return 0
  fi

  if [ -n "$slot_num" ]; then
    if [ -n "$slot_name" ]; then
      printf 'slot %s (%s)' "$slot_num" "$slot_name"
    else
      printf 'slot %s' "$slot_num"
    fi
    return 0
  fi

  printf 'PM'
}

FROM_LABEL="$(infer_sender)"
MESSAGE_PREFIX_TS="$(message_prefix_ts)"
PREFIX="[${MESSAGE_PREFIX_TS}] ${FROM_LABEL} -> slot ${TARGET_SLOT}:"

assert_pm_delivery_has_slot_owner() {
  [ "$FROM_LABEL" = "PM" ] || return 0
  [ "$DRY_RUN" = "1" ] && return 0
  [ "${MESSAGE_SLOT_FREE_CONTROL_BYPASS:-0}" = "1" ] && return 0
  local candidate snapshot occupied issue pr epoch
  if [ -n "$MESSAGE" ]; then
    candidate="$MESSAGE"
  else
    candidate="$(awk 'NF{print; exit}' "$FILE" 2>/dev/null || true)"
  fi
  if printf '%s\n' "$candidate" | grep -Eq '^HEALTH_PING([[:space:]]|$)'; then
    return 0
  fi
  snapshot="$(curl -sS -m 4 "$MOP_BASE/slots/${TARGET_SLOT}" 2>/dev/null || true)"
  occupied="$(printf '%s' "$snapshot" | jq -r '.occupied | if . == true then "true" elif . == false then "false" else empty end' 2>/dev/null || true)"
  issue="$(printf '%s' "$snapshot" | jq -r '.issue // empty' 2>/dev/null || true)"
  pr="$(printf '%s' "$snapshot" | jq -r '.pr // empty' 2>/dev/null || true)"
  epoch="$(printf '%s' "$snapshot" | jq -r '.assignment_epoch // empty' 2>/dev/null || true)"
  [ "$occupied" = "true" ] && return 0
  if [ "$occupied" != "false" ] || ! [[ "$epoch" =~ ^[0-9]+$ ]]; then
    fail 46 "mop_slot_authority_unavailable_use_native_mop_operator"
  fi
  fail 46 "slot_free_no_assignment_owner_issue_${issue:-none}_pr_${pr:-none}_epoch_${epoch}_assign_via_Skill_direct-assign_with_complete_tuple"
}

assert_pm_delivery_has_slot_owner

SEND_ARGS=()
[ "$FORCE" = "1" ] && SEND_ARGS+=("--force")
[ "$WAIT" = "1" ] && SEND_ARGS+=("--wait")

if [ -n "$FILE" ]; then
  [ -r "$FILE" ] || fail 2 "file_not_readable:$FILE"
  mkdir -p "$ARCHIVE_DIR" || fail 1 "archive_dir_unavailable:$ARCHIVE_DIR"

  if [ "$STDIN_FILE" = "1" ]; then
    source_base="stdin.md"
    source_label="stdin"
  else
    source_base="$(basename "$FILE" | tr -c 'A-Za-z0-9._-' '-')"
    source_label="$(basename "$FILE")"
  fi
  archive_file=$(mktemp "${ARCHIVE_DIR}/message-slot-${TARGET_SLOT}-$(archive_ts)-${source_base}.XXXXXX") || fail 1 "archive_mktemp_failed"
  if ! cat "$FILE" > "$archive_file"; then
    rm -f "$archive_file"
    cleanup_stdin_tmp
    fail 1 "archive_copy_failed:$FILE"
  fi
  cleanup_stdin_tmp

  size=$(wc -c < "$archive_file" | tr -d ' ')
  if [ "$size" = "0" ]; then
    rm -f "$archive_file"
    if [ "$STDIN_FILE" = "1" ]; then
      fail 2 "file_empty:stdin"
    else
      fail 2 "file_empty:$FILE"
    fi
  fi

  first_non_blank=$(awk 'NF{print; exit}' "$archive_file")
  if [ "$ALLOW_COMMAND" != "1" ] && printf '%s\n' "$first_non_blank" | grep -qE '^/[A-Za-z][A-Za-z0-9_-]*([[:space:]]|$)'; then
    rm -f "$archive_file"
    fail 2 "slash_command_file_requires_allow_command"
  fi

  sha256=$(sha256_file "$archive_file") || {
    rm -f "$archive_file"
    fail 1 "sha256_unavailable"
  }
  preview=$(file_preview "$archive_file" 2>/dev/null || true)

  read_instruction="FILE_PACKET path=${archive_file} source=${source_label} bytes=${size} sha256=${sha256} preview=\"${preview}\". Read this file with the Read tool, acknowledge path+sha256, then execute its instructions. The preview is only a hint; the file body is not pasted inline."

  if [ "$DRY_RUN" = "1" ]; then
    rm -f "$archive_file"
    echo "MESSAGE_SLOT_OK slot:${TARGET_SLOT} from=\"${FROM_LABEL}\" force=${FORCE} wait=${WAIT} mode=file-ref bytes=${size} sha256=${sha256} dry_run=1 ts=$(now_ts)"
    exit 0
  fi

  output=$("$SEND_SCRIPT" "$TARGET_SLOT" "${PREFIX} ${read_instruction}" ${SEND_ARGS[@]+"${SEND_ARGS[@]}"} 2>&1)
  rc=$?
  if [ "$rc" = "0" ]; then
    echo "MESSAGE_SLOT_OK slot:${TARGET_SLOT} from=\"${FROM_LABEL}\" force=${FORCE} wait=${WAIT} mode=file-ref bytes=${size} sha256=${sha256} archive=${archive_file} ts=$(now_ts)"
    exit 0
  fi
  reason=$(printf '%s' "$output" | tr '\n' ' ' | sed 's/[[:space:]]\{1,\}/ /g' | head -c 220)
  fail "$rc" "$reason"
fi

trimmed=$(printf '%s' "$MESSAGE" | sed 's/^[[:space:]]*//')
if [ "$ALLOW_COMMAND" != "1" ] && printf '%s\n' "$trimmed" | grep -qE '^/[A-Za-z][A-Za-z0-9_-]*([[:space:]]|$)'; then
  fail 2 "slash_command_message_requires_allow_command"
fi

payload="${PREFIX} ${MESSAGE}"

if [ "$DRY_RUN" = "1" ]; then
  echo "MESSAGE_SLOT_OK slot:${TARGET_SLOT} from=\"${FROM_LABEL}\" force=${FORCE} wait=${WAIT} mode=message dry_run=1 ts=$(now_ts)"
  exit 0
fi

output=$("$SEND_SCRIPT" "$TARGET_SLOT" "$payload" ${SEND_ARGS[@]+"${SEND_ARGS[@]}"} 2>&1)
rc=$?
if [ "$rc" = "0" ]; then
  echo "MESSAGE_SLOT_OK slot:${TARGET_SLOT} from=\"${FROM_LABEL}\" force=${FORCE} wait=${WAIT} mode=message ts=$(now_ts)"
  exit 0
fi

reason=$(printf '%s' "$output" | tr '\n' ' ' | sed 's/[[:space:]]\{1,\}/ /g' | head -c 220)
fail "$rc" "$reason"
