---
name: anthropic-529-overload-inline-fallback
description: |
  Recovery pattern when the Anthropic API returns `529 Overloaded` on Agent
  tool dispatches (background or foreground subagents). Use when: (1) an
  `Agent(...)` invocation completes with `total_tokens: 0` and the result body
  contains "API Error: 529 Overloaded", (2) multiple consecutive bg-agent
  dispatches fail at startup with zero tool_uses, (3) a time-sensitive
  PM/orchestrator workflow (merge-main, cleanup-pr, label flips, alert
  acknowledgement) needs to proceed before API recovery. Fallback strategy:
  identify which steps actually need an LLM vs which are deterministic shell
  / git / gh / API calls, then execute the deterministic subset inline from
  the main thread (no Anthropic API involvement) and defer the LLM-needing
  steps to a retry once the API recovers.
  NOT for: (1) `429 Rate limited` (use exponential backoff retry, not
  fallback), (2) regular subagent task failures with non-zero token usage
  (those are real errors, debug normally), (3) tasks where every step
  genuinely requires LLM reasoning (no fallback exists — just retry).
author: Claude Code
version: 1.0.0
date: 2026-05-27
last-validated: 2026-05-27
supersedes: []
---

# Anthropic 529-Overload Inline Fallback

## When NOT to Use
- 429 Rate-limit errors — use exponential backoff, not fallback (response includes `retry-after`)
- Subagent failed mid-execution with non-zero token usage — that is a real task failure; debug the subagent's actual work
- Tasks where every step needs LLM reasoning (code review, ambiguous diagnosis) — no inline fallback possible; just retry the dispatch
- Long-horizon work that benefits from the subagent's larger context window — inline fallback collapses that benefit

## Problem
Anthropic API returns `HTTP 529 Overloaded` during periods of cluster strain.
For `Agent` tool dispatches this manifests as the bg-agent completing immediately
with:
- `status: completed`
- `total_tokens: 0`
- `tool_uses: 0`
- Result body: `"API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again in a moment."`

The subagent never started. Any work it was supposed to do is unstarted. If
the orchestrator (PM, automation script, scheduling loop) blindly assumes the
agent ran and trusts its summary, downstream state is silently corrupted (PRs
left in pre-cleanup labels, branches not merged, alerts not acknowledged).

## Context / Trigger Conditions
- `Agent(...)` returned `total_tokens: 0` AND the result string contains `"529 Overloaded"`
- Multiple consecutive Agent dispatches return the same shape within a few minutes
- `status.claude.com` shows API degradation
- Time-pressured workflow that cannot wait for retry (CI merge train, customer alert, cron-scheduled cleanup)

## Solution

### Step 1 — Detect the 529 signature precisely

Do NOT confuse 529 with other failure modes. The 529 signature is:

```
total_tokens == 0
AND tool_uses == 0
AND duration_ms < 30000     # never started
AND result contains "529 Overloaded"
```

Any other shape (`total_tokens > 0`, `tool_uses > 0`, real stack trace) is a
different failure and needs different handling.

### Step 2 — Decompose the bg-agent task into LLM-requiring vs deterministic steps

For each step the bg-agent was supposed to do, classify it:

| Step type | Can inline? | Why |
|---|---|---|
| `gh api`, `gh pr edit`, `gh issue close`, `gh pr view --json` | ✅ Yes | gh CLI talks to GitHub, no Anthropic API |
| `git checkout`, `git merge`, `git push` (no conflict resolution) | ✅ Yes | git binary, no LLM |
| `slack-send.sh` | ✅ Yes | local scripts |
| File reads, sentinel writes, log appends | ✅ Yes | filesystem |
| `modal volume get`, `modal app logs` | ✅ Yes | Modal CLI |
| Diff classification, root-cause diagnosis, code review | ❌ No | needs LLM reasoning |
| Conflict resolution, ambiguous file selection | ❌ No | needs judgment |
| Memo authoring, skill extraction | ❌ No | needs writing |

### Step 3 — Execute the deterministic subset inline

Run the can-inline steps from the orchestrator's main thread using the
appropriate tool (Bash for shell ops, Edit/Write for files, gh CLI via Bash).

Crucial: serialize git operations against the shared clone. The bg-agent
would have held the working tree; inlining loses that exclusivity. If
multiple inline fallbacks need the clone, run them sequentially in one Bash
invocation (`for BRANCH in ...; do ... done`), not in parallel Bash calls.

### Step 4 — Track deferred LLM steps

In persistent state (e.g., `pm-todo.md`, a JSON file, a Slack post), record
which steps were skipped because they needed the LLM. Examples:

- `cleanup-pr` Step 5c (Codex thread resolution via GraphQL) — needs LLM
  to read each thread and decide if resolved. Defer.
- `cleanup-pr` Step 9.55 (Codex meta-analysis) — needs LLM. Defer.
- `ci-failure-investigation` Step 2 classification — needs LLM. Defer with
  raw logs preserved at known paths.

When the API recovers, re-dispatch a bg-agent with the explicit deferred list.

### Step 5 — Report the partial completion honestly

Do NOT claim full completion. State explicitly:
- Which inline steps ran (with proof — gh URLs, commit SHAs)
- Which steps were deferred and why
- When the deferred work will be retried (next API recovery / next session)

This prevents downstream consumers from assuming work is finished.

## Verification

After inline fallback, verify state with read-only commands (still gh API,
no LLM needed):

```bash
# For cleanup-pr: verify labels flipped
gh pr view <PR> --json labels --jq '[.labels[].name]'
gh issue view <ISSUE> --json state --jq '.state'

# For merge-main: verify push landed
gh pr view <PR> --json headRefOid --jq '.headRefOid'
git log --oneline -3

# For ci-failure-investigation: verify verdict was posted
# (LLM-needed for full investigation; inline fallback can only acknowledge in alert thread)
```

## Example (from real session 2026-05-27)

PM dispatched 3 bg-agents in parallel within 4 minutes:
1. `cleanup-pr 5049` — failed 529 (0 tokens)
2. `cleanup-pr 5042` — failed 529 (0 tokens)
3. `merge-main` for PRs #5051/#5046/#5043 — failed 529 (0 tokens)

PM ran inline fallback:

```bash
# cleanup-pr #5049 minimal inline:
gh issue close 5048 --reason completed
gh issue edit 5048 --remove-label status:in-progress --add-label status:done
# (terminal state flips now go through the installed pm-cleanup-pr.py
# label-only caller, not inline history)
gh pr edit 5049 --remove-label slot:1 --remove-label slot:2 ...

# merge-main inline (sequential, all 3 PRs):
for BRANCH in fix/5033-severity-model-v2 fix/5045-editor-feedback-fixes fix/4879-e2e-rerun-discipline; do
  git checkout "$BRANCH" && git pull origin "$BRANCH" --ff-only
  git merge origin/main --no-ff -m "Merge main into $BRANCH"
  git push origin "$BRANCH"
done
git checkout main
```

Inline results: all 3 merges + 2 label flips landed in <2 min. Deferred to
post-API-recovery: Codex thread resolution, buddhi memo classifier, Codex
meta-analysis, artifact cleanup, orphan-sweep — recorded in pm-todo.md
"Cleanup-pr deferred work" section.

## Notes

- 529 retries are cheap on the dispatch side (zero tokens consumed), so a
  one-shot re-dispatch is acceptable before falling back. But do NOT retry
  in a tight loop — the cluster is overloaded; back off.
- The fallback collapses the bg-agent's context isolation benefit. Inline
  execution puts raw command output in the orchestrator's context window.
  Use focused commands with narrow output to limit context bloat.
- The fallback does NOT free the orchestrator to be event-driven — it
  blocks the main thread on the inline work. Reserve the pattern for
  time-pressured work where blocking is acceptable.
- Track 529-fallback frequency. If it happens 3+ times in a single session,
  surface to user — the API outage is impacting their ability to leverage
  bg-agents and may justify pausing time-pressured workflows.

## References

- Anthropic API status: https://status.claude.com
- HTTP 529: non-standard, used by Anthropic to signal cluster overload (distinct from 429 rate-limit)
