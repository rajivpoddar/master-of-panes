#!/usr/bin/env bash
# Single PM launcher. Rajiv DM D0AMF0XE6TS thread 1794015303.639509, ts 1794019433.856239:
# "can we combine the launcher script into one and make cliproxy or direct a param or something?"
#
#   --via cliproxy (default): PM on the shared CLIProxyAPI (8317); sonnet -> gpt-6.1-terra; haiku -> gpt-6-luna
#                             (Rajiv 2026-11-02, D0AMF0XE6TS ts 1793605119.883429 / 1793605287.339749).
#   --via direct:             direct Claude on the Max subscription, no proxy (Rajiv ts 1794018254.090169:
#                             "for direct claude, it has to use launch-pm.sh"). No base URL, API key or proxy
#                             token; no proxy-only sonnet/haiku GPT aliases; no proxy API_TIMEOUT_MS.
#   --model <alias|id>:       default claude-opus-5-5. cliproxy: `deepseek` = deepseek-flash-4.1 (DeepSeek API
#                             upstream "deepseek-api" in /opt/homebrew/etc/cliproxyapi.conf). direct: `opus` =
#                             claude-opus-5-5. Only the main model changes.
# Remaining args pass through to claude. MoP restarts the PM with
# `launch-pm.sh --via <pm_launcher> --model <pm_model>` (POST /pm-launch-config).
# launch-pm-cliproxy.sh is a shim: exec launch-pm.sh --via cliproxy "$@".
# Tracked copy: master-of-panes scripts/pm/launchers/ (this deployed copy is authoritative, S19).
set -euo pipefail
VIA=cliproxy
MODEL=claude-opus-5-5
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --via) VIA="${2:?--via needs cliproxy or direct}"; shift 2 ;;
    --via=*) VIA="${1#*=}"; shift ;;
    --model) MODEL="${2:?--model needs a value}"; shift 2 ;;
    --model=*) MODEL="${1#*=}"; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
case "$VIA" in
  cliproxy)
    # Rajiv 2026-10-16 DM D0AMF0XE6TS thread 1792129034.811779 ("lets try the simpler way").
    # Rajiv 2026-11-03 D0AMF0XE6TS thread 1793687676.079679 ts 1793688678.331899: "it has to be through cliproxyapi. it should be a param in cliproxyapi script".
    [[ "$MODEL" == deepseek ]] && MODEL=deepseek-flash-4.1
    source "$HOME/.config/cliproxyapi/client.env"
    unset ANTHROPIC_API_KEY
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8317
    export ANTHROPIC_AUTH_TOKEN="$CLIPROXY_API_KEY"
    # Only the PM main model changes; sonnet/haiku tiers stay fixed (Rajiv ts 1793688995.239109).
    export ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-6.1-terra
    export ANTHROPIC_DEFAULT_HAIKU_MODEL=gpt-6-luna
    if [[ "$MODEL" == deepseek-flash-4.1 ]]; then
      # DeepSeek V4.1 Flash: 1M = 1,048,576 tokens (input + output).
      # Use Claude Code's explicit limit while preserving the CLIProxyAPI alias.
      export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1048576
    fi
    [[ "$MODEL" == claude-* ]] || export API_TIMEOUT_MS="${API_TIMEOUT_MS:-1200000}"
    ;;
  direct)
    [[ "$MODEL" == opus ]] && MODEL=claude-opus-5-5
    unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
    unset ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL API_TIMEOUT_MS CLAUDE_CODE_MAX_CONTEXT_TOKENS
    ;;
  *)
    echo "launch-pm.sh: --via must be cliproxy or direct (got '$VIA')" >&2
    exit 2
    ;;
esac
cd /Users/rajiv/Downloads/projects/heydonna-app
exec claude --model "$MODEL" --dangerously-skip-permissions ${ARGS[@]+"${ARGS[@]}"}
