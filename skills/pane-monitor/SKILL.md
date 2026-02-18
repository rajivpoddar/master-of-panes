---
name: pane-monitor
description: Monitor a tmux dev pane — poll activity and report progress. Usage /master-of-panes:pane-monitor <pane> [goal]
arguments: "<pane> [goal]"
---

# /master-of-panes:pane-monitor

Launch a background monitoring agent for a tmux dev pane. Polls activity, detects stages (planning → implementation → testing → QA → PR), and takes autonomous actions.

## Instructions

Parse `$ARGUMENTS`: first token is the pane number, the rest is an optional goal description.

### Monitoring Protocol

Use the Task tool with `run_in_background: true` to launch a monitoring agent. Build the prompt from the template below, filling in pane number and goal.

#### Background Agent Prompt Template

```
You are monitoring pane <PANE> for: <GOAL>.

## Polling

Poll every 60 seconds using three-valued exit codes:
  bash <PLUGIN_ROOT>/scripts/is-active.sh <PANE>
  rc=$?
  # 0=ACTIVE, 1=IDLE, 2=ERROR — NEVER use && || pattern

Capture recent output:
  bash <PLUGIN_ROOT>/scripts/capture-output.sh <PANE> 20

## Stage Detection & Actions

**Planning Stage**: Look for "ExitPlanMode", "plan mode", "approve", numbered options.
- Check issue labels: `gh issue view <ISSUE> --json labels -q '.labels[].name'`
- Auto-approve if: labels contain bug/enhancement/infra/types/tech-debt/perf/refactor AND plan touches ≤5 files
  → Send option 2: `bash <PLUGIN_ROOT>/scripts/send-to-pane.sh <PANE> '2'`
- Otherwise → notify PM via tmux send-keys to manager pane (BEFORE Slack):
  ```bash
  LOCAL_TIME=$(date "+%H:%M:%S")
  tmux send-keys -t "0:0.0" "[slot <PANE> plan ready — #<ISSUE>] [$LOCAL_TIME]" 2>/dev/null || true
  sleep 0.3
  tmux send-keys -t "0:0.0" Enter 2>/dev/null || true
  ```
  Then also notify via Slack (see below) with plan details.

**Implementation Stage**: Look for code changes, file edits, "vitest", "tsc", "lint".
- On test failure: let pane fix (up to 3 attempts), then escalate
- On stall (idle >10 min with no new output):
  → Nudge: `bash <PLUGIN_ROOT>/scripts/send-to-pane.sh <PANE> 'continue with the implementation'`
  → Max 3 nudges, then escalate

**QA Stage**: Check for `/tmp/qa-report-<ISSUE>*.md`.
- HIGH confidence + PASS → trigger PR creation:
  `bash <PLUGIN_ROOT>/scripts/send-to-pane.sh <PANE> '/review-and-pr'`
  CRITICAL: Use EXACTLY '/review-and-pr'. Do NOT paraphrase.
- PARTIAL → notify PM: "QA report needs review"
- FAIL → notify PM: "QA failed"

**PR Created**: Look for "github.com/.../pull/" in pane output.
- Extract PR number
- Update labels: `gh issue edit <ISSUE> --add-label status:in-review --remove-label status:in-progress`
- Release pane: `bash <PLUGIN_ROOT>/scripts/update-pane-state.sh <PANE> --release`
- Notify PM via Slack: "✅ PR #N open for #ISSUE"

## Notifications

### Slack Notifications

Use mcp__slack__conversations_add_message with channel_id from project config.
NEVER post to public channels.

Slack uses mrkdwn, NOT markdown:
- Bold: *text* (single asterisks)
- Code: backticks
- Lists: • bullets
- Links: <url|display text>
- NO tables, NO headers

Message format:
  🤖 Pane <N> Supervisor — #<ISSUE>
  <emoji> *<stage>*
  <details>
  *Action needed:* <what PM should do>

## Logging

Write all actions to /tmp/pane-<N>-monitor.log:
  [YYYY-MM-DD HH:MM:SS] STAGE=<stage> ACTION=<action> ISSUE=<issue>

## Guardrails

1. NEVER merge PRs — PM merges manually
2. NEVER touch dev servers
3. NEVER approve plans for features or multi-file refactors — escalate
4. Max 3 nudges per stage, then escalate
5. Max 60 minutes total runtime, then escalate and stop
6. Always send Slack notification on completion
```

### Filling In the Template

Replace:
- `<PANE>` → pane number from arguments
- `<GOAL>` → goal from arguments (or "Monitor pane to completion" if not provided)
- `<PLUGIN_ROOT>` → `${CLAUDE_PLUGIN_ROOT}` (resolved at runtime)
- `<ISSUE>` → extract issue number from goal text (e.g., "#1284" → "1284"), or read from pane state file:
  ```bash
  jq -r '.task' ~/.claude/tmux-panes/pane-<PANE>.json | grep -o '#[0-9]*' | tr -d '#'
  ```

### Output

Provide confirmation:
- Pane being monitored
- Goal
- Issue number (if detected)
- Log file: `/tmp/pane-<N>-monitor.log`
- Background agent ID (for checking progress)

The background agent continues monitoring autonomously until goal is met or timeout.

## Quick Reference: Stage Flow

```
PLANNING → [auto-approve or notify PM]
    ↓
IMPLEMENTING → [nudge on stall, escalate on repeated failure]
    ↓
TESTING → [watch for pass/fail]
    ↓
QA → [trigger /review-and-pr on PASS]
    ↓
PR CREATED → [update labels, release pane, notify PM]
```
