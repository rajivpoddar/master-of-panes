#!/bin/bash

set -euo pipefail

SLOT_NUMBER="${1:-}"
shift || true

case "$SLOT_NUMBER" in
  1) SLOT_NAME="Rohini" ;;
  2) SLOT_NAME="Hasta" ;;
  3) SLOT_NAME="Ashwini" ;;
  4) SLOT_NAME="Chitra" ;;
  5) SLOT_NAME="S5" ;;
  6) SLOT_NAME="S6" ;;
  *) echo "Usage: launch-dev-slot-claude.sh <1|2|3|4|5|6> [claude args...]" >&2; exit 2 ;;
esac

SLOT_CLONE="/Users/rajiv/Downloads/projects/heydonna-app-300${SLOT_NUMBER}"
SPARK_MODEL="${DEV_SLOT_SPARK_MODEL:-ornith-1.5-35b-a3b}"
SPARK_BASE_URL="${DEV_SLOT_SPARK_BASE_URL:-${ORNITH15_SPARK_BASE_URL:-${QWEN38_SPARK_BASE_URL:-http://192.168.68.113:30000}}}"
SPARK_KEY_FILE="${DEV_SLOT_SPARK_API_KEY_FILE:-${ORNITH15_SPARK_API_KEY_FILE:-${QWEN38_SPARK_API_KEY_FILE:-/Users/rajiv/.config/heydonna/qwen38-spark-api-key}}}"

if [[ ! -d "$SLOT_CLONE" ]]; then
  echo "ERROR: slot checkout missing: $SLOT_CLONE" >&2
  exit 1
fi
if [[ ! -s "$SPARK_KEY_FILE" ]]; then
  echo "ERROR: Spark API key file missing or empty: $SPARK_KEY_FILE" >&2
  exit 1
fi

/Users/rajiv/.claude/scripts/sync-dev-slot-skill-allowlist.mjs

export SLOT_NUMBER
export SLOT_NAME
export AGENT_BROWSER_SESSION="slot${SLOT_NUMBER}"
export AGENT_BROWSER_PROFILE="/Users/rajiv/.agent-browser/profiles/admin-slot${SLOT_NUMBER}"
export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1"
export API_TIMEOUT_MS="${DEV_SLOT_SPARK_API_TIMEOUT_MS:-600000}"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="${DEV_SLOT_SPARK_MAX_CONTEXT_TOKENS:-240000}"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${DEV_SLOT_SPARK_MAX_OUTPUT_TOKENS:-16384}"
export MAX_THINKING_TOKENS="${DEV_SLOT_SPARK_MAX_THINKING_TOKENS:-2048}"
export CLAUDE_TEXT_ONLY_VISION_BRIDGE="${DEV_SLOT_TEXT_ONLY_VISION_BRIDGE:-codex}"
export ANTHROPIC_BASE_URL="$SPARK_BASE_URL"
export ANTHROPIC_AUTH_TOKEN
ANTHROPIC_AUTH_TOKEN="$(tr -d '\r\n' < "$SPARK_KEY_FILE")"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$SPARK_MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$SPARK_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$SPARK_MODEL"
unset ANTHROPIC_API_KEY

cd "$SLOT_CLONE"
exec /opt/homebrew/bin/claude \
  --model "$SPARK_MODEL" \
  --effort low \
  --permission-mode bypassPermissions \
  "$@"
