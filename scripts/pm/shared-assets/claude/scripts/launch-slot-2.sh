#!/bin/bash
set -euo pipefail

# GPT-6.1 Sol via the local CLIProxyAPI; an explicit --spark-profile still overrides it.
export DEV_SLOT_SPARK_PROFILE="${DEV_SLOT_SPARK_PROFILE:-gpt61sol}"
# Discard the keep-alive's legacy Ornith budgets; use the selected profile's limits.
unset DEV_SLOT_SPARK_MAX_CONTEXT_TOKENS DEV_SLOT_SPARK_MAX_OUTPUT_TOKENS
exec /Users/rajiv/.claude/scripts/launch-dev-slot-claude.sh 2 "$@"
