#!/bin/bash
set -euo pipefail

# This slot's saved backend; an explicit --spark-profile still overrides it.
export DEV_SLOT_SPARK_PROFILE=swift-qwen38
# 2026-09-20: the engine now serves only qwen3.8-flash-next (the swift alias was
# retired). Keep this profile's budgets, just retarget the model id.
export SWIFT_QWEN38_SPARK_MODEL=qwen3.8-flash-next
# Discard the keep-alive's legacy Ornith budgets; use the selected profile's limits.
unset DEV_SLOT_SPARK_MAX_CONTEXT_TOKENS DEV_SLOT_SPARK_MAX_OUTPUT_TOKENS
exec /Users/rajiv/.claude/scripts/launch-dev-slot-claude.sh 5 "$@"
