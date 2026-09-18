---
name: codex-review-companion
description: Custom Codex review companion (v1.1+) using the `codex app-server` protocol. Drop-in replacement for the deprecated upstream `codex-companion.mjs` and for slot-side `codex exec` invocations. Used internally by reviewer agents (`codex-diag-reviewer`, `codex-plan-reviewer`, `codex-code-reviewer`, `codex-qa-reviewer`, `codex-arch-reviewer`). The `codex-app-*-review` skills now only hold templates and compatibility pointers.
---

# codex-review-companion

## Entrypoint Policy

This is the preferred foreground review harness for dev slots. Dev slots may
either call this script directly from the slot pane or launch the matching
reviewer Agent in foreground mode. Background Agent calls are blocked in dev
slots; use `run_in_background:false` or omit the flag.

PM panes may still launch the matching reviewer agent
(`codex-diag-reviewer`, `codex-plan-reviewer`, `codex-code-reviewer`,
`codex-qa-reviewer`, or `codex-arch-reviewer`) when PM needs background review.
Those agents call this script, parse the JSON verdict, preserve marker files,
and prevent verdict reframing.


Local Codex review orchestration script that talks to Codex via the **app-server protocol** (same wire format the upstream `~/.claude/plugins/marketplaces/openai-codex/plugins/codex/scripts/codex-companion.mjs` uses) instead of `codex exec`.

## Why this exists

- **`codex exec` randomly blocks.** Per Rajiv directive 2026-05-08 13:19 IST (thread `1778225422.613069`): *"note that codex exec sometime randomly blocks. codex app is fine."* The exec wire shape locks the CLI on certain prompt sizes / sandbox-permission paths; the app-server protocol is stable.
- **Upstream companion isn't review-specialised.** The upstream companion is a generic relay. This fork wires in HeyDonna review prompts (`codex-app-plan-review`, `codex-app-code-review`, `codex-app-qa-review`, `codex-app-arch-review` skill bodies), binary-file exclusion flags, and the local-branch fallback for plan reviews.
- **Lives outside the heydonna-app repo.** Per CP #8b (PM-direct infra surface), tooling/infra scripts ship via `~/.claude/skills/` — not via app PRs. Relocated here 2026-05-08 19:14 IST per Rajiv directive (Slack thread `1778247389.415769`).

## Invocation

### Pre-implementation architecture review (`--issue` only)

Architecture review may run before a PR or feature branch exists. In that case,
pass the issue and focus text without inventing a `--branch main` or throwaway
anchor branch:

```bash
node ~/.claude/skills/codex-review-companion/codex-review-companion.mjs \
  --review-type arch \
  --issue <ISSUE> \
  --focus-text "<root cause / proposal text>" \
  --output-format json
```

The companion fetches `origin/main`, records its SHA, injects the issue body,
and tells Codex that no implementation diff is expected. Issue-only source mode
is not available to plan, code, or QA review.

### Pre-PR plan-review (`--branch` — no PR exists yet)

Internal command shape used by `codex-plan-reviewer`. Plan-review fires BEFORE PR creation. The companion requires `--branch <name>` (NOT `--pr <N>`) since no PR exists yet. Push your plan commit to the remote first so the companion can capture the diff via `git diff origin/main..<branch>`:

```bash
# 1. Push your plan branch to remote
git push -u origin <branch-name>

# 2. Run plan-review with --branch
node ~/.claude/skills/codex-review-companion/codex-review-companion.mjs \
  --review-type plan \
  --branch <branch-name> \
  --plan-file docs/plans/issue-<N>-*.md \
  --output-format json
```

### Post-PR review (`--pr` — PR exists)

Internal command shape used by reviewer agents for code/qa/arch reviews after PR creation, or post-PR plan-review:

```bash
node ~/.claude/skills/codex-review-companion/codex-review-companion.mjs \
  --review-type <plan|code|qa|arch> \
  --pr <PR-number> \
  [--branch <branch-name>] \
  --output-format json
```

For `--review-type code`, the companion must be run from the checked-out PR
branch at the PR head. It refuses to review from PM `main`, a stale slot clone,
or a mismatched branch, and computes the diff as `origin/<base>...HEAD`. This is
intentional: code review grades the current slot branch, not a remote branch
name that may be stale or a PM clone HEAD that belongs to another issue.

All review types default to `gpt-5.5` with `effort: high` unless explicitly
overridden with `--model` or `--effort`.

## Timeout contract

The companion owns a 9-minute internal app-server deadline and requires a
10-minute outer Bash deadline. When invoking it through Claude's Bash tool, set
`timeout: 600000`. Never use the old 180000ms/3-minute deadline: high-effort
reviews commonly take 2-5 minutes, and Bash terminates the process with exit
143 before the companion's own deadline.

The companion emits phase lines and a heartbeat every 30 seconds on stderr. A
SIGTERM diagnostic with
`classification=CALLER_TIMEOUT_OR_TERMINATION` means the caller killed a live
review; it is not evidence that Codex is down. Do not launch parallel retries.
Check for a current-head canonical marker, then rerun once with the required
10-minute outer timeout if no valid marker exists.

Only classify Codex unavailable when the companion itself exits 3 before caller
termination with an app-server initialization/transport error, or when a
separate app-server health probe fails. While a review is slow, preserve
`review_pending:tool_slow`; do not use Zen as an approval substitute and do not
self-author a marker.

### Common trip — `--pr <issue-num>` (2026-05-11 23:25 IST)

**Anti-pattern (CP #1 violation):** Slot at plan-review phase passes the GitHub ISSUE number to `--pr`. The issue number is NOT a PR number — `gh pr view <issue-num>` fails with:

```
gh pr view <N> failed: GraphQL: Could not resolve to a PullRequest with the number <N>
```

When the companion errors, slots have hit the forbidden `codex exec` fallback (blocked by `~/.claude/hooks/block-codex-exec.sh`) and then SELF-REVIEWED the plan — a Constitutional Principle #1 violation. **Use `--branch <name>` for pre-PR plan-review.** See `feedback_plan_review_companion_uses_branch_not_pr_when_no_pr_exists.md`.

The wrapper agents at `~/.claude/agents/codex-{plan,code,qa,arch}-reviewer.md` invoke this script directly. The `codex-app-*-review` skill bodies are retained only for templates, legacy docs, and harness maintenance.

## Marker contract

Every generated marker begins with:

```text
VERDICT: <exact companion verdict>
COMPANION_VERDICT: <same exact companion verdict>
FINAL_REVIEWER_VERDICT: <same exact companion verdict unless a wrapper explicitly returns PM_ADJUDICATION_REQUIRED outside the marker>
MARKER_PROVENANCE: codex-review-companion
TYPE: <plan|code|qa|arch>-review
TIMESTAMP: <epoch seconds>
```

The marker is owned by this companion. Wrapper agents and slots must not
hand-author or rewrite `/tmp/codex-app-*.txt`. If a wrapper disagrees with a
negative companion verdict after finding classification, it must preserve the
marker and return `FINAL_REVIEWER_VERDICT: PM_ADJUDICATION_REQUIRED` outside the
marker rather than changing `VERDICT`.

## Local-branch fallback (commit `73bf9990c`)

When a plan review is requested but the PR's branch is not visible to the remote yet (Codex pulls from origin by default), the companion auto-detects whether the requested branch exists locally and feeds Codex the local working tree instead of failing with "branch not found on origin". Implemented at `codex-review-companion.mjs:250-253`. Without this fallback, plan reviews on freshly-pushed branches race the remote and fail intermittently.


## Code/QA review plan injection

For code and QA reviews, the companion injects `docs/plans/issue-<N>-*.md` into review templates through the `[paste plan content if present]` placeholder when a matching plan file exists. Code review uses this to compare implementation/tests against the accepted executable AC/test contract. QA review uses it to require AC-by-AC Plan Contract Coverage evidence in the QA report.

If no matching plan file is present, the placeholder is replaced with an explicit no-plan marker and the review falls back to the issue body, QA brief/report, and PR diff.

## Hook exemption

The companion may be subject to Claude Code hook policies that block direct `codex exec` invocations. Because this script uses `codex app-server` (not `exec`), the exemption is no longer required as a runtime gate — kept as defensive backstop only.

## Provenance

- v1.0: commit `49daf2fe3` (in-repo `scripts/codex-review-companion.mjs`)
- v1.1: commit `02bc2f6dd` — switched from `codex exec` to `codex app-server` protocol
- local-branch fallback: commit `73bf9990c`
- relocated to `~/.claude/skills/codex-review-companion/` 2026-05-08 (this file)

## Same-head review suppression and the explicit re-review path (verified 2026-08-04, #6518/#6544)

The companion refuses a plain re-review of a head it has already recorded at
layer `slot_codex`:

```
SAME_HEAD_REVIEW_SUPPRESSED type=code head=<sha>. Reuse the durable exact-head marker; no Codex invocation was started.  (exit 42)
```

This bites when the existing marker is NOT consumable even though the head is
unchanged — the two known cases:

1. **Marker carries `PR: -`** (review ran before PR creation) — the readiness
   gate rejects a pre-PR marker as PR-bound approval.
2. **A PR-bound marker is needed but the last review was branch-bound** — same
   head, different binding requirement.

The escape is the documented explicit re-review path. Two requirements:

- `--previous-head <sha>` must be a PRIOR REVIEW head that is an **ancestor of
  the current head** (e.g. the prior review cycle's head, or the PR diff's
  merge-base). Passing the CURRENT head as `--previous-head` re-suppresses.
- `--rework-items "<delta description>"` must accompany it.

```bash
node ~/.claude/skills/codex-review-companion/codex-review-companion.mjs \
  --review-type code --pr <PR> --issue <N> \
  --previous-head <prior-review-head-sha> \
  --rework-items "delta since prior head" \
  --model gpt-5.5 --effort high --output-format json --verbose
```

This starts a GENUINE new Codex invocation and the companion writes a fresh
marker bound to the current head (now carrying `PR: <N>`). Do not hand-edit the
old marker's `PR:` line — the gate validates provenance and head binding.

Pre-firing note: emit the PR-bound marker proactively right after PR creation
(when the head is already reviewed) so the readiness gate does not reject
the packet and cost a rejection → re-fire cycle.
