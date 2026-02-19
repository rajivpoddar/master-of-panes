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
- Otherwise → notify PM via Slack (see below) with plan details.

**Implementation Stage**: Once plan is approved and pane enters implementation, the supervisor's job is DONE. Exit immediately after plan approval.

**Do NOT monitor through implementation, testing, QA, or PR creation.** The idle notification hook handles all of that — when the pane goes idle, the PM receives a `[slot N idle — #ISSUE]` message and handles the QA report and PR trigger directly.

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
5. Max 15 minutes total runtime — supervisor only covers the planning stage. Exit after plan approved or escalated.
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
PLANNING → [auto-approve or notify PM] → EXIT
           (idle notification handles QA + PR from here)
```
