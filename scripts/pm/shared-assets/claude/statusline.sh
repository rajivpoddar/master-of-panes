#!/bin/bash
input=$(cat)
MODEL=$(echo "$input" | jq -r '.model.display_name // "unknown"')
case "$MODEL" in
  deepseek-flash)
    MODEL="ds4.1-flash"
    ;;
  deepseek-v4-flash-0731|spark_ds4/ds4-flash-spark)
    MODEL="DS4 Flash (Spark)"
    ;;
esac
# Panes launched via a CLIProxyAPI launcher (e.g. launch-pm-cliproxy.sh) route through
# 127.0.0.1:8317; tag the model so the routing is visible (Rajiv 2026-10-28).
case "${ANTHROPIC_BASE_URL:-}" in
  *127.0.0.1:8317*|*localhost:8317*) MODEL="$MODEL (cliproxy)" ;;
esac

# Account email with caching (5-min TTL via claude auth status)
CACHE_FILE="/tmp/claude-auth-cache-$$"
GLOBAL_CACHE="/tmp/claude-auth-cache"
CACHE_TTL=300
ACCOUNT=""
if [ -f "$GLOBAL_CACHE" ]; then
  cache_age=$(( $(date +%s) - $(stat -f %m "$GLOBAL_CACHE" 2>/dev/null || echo 0) ))
  if [ "$cache_age" -lt "$CACHE_TTL" ]; then
    ACCOUNT=$(cat "$GLOBAL_CACHE")
  fi
fi
if [ -z "$ACCOUNT" ]; then
  # Fast path: read ~/.claude.json directly (~5ms)
  ACCOUNT=$(jq -r '.oauthAccount.emailAddress // empty' ~/.claude.json 2>/dev/null)
  [ -n "$ACCOUNT" ] && echo "$ACCOUNT" > "$GLOBAL_CACHE"
fi
PROJECT_ROOT=$(echo "$input" | jq -r '.workspace.current_dir // ""')
# Fallback: if PROJECT_ROOT is empty or .env.local not found, use git root
if [ -z "$PROJECT_ROOT" ] || [ ! -f "${PROJECT_ROOT}/.env.local" ]; then
  PROJECT_ROOT=$(git -C "${PROJECT_ROOT}" rev-parse --show-toplevel 2>/dev/null || echo "")
fi
# Numbered checkout identity wins over stale or missing .env.local values. This
# also works when Claude is relaunched directly from the pane, bypassing the launcher.
case "$PROJECT_ROOT" in
  /Users/rajiv/Downloads/projects/heydonna-app-3001) SLOT_NAME=Rohini ;;
  /Users/rajiv/Downloads/projects/heydonna-app-3002) SLOT_NAME=Hasta ;;
  /Users/rajiv/Downloads/projects/heydonna-app-3003) SLOT_NAME=Ashwini ;;
  /Users/rajiv/Downloads/projects/heydonna-app-3004) SLOT_NAME=Chitra ;;
  /Users/rajiv/Downloads/projects/heydonna-app-3005) SLOT_NAME=Revati ;;
  /Users/rajiv/Downloads/projects/heydonna-app-3006) SLOT_NAME=Pushya ;;
  /Users/rajiv/Downloads/projects/heydonna-app-3007) SLOT_NAME=Swati ;;
  /Users/rajiv/Downloads/projects/heydonna-app-3008) SLOT_NAME=Anuradha ;;
  *) SLOT_NAME=${SLOT_NAME:-$(grep '^SLOT_NAME=' "${PROJECT_ROOT}/.env.local" 2>/dev/null | cut -d= -f2- | tr -d '"' || echo "")} ;;
esac
ROOT_NAME=$(basename "$PROJECT_ROOT")
CWD=$(pwd)

# Show root dir + cwd relative to it
if [ -n "$PROJECT_ROOT" ] && [ "$CWD" != "$PROJECT_ROOT" ]; then
  REL_CWD="$ROOT_NAME/${CWD#$PROJECT_ROOT/}"
else
  REL_CWD="$ROOT_NAME"
fi

# Git branch (fast — reads .git/HEAD directly, no subprocess)
BRANCH=""
GIT_HEAD="${PROJECT_ROOT}/.git/HEAD"
if [ -f "$GIT_HEAD" ]; then
  read -r HEAD_CONTENT < "$GIT_HEAD"
  case "$HEAD_CONTENT" in
    ref:*) BRANCH="${HEAD_CONTENT#ref: refs/heads/}" ;;
    *)     BRANCH="${HEAD_CONTENT:0:8}" ;;  # detached HEAD — show short SHA
  esac
fi

# Magenta background + white text
BG="\033[45;97m"
RESET="\033[0m"

# Build account suffix
ACCT_PART=""
[ -n "$ACCOUNT" ] && ACCT_PART=" · \033[36m${ACCOUNT}\033[0m"

if [ -n "$SLOT_NAME" ]; then
  if [ -n "$BRANCH" ]; then
    echo -e "${BG} ${SLOT_NAME} ${RESET} $MODEL · $REL_CWD · $BRANCH${ACCT_PART}"
  else
    echo -e "${BG} ${SLOT_NAME} ${RESET} $MODEL · $REL_CWD${ACCT_PART}"
  fi
else
  if [ -n "$BRANCH" ]; then
    echo -e "$MODEL · $REL_CWD · $BRANCH${ACCT_PART}"
  else
    echo -e "$MODEL · $REL_CWD${ACCT_PART}"
  fi
fi
