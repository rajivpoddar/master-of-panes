#!/bin/bash
set -euo pipefail

# GPT-6 Luna via the local CLIProxyAPI (Rajiv DM D0AMF0XE6TS ts 1793757949.354869); an explicit --spark-profile still overrides it.
export DEV_SLOT_SPARK_PROFILE="${DEV_SLOT_SPARK_PROFILE:-gpt6luna}"
# Discard the keep-alive's legacy Ornith budgets; use the selected profile's limits.
unset DEV_SLOT_SPARK_MAX_CONTEXT_TOKENS DEV_SLOT_SPARK_MAX_OUTPUT_TOKENS
exec /Users/rajiv/.claude/scripts/launch-dev-slot-claude.sh 5 "$@"
