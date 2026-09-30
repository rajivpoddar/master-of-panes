#!/bin/bash

set -euo pipefail

SLOT_NUMBER="${1:-}"
shift || true

SPARK_PROFILE="${DEV_SLOT_SPARK_PROFILE:-ornith}"
CLAUDE_ARGS=()
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --spark-profile)
      if [[ "$#" -lt 2 ]]; then
        echo "ERROR: --spark-profile requires ornith, ornstein, ling-mia, deepseek-v4, deepseek-flash-4.1, qwen38-next, qwen38-mia, swift-qwen38, qwopus, tiel, nemotron, spark25, neohorse, glimmer, oxcoder, muse13-contributor, or opus55" >&2
        exit 2
      fi
      SPARK_PROFILE="$2"
      shift 2
      ;;
    --spark-profile=*)
      SPARK_PROFILE="${1#*=}"
      shift
      ;;
    *)
      CLAUDE_ARGS+=("$1")
      shift
      ;;
  esac
done
if [[ "${#CLAUDE_ARGS[@]}" -gt 0 ]]; then
  set -- "${CLAUDE_ARGS[@]}"
else
  set --
fi

case "$SLOT_NUMBER" in
  1) SLOT_NAME="Rohini" ;;
  2) SLOT_NAME="Hasta" ;;
  3) SLOT_NAME="Ashwini" ;;
  4) SLOT_NAME="Chitra" ;;
  5) SLOT_NAME="Revati" ;;
  6) SLOT_NAME="Pushya" ;;
  *) echo "Usage: launch-dev-slot-claude.sh <1|2|3|4|5|6> [claude args...]" >&2; exit 2 ;;
esac

SLOT_CLONE="/Users/rajiv/Downloads/projects/heydonna-app-300${SLOT_NUMBER}"
# Profiles that use Claude Code's own claude.ai login against Anthropic
# directly. No base URL, token, key file, or proxy sits in the path.
NATIVE_ANTHROPIC=0
case "$SPARK_PROFILE" in
  opus55)
    NATIVE_ANTHROPIC=1
    SPARK_MODEL="claude-opus-5-5[1m]"
    SPARK_MAX_CONTEXT_TOKENS="1000000"
    SPARK_MAX_OUTPUT_TOKENS="32000"
    SPARK_MAX_THINKING_TOKENS="32000"
    ;;
  muse13-contributor)
    SPARK_MODEL="muse-spark-1.3-contributor"
    SPARK_BASE_URL="https://api.meta.ai"
    SPARK_KEY_FILE="/Users/rajiv/.config/meta/api-key"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="30"
    SPARK_MAX_CONTEXT_TOKENS="240000"
    SPARK_MAX_OUTPUT_TOKENS="32000"
    SPARK_MAX_THINKING_TOKENS="2048"
    ;;
  ornith)
    SPARK_MODEL="${DEV_SLOT_SPARK_MODEL:-ornith-1.5-35b-a3b}"
    SPARK_BASE_URL="${DEV_SLOT_SPARK_BASE_URL:-${ORNITH15_SPARK_BASE_URL:-${QWEN38_SPARK_BASE_URL:-http://192.168.68.113:30000}}}"
    SPARK_KEY_FILE="${DEV_SLOT_SPARK_API_KEY_FILE:-${ORNITH15_SPARK_API_KEY_FILE:-${QWEN38_SPARK_API_KEY_FILE:-/Users/rajiv/.config/ornith15/api-key}}}"
    ;;
  ornstein)
    SPARK_MODEL="${ORNSTEIN_SPARK_MODEL:-ornstein3.8-27b}"
    SPARK_BASE_URL="${ORNSTEIN_SPARK_BASE_URL:-http://192.168.68.113:30000}"
    SPARK_KEY_FILE="${ORNSTEIN_SPARK_API_KEY_FILE:-${DEV_SLOT_SPARK_API_KEY_FILE:-/Users/rajiv/.config/ornith15/api-key}}"
    ;;
  ling-mia)
    SPARK_MODEL="${LING_MIA_SPARK_MODEL:-ling-3.0-flash}"
    SPARK_BASE_URL="${LING_MIA_SPARK_BASE_URL:-http://192.168.68.113:30000}"
    SPARK_KEY_FILE="${LING_MIA_SPARK_API_KEY_FILE:-${DEV_SLOT_SPARK_API_KEY_FILE:-/Users/rajiv/.config/ornith15/api-key}}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="10"
    SPARK_API_TIMEOUT_MS="1200000"
    SPARK_MAX_CONTEXT_TOKENS="240000"
    SPARK_MAX_OUTPUT_TOKENS="32000"
    ;;
  deepseek-v4)
    SPARK_MODEL="${DEEPSEEK_V4_SPARK_MODEL:-deepseek-v4-flash-0731}"
    SPARK_BASE_URL="${DEEPSEEK_V4_SPARK_BASE_URL:-http://192.168.68.113:30000}"
    SPARK_KEY_FILE="${DEEPSEEK_V4_SPARK_API_KEY_FILE:-${DEV_SLOT_SPARK_API_KEY_FILE:-/Users/rajiv/.config/ornith15/api-key}}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="10"
    SPARK_API_TIMEOUT_MS="1200000"
    # The deployed profile exposes a 100k server window. Leave headroom for
    # Claude's response rather than advertising a context the server cannot fit.
    SPARK_MAX_CONTEXT_TOKENS="98304"
    SPARK_MAX_OUTPUT_TOKENS="8192"
    ;;
  deepseek-flash-4.1)
    SPARK_MODEL="${DEEPSEEK_FLASH_41_SPARK_MODEL:-deepseek-flash}"
    # DeepSeek's own Anthropic-compatible surface. Nothing local sits in the
    # path: the slot talks to the DeepSeek API directly.
    SPARK_BASE_URL="${DEEPSEEK_FLASH_41_SPARK_BASE_URL:-https://api.deepseek.com/anthropic}"
    # That base URL has no OpenAI model list, so the readiness probe asks the
    # account's own /v1/models endpoint for the same key.
    SPARK_MODELS_BASE_URL="${DEEPSEEK_FLASH_41_SPARK_MODELS_BASE_URL:-https://api.deepseek.com}"
    SPARK_KEY_FILE="${DEEPSEEK_FLASH_41_SPARK_API_KEY_FILE:-/Users/rajiv/.config/deepseek/api-key}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="15"
    SPARK_API_TIMEOUT_MS="1200000"
    # DeepSeek advertises a 1M window for this model; keep response headroom
    # instead of spending the whole window on input.
    SPARK_MAX_CONTEXT_TOKENS="900000"
    SPARK_MAX_OUTPUT_TOKENS="32768"
    SPARK_MAX_THINKING_TOKENS="2048"
    ;;
  qwen38-next)
    SPARK_MODEL="qwen3.8-flash-next"
    SPARK_BASE_URL="http://192.168.68.113:30000"
    SPARK_KEY_FILE="/Users/rajiv/.config/ornith15/api-key"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_API_TIMEOUT_MS="1200000"
    SPARK_MAX_CONTEXT_TOKENS="240000"
    SPARK_MAX_OUTPUT_TOKENS="16384"
    # This lane runs --effort xhigh (see the effort block below), and the CLI
    # rejects xhigh/max while thinking is disabled, so the budget must be real.
    # 2048 matches the swift-qwen38 profile on the same engine. Restore the old
    # behaviour with QWEN38_NEXT_MAX_THINKING_TOKENS=0 plus DEV_SLOT_EFFORT=low.
    SPARK_MAX_THINKING_TOKENS="${QWEN38_NEXT_MAX_THINKING_TOKENS:-2048}"
    ;;
  qwen38-mia)
    SPARK_MODEL="${QWEN38_MIA_SPARK_MODEL:-qwen3.8-27b}"
    SPARK_BASE_URL="${QWEN38_MIA_SPARK_BASE_URL:-http://192.168.68.113:30000}"
    SPARK_KEY_FILE="${QWEN38_MIA_SPARK_API_KEY_FILE:-${DEV_SLOT_SPARK_API_KEY_FILE:-/Users/rajiv/.config/heydonna/qwen38-spark-api-key}}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_API_TIMEOUT_MS="1200000"
    SPARK_MAX_CONTEXT_TOKENS="240000"
    SPARK_MAX_OUTPUT_TOKENS="16384"
    SPARK_MAX_THINKING_TOKENS="0"
    ;;
  swift-qwen38)
    SPARK_MODEL="${SWIFT_QWEN38_SPARK_MODEL:-swift-qwen3.8-27b-nvfp4}"
    SPARK_BASE_URL="${SWIFT_QWEN38_SPARK_BASE_URL:-http://192.168.68.113:30000}"
    SPARK_KEY_FILE="${SWIFT_QWEN38_SPARK_API_KEY_FILE:-${DEV_SLOT_SPARK_API_KEY_FILE:-/Users/rajiv/.config/ornith15/api-key}}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="10"
    SPARK_API_TIMEOUT_MS="1200000"
    SPARK_MAX_CONTEXT_TOKENS="240000"
    SPARK_MAX_OUTPUT_TOKENS="16384"
    SPARK_MAX_THINKING_TOKENS="2048"
    ;;
  qwopus)
    SPARK_MODEL="${QWOPUS_SPARK_MODEL:-qwopus3.8-27b-flash}"
    SPARK_BASE_URL="${QWOPUS_SPARK_BASE_URL:-http://192.168.68.113:30000}"
    SPARK_KEY_FILE="${QWOPUS_SPARK_API_KEY_FILE:-${DEV_SLOT_SPARK_API_KEY_FILE:-/Users/rajiv/.config/heydonna/qwen38-spark-api-key}}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_API_TIMEOUT_MS="1200000"
    SPARK_MAX_CONTEXT_TOKENS="122880"
    SPARK_MAX_OUTPUT_TOKENS="8192"
    SPARK_MAX_THINKING_TOKENS="0"
    ;;
  tiel)
    SPARK_MODEL="${TIEL_SPARK_MODEL:-tiel/tiel-coder-35b-a3b}"
    SPARK_BASE_URL="${TIEL_SPARK_BASE_URL:-http://127.0.0.1:8318}"
    SPARK_AUTH_TOKEN="${TIEL_SPARK_AUTH_TOKEN:-local-tiel-loopback}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="10"
    SPARK_API_TIMEOUT_MS="1200000"
    SPARK_MAX_CONTEXT_TOKENS="240000"
    SPARK_MAX_OUTPUT_TOKENS="16384"
    SPARK_MAX_THINKING_TOKENS="0"
    ;;
  nemotron)
    SPARK_MODEL="${NEMOTRON_SPARK_MODEL:-nemotron-3.5-lightning-30b-a3b}"
    SPARK_BASE_URL="${NEMOTRON_SPARK_BASE_URL:-http://192.168.68.113:30000}"
    SPARK_AUTH_TOKEN="${NEMOTRON_SPARK_AUTH_TOKEN:-local-nemotron-loopback}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="10"
    SPARK_API_TIMEOUT_MS="1200000"
    SPARK_MAX_CONTEXT_TOKENS="240000"
    SPARK_MAX_OUTPUT_TOKENS="16384"
    SPARK_MAX_THINKING_TOKENS="0"
    ;;
  spark25)
    SPARK_MODEL="${SPARK25_SPARK_MODEL:-spark25/spark-x2.5-4b}"
    SPARK_BASE_URL="${SPARK25_SPARK_BASE_URL:-http://127.0.0.1:8319}"
    SPARK_AUTH_TOKEN="${SPARK25_SPARK_AUTH_TOKEN:-local-spark25-loopback}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="10"
    SPARK_API_TIMEOUT_MS="1200000"
    SPARK_MAX_CONTEXT_TOKENS="240000"
    SPARK_MAX_OUTPUT_TOKENS="16384"
    SPARK_MAX_THINKING_TOKENS="0"
    ;;
  neohorse)
    SPARK_MODEL="${NEOHORSE_SPARK_MODEL:-neohorse-1-9b-q5-k-m}"
    SPARK_BASE_URL="${NEOHORSE_SPARK_BASE_URL:-http://192.168.68.113:30000}"
    SPARK_AUTH_TOKEN="${NEOHORSE_SPARK_AUTH_TOKEN:-local-neohorse-loopback}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="10"
    SPARK_API_TIMEOUT_MS="1200000"
    # NeoHorse is served with four independent 262K slots. Keep 8K available
    # for generation while exposing a useful Claude Code working context.
    SPARK_MAX_CONTEXT_TOKENS="253952"
    SPARK_MAX_OUTPUT_TOKENS="8192"
    SPARK_MAX_THINKING_TOKENS="0"
    ;;
  glimmer)
    SPARK_MODEL="${GLIMMER_SPARK_MODEL:-muse-glimmer-30b}"
    SPARK_BASE_URL="${GLIMMER_SPARK_BASE_URL:-http://192.168.68.113:30000}"
    SPARK_AUTH_TOKEN="${GLIMMER_SPARK_AUTH_TOKEN:-local-glimmer-loopback}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="10"
    SPARK_API_TIMEOUT_MS="1200000"
    # The Mia Labs server exposes 262K. Reserve room for Claude's response.
    SPARK_MAX_CONTEXT_TOKENS="240000"
    SPARK_MAX_OUTPUT_TOKENS="16384"
    SPARK_MAX_THINKING_TOKENS="2048"
    ;;
  oxcoder)
    SPARK_MODEL="${OXCODER_SPARK_MODEL:-oxcoder-9b-q5-k-m}"
    SPARK_BASE_URL="${OXCODER_SPARK_BASE_URL:-http://192.168.68.113:30000}"
    SPARK_AUTH_TOKEN="${OXCODER_SPARK_AUTH_TOKEN:-local-oxcoder-loopback}"
    SPARK_EXPECTED_MODEL="$SPARK_MODEL"
    SPARK_READINESS_TIMEOUT_SECONDS="10"
    SPARK_API_TIMEOUT_MS="1200000"
    # llama.cpp exposes four independent 262K slots. Reserve 8K for output.
    SPARK_MAX_CONTEXT_TOKENS="253952"
    SPARK_MAX_OUTPUT_TOKENS="8192"
    SPARK_MAX_THINKING_TOKENS="0"
    ;;
  *)
    echo "ERROR: unsupported profile '$SPARK_PROFILE'; expected ornith, ornstein, ling-mia, deepseek-v4, deepseek-flash-4.1, qwen38-next, qwen38-mia, swift-qwen38, qwopus, tiel, nemotron, spark25, neohorse, glimmer, oxcoder, muse13-contributor, or opus55" >&2
    exit 2
    ;;
esac
CLAUDE_BIN="${CLAUDE_SLOT_BIN:-/opt/homebrew/bin/claude}"
SKILL_SYNC="${CLAUDE_SLOT_SKILL_SYNC:-/Users/rajiv/.claude/scripts/sync-dev-slot-skill-allowlist.mjs}"
DEV_SLOT_RULES="${CLAUDE_DEV_SLOT_RULES:-/Users/rajiv/.claude/dev-slot-rules}"
PROJECT_RULES="${CLAUDE_PROJECT_RULES:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/rules}"

case "$SLOT_NUMBER" in
  1) IDENTITY_RULE="22-slot-rohini.md" ;;
  2) IDENTITY_RULE="22-slot-hasta.md" ;;
  3) IDENTITY_RULE="22-slot-ashwini.md" ;;
  4) IDENTITY_RULE="22-slot-chitra.md" ;;
  5) IDENTITY_RULE="22-slot-revati.md" ;;
  6) IDENTITY_RULE="22-slot-pushya.md" ;;
esac
IDENTITY_RULE_PATH="$DEV_SLOT_RULES/$IDENTITY_RULE"

repair_rule_link() {
  local source="$1" destination="$2" current=""
  if [[ -L "$destination" ]]; then
    current="$(readlink "$destination")"
    [[ "$current" == "$source" ]] && return 0
    rm -f "$destination"
  elif [[ -e "$destination" ]]; then
    echo "ERROR: refusing to replace non-symlink slot rule: $destination" >&2
    exit 1
  fi
  ln -s "$source" "$destination"
}

preflight_rule_link() {
  local source="$1" destination="$2"
  if [[ ! -f "$source" ]]; then
    echo "ERROR: canonical slot rule missing: $source" >&2
    exit 1
  fi
  if [[ -e "$destination" && ! -L "$destination" ]]; then
    echo "ERROR: refusing to replace non-symlink slot rule: $destination" >&2
    exit 1
  fi
}

if [[ ! -d "$SLOT_CLONE" ]]; then
  echo "ERROR: slot checkout missing: $SLOT_CLONE" >&2
  exit 1
fi
if [[ "$NATIVE_ANTHROPIC" == "1" ]]; then
  # Clear inherited proxy routing first; a stray token overrides the login.
  unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY
  if ! "$CLAUDE_BIN" auth status 2>/dev/null | jq -e '.loggedIn == true and .authMethod == "claude.ai"' >/dev/null; then
    echo "ERROR: $SPARK_PROFILE needs Claude Code's claude.ai login; run: claude auth login" >&2
    exit 1
  fi
elif [[ -n "${SPARK_AUTH_TOKEN:-}" ]]; then
  :
elif [[ -n "${SPARK_KEY_ENV_FILE:-}" ]]; then
  if [[ ! -s "$SPARK_KEY_ENV_FILE" ]]; then
    echo "ERROR: Spark API key environment file missing or empty: $SPARK_KEY_ENV_FILE" >&2
    exit 1
  fi
elif [[ ! -s "$SPARK_KEY_FILE" ]]; then
  echo "ERROR: Spark API key file missing or empty: $SPARK_KEY_FILE" >&2
  exit 1
fi

if [[ ! -x "$CLAUDE_BIN" ]]; then
  echo "ERROR: Claude launcher is unavailable: $CLAUDE_BIN" >&2
  exit 70
fi
if [[ ! -x "$SKILL_SYNC" ]]; then
  echo "ERROR: slot skill sync is unavailable: $SKILL_SYNC" >&2
  exit 70
fi

mkdir -p "$SLOT_CLONE/.claude/rules"
preflight_rule_link "$DEV_SLOT_RULES/20-buddhi-dev.md" "$SLOT_CLONE/.claude/rules/20-buddhi-dev.md"
preflight_rule_link "$IDENTITY_RULE_PATH" "$SLOT_CLONE/.claude/rules/$IDENTITY_RULE"
preflight_rule_link "$PROJECT_RULES/21-lessons.md" "$SLOT_CLONE/.claude/rules/21-lessons.md"
repair_rule_link "$DEV_SLOT_RULES/20-buddhi-dev.md" "$SLOT_CLONE/.claude/rules/20-buddhi-dev.md"
repair_rule_link "$IDENTITY_RULE_PATH" "$SLOT_CLONE/.claude/rules/$IDENTITY_RULE"
repair_rule_link "$PROJECT_RULES/21-lessons.md" "$SLOT_CLONE/.claude/rules/21-lessons.md"

"$SKILL_SYNC"

export SLOT_NUMBER
export SLOT_NAME
export DEV_SLOT_SPARK_PROFILE="$SPARK_PROFILE"
export AGENT_BROWSER_SESSION="slot${SLOT_NUMBER}"
export AGENT_BROWSER_PROFILE="/Users/rajiv/.agent-browser/profiles/admin-slot${SLOT_NUMBER}"
export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1"
export CLAUDE_CODE_SUBAGENT_MODEL="${SPARK_MODEL%\[1m\]}"
export API_TIMEOUT_MS="${DEV_SLOT_SPARK_API_TIMEOUT_MS:-${SPARK_API_TIMEOUT_MS:-1200000}}"
# Local Spark prefill can pause a stream for minutes. Disable the separate
# body-idle deadline, but retain bounded 20-minute request and stream timers.
# Keep the existing Qwen override as a compatibility fallback.
export API_FORCE_IDLE_TIMEOUT="${DEV_SLOT_SPARK_FORCE_IDLE_TIMEOUT:-${DEV_SLOT_QWEN_FORCE_IDLE_TIMEOUT:-0}}"
export CLAUDE_STREAM_IDLE_TIMEOUT_MS="${DEV_SLOT_SPARK_STREAM_IDLE_TIMEOUT_MS:-1200000}"
export CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS="${DEV_SLOT_SPARK_BYTE_STREAM_IDLE_TIMEOUT_MS:-1200000}"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="${DEV_SLOT_SPARK_MAX_CONTEXT_TOKENS:-${SPARK_MAX_CONTEXT_TOKENS:-240000}}"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${DEV_SLOT_SPARK_MAX_OUTPUT_TOKENS:-${SPARK_MAX_OUTPUT_TOKENS:-32000}}"
export MAX_THINKING_TOKENS="${SPARK_MAX_THINKING_TOKENS:-${DEV_SLOT_SPARK_MAX_THINKING_TOKENS:-2048}}"
if [[ "$NATIVE_ANTHROPIC" == "1" ]]; then
  # Direct Anthropic: drop any inherited proxy routing so Claude Code uses its
  # own claude.ai login. Opus reads images natively, so no vision bridge.
  unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY \
    ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL \
    CLAUDE_TEXT_ONLY_VISION_BRIDGE
else
export CLAUDE_TEXT_ONLY_VISION_BRIDGE="${DEV_SLOT_TEXT_ONLY_VISION_BRIDGE:-codex}"
export ANTHROPIC_BASE_URL="$SPARK_BASE_URL"
export ANTHROPIC_AUTH_TOKEN
if [[ -n "${SPARK_AUTH_TOKEN:-}" ]]; then
  ANTHROPIC_AUTH_TOKEN="$SPARK_AUTH_TOKEN"
elif [[ -n "${SPARK_KEY_ENV_FILE:-}" ]]; then
  ANTHROPIC_AUTH_TOKEN="$(
    source "$SPARK_KEY_ENV_FILE"
    printf '%s' "${CLIPROXY_API_KEY:-}"
  )"
  if [[ -z "$ANTHROPIC_AUTH_TOKEN" ]]; then
    echo "ERROR: CLIPROXY_API_KEY is missing from $SPARK_KEY_ENV_FILE" >&2
    exit 1
  fi
else
  ANTHROPIC_AUTH_TOKEN="$(tr -d '\r\n' < "$SPARK_KEY_FILE")"
fi
if [[ -n "${SPARK_EXPECTED_MODEL:-}" ]]; then
  if ! curl -fsS --max-time "${SPARK_READINESS_TIMEOUT_SECONDS:-5}" \
    -H "Authorization: Bearer ${ANTHROPIC_AUTH_TOKEN}" \
    "${SPARK_MODELS_BASE_URL:-${SPARK_BASE_URL}}/v1/models" \
    | jq -e --arg model "${SPARK_EXPECTED_MODEL}" '.data[]? | select(.id == $model)' >/dev/null; then
    echo "ERROR: Spark endpoint does not expose ${SPARK_EXPECTED_MODEL} at ${SPARK_BASE_URL}" >&2
    exit 1
  fi
fi
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$SPARK_MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$SPARK_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$SPARK_MODEL"
unset ANTHROPIC_API_KEY
fi

cd "$SLOT_CLONE"
fresh_session=1
for arg in "$@"; do
  case "$arg" in
    --continue|-c|--resume|-r|--from-pr|--teleport|--fork-session)
      fresh_session=0
      ;;
  esac
done

if [[ "$fresh_session" -eq 1 ]]; then
  for arg in "$@"; do
    case "$arg" in
      --session-id|--session-id=*)
        echo "ERROR: fresh slot launch refuses a caller-supplied session ID; use explicit --resume/--continue for an authorized continuation" >&2
        exit 78
        ;;
    esac
  done
fi

if [[ "$fresh_session" -eq 1 ]]; then
  session_id="$(uuidgen 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  if [[ ! "$session_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
    echo "ERROR: fresh slot session identity could not be created" >&2
    exit 70
  fi
  set -- --session-id "$session_id" "$@"
fi

# Fixed-checkout Qwen dev slots use PM handoffs, not native workflow/scheduling
# or worktree management. Omit those schemas from every request while retaining
# Agent, Skill, coding, task-tracking, and review tools. No hooks/rules are disabled.
# Rollback for the next launch: DEV_SLOT_QWEN_COMPACT_TOOLS=0.
if [[ "$SPARK_PROFILE" == "qwen38-mia" && "${DEV_SLOT_QWEN_COMPACT_TOOLS:-1}" == "1" ]]; then
  set -- --disallowedTools \
    "Workflow,CronCreate,CronDelete,CronList,ScheduleWakeup,EnterWorktree,ExitWorktree" \
    "$@"
fi

# Reasoning effort is per lane. The qwen-next slots (the Flash-Next engine)
# run at xhigh; every other profile keeps the historical low default.
# Rajiv, 2026-09-23. Override for any lane with DEV_SLOT_EFFORT.
SLOT_EFFORT="${DEV_SLOT_EFFORT:-low}"
if [[ "$SPARK_PROFILE" == "qwen38-next" || "$SPARK_MODEL" == "qwen3.8-flash-next" ]]; then
  SLOT_EFFORT="${DEV_SLOT_EFFORT:-xhigh}"
fi
# Opus 5.5 matches the PM launcher's opus55 lane: xhigh.
if [[ "$SPARK_PROFILE" == "opus55" ]]; then
  SLOT_EFFORT="${DEV_SLOT_EFFORT:-xhigh}"
fi

exec "$CLAUDE_BIN" \
  --model "$SPARK_MODEL" \
  --effort "$SLOT_EFFORT" \
  --append-system-prompt-file "$IDENTITY_RULE_PATH" \
  --permission-mode bypassPermissions \
  "$@"
