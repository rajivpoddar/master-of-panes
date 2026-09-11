#!/usr/bin/env bash
# PreToolUse Bash hook: RETIRED compatibility no-op.
#
# Rajiv directive Ev0C14NF0VED (2026-09-11): PM is blocked only from
# label-gated CI. Clearing pm-blocked:* labels is an ordinary label edit and is
# allowed without review-marker, CTO, Ready-Pool, or promotion ceremony.
#
# The previous body blocked `gh pr edit --remove-label pm-blocked:*` unless a
# current-head PM Claude/Opus review marker existed. That gate is retired. The
# file is retained as a path-stable no-op so the existing settings.json
# registration stays valid; it performs no classification, writes nothing, and
# never blocks. The stdin payload is drained to avoid a broken pipe upstream.
set -euo pipefail
cat >/dev/null 2>&1 || true
exit 0
