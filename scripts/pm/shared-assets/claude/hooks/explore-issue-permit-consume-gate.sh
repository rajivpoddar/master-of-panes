#!/usr/bin/env bash
# explore-issue-permit-consume-gate.sh — RETIRED finalizer; intentional no-op.
#
# It used to be the "last" PreToolUse hook that moved a pending permit to
# consumed right before `gh issue create` ran. That design could not be correct:
# Claude Code runs matching PreToolUse hooks in parallel, so no hook is "last";
# every hook re-parsed the command and re-read the body file at its own moment;
# and the permit was consumed BEFORE gh ran, so a create that never started (or a
# command rewritten by rtk / rewrite-github-priority-label.py) still burned it.
#
# The permit is now consumed in exactly one place: issue-create-owner.py, the
# single owner of a permit-required create. It captures the body bytes once, runs
# the audit and ledger/tracker gates on them, locks, writes durable in-flight
# state before gh, feeds gh the identical bytes on stdin and consumes the permit
# only on a verified issue URL. A raw permit-required `gh issue create` is denied
# by pre-issue-create-audit.sh before any permit write.
#
# This file stays only because ~/.claude/settings.json still registers it as a
# PreToolUse hook (the settings entry can be dropped later). It decides nothing,
# writes nothing and consumes nothing. It drains stdin so the caller's pipe
# never breaks.

cat >/dev/null 2>&1 || true
exit 0
