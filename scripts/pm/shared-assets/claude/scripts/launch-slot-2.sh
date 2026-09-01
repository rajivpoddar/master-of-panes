#!/bin/bash
set -euo pipefail

export DEV_SLOT_SPARK_PROFILE="${DEV_SLOT_SPARK_PROFILE:-ornith}"
exec /Users/rajiv/.claude/scripts/launch-dev-slot-claude.sh 2 "$@"
