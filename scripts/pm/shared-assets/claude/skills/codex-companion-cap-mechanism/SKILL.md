---
name: codex-companion-cap-mechanism
description: |
  Understand how the Codex companion's plan/code/qa review cap is computed.
  Use when: (1) the companion refuses a review with PLAN_REVIEW_CAP_REACHED
  or CODE_REVIEW_CAP_REACHED, (2) a slot reports a cap that seems wrong,
  (3) debugging why a review round was blocked. The cap is LOCAL, not
  server-side — computed by .claude/scripts/pr-review-budget.py which
  globs marker files in /tmp and /tmp/codex-review-companion/.
  NOT for: server-side rate limits from the codex app-server cloud API,
  Claude Code session caps, or weekly-limit exhaustion.
author: Claude Code
version: 1.1.0
date: 2026-07-26
last-validated: 2026-08-04
supersedes: []
---

# Codex Companion Review Cap Mechanism

## When NOT to Use
- Claude Code session/active-hour cap exhaustion — check `/compact` or session restart
- Codex CLI cloud API rate limit — different mechanism (server-side)
- OpenAI/ChatGPT usage limits — unrelated to this project
- Slot hit a `CODE_REVIEW_CAP_REACHED` from a legitimate 3 previous rounds — the cap may be correct, just verify with the script

## Problem

The Codex companion (`codex-review-companion.mjs`) refuses a review with:

```
ERROR: PLAN_REVIEW_CAP_REACHED issue=6801 pr=pre-pr negative_plan_rounds=3 cap_reasons=hard_total_plan_rounds:3
```

The cap seems wrong — you've only run 1 round on this issue. Or PM tells you the cap is wrong and to re-fire, but the companion still refuses.

## Context / Trigger Conditions

- Companion returns `PLAN_REVIEW_CAP_REACHED`, `CODE_REVIEW_CAP_REACHED`, or `QA_REVIEW_CAP_REACHED`
- The `negative_X_rounds=N` count seems higher than what you've actually run
- PM says "re-fire, the cap is not real" but the companion still blocks
- Error message mentions `explicit_cap_marker` as one of the cap reasons

## Solution

The cap is NOT server-side and NOT stored in the codex app-server cloud state. It is computed **locally** by the script `.claude/scripts/pr-review-budget.py`, which globs marker files from two directories:

1. **`/tmp/codex-app-{plan,code,qa}-review-{ISSUE}.txt`** — the canonical marker files written by the companion
2. **`/tmp/codex-review-companion/{plan,code,qa}-issue-{ISSUE}-{TIMESTAMP}.md`** — timestamped copies kept by the companion for its 48h window
3. **`/tmp/plan-review-cap-{ISSUE}.txt`** — cap packets written by slot scripts (these HARDEN the cap by adding `explicit_cap_marker`)

### Verify the cap — MUST use the companion's exact invocation flags

The budget's verdict is **invocation-sensitive**. A naive call can return `within_budget` while the companion's exact call returns `rescue_required`. This bit a slot hard: a reviewer reported `REVIEW_CAP_REACHED`, the slot verified with `--pr <PR> --head <H>` alone → `within_budget`, and PM adjudicated the cap "stale for the new head" based on that — but the cap was genuinely active. The slot then had to send PM a correction.

The companion invokes the budget with **`--pr <PR> --issue <ISSUE> --head <H> --live-pr`** — those extra flags change which markers are counted (issue-scoped events + live-PR head binding). Verify with the exact invocation:

```bash
python3 .claude/scripts/pr-review-budget.py --pr <PR> --issue <ISSUE> --head <HEAD_SHA> --live-pr --json
```

Look at `decision` (`rescue_required` = capped) and `required_pm_action` (`run_pr_rescue`). A bare `--pr --head` call that returns `within_budget` is NOT proof the cap is clear.

### The cap is LIFECYCLE-scoped, NOT head-bound

`pr-review-budget.py` states: *"The independent three-round hard cap below remains lifecycle-scoped."* A new commit on the branch, a main merge, or a rework push **does NOT reset** the 3 negative rounds in the 48h window. The cap packet (`/tmp/code-review-cap-{PR}.txt`) records a head, but the cap itself survives head advances — the head recorded is just the head at cap time. So "cap marker binds head X, current head is Y, therefore cap is stale" is a FALSE inference.

**Consequence for reworks:** after a rework on a PR that already consumed 3 negative code rounds, the companion stays cap-blocked (exit 42 `REVIEW_CAP_TERMINAL`) even with a genuinely-reviewed new head. A fresh companion marker for the new head requires the sanctioned override — the exact-head override files (`/tmp/pm-rescope-pr-{PR}.json` / `/tmp/pm-review-pending-{PR}.json` bound to the NEW head) or the cap-rescue path (`pm-codex-pr-rescue` / `pm-kimi3-pr-rescue`). The independent `codex-code-reviewer` subagent verdict (`APPROVE`, P0/P1 none) is NOT a companion-bound marker; report it as such and do not consume the stale marker.
- `cap_reasons` — why each type is capped

Example output:
```
blocking_round_counts_48h = {'plan': 3, 'code': 1}
review_type_caps          = ['plan']
```

### Why your round count is higher than expected

The companion stores a timestamped COPY of each marker in `/tmp/codex-review-companion/`. These persist across companion restarts, machine reboots, and even across different branches/issues. If a prior slot ran 2 plan reviews for a different branch on the same issue last night, those count toward your cap.

Check them:
```bash
ls -la /tmp/codex-review-companion/plan-issue-{ISSUE}-*.md
```

### Fixing a stale cap

If the cap is genuinely wrong (stale rounds from prior work that shouldn't count):

```bash
# Check what markers exist
ls /tmp/codex-app-*-review-{ISSUE}.* /tmp/codex-review-companion/*-issue-{ISSUE}-*.md 2>/dev/null

# Remove stale markers (only if you're sure they're for a different issue/context)
rm /tmp/codex-app-*-review-{ISSUE}.* /tmp/codex-review-companion/*-issue-{ISSUE}-*.md 2>/dev/null

# Don't remove cap packets with explicit_cap_marker — PM must clear those
```

**Important**: The cap is computed by a 48h rolling window. Markers older than 48h are automatically excluded. If you can wait 48h, the cap expires naturally.

### What NOT to do

- **Do not** assume the cap is server-side cloud state — it isn't, and reporting it as such to PM wastes a cycle.
- **Do not** hand-author a marker or verdict — `slot-report-codex-verdict.sh` will reject it and PM will see the bypass.
- **Do not** write a `/tmp/plan-review-cap-*.txt` packet unless instructed — it adds `explicit_cap_marker` to the cap reasons and hardens the block.

## Verification

```bash
python3 .claude/scripts/pr-review-budget.py --issue <ISSUE> --json
# Confirm the blocking_round_counts_48h and review_type_caps match expectations
```

## Example

A slot is dispatched for issue #6801 (child of #6725). The companion refuses R2:
```
PLAN_REVIEW_CAP_REACHED issue=6801 negative_plan_rounds=3
```

The slot ran only 1 round. But checking reveals:
```bash
$ ls /tmp/codex-review-companion/plan-issue-6801-*.md
/tmp/codex-review-companion/plan-issue-6801-1784993627.md  # Previous slot, night before
/tmp/codex-review-companion/plan-issue-6801-1784993821.md  # Previous slot, night before
/tmp/codex-app-plan-review-6801.txt                         # This slot's R1
```

Two rounds ran the night before on the same issue (different branch). Combined with R1 = 3 total. The cap is legitimate.

The cap packet `/tmp/plan-review-cap-6801.txt` hardened it by adding `explicit_cap_marker` — writing that packet made the cap stronger, not weaker.

Correct response: report the cap as verified legitimate to PM, who runs Rule-12c adjudication or CTO rescue.

### Cap is PR-scoped, not head-scoped — clean reworks inherit prior rounds

A CTO disposition or PM rework that replaces the entire diff (e.g. removing the
layer the prior reviews were criticizing) does NOT reset the cap. The companion's
`pr-review-budget.py` tracks markers per PR/issue number, not per git head SHA.
So a rework commit that removes 441 lines and adds 0 still cannot get a fresh
Codex code review if 3 prior negative rounds consumed the cap on the same PR.

**What to do:** the cap is legitimate and cannot be cleared locally (removing
stale markers is not allowed — they're valid markers from the same PR). The
correct path is PM Fable rescue (`Skill(pm-codex-pr-rescue)`) per the PM Fable
Review-Cap Policy. The Fable rescue produces either `PATCH_READY` (apply the
patch and skip further review) or `NO_PATCH_REQUIRED` (terminal adjudication —
the current head passes review, cap bypassed).

**QA review cap** is separate from code/plan caps. But the companion's QA marker
format lacks `HEAD_SHA` — `slot-report-codex-verdict.sh --kind qa` rejects QA
markers for missing HEAD_SHA. Use plain `message-pm` to report QA status instead.

### Cap blocks BEFORE invocation → NO marker is written → slot-report errors

When the cap is hit, the companion returns `allowed:false` at
`reviewBudgetPreflight` (companion line ~339-362) and exits **before invoking
Codex and before writing any marker file** — not even a cap-state marker like
`/tmp/codex-app-code-review-<PR>-cap.txt`. Consequence:

```text
$ slot-report-codex-verdict.sh --pr <PR> --kind code --next "..."
ERROR: marker not found: /tmp/codex-app-code-review-<PR>.txt
```

The canonical reporter hard-errors without a marker and cannot send the cap
report. Do NOT hand-author a marker to satisfy it. The correct report path:

1. Read the companion's exact `..._REVIEW_CAP_REACHED issue=... pr=... negative_..._rounds=... cap_reasons=...` message from the invocation output (or from the subagent that ran the companion).
2. Send it **verbatim** via `message-pm` — quote the cap message exactly, state that no canonical marker was written and you are not claiming a Codex verdict, then give PM the packet-prescribed next action. If that action names the retired pm-review-done transition, PM returns `UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done` with the accepted `PM_RESCUE_MOP_PROVENANCE_ACCEPTED=<sha>` attached as evidence instead of running it — that is the companion's `exactHeadOverrideReviewAdmission` override path, which has no supported invocation.
3. Per the PM Fable Review-Cap Policy the slot keeps ownership and stops ordinary review/rework; no retry (a rerun hits the same local cap deterministically).

Note the cap-reason string can mix classes from different review types, e.g.
`cap_reasons=repeated_same_class:proof,hard_total_plan_rounds:3` on a CODE
review — plan-round history contributes to the reasons list, so don't treat the
reasons text as proof the cap is mis-scoped.

### Plan-cap packet bleeds into the CODE cap — archiving the packet is NOT enough

A `PLAN_REVIEW_CAP_REACHED` packet (`/tmp/plan-review-cap-<issue>.txt`) with
`explicit_cap_marker` hardens the cap for OTHER review types too: the CODE
review for the same issue refuses with `cap_reasons=...,hard_total_plan_rounds:3,explicit_cap_marker`
even when `negative_code_rounds=1`. PM archiving the plan-cap packet AND the
`/tmp/codex-app-plan-review-<issue>.txt` marker (e.g. renaming to
`.superseded-by-cto-override-<ts>`) removes `explicit_cap_marker` from the
reasons but NOT the bleed: `hard_total_plan_rounds:3` persists because
`pr-review-budget.py` also counts the timestamped copies in
`/tmp/codex-review-companion/plan-issue-<issue>-*.md` within the 48h window.
Archiving the `/tmp/codex-app-*` markers alone cannot clear those.

**Resolution path (proven #6968, 2026-08-01):** the slot does not self-clear.
PM clears the plan-cap packet via the supported archive step, the companion
still caps on `hard_total_plan_rounds` bleed, and PM then delegates a rescue
(`pm-kimi3-pr-rescue` / `pm-claude-pr-rescue` with an immutable ledger bound to
the exact head) whose `NO_PATCH_REQUIRED` (or `PATCH_READY`) is the terminal
review adjudication: `skip_further_review=true`, slot runs only the packet's
named affected proof, then proceeds to PR creation. The rescue verdict is NOT a
companion marker — report it as a rescue packet, not as a Codex verdict. After
a validated rescue, the slot must not re-run ordinary reviews (policy: no
Codex/PM/plan/QA review as rescue proof).

## Notes

- The companion app-server (`codex app-server`) process has its own in-memory state but the ACTUAL cap decision is made by `pr-review-budget.py` reading files on disk.
- `review_type_caps` is computed separately per type: `plan`, `code`, `qa`. One type being capped does NOT affect the others.
- The hard cap is 3 rounds per review type. After that, PM Rule-12c adjudication or CTO rescue is the only path.
- Writing any marker file to `/tmp` with issue number counts toward the cap. Including manual test files, cap packets, or debug output.
- A CTO disposition rescoping the PR does not reset the cap — markers persist by PR/issue number, not by head SHA.
