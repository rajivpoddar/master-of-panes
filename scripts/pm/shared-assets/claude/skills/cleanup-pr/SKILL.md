<!-- caveman-boundary: inserted 2026-04-19 -->
> **Caveman boundary:** Output from this skill goes to **GitHub PR body + merge commit + cleanup comments**. Write full, professional, grammatical English. If caveman mode is active in the session, treat this skill as an Auto-Clarity exception (see `~/.claude/plugins/marketplaces/caveman/rules/caveman-activate.md`). No fragments, no dropped articles, no abbreviations.

# Cleanup PR

Post-merge cleanup for a PR that Rajiv has already merged. Runs all housekeeping steps.

## SLA: 10 minutes post-merge (Rajiv directive 2026-05-21 10:47 IST thread `1779340644.135029`)

A merged PR without either (a) visible `pm-state:closed-clean` flip plus cleanup tracker reply in the PR's PM transition thread, or (b) an explicit `blocked_deploy` / `blocked_migration` / `blocked_missing_origin` tracker reply in that same transition thread within 15 minutes of the merge timestamp is an exception requiring remediation. Cleanup does **not** wait for post-merge CI. Shipping communication is gated only on Cloudflare production deploy completion and required prod migrations. Post-merge CI failures already alert Slack separately and are processed as their own incident stream. Detection surfaces:

- **`pm-status` drift section** reports the exception under a "Stale cleanup" row.
- **`heartbeat-tasks` Step 6.5** queries:
  ```bash
  RECENT_MERGED=$(gh pr list --search "is:merged merged:>$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)" --json number,mergedAt)
  STALE_CLEANUP=$(gh pr list --label "pm-state:merged-cleanup-pending" --json number,mergedAt)
  # Flag stale-cleanup if RECENT_MERGED ∩ STALE_CLEANUP delta > 10min
  ```
- **`pm-pr-review` drift gate** cross-checks merged-but-not-closed-clean PRs and requires an explicit blocker post when cleanup cannot close cleanly.

Exception remediation:
1. Re-fire `cleanup-pr` skill (bg agent re-runs Steps 1-10 from current state).
2. OR PM-direct closeout via `~/.claude/scripts/pm-state-replace.sh <PR> closed-clean` if all cleanup steps verifiable as done — verify by re-running Step 1 (issue close), Step 5 (branch delete), Step 5c (Codex thread resolution), Step 6 (prod migrations if required), Step 6.5 (Cloudflare production deploy completed), and Step 3.5 (umbrella body update where applicable).

Banned: silently leaving `pm-state:merged-cleanup-pending` past 15 min without remediation. Either complete, post an explicit blocker status, or PM-direct closeout — never skip.

## PM State Label Transitions (Rajiv directive 2026-05-20 11:48 IST thread `1779250700.624619`)

Cleanup-pr is the canonical mover for the post-merge label flow:

- **At Step 1 (immediately on merge detection)** → `~/.claude/scripts/pm-state-replace.sh <PR> merged-cleanup-pending` + `gh pr edit <PR> --add-label pm-cleanup:needed`. This marks the PR as "merged but cleanup procedure not yet complete" so concurrent skills (`pm-status`, idle handlers) can identify in-progress cleanup.
- **At Step 9 final tracker reply (cleanup complete; required prod migrations done, Cloudflare production deploy completed, Step 5c Codex thread resolution done, and Step 3.5 umbrella body updated where applicable)** → `~/.claude/scripts/pm-state-replace.sh <PR> closed-clean` + `gh pr edit <PR> --remove-label pm-cleanup:needed`. Terminal state.

Helper enforces mutual exclusion on `pm-state:*`; `pm-cleanup:needed` is the additive companion that indicates the procedure is in-flight (cleared at Step 9).

If cleanup-pr is interrupted between Step 1 and Step 9 (PM session crash, autocompact), the PR will retain `pm-state:merged-cleanup-pending` + `pm-cleanup:needed` — those labels are the resumption signal on next PM heartbeat / pm-status sweep.

## Usage

```
/cleanup-pr 3502
```

## When to Use

- After Rajiv merges a PR manually
- When PM needs to run the post-merge steps (sync, labels, release, notify)

## When NOT to Use

- PR is not yet merged — use `/merge-pr` instead
- Creating a new PR — use `/review-and-pr`

## Procedure (PM main thread — dispatch only)

Per Constitutional Principle #8b (all infra/tooling work is PM-direct via bg
agents — never slots), the PM main thread does NOT execute the 10-step cleanup
inline. PM main thread does only:

1. **Parse the trigger.** Either:
   - `[PR_MERGED_DETECTED] PR #NNNN merge detected` system-reminder injected by
     `<repo>/.claude/hooks/pm-context-injector.sh` (auto-fires on GitHub Slack
     "merged into main" notifications, `gh pr merge` success output, or
     `:tada:` merge-bot reactions), OR
   - Explicit `/cleanup-pr <PR>` invocation by Rajiv.

2. **Launch a Sonnet bg agent** (`Agent(subagent_type="cleanup-pr-runner", run_in_background=True, ...)`)
   with the 10-step procedure below as the agent prompt. The `cleanup-pr-runner`
   project agent is pinned to `model: sonnet`; do not use `general-purpose`,
   Opus, or the PM main session's backing model for cleanup-pr. If the
   `cleanup-pr-runner` agent is unavailable, stop and report a model-routing
   blocker instead of falling back. The bg agent inherits PM's gh / npx convex /
   aws / git auth and runs the full sequence independently.

3. **Return control to the PM event loop.** PM does NOT block on the bg agent.
   The agent posts its own cleanup tracker as a reply in the PR's PM transition
   thread via `~/.claude/scripts/pm-transition-alert.sh --event cleanup-tracker`
   (never as a fresh top-level `#heydonna-dev` message), posts the
   customer-origin thread reply if applicable, and writes its completion summary
   to PM via the notification channel. PM resumes normal event processing
   (slot-idle, handoff, plan approval) immediately.

**Why this shape:** the cleanup includes long-running calls
(`gh api --paginate`, `find ~/.claude/projects/...`, `ps -A`, optional
migration loops with up to 30 iterations). Running them on the PM main thread
blocks event-driven processing for minutes per merge. Per CP #5 (stay
event-driven — never block the main thread), all such work goes to bg agents.

The bg agent prompt should be: "Run cleanup-pr Steps 1-10 below for PR
#NNNN as the `cleanup-pr-runner` Sonnet agent. Do not wait on post-merge CI.
After required prod migrations and Cloudflare production deploy completion,
post the customer-thread reply per Step 6.6 if applicable, then continue deep
cleanup and post the final tracker to the PR's PM transition thread using
`~/.claude/scripts/pm-transition-alert.sh --event cleanup-tracker --pr
$PR_NUMBER --issue $ISSUE --state closed-clean --message-file
/tmp/cleanup-pr-$PR_NUMBER-tracker.md`. If this task is not running under
`cleanup-pr-runner`, STOP and report a cleanup-pr agent-routing blocker; do not
fall back to Opus or general-purpose. If you ARE running under
`cleanup-pr-runner` but the backing model is NOT Sonnet, REPORT the non-Sonnet
backing in your completion summary and CONTINUE — do not stop. The model pin is
declared at source (`cleanup-pr-runner.md` -> `model: sonnet`) but the runtime
substitutes the backing model regardless of it (verdict 2026-09-19: the #7919
cleanup ran under `cleanup-pr-runner` with `deepseek-flash` backing on all 195
model-tagged messages). A clause that can never be satisfied in this runtime
would wedge every post-merge cleanup, so the requirement is REPORT, not stop.
Enforcing a true Sonnet backing is a routing-layer change, tracked separately,
and gates nothing here." then paste Steps 1 through 10
verbatim.

---

## Process (Sonnet bg agent — full execution)

The `cleanup-pr-runner` Sonnet bg agent runs all steps automatically for the given PR number (passed as ARGUMENTS):

```bash
PR_NUMBER=ARGUMENTS
```

### Step 0.5: Post-merge CI is out of scope for cleanup (updated 2026-06-17 — Rajiv directive)

Do **not** watch or block on post-merge CI in cleanup-pr. Main-branch CI and deploy failures already produce Slack alerts through their own workflows and are processed separately. Cleanup-pr owns issue/label cleanup, required prod migrations, Cloudflare production deploy verification, customer/internal shipping communication, and housekeeping. Waiting on post-merge CI delays the customer update and caused Abi/PM to manually chase shipped-status threads.

If a post-merge CI alert lands while cleanup is running, leave it for the alert-processing path. Do not downgrade or delay `closed-clean` solely because CI is pending.

### Step 1: Verify PR is merged

```bash
STATE=$(gh pr view $PR_NUMBER --json state --jq '.state')
```

If not `MERGED` → STOP with error: "PR #$PR_NUMBER is not merged (state: $STATE)"

Immediately record the durable cleanup start and canonical pending labels:

```bash
~/.claude/scripts/pm-transition.sh cleanup-start --pr "$PR_NUMBER"
```

This event is the retro/sweep proof that cleanup began within the 15-minute SLA.
It is idempotent per PR and must happen before sync, issue closure, migrations, or
other long-running cleanup work.

### Step 2: Sync PM clone

```bash
git checkout main && git pull --ff-only origin main
```

### Step 3: Extract linked issue and decide whether issue close is gated

```bash
# The linked issue comes from GitHub's AUTHORITATIVE closing-issue set, or from
# an explicit caller-supplied issue that the canonical caller verifies. It is
# NEVER derived from the PR title. (CTO ruling 2026-09-19 21:00 IST.)
ISSUE=$(gh pr view $PR_NUMBER --json closingIssuesReferences \
  --jq '.closingIssuesReferences[0].number // empty')
if [ -z "$ISSUE" ]; then
  echo "REFUSE: PR #$PR_NUMBER carries no authoritative closing-issue reference." >&2
  echo "This legacy skill will NOT infer a linked issue from the PR title or body." >&2
  echo "Delegate to the canonical caller instead:" >&2
  echo "  python3 /Users/rajiv/.claude/scripts/pm-cleanup-pr.py" >&2
  echo "  (cleanup_mode=linked_issue with an explicit caller-supplied issue, or" >&2
  echo "   cleanup_mode=merged_pr_issue_less when the closing-issue set is empty.)" >&2
  exit 1
fi
ISSUE_STATE=$(gh issue view $ISSUE --json state --jq '.state')
ISSUE_BODY=$(gh issue view $ISSUE --json body --jq '.body // ""')
RECOVERY_REQUIRED=0
RECOVERY_STATUS=not_required

if printf '%s\n' "$ISSUE_BODY" | grep -qiE 'adminRetryAutoProcess|Recovery post-merge|post-merge recovery|recover(y|ed)? .*transcript|customer transcript .*recover'; then
  RECOVERY_REQUIRED=1
  RECOVERY_STATUS=pending
fi
```

If `RECOVERY_REQUIRED=0` and the issue is OPEN: `gh issue close $ISSUE`.

If `RECOVERY_REQUIRED=1`, do **not** close the issue in Step 3. Leave it open
until Step 6.7 writes recovery proof. A merged code fix is not the terminal
condition for a customer-impacting recovery issue; the terminal condition is the
customer transcript/artifact recovered, with proof attached to the issue and/or
originating thread.

### Step 4: Update labels

Derive every slot label from the issue's live labels — never a fixed
slot:1..slot:4 list, which leaves slot:5 and higher behind:

```bash
set -o pipefail
SLOT_LABELS=$(gh issue view "$ISSUE" --json labels \
  --jq -r '.labels[].name | select(test("^slot:[0-9]+$"))') || {
  echo "Step 4: cannot read live labels for issue $ISSUE; refusing any label mutation." >&2
  exit 1
}
REMOVE_ARGS=(--remove-label status:todo --remove-label status:in-progress \
  --remove-label status:in-review)
while IFS= read -r SLOT_LABEL || [ -n "$SLOT_LABEL" ]; do
  if [ -n "$SLOT_LABEL" ]; then
    REMOVE_ARGS+=(--remove-label "$SLOT_LABEL")
  fi
done <<< "$SLOT_LABELS"
gh issue edit "$ISSUE" "${REMOVE_ARGS[@]}" --add-label status:done
```

**Step 4b: PR pm-state transition to merged-cleanup-pending (Rajiv directive 2026-05-20 11:48 IST)**

The canonical `cleanup-start` transition from Step 1 already atomically applied
the state, cleanup marker, blocker cleanup, and slot release labels. Verify that
the marker is still live; do not mutate it a second time here:

```bash
gh pr view "$PR_NUMBER" --json labels --jq '[.labels[].name] as $labels | (($labels | index("pm-state:merged-cleanup-pending")) != null and ($labels | index("pm-cleanup:needed")) != null)' | grep -qx true
```

At Step 9.6 (cleanup-pr complete, after the delivery retro exists), transition
to the terminal state through the canonical transition guard:

```bash
~/.claude/scripts/pm-transition.sh cleanup --pr $PR_NUMBER --retro-path "/tmp/pr-retro-$PR_NUMBER.md"
gh pr edit $PR_NUMBER --remove-label "pm-cleanup:needed" 2>/dev/null || true
```

(Per Rajiv directive 2026-05-20 11:48 IST — `pm-state:*` labels are canonical. `merged-cleanup-pending` covers Steps 1-9.6; flipping to `closed-clean` happens after the delivery retro has `retro_status: complete` and immediately before the cleanup tracker reply in the PR's PM transition thread.)

**Material-event Kanban trigger (Phase 3.5 of slot-claim spec — 2026-05-24).** Both `pm-state-replace.sh` invocations above (Step 4b `merged-cleanup-pending` and Step 9 `closed-clean`) append one line to `/tmp/kanban-pending.flag` on success. `pm-context-injector` hook reads that flag on next UserPromptSubmit and emits a Kanban-refresh reminder. No additional flag-write needed from this skill — the writer is owned by the canonical state-mutator.

### Step 5: Slot ownership is NOT released here

Post-merge housekeeping must **never** call `mop release` (or any release path).

Slot ownership transitions at the **assignment boundary**, not here. A release is an
ownership operation keyed to the identity of the work that just finished; cleanup-pr runs
downstream of the merge, so it holds a historical identity by definition — best case a
no-op, worst case a stale release landing on a slot that has legitimately moved on to new
work. The epoch/tuple guards fail closed, but they cannot make a downstream trigger correct.

Ownership moves when the **next assignment** takes the slot over atomically with the live
identity in hand (`POST /slots/:n/assign` with the complete expected tuple + expected_epoch,
which CAS-rebinds via `db.rebindSlot` and advances the ownership epoch exactly once), and the
session `/clear` happens at that boundary. No release is required for an assignment onto an
occupied slot to succeed.

`mop release` stays available for explicit operator transitions only — transfer,
capacity, or a terminal slot with no successor — issued from a fresh read with its exact
tuple / idle / inactive requirements.

### Step 5b: Clean up remote branch

GitHub auto-deletes the head branch on merge for this repo (default), but verify
and clean up explicitly in case that setting was toggled off or the merge used
`Update branch` after the auto-delete ran.

```bash
BRANCH=$(gh pr view $PR_NUMBER --json headRefName --jq '.headRefName')
if git ls-remote --heads origin "$BRANCH" 2>/dev/null | grep -q .; then
  git push origin --delete "$BRANCH"
  echo "Deleted remote branch: $BRANCH"
fi
```

Also clean up any local branch in the PM clone and any dev slot clone:

```bash
# PM clone
git branch -D "$BRANCH" 2>/dev/null || true

# Dev slot clones — each slot has its own clone at heydonna-app-3001..3004
for N in 1 2 3 4; do
  git -C "$HOME/Downloads/projects/heydonna-app-300$N" branch -D "$BRANCH" 2>/dev/null || true
done
```

If the branch was owned by a slot, invoke the `slot-post-merge-cleanup` skill
on that slot instead of raw git commands — it handles the whole
post-squash-merge state reset cleanly. (Rajiv directive 2026-04-19:
`feedback_slot_cleanup_post_squash_merge.md`.)

### Step 5c: Resolve unresolved Codex review threads (MANDATORY — Rajiv directive 2026-05-19 09:47 IST + 10:34 IST clarification)

PM-side cleanup coverage for the case where the slot pushes a fix to address Codex inline P0/P1/P2 review comments but skips Step 7 of `address-pr-review` (`resolveReviewThread` GraphQL mutation). GitHub does NOT auto-resolve threads on push or merge. Replies do NOT auto-resolve either. The mutation must be invoked explicitly.

**Trigger incident:** PR #4686 — slot 4 pushed `6e4acab61` which addressed two Codex P1 findings (auto-process overwrite + recursive fixture-wrapper) but never ran the resolve mutation. Both threads stayed `isResolved: false` post-merge until Rajiv flagged the inconsistency. PM ran the mutation manually to clear state. Rajiv DM `1779163902.047329` 2026-05-19 09:47 IST: *"Approved. Please add Step 5c to cleanup-pr."*

**Rule (Rajiv directive 2026-05-19 10:34 IST thread `1779166624.518699`):** Gate blocks on ALL unresolved Codex-authored review threads on the PR, not just threads whose first comment commit matches HEAD. If code fixed the finding, resolve the thread; if obsolete or noise, resolve with a one-line rationale comment first; unresolved = NOT merge-ready.

**Procedure (idempotent + verification):**

The GraphQL query below pulls ALL Codex-authored review threads on the PR (filter is `author.login matches /codex/i` + `isResolved == false`), regardless of which commit the first comment was made against. There is no commit-SHA restriction — older threads on prior commits are gated too.

```bash
# 1. Fetch all unresolved Codex-authored review threads on this PR (ALL commits, not just HEAD)
UNRESOLVED_THREADS=$(gh api graphql -f query='
query($pr: Int!) {
  repository(owner: "heydonna-app", name: "heydonna-app") {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes { author { login } }
          }
        }
      }
    }
  }
}' -F pr=$PR_NUMBER --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | select(.comments.nodes[0].author.login | test("codex"; "i")) | .id')

UNRESOLVED_COUNT=$(echo "$UNRESOLVED_THREADS" | grep -c . || echo 0)

if [ "$UNRESOLVED_COUNT" -eq 0 ]; then
  echo "Step 5c: 0 unresolved Codex threads — silent skip"
else
  # 2. Resolve each thread via GraphQL mutation
  RESOLVED_COUNT=0
  FAILED_COUNT=0
  for THREAD_ID in $UNRESOLVED_THREADS; do
    RESULT=$(gh api graphql -f query='
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}' -F threadId="$THREAD_ID" 2>&1)
    if echo "$RESULT" | grep -q '"isResolved":true'; then
      RESOLVED_COUNT=$((RESOLVED_COUNT + 1))
    else
      FAILED_COUNT=$((FAILED_COUNT + 1))
      echo "Step 5c: failed to resolve $THREAD_ID — $RESULT"
    fi
  done

  # 3. Verify post-state via second GraphQL query (no stale read)
  STILL_UNRESOLVED=$(gh api graphql -f query='
query($pr: Int!) {
  repository(owner: "heydonna-app", name: "heydonna-app") {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 1) { nodes { author { login } } }
        }
      }
    }
  }
}' -F pr=$PR_NUMBER --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | select(.comments.nodes[0].author.login | test("codex"; "i"))] | length')

  echo "Step 5c: resolved $RESOLVED_COUNT / $UNRESOLVED_COUNT Codex threads (failed: $FAILED_COUNT, still unresolved post-verify: $STILL_UNRESOLVED)"

  # 4. Surface failures to Rajiv DM if verification shows residual unresolved threads
  if [ "$STILL_UNRESOLVED" -gt 0 ]; then
    echo "WARNING: $STILL_UNRESOLVED Codex threads remain unresolved on PR #$PR_NUMBER after mutation attempts" \
      | bash ~/.claude/skills/slack-message/scripts/slack-send.sh -f
  fi
fi
```

**Include the count in the tracker post** under Step 9 / final tracker message:
```
- Step 5c: <N> Codex review threads resolved via GraphQL
```

**Why this exists:**
- Slots SHOULD run Step 7 of `address-pr-review` (`~/.claude/commands/address-pr-review.sh <PR> resolve`) after pushing a fix. In practice they often skip it — the fix-push-merge cycle completes without the resolution mutation.
- GitHub UI shows the thread as "Unresolved" even when the underlying line is patched, leaving misleading state on merged PRs.
- The codex-comment-processing skill (CP #12) handles PM-side replies via `gh-graphql-reply.sh` but doesn't auto-resolve.
- This step is the choke point: PM-side cleanup-pr runs once per merge and can sweep unresolved Codex threads idempotently.
- Filter is `author.login matches /codex/i` to avoid resolving threads from human reviewers (which require human judgment to mark resolved).
- Idempotent: skips silently when 0 unresolved threads; safe to re-run.
- Verification: a second `reviewThreads` query after the mutation loop catches any failed mutations (transient API errors, GraphQL rate limiting).

### Step 5c.1: Also check issue-level Codex bot comments (MANDATORY — cleanup-pr meta-analysis 2026-09-17 PR #7845 trigger)

<!-- Codex meta-analysis 2026-09-17 PR #7845 trigger: bot-review-posted-as-issue-comment -->

The `reviewThreads` GraphQL query in Step 5c only sees Codex findings posted as
**inline PR review comments** (comments attached to a specific diff line that
create a resolvable thread). GitHub Codex can also post a review as a **plain
issue-level comment** (`/repos/.../issues/<PR>/comments`) containing one or
more `P0`/`P1`/`P2` findings with file/line references embedded in the comment
body as markdown links, not as GitHub review comments. `reviewThreads` returns
zero results for these — they are structurally invisible to Step 5c's
detection and resolution mechanism, and there is nothing to "resolve" via
`resolveReviewThread` (no thread ID exists).

**Trigger incident:** PR #7845 — Codex posted 2 P1 findings
(`guarded-recovery-adoption.ts:72` recovery-adoption race,
`page.tsx:3447-3448` owner-only recovery gate) as a plain issue comment at
commit `f7365e0c`. Both `pulls/<PR>/comments` (review comments) and GraphQL
`reviewThreads` returned empty/zero, so Step 5c reported "0 unresolved Codex
threads" while 2 unaddressed P1s existed at the merged head — neither
subsequent commit touched the flagged files. Caught only because cleanup-pr
separately fetched `issues/<PR>/comments` while investigating an unrelated
question.

**Procedure (run alongside Step 5c, same idempotent pattern):**

```bash
BOT_ISSUE_COMMENTS=$(gh api /repos/heydonna-app/heydonna-app/issues/$PR_NUMBER/comments --paginate \
  --jq '[.[] | select(.user.login | test("codex"; "i"))] | length')

if [ "$BOT_ISSUE_COMMENTS" -gt 0 ]; then
  echo "Step 5c.1: $BOT_ISSUE_COMMENTS Codex issue-level comment(s) found — read in full and verify each P0/P1/P2 finding against the current merged/head diff (git diff <reviewed-commit>..<merge-head> -- <flagged-file>)."
  # For each finding still live at the merge head:
  #   - if merged PR: post an ack reply on the PR + file a bounded follow-up
  #     issue (Directive Contract + Issue Contract Ledger sections required by
  #     repo hooks even for a non-directive-derived engineering finding).
  #   - if not yet merged: this blocks merge-ready per CP #12 (codex-comment-processing).
else
  echo "Step 5c.1: 0 Codex issue-level comments — silent skip"
fi
```

Include the count in the tracker post alongside Step 5c's count:
```
- Step 5c.1: <N> Codex issue-level comments found, <M> still live at merge head (follow-up issue: #<NNNN> or none)
```

### Step 5c.2: Also check Codex review-body findings (MANDATORY)

Codex can submit a finding in `pulls/<PR>/reviews[].body` with state
`COMMENTED` and no inline review comment. Fetch review objects separately;
`pulls/<PR>/comments` does not include review bodies:

```bash
gh api --paginate /repos/heydonna-app/heydonna-app/pulls/$PR_NUMBER/reviews \
  > "/tmp/pm-codex-review-bodies-PR-$PR_NUMBER.json"
```

For each review whose `user.login` matches `/codex/i`, inspect its non-empty
`body`. A body carrying a P0, P1, or P2 badge is a finding bound to that
review's `commit_id`; include the review ID and commit in the cleanup record.
A dashboard-only `Review Summary` body without a P0/P1/P2 badge is not a
finding. Treat each finding as open unless the current head has moved beyond
`commit_id` **and** the finding is addressed: compare the diff from that
reviewed commit to the current head against the finding, and retain it as open
if the change does not address it or the relationship is unclear. A newer head
alone does not resolve a finding. An unaddressed P0/P1/P2 review-body finding
blocks merge-ready; use the existing cleanup decision path and do not invent
an inline-thread mutation for a review body.

The read-only `pr-state-sweep` check emits each badge-bearing Codex review with
its review ID, `commit_id`, current head, and whether the head moved. An
advanced head is explicitly marked for addressed-verification and is not
auto-cleared; API read failures are surfaced rather than reported as zero
findings.

### Step 6: Classify whether the PR requires a migration, then run it

**Don't use a regex.** Regexes miss real cases (e.g., `convex/migrations.ts` root file vs `convex/migrations/*.ts` directory vs `*-backfill.ts` vs migrations declared inside `convex/admin.ts`). 2026-05-01 incident: PR #3936 modified `convex/migrations.ts` (singular root file) which the old regex `convex/migrations/|backfill|migration\\.ts$` missed → migration silently skipped → customer-facing UI bug surfaced 6h later (Abilaasha #3918 follow-up).

**Use PM judgment + the PR body as the authoritative signal.**

1. Pull the PR body + file list:
   ```bash
   gh pr view $PR_NUMBER --json body,files --jq '{body, files: [.files[].path]}'
   ```

2. **Classify migration intent** by reading the PR body and file changes. A migration is required if ANY of these is true:
   - PR body has a `## Migration` / `## Migration Required` / `## Post-merge migration` section
   - PR body explicitly lists `npx convex run migrations:<...>` or `npx convex run <module>:<backfillFn>` commands
   - PR body mentions "migrate", "backfill", "one-shot mutation" + "post-merge" / "after merge" / "run on prod"
   - File changes add or modify a function in `convex/migrations*.ts` OR a function under `convex/migrations/*.ts` directory OR an admin mutation with `Backfill` / `migrate` / `repair` in its name
   - PR title or body contains "(migration)" or follows the pattern "fix(...): ... migration"
   - File changes include `*-backfill.ts` or `*-backfill-*.ts`

3. If you're uncertain after reading the body + files, **default to YES** (assume migration required) and ask Rajiv via DM with the candidate command(s) extracted from the PR body. Surfacing a false-positive migration ask is cheaper than missing a real one.

4. **If migration required:**

   a. Post to #heydonna-dev before running:
   ```
   :construction: Running post-merge migration(s) from PR #$PR_NUMBER on prod:
   <list extracted commands>
   ```

   b. Extract the EXACT commands from the PR body's migration section. The PR author lists them deliberately. If the body doesn't list commands explicitly, infer from the modified migration function names (e.g., function `migrateFooBars` exported in `convex/migrations.ts` → `npx convex run migrations:migrateFooBars --prod`).

   c. **Validate args before running:** read the migration function's `args:` schema in the source file. Common gotchas:
   - `cursor: v.optional(v.string())` REJECTS `null` — must omit cursor on first call (`'{}'` not `'{"cursor":null}'`).
   - `table: v.union(v.literal(...), v.literal(...))` requires exact string match.
   - Some migrations require `dryRun: false` to actually write.

   d. Capture output, loop until `isDone: true` or `remaining: 0`:
   ```bash
   CURSOR=""
   for i in {1..30}; do
     ARGS="{\"table\":\"...\"}"
     [ -n "$CURSOR" ] && ARGS="{\"table\":\"...\",\"cursor\":\"$CURSOR\"}"
     RESULT=$(npx convex run migrations:<functionName> --prod "$ARGS" 2>&1)
     echo "$RESULT"
     DONE=$(echo "$RESULT" | grep -oE '"isDone":\s*true')
     [ -n "$DONE" ] && { echo "DONE on pass $i"; break; }
     CURSOR=$(echo "$RESULT" | grep -oE '"cursor":\s*"[^"]+"' | head -1 | sed 's/"cursor":\s*//;s/"//g')
     [ -z "$CURSOR" ] && break
   done
   ```

   e. Post completion to #heydonna-dev with final counts (scanned / updated / skipped / errors).

   f. **Ambiguity escalation:** if migration has unclear arg schema, scope (which table), or safety (irreversible writes against prod), **STOP and DM Rajiv** with the extracted command + your interpretation. Do NOT guess args against prod data.

5. If no migration is required, set `MIGRATION_STATUS=not_required`. If migration is required and completes, set `MIGRATION_STATUS=completed`. If migration is blocked or unsafe to run, STOP before customer notification and post `customer_update_status=blocked_migration` to the PR's PM transition thread with `pm-transition-alert.sh --event cleanup-blocked`.

### Step 6.5: Verify Cloudflare production deploy completed (shipping gate)

This is the shipping gate for thread updates. A PR is not customer-shipped until the Cloudflare production build for the merge SHA has completed successfully and any required prod migrations from Step 6 have run successfully. Do not wait for unrelated post-merge CI. CI failures alert Slack separately.

```bash
MERGE_SHA=$(gh pr view $PR_NUMBER --json mergeCommit --jq '.mergeCommit.oid')
CF_CHECK_NAME="Workers Builds: heydonna-app"
CF_CHECK_NAME_ENCODED=$(python3 - <<'PY'
import urllib.parse
print(urllib.parse.quote("Workers Builds: heydonna-app"))
PY
)
CF_STATUS=""
CF_CONCLUSION=""
CF_URL=""

for i in {1..60}; do
  CF_JSON=$(gh api "/repos/heydonna-app/heydonna-app/commits/$MERGE_SHA/check-runs?check_name=$CF_CHECK_NAME_ENCODED" 2>/dev/null || printf '{"check_runs":[]}')
  CF_FIELDS=$(printf '%s' "$CF_JSON" | python3 -c '
import json, re, sys
raw = sys.stdin.read()
# Guard against terminal/control-character contamination in gh output.
raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
try:
    data = json.loads(raw)
except Exception:
    data = {}
runs = [r for r in (data.get("check_runs") or []) if isinstance(r, dict)]
runs.sort(key=lambda r: r.get("started_at") or r.get("created_at") or "")
run = runs[-1] if runs else {}
print(run.get("status") or "")
print(run.get("conclusion") or "")
print(run.get("html_url") or "")
')
  CF_STATUS=$(printf '%s\n' "$CF_FIELDS" | sed -n '1p')
  CF_CONCLUSION=$(printf '%s\n' "$CF_FIELDS" | sed -n '2p')
  CF_URL=$(printf '%s\n' "$CF_FIELDS" | sed -n '3p')

  if [ "$CF_STATUS" = "completed" ] && [ "$CF_CONCLUSION" = "success" ]; then
    echo "Cloudflare production deploy complete for $MERGE_SHA: $CF_URL"
    CLOUDFLARE_DEPLOY_STATUS=completed
    break
  fi

  if [ "$CF_STATUS" = "completed" ] && [ "$CF_CONCLUSION" != "success" ]; then
    CLOUDFLARE_DEPLOY_STATUS=failed
    echo "Cloudflare production deploy failed for $MERGE_SHA: $CF_URL"
    break
  fi

  sleep 20
done

if [ "${CLOUDFLARE_DEPLOY_STATUS:-}" != "completed" ]; then
  CUSTOMER_UPDATE_STATUS=blocked_deploy
  CLEANUP_BLOCKED="/tmp/cleanup-pr-$PR_NUMBER-blocked.md"
  cat > "$CLEANUP_BLOCKED" <<EOF
:warning: *Cleanup PR #$PR_NUMBER blocked before customer shipping update*
customer_update_status=blocked_deploy
cloudflare_deploy_status=${CF_STATUS:-missing}
cloudflare_deploy_conclusion=${CF_CONCLUSION:-missing}
cloudflare_deploy_url=${CF_URL:-none}
EOF
  ~/.claude/scripts/pm-transition-alert.sh --event cleanup-blocked --pr "$PR_NUMBER" --issue "$ISSUE" --state blocked_deploy --reason blocked_deploy --message-file "$CLEANUP_BLOCKED"
  exit 1
fi
```

### Step 6.6: Notify the bug originator ASAP (MANDATORY — Rajiv directive 2026-04-30 11:01; resequenced 2026-06-17)

Run this step immediately after Step 6.5 succeeds, before artifact cleanup, process sweeps, Codex retrospective, or any other deep housekeeping. Customers who reported a bug should hear that it shipped as soon as the production deploy and required prod migrations are done.

Initialize an explicit status:

```bash
CUSTOMER_UPDATE_STATUS=not_customer_report
CUSTOMER_UPDATE_DETAIL=""
```

If the closed issue was triggered by a customer report (Slack thread, support email, in-app feedback), post a merge update to the originating thread per the `external-comms-investigation` protocol.

**Procedure:**

1. Detect the originator. Sources, in priority order:
   - Issue body has a `Source:` or `Reporter:` line referencing a Slack thread URL, email, or customer name.
   - Issue body has a Slack permalink (`https://*.slack.com/archives/<CHAN>/p<TS>`).
   - Issue title matches a known customer Slack thread captured in `/tmp/heydonna-feedback-channel.log` or `/tmp/heydonna-pm-channel.log`.
   - Linked PR comments mention a customer by handle.
2. Resolve the Slack target:
   ```bash
   THREAD_URL=$(gh issue view $ISSUE --json body --jq '.body' \
     | grep -oE 'https://[^.]+\.slack\.com/archives/[A-Z0-9]+/p[0-9]+' | head -1)
   if [ -n "$THREAD_URL" ]; then
     CHANNEL=$(echo "$THREAD_URL" | grep -oE 'archives/[A-Z0-9]+' | cut -d/ -f2)
     RAW_TS=$(echo "$THREAD_URL" | grep -oE 'p[0-9]+' | tr -d 'p')
     THREAD_TS="${RAW_TS:0:10}.${RAW_TS:10}"
   fi
   ```
3. If the issue looks customer-reported but no Slack target can be resolved, do **not** skip silently:
   ```bash
   ISSUE_BODY=$(gh issue view $ISSUE --json body --jq '.body // ""')
   ISSUE_TITLE=$(gh issue view $ISSUE --json title --jq '.title // ""')
  if echo "$ISSUE_BODY $ISSUE_TITLE" | grep -qiE 'customer|reporter|reported by|support|feedback|abi|abilaasha|nick|user'; then
    CUSTOMER_UPDATE_STATUS=blocked_missing_origin
    CUSTOMER_UPDATE_DETAIL="Issue appears customer-reported but no Slack origin thread was resolvable."
    CLEANUP_BLOCKED="/tmp/cleanup-pr-$PR_NUMBER-blocked.md"
    cat > "$CLEANUP_BLOCKED" <<EOF
:warning: *Customer shipping update blocked for PR #$PR_NUMBER / issue #$ISSUE*
customer_update_status=blocked_missing_origin
customer_update_detail=$CUSTOMER_UPDATE_DETAIL

Please add the source Slack permalink or confirm this is not customer-reported.
EOF
    ~/.claude/scripts/pm-transition-alert.sh --event cleanup-blocked --pr "$PR_NUMBER" --issue "$ISSUE" --state blocked_missing_origin --reason blocked_missing_origin --message-file "$CLEANUP_BLOCKED"
  fi
   ```
4. Decide whether there is a customer thread to update:
   - If `CHANNEL` and `THREAD_TS` are both set, continue to external-comms verification and posting.
   - If `CUSTOMER_UPDATE_STATUS=blocked_missing_origin`, do not post a customer update; the internal blocker post from step 3 is the required output.
   - If no customer signal was found, keep `CUSTOMER_UPDATE_STATUS=not_customer_report`, set `CUSTOMER_UPDATE_DETAIL=none`, and do not post a customer update.
5. Apply the external-comms protocol BEFORE posting:
   - Read the originating message in full context using Slack search/read tools.
   - Verify the diagnosis matches the merged PR. If the PR title says "fix scroll jump" but the customer's complaint was actually about a different symptom, set `CUSTOMER_UPDATE_STATUS=blocked_diagnosis_mismatch`, post the mismatch to #heydonna-dev, and do not auto-claim victory.
6. Post the merge update only when `CHANNEL` and `THREAD_TS` are set and diagnosis verification passed:
   ```bash
   cat > /tmp/cleanup-pr-customer-msg.txt <<EOF
   Hi <CUSTOMER>, the fix for <one-line summary> shipped today (PR #$PR_NUMBER).
   <one-line technical summary that an end-user can understand>.
   It's now live on app.heydonna.chat — please give it a try and let us know
   if you see any remaining issues. Thanks for the detailed report.
   EOF
   source /Users/rajiv/Downloads/projects/heydonna-app/.env.local 2>/dev/null && \
     bash ~/.claude/skills/slack-message/scripts/slack-send.sh \
       -c "$CHANNEL" -t "$THREAD_TS" -f < /tmp/cleanup-pr-customer-msg.txt
   ```
7. Verify the response was `OK ts=...`. If verified, set:
   ```bash
   CUSTOMER_UPDATE_STATUS=posted
   CUSTOMER_UPDATE_DETAIL="$CHANNEL/$THREAD_TS"
   ```

Sign-off rules:
- Slack customer updates carry NO sign-off. Never sign as "Rajiv" or PM personal names. (Email drafts keep the "HeyDonna Team" signature.)
- No technical jargon (no commit hashes, no internal file paths).
- Production URL is `app.heydonna.chat` (NOT `app.heydonna.com`).
- Never use `skip silently` for customer-reported issues. Final cleanup tracker must include `customer_update_status=<status>` where status is one of `posted`, `not_customer_report`, `blocked_missing_origin`, `blocked_deploy`, `blocked_migration`, `blocked_diagnosis_mismatch`, or `blocked_missing_status`.

### Step 6.7: Verify post-merge customer recovery before issue close

This step is mandatory when Step 3 set `RECOVERY_REQUIRED=1`. It exists for
customer-impacting pipeline fixes where the merged PR only repairs future
behavior and PM still must recover a specific stuck/failed customer artifact.

1. Extract the exact recovery instruction from the issue body or PR body. Common
   example:
   ```text
   adminRetryAutoProcess <transcriptId or project/file id>
   ```
2. Run the recovery only after Step 6 production deploy and any required
   migration completed.
3. Verify the artifact reached a healthy terminal state using the production
   control point named by the issue: Convex row, admin retry output, pipeline
   status, alert thread, or customer artifact inspection. A retry command exit
   code alone is not recovery proof.
4. Add a GitHub issue comment with an explicit proof block:
   ```text
   RECOVERY_PROOF
   recovered_at: <UTC timestamp>
   recovery_command: <redacted command or admin mutation>
   transcript_or_artifact: <id>
   terminal_state: <healthy state observed>
   evidence: <Convex/admin output/Slack ts/run id>
   ```
5. Only after that proof exists, close the issue and apply `status:done`.

If recovery cannot be completed, set:

```bash
RECOVERY_STATUS=blocked
CUSTOMER_UPDATE_STATUS=blocked_recovery
```

Post a blocker to `#heydonna-dev` with the issue, artifact id, and next owner.
Do not flip the PR to `pm-state:closed-clean` while recovery is pending or
blocked. The final cleanup tracker must include
`customer_recovery_status=<not_required|completed|blocked>`.

### Step 7: Pre-push concurrency check (PM safety)

Before pushing anything to main within 15 min of a merge, check for in-flight runs:

```bash
gh run list --branch main --status in_progress \
  --json workflowName --jq '.[] | select(.workflowName | IN("CI", "E2E Smoke Tests"))'
```

If any are in_progress, either (a) wait for completion, or (b) post a preemptive note to
#heydonna-dev explaining the expected cancelled-CI alert noise.

(Rajiv directive 2026-04-20: avoid alert noise from concurrency cancellation. See
`memory/feedback_main_push_concurrency_cancel.md`.)

### Step 8: Bulk-delete expired GitHub Actions artifacts

Expired artifacts count toward the repo's GitHub Actions storage quota until explicitly deleted. When quota fills up, `upload-artifact` steps silently fail on subsequent runs — the step log shows "Artifact storage quota has been hit" but GitHub's UI doesn't surface this prominently, leaving E2E failures undebuggable (no DOCX to inspect, no screenshots).

Rajiv directive 2026-04-20: clean up expired artifacts in every `/cleanup-pr` so quota stays healthy.

```bash
# Count + size of expired artifacts
EXPIRED_COUNT=$(gh api /repos/heydonna-app/heydonna-app/actions/artifacts --paginate --jq '[.artifacts[] | select(.expired == true)] | length')
EXPIRED_SIZE_GB=$(gh api /repos/heydonna-app/heydonna-app/actions/artifacts --paginate --jq '[.artifacts[] | select(.expired == true) | .size_in_bytes] | add / 1e9 | . * 100 | floor / 100')

echo "Expired artifacts: $EXPIRED_COUNT ($EXPIRED_SIZE_GB GB) — deleting..."

# Bulk-delete expired
gh api /repos/heydonna-app/heydonna-app/actions/artifacts --paginate --jq '.artifacts[] | select(.expired == true) | .id' \
  | while read id; do
      gh api -X DELETE "/repos/heydonna-app/heydonna-app/actions/artifacts/$id" 2>&1 | head -1
    done

echo "Done. Quota recalculates every 6-12h."
```

If `EXPIRED_COUNT == 0` → skip silently (no-op). Idempotent.

### Step 8.5: Sweep orphaned dev processes (MANDATORY — Rajiv directive 2026-04-30 16:45, extended 2026-05-05 12:29 IST)

Slots spawn `npx convex dev --once` and `npx tsc --noEmit` as short-lived foreground or background commands during their workflow. Slots also spawn `qa-tester` subagents which launch `agent-browser` → headless Chrome instances with persistent profiles at `~/.agent-browser/profiles/<slot-name>`. When a slot crashes mid-run, autocompacts, context-switches branches, or the qa-tester subagent exits without tearing down its browser, these processes can orphan and accumulate. Without periodic sweeps, they pile up across the day and silently consume CPU.

**Incident 2026-04-30 14:32:** Rajiv flagged "CPU is spiking". Investigation found 18 orphaned `convex dev` procs across all 4 slot clones, etime ranging from 30 minutes to 3 days, plus 2 hung `tsc --noEmit` processes from prior pre-push hooks. Killing all 18 dropped top-CPU readings from 20-72% per process to background levels.

**Incident 2026-04-30 12:25 IST:** Rajiv flagged CPU spiking again. Investigation found ~30 stale agent-browser headless Chrome instances across slots 1/3/4 (admin-slot1 37min, admin-slot3 5h22m, admin-slot4 31min) plus a 7h orphan. Total ~80% CPU just from leaked Chrome instances. Subagents (qa-tester) launch `agent-browser` → headless Chrome via `--user-data-dir=/Users/rajiv/.agent-browser/profiles/<slot-name>` but don't tear down on subagent exit. Manual sweep killed 14 processes and dropped CPU to baseline. This step now sweeps agent-browser Chromes alongside the original convex/tsc sweep.

**Procedure:**

```bash
# (a) Kill orphaned convex dev / tsc procs with etime > 30 minutes.
# Slots spawn `--once` runs which complete in seconds; anything older is orphaned.
ps -A -o pid,etime,command | grep -E "(convex dev|tsc --noEmit)" | grep -v grep | awk '
{
  pid = $1
  etime = $2
  # parse etime: D-HH:MM:SS or HH:MM:SS or MM:SS
  if (etime ~ /-/) { print pid }                                          # has days separator → very old
  else if (split(etime, p, ":") == 3) { print pid }                       # HH:MM:SS = at least 1 hour
  else if (split(etime, p, ":") == 2 && p[1] >= 30) { print pid }         # MM:SS where MM >= 30
}' | xargs -r kill -9 2>/dev/null

# Report what was killed (the awk above already filtered, this re-runs to count)
KILLED=$(ps -A -o pid,etime,command | grep -E "(convex dev|tsc --noEmit)" | grep -v grep | awk '
  $2 ~ /-/ || (split($2, p, ":") == 3) || (split($2, p, ":") == 2 && p[1] >= 30) { c++ }
  END { print c+0 }')
echo "Orphan sweep: $KILLED stale convex/tsc processes killed"

# (b) Kill stale agent-browser headless Chrome instances with etime > 30 minutes.
# qa-tester subagents launch agent-browser → Chrome with persistent profile at
# ~/.agent-browser/profiles/<slot-name>, but don't tear down on subagent exit.
# 30-min threshold preserves any actively running qa-tester (Chrome will be < 30 min old).
# If a qa-tester ran > 30 min on a single PR, that's already a wedge symptom — killing is safe.
CHROME_KILL_LIST=$(ps -A -o pid,etime,command | grep "agent-browser" | grep "user-data-dir=/Users/rajiv/.agent-browser/profiles/" | grep -v grep | awk '
{
  pid = $1
  etime = $2
  if (etime ~ /-/) { print pid }                                          # has days separator
  else if (split(etime, p, ":") == 3) { print pid }                       # HH:MM:SS = at least 1 hour
  else if (split(etime, p, ":") == 2 && p[1] >= 30) { print pid }         # MM:SS where MM >= 30
}')

CHROME_KILLED=0
if [ -n "$CHROME_KILL_LIST" ]; then
  echo "$CHROME_KILL_LIST" | xargs kill -9 2>/dev/null
  CHROME_KILLED=$(echo "$CHROME_KILL_LIST" | wc -l | tr -d ' ')
fi
echo "Chrome sweep: $CHROME_KILLED stale agent-browser processes killed"
```

If both counts are 0 → skip silently (no-op). Idempotent and safe — slots only spawn `--once` runs which complete in seconds, and any active qa-tester Chrome will be under 30 minutes old. Anything older is genuinely abandoned or wedged.

(See `~/.claude/projects/.../memory/feedback_pm_run_cleanup_pr_skill_on_every_merge.md` for the broader cleanup-pr discipline. Specific incidents: 2026-04-30 14:32 CPU spike investigation, 2026-05-05 12:25 IST agent-browser Chrome leak. See `feedback_agent_browser_chrome_leak_cleanup.md` for the agent-browser-specific fix.)

### Step 9: Update PM ops ledger and render todo.md (MANDATORY)

First promote any dependent issues that were blocked on this PR, then update PM
ops and render `pm-todo.md`:

- Run the deterministic unblock sweep:
  ```bash
  python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/cleanup-pr-unblock-dependencies.py --pr "$PR_NUMBER" --issue "$ISSUE"
  ```
- The sweep scans open issues whose Ready Pool `blockers` field or explicit
  `Blocked on` / `Blocked by` / `Depends on` / `Unblock condition` body line
  references either PR `#$PR_NUMBER` or the linked issue `#$ISSUE`.
- For each candidate:
  - if any other blocker is still open, leave it parked/blocked and report it
    as skipped;
  - if no blockers remain, ensure Ready Pool frontmatter is present with
    `blockers: null`, remove stale `status:*` parking labels, add
    `status:todo`, and upsert a PM ops `ready_pool` obligation;
  - if no blockers remain but Ready Pool says `claimable_slot_type: none`,
    `pm-direct`, `rajiv`, `external`, or another non-claimable value, do not
    add `status:todo`; leave the issue parked/backlog and upsert a PM ops
    routing obligation so PM decides ready-pool routing, PM-direct handling, or
    Rajiv/product escalation;
  - never promote a dependent issue to plain `status:todo` without Ready Pool
    metadata.

Then update PM ops, then render `pm-todo.md`:
- Resolve the `cleanup_pr` obligation for this PR only after `pm-state:closed-clean` is live and, if `RECOVERY_REQUIRED=1`, `RECOVERY_STATUS=completed`
- Verify the unblock sweep output and include promoted/skipped counts in the
  final cleanup tracker reply
- Record any migrations run and their final counts as event payload
- Record any artifact cleanup count if > 0

```bash
python3 /Users/rajiv/.claude/scripts/pm-ops.py sync --write --no-live --reason cleanup-pr
```

`pm-todo.md` is the rendered view; do not clear cleanup debt with a manual Markdown edit.

### Step 9.5: Buddhi-update classifier (MANDATORY — Rajiv directive 2026-04-30 11:01)

When `cleanup-pr` runs, scan for any new `feedback_*.md` memos written under
`~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/`
since the PR's branch base. Each new memo gets classified and mirrored into
the layer that the right audience can actually read.

**Why this exists:** PM private memory is invisible to dev slots. Memos like
`feedback_xml_regex_tempered_greedy_for_element_scoping` (born from Ashwini's
PR #3892 cover-squeeze regression) help every slot writing element-scoped
regex, but if the lesson lives only in PM memory, slots rediscover the same
bug. Three layers:

| Layer | Path | Visible to |
|-------|------|------------|
| PM-only | `~/.claude/projects/.../memory/feedback_*.md` | PM only |
| Dev-relevant | `.claude/rules/21-lessons.md` (file-synced) | All 4 dev slots + PM on session start |
| Domain-specific | `~/.claude/skills/<slug>/SKILL.md` (file-synced) | Whoever invokes the skill |

> **Propagation note (2026-06-27 — cleanup-pr #5809 finding):** in THIS repo `.claude/` is **gitignored** (`.gitignore:95`; zero `.claude/` files are git-tracked). Do **NOT** `git commit + push` the 21-lessons / SKILL edits — a commit is rejected by `.gitignore` and the attempt can trip the pre-push `tsc` hook on unrelated pre-existing untracked files, generating a false "docs commit incoming / cancelled-CI" heads-up that must then be retracted. The edit propagates via a **real-time file-sync** that mirrors `.claude/` to all 6 slot clones automatically. Just save the edit (no git step); optionally verify with `md5` parity across the PM clone + `heydonna-app-300{1..6}`.

**Procedure:**

```bash
# 1. List memos created/modified during this PR's lifetime.
PR_BASE_DATE=$(gh pr view $PR_NUMBER --json createdAt --jq '.createdAt')
NEW_MEMOS=$(find ~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory \
  -name "feedback_*.md" -newer <(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$PR_BASE_DATE" +%Y%m%d%H%M.%S))
```

For each new memo:

1. Read its YAML frontmatter and body.
2. Classify:
   - **PM-only** — PM workflow, slot management, customer comms protocol,
     Codex orchestration, infra repair playbooks. Stays in PM memory.
   - **Dev-relevant** — coding patterns, debugging heuristics, tool gotchas,
     test discipline, bug-fix protocols. Mirror to `.claude/rules/21-lessons.md`
     as a one-line bullet under the appropriate section (Patterns / Testing
     Discipline / Modal & Deployment / Slot Operations / Communication / etc.).
   - **Domain-specific** — a reusable code-pattern or recipe (e.g., regex,
     Convex, Modal, OOXML, ProseMirror). If a `~/.claude/skills/<slug>/SKILL.md`
     already exists for this pattern, append a "See also" link pointing at the
     memo. Otherwise, suggest in the cleanup-pr post that a new skill should
     be extracted via `claudeception`.
3. Write the mirror entry. For `21-lessons.md`, use the existing format:
   ```markdown
   - [HIGH|MEDIUM|LOW] **<Short rule title>** (chitta YYYY-MM-DD or Rajiv directive YYYY-MM-DD) — <one-sentence rule>. <one-line why or evidence>. (See `~/.claude/projects/.../memory/feedback_<slug>.md`.)
   ```
4. Add the classification to the cleanup-pr Slack post:
   ```
   *Buddhi updates from PR #PPPP:*
   • feedback_<slug> — *dev-relevant* — mirrored to .claude/rules/21-lessons.md
   • feedback_<slug> — *PM-only* — kept in PM memory
   • feedback_<slug> — *domain-specific* — recommend new skill `<slug>`
   ```

If 0 new memos → skip silently.

### Step 9.54: PR delivery retro (MANDATORY)

Run the deterministic delivery retro before Codex meta-analysis and before
`closed-clean`. This is separate from Step 9.55: Step 9.55 improves Codex/review
gates; Step 9.54 explains why the PR took time to move and records operational
root causes such as CI churn, wall-budget/cancel loops, capture/cache waits,
dependency waits, plan/code review round counts, review/rework loops, scope
size, and cleanup label hygiene.

Closeout SLA: after Step 4b sets `pm-state:merged-cleanup-pending` /
`pm-cleanup:needed`, cleanup-pr must either reach `pm-state:closed-clean` or
post an explicit cleanup blocker within 15 minutes. `pr-state-sweep` now emits
`PR_CLEANUP_CLOSEOUT_REQUIRED` for merged PRs older than that SLA; do not ignore
it as stale closed-PR noise.

```bash
PR_RETRO_MD="/tmp/pr-retro-$PR_NUMBER.md"
PR_RETRO_JSON="/tmp/pr-retro-$PR_NUMBER.json"

if ! python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pr-delivery-retro.py \
  --pr "$PR_NUMBER" \
  --issue "$ISSUE" \
  --out "$PR_RETRO_MD" \
  --json-out "$PR_RETRO_JSON"; then
  CLEANUP_BLOCKED="/tmp/cleanup-pr-$PR_NUMBER-blocked.md"
  cat > "$CLEANUP_BLOCKED" <<EOF
:warning: *Cleanup PR #$PR_NUMBER blocked*
reason=retro_failed
failed_command=pr-delivery-retro.py
retro_path=$PR_RETRO_MD
EOF
  ~/.claude/scripts/pm-transition-alert.sh --event cleanup-blocked --pr "$PR_NUMBER" --issue "$ISSUE" --state retro_failed --reason retro_failed --message-file "$CLEANUP_BLOCKED"
  exit 1
fi

if ! grep -q '^retro_status: complete$' "$PR_RETRO_MD"; then
  CLEANUP_BLOCKED="/tmp/cleanup-pr-$PR_NUMBER-blocked.md"
  cat > "$CLEANUP_BLOCKED" <<EOF
:warning: *Cleanup PR #$PR_NUMBER blocked*
reason=retro_missing_completion_marker
retro_path=$PR_RETRO_MD
EOF
  ~/.claude/scripts/pm-transition-alert.sh --event cleanup-blocked --pr "$PR_NUMBER" --issue "$ISSUE" --state retro_missing_completion_marker --reason retro_failed --message-file "$CLEANUP_BLOCKED"
  exit 1
fi

PR_RETRO_CLASSES=$(python3 - "$PR_RETRO_JSON" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(",".join(data.get("root_causes") or ["none"]))
PY
)
PR_RETRO_VERDICT=$(python3 - "$PR_RETRO_JSON" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data.get("verdict") or "unknown")
PY
)
PR_RETRO_REVIEW_ROUNDS=$(python3 - "$PR_RETRO_JSON" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
rounds = data.get("review_rounds") or {}
print(
    "plan={plan},code={code},qa={qa},arch={arch},total={total}".format(
        plan=rounds.get("plan_rounds", 0),
        code=rounds.get("code_rounds", 0),
        qa=rounds.get("qa_rounds", 0),
        arch=rounds.get("arch_rounds", 0),
        total=rounds.get("total_rounds", 0),
    )
)
PY
)
PR_RETRO_CI_CLASSES=$(python3 - "$PR_RETRO_JSON" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
runs = data.get("runs") or {}
classes = runs.get("bad_ci_e2e_by_class") or {}
if not classes:
    print("none")
else:
    print(",".join(f"{k}={v}" for k, v in sorted(classes.items())))
PY
)
PR_RETRO_MISSED_TRANSITIONS=$(python3 - "$PR_RETRO_JSON" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(",".join(data.get("missed_pm_transitions") or ["none"]))
PY
)
```

The final cleanup tracker MUST include:

```text
delivery_retro_status=complete
delivery_retro_path=/tmp/pr-retro-$PR_NUMBER.md
delivery_retro_verdict=<verdict from JSON>
delivery_retro_classes=<comma-separated root causes>
delivery_review_rounds=plan=<n>,code=<n>,qa=<n>,arch=<n>,total=<n>
delivery_ci_classes=<bad CI/E2E classes or none>
delivery_missed_pm_transitions=<comma-separated transitions or none>
```

If the retro says `ci_churn`, `wall_budget_or_timeout`,
`capture_or_llm_proxy_cache`, `dependency_wait`, `review_rework_loop`,
`broad_review_churn`, `review_cap_reached`, `superseded_head_ci_churn`,
`local_preflight_missing`, `large_scope`, `cleanup_label_hygiene`, or
`cleanup_closeout_miss`, or if `delivery_missed_pm_transitions` is not `none`,
PM must
convert the follow-up into either an existing control-plane issue, a newly filed
bounded issue, or a noted non-actionable one-off in the cleanup tracker. Do not
leave repeated PR-stall causes only in `/tmp`.

### Step 9.55: Codex review meta-analysis — BOT + slot-side companion (MANDATORY — Rajiv directive 2026-05-16 17:50 IST + PM enhancement 2026-05-16 18:40 IST)

For every merged PR, retrospectively analyze BOTH (a) GitHub Codex bot inline comments AND (b) slot-side `codex-review-companion.mjs` invocations from MoP PostToolUse events + `/tmp/codex-review-companion/*.md` verdict markers, to identify gaps in the pre-Codex review gates (arch/plan/code/qa review skills) and update those skills so the same class of finding is caught earlier on future PRs.

Step 9.54 already records plan/code/qa/arch round counts into
`/tmp/pr-retro-$PR_NUMBER.{md,json}`. Step 9.55 still reads the same slot-side
sources because its job is deeper gate improvement: classify the finding classes
and land/dedupe review-skill CHECK updates.

Rajiv directive 2026-05-16 17:50 IST DM thread `1778933826.170419`: *"Analyze all the comments by codex bot and update our review agents so that this is caught earlier. Use a bg agent. Update cleanup pr skill and add this step."*

PM enhancement 2026-05-16 18:40 IST (Rajiv DM "Your call" on PR #4585 NEEDS_DATA retrospective): Step 9.55 also reads MoP slot events because slot-side companion CLI runs (e.g., PR #4585's 7 iteration rounds pre-PR) are INVISIBLE to PM-side GitHub-only fetch. Closes the visibility gap for "slot iterates via companion before opening PR" class of PRs.

**Skip conditions:**
- PR with **0** bot comments AND **0** companion runs → silent skip
- PR with only **nit-class** comments (no P0/P1/P2 from either source) → log to retrospective file, no skill updates
- Pure revert / chore-only / docs-only PR → silent skip

**NOT skip-eligible:** companion-only PRs (0 bot comments + N>0 companion runs) — these MUST run full analysis. PR #4585 is the canonical trigger: 7 companion rounds, 0 bot comments, v1 retrospective came back NEEDS_DATA because Step 9.55 only fetched bot side.

**Procedure (Sonnet bg agent — runs in parallel with Step 9.6):**

This analysis runs inside the `cleanup-pr-runner` Sonnet cleanup agent. Do not
launch a separate Opus/general-purpose subagent for this retrospective path
unless Rajiv explicitly approves an exception.

```bash
PR_NUMBER=<arg>
PR_CREATED=$(gh pr view $PR_NUMBER --json createdAt --jq '.createdAt')
PR_HEAD_REF=$(gh pr view $PR_NUMBER --json headRefName --jq '.headRefName')
```

#### Step 9.55.a — BOT-side fetch (GitHub inline comments)

```bash
COMMENTS_RAW=/tmp/pm-codex-comments-PR-$PR_NUMBER.raw.json
COMMENTS_JSON=/tmp/pm-codex-comments-PR-$PR_NUMBER.json

gh api /repos/heydonna-app/heydonna-app/pulls/$PR_NUMBER/comments > "$COMMENTS_RAW"
python3 - "$COMMENTS_RAW" "$COMMENTS_JSON" <<'PY'
import json, re, sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
raw = src.read_text(errors="replace")
# GitHub comment bodies can carry copied terminal/control bytes. Normalize once,
# then write JSONL with escaped strings for downstream processing.
raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
try:
    comments = json.loads(raw)
except Exception:
    comments = []

with dst.open("w") as out:
    for comment in comments if isinstance(comments, list) else []:
        login = ((comment.get("user") or {}).get("login") or "")
        if not re.search("codex", login, re.I):
            continue
        rec = {
            "id": comment.get("id"),
            "path": comment.get("path"),
            "line": comment.get("line"),
            "commit_id": (comment.get("commit_id") or "")[:8],
            "created_at": comment.get("created_at"),
            "body": comment.get("body") or "",
        }
        out.write(json.dumps(rec, ensure_ascii=True) + "\n")
PY

BOT_COUNT=$(python3 - "$COMMENTS_JSON" <<'PY'
import sys
from pathlib import Path
print(sum(1 for line in Path(sys.argv[1]).read_text(errors="replace").splitlines() if line.strip()))
PY
)
echo "Bot-side: $BOT_COUNT Codex inline comments"
```

#### Step 9.55.b — SLOT-side companion read (MoP events + marker files)

Slots invoke `codex-review-companion.mjs` during Phase 2 (plan-review) and Phase 4 (code-review) BEFORE opening the PR. These runs emit MoP `PostToolUse` events and write verdict markers to `/tmp/codex-review-companion/*.md`. PM-side bot fetch sees neither.

```bash
# 1. Identify slot(s) that worked on this PR via labels.
SLOT_LABELS=$(gh pr view $PR_NUMBER --json labels --jq '.labels[].name | select(test("^slot:[1-6]$"))')
# Fallback: if labels were stripped at cleanup, resolve the issue ONLY from the
# authoritative closing-issue set. No title inference, no prose inference.
if [ -z "$SLOT_LABELS" ]; then
  # Resolve the issue ONLY from the authoritative closing-issue set. Never from
  # the PR title. (CTO ruling 2026-09-19 21:00 IST.)
  ISSUE=$(gh pr view $PR_NUMBER --json closingIssuesReferences \
    --jq '.closingIssuesReferences[0].number // empty')
  if [ -z "$ISSUE" ]; then
    echo "Step 9.55.b: no authoritative closing issue and no slot:N label — skipping companion read (no title inference)." >&2
  else
    SLOT_LABELS=$(gh issue view "$ISSUE" --json timelineItems --jq '.timelineItems[] | .label.name? // empty | select(test("^slot:[1-6]$"))' 2>/dev/null | sort -u)
  fi
fi

# 2. For each slot, query MoP PostToolUse events covering the PR window.
COMPANION_RUNS_JSON=/tmp/pm-codex-companion-runs-PR-$PR_NUMBER.json
echo "[]" > "$COMPANION_RUNS_JSON"

for SLOT_LABEL in $SLOT_LABELS; do
  N="${SLOT_LABEL#slot:}"
  MOP_EVENTS_JSON=/tmp/pm-codex-companion-events-PR-$PR_NUMBER-slot-$N.json
  curl -s "http://127.0.0.1:3100/events?slot=$N&limit=500&type=PostToolUse" > "$MOP_EVENTS_JSON" || printf '{"events":[]}' > "$MOP_EVENTS_JSON"
  python3 - "$MOP_EVENTS_JSON" "$PR_NUMBER" "$N" "$PR_CREATED" <<'PY' >> "$COMPANION_RUNS_JSON.raw"
import json, re, sys
from pathlib import Path

path, pr, slot, created = sys.argv[1:5]
raw = Path(path).read_text(errors="replace")
raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
try:
    data = json.loads(raw)
except Exception:
    data = {"events": []}

for event in data.get("events") or []:
    if not isinstance(event, dict):
        continue
    if (event.get("timestamp") or "") < created:
        continue
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload_raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", payload)
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    tool_input = payload.get("tool_input") or {}
    skill_name = ""
    if isinstance(tool_input, dict):
        skill_name = str(tool_input.get("skill") or "")
    else:
        tool_input = {"value": tool_input}
    tool = str(event.get("tool_name") or payload.get("tool_name") or "")
    tool_input_raw = json.dumps(tool_input, ensure_ascii=True, default=str).lower()
    matches_tool = (
        (tool == "Bash" and "codex-review-companion" in tool_input_raw)
        or (tool == "Skill" and re.search(r"codex.*review|zen.*review", skill_name))
        or (tool in {"Agent", "Task"} and re.search(r"codex|plan review|code review|qa review|verdict", tool_input_raw))
    )
    if not matches_tool or pr not in tool_input_raw:
        continue
    rec = {
        "timestamp": event.get("timestamp"),
        "slot": slot,
        "mop_event_id": event.get("id"),
        "tool": tool,
        "tool_input": tool_input,
    }
    print(json.dumps(rec, ensure_ascii=True, default=str))
PY
done

# 3. Read companion verdict marker files written since PR_CREATED.
MARKER_DIR=/tmp/codex-review-companion
if [ -d "$MARKER_DIR" ]; then
  find "$MARKER_DIR" -name "*.md" -newermt "$PR_CREATED" -print0 2>/dev/null | while IFS= read -r -d '' MARKER; do
    # Filter to markers matching this PR's branch or PR number.
    if grep -qE "(PR #?$PR_NUMBER|branch.*$PR_HEAD_REF|$PR_HEAD_REF)" "$MARKER" 2>/dev/null; then
      VERDICT=$(grep -oE 'VERDICT:\s*(APPROVE|REVISE|REJECT|REQUEST_CHANGES|NEEDS_REVISION|NEEDS_DEEPER_INVESTIGATION)' "$MARKER" | head -1 | sed 's/VERDICT:\s*//')
      ROUND=$(grep -oE '(Round|R)[0-9]+' "$MARKER" | head -1)
      REVIEW_TYPE=$(grep -oE 'review-type[: =]+(plan|code|qa|arch)' "$MARKER" | head -1 | grep -oE '(plan|code|qa|arch)$')
      jq -n --arg marker "$MARKER" --arg verdict "${VERDICT:-UNKNOWN}" --arg round "${ROUND:-R?}" --arg rtype "${REVIEW_TYPE:-unknown}" --arg mtime "$(stat -f %m "$MARKER" 2>/dev/null || stat -c %Y "$MARKER")" \
        '{source: "marker", path: $marker, verdict: $verdict, round: $round, review_type: $rtype, mtime: ($mtime | tonumber)}'
    fi
  done > "$COMPANION_RUNS_JSON.markers"
fi

# 4. Compute companion-run summary: round count, verdict trajectory, review-type breakdown.
COMPANION_COUNT=$(wc -l < "$COMPANION_RUNS_JSON.raw" 2>/dev/null | tr -d ' ')
COMPANION_COUNT="${COMPANION_COUNT:-0}"
MARKER_COUNT=$(wc -l < "$COMPANION_RUNS_JSON.markers" 2>/dev/null | tr -d ' ')
MARKER_COUNT="${MARKER_COUNT:-0}"

echo "Slot-side: $COMPANION_COUNT companion JSONL invocations + $MARKER_COUNT verdict markers"
```

#### Step 9.55.c — COMBINE + classify (bot + companion findings)

Merge bot-side (9.55.a) + slot-side (9.55.b) into a single analysis stream.

**Combined skip gate:**
```bash
if [ "$BOT_COUNT" -eq 0 ] && [ "$COMPANION_COUNT" -eq 0 ] && [ "$MARKER_COUNT" -eq 0 ]; then
  echo "Step 9.55 skipped: 0 bot comments AND 0 companion runs"
  exit 0
fi
```

**Classify each finding (from either source) by severity (P0/P1/P2/nit) and recurring class:**

- `sibling-call-site` — helper changes one site but parallel sites untouched
- `error-path-cleanup` — success path cleans, error path orphans
- `wizard-state-flag-reset` — programmatic clear leaves derived flag stale
- `pre-flight-invariant` — gate checks N-1 dimensions of N required
- `whitespace/trim-parity` — frontend trims, API doesn't (or vice versa)
- `attribution-thread` — userId/createdBy/pipelineRunId dropped through call chain
- `stale-pending-state` — UI shows in-flight after backend transitioned
- `failed-status-rendering` — error-state UX gap
- `iterative-companion-cycle` — slot iterates ≥3 rounds via companion on SAME class of finding (visible only via slot-side data; PR #4585 trigger pattern)
- `other` — file under retrospective for future class extraction

**Identify which pre-Codex gate SHOULD have caught each finding.** Cross-reference against:

- `~/.claude/skills/codex-app-arch-review/SKILL.md` (Criteria list)
- `~/.claude/skills/codex-app-plan-review/SKILL.md` (CHECK list)
- `~/.claude/skills/codex-app-code-review/SKILL.md` (CHECK list)
- `~/.claude/skills/codex-app-qa-review/SKILL.md` (CHECK list)

If no existing gate would have caught it → DRAFT a new gate (Criterion or CHECK) addressing that class. Add to the appropriate skill `SKILL.md` + any companion templates. Tag each addition with `Codex meta-analysis YYYY-MM-DD PR #NNNN trigger` for traceability.

For `iterative-companion-cycle` findings: surface as a process gap rather than a content gap — the recurring class is "slot kept iterating via companion instead of escalating to PM after R3" → cross-reference `Rule 4: Codex Review Fatigue → Cap at 3 Rounds` in `buddhi-dev.md` and verify the slot's behavior matches the rule.

**Validate retrospectively:** pick 2-3 of the PR's findings (bot or companion) and assert in a comment-block at the top of the new gate that the new text would have flagged them pre-Codex.

#### Step 9.55.e — Dedupe + land CHECK into review skill (MANDATORY — Rajiv directive 2026-05-19 10:34 IST)

For each finding classified as `generalizable` in 9.55.c, the bg agent MUST land a new CHECK into the appropriate `codex-app-<type>-review` SKILL.md file (NOT just write it into the retrospective — actually append it so the next PR is gated). Per Rajiv directive 2026-05-19 10:34 IST thread `1779166624.518699`: "Step 9.55 must dedupe + land + record-why-non-generalizable. Writing to retrospective is necessary but not sufficient."

**Procedure for EACH generalizable finding:**

1. **Identify target skill file** based on finding stage (one of):
   - Arch-level (criterion-class) → `~/.claude/skills/codex-app-arch-review/SKILL.md`
   - Plan-level (pre-implementation) → `~/.claude/skills/codex-app-plan-review/SKILL.md`
   - Code-level (diff-review) → `~/.claude/skills/codex-app-code-review/SKILL.md`
   - QA-level (post-impl verification) → `~/.claude/skills/codex-app-qa-review/SKILL.md`

2. **Dedupe — grep the target file for an existing CHECK with overlapping keywords:**
   ```bash
   TARGET_SKILL=~/.claude/skills/codex-app-${STAGE}-review/SKILL.md
   KEYWORDS=$(echo "$FINDING_TITLE" | tr ' ' '\n' | grep -E '^[a-zA-Z]{4,}$' | head -3 | tr '\n' '|' | sed 's/|$//')
   if grep -qiE "$KEYWORDS" "$TARGET_SKILL"; then
     echo "DEDUPED: finding overlaps existing CHECK in $TARGET_SKILL"
     DEDUPED_COUNT=$((DEDUPED_COUNT + 1))
     # Log to retrospective Skipped (rationale) section with reason "deduped"
     continue
   fi
   ```

3. **Land — append a new numbered CHECK to the target skill** (use Edit tool — never overwrite the file). Format:
   ```markdown
   <!-- Codex meta-analysis YYYY-MM-DD PR #NNNN trigger: <finding class> -->
   ### CHECK <next-available-number>: <Rule title in imperative voice>

   <One-paragraph rule body: what to look for + why + concrete repro signal.>

   **Pattern (from PR #NNNN):** <Verbatim or summarized finding from bot/companion source.>

   **Action when matched:** <REVISE / REJECT / NEEDS_REVISION + what slot must do.>
   ```

   The HTML comment marker is REQUIRED — it gives future meta-analyses traceability when grepping for the source PR.

4. **Increment counter:**
   ```bash
   ADDED_COUNT=$((ADDED_COUNT + 1))
   SKILLS_TOUCHED["$TARGET_SKILL"]=1
   ```

**Procedure for each NON-generalizable finding (one-shot symptom, fixture-only, single-call-site):**

Add a line to the retrospective `## Skipped (rationale)` section. Do NOT touch any skill file. Format:
```
- Finding: <id-or-path>; Class: <class-from-9.55.c>; Reason: <one-line — why non-generalizable, e.g. "single call-site fixture-only" / "production-grounded but one-shot" / "deduped with existing CHECK <N>" / "iterative-companion-cycle on healthy fixture authoring (no gate needed per feedback_codex_high_round_count_healthy_iterative_fixture_authoring)">
```

#### Step 9.55.d — SAVE retrospective + Slack summary

```bash
# Save combined retrospective with both data sources
cat > /tmp/pm-codex-meta-analysis-PR-$PR_NUMBER.md <<EOF
# PR #$PR_NUMBER Codex Meta-Analysis ($(date -Iseconds))

## Data sources
- Bot-side (GitHub inline): $BOT_COUNT comments — /tmp/pm-codex-comments-PR-$PR_NUMBER.json
- Slot-side (JSONL companion runs): $COMPANION_COUNT invocations — /tmp/pm-codex-companion-runs-PR-$PR_NUMBER.json.raw
- Slot-side (verdict markers): $MARKER_COUNT markers — /tmp/pm-codex-companion-runs-PR-$PR_NUMBER.json.markers

## Findings classification
<populated by bg agent per 9.55.c classification>

## Gate updates
<list of skill SKILL.md files updated + new CHECK/Criterion numbers — populated by 9.55.e>
Total: $ADDED_COUNT new CHECKs added across ${#SKILLS_TOUCHED[@]} review skills (deduped: $DEDUPED_COUNT)

## Skipped (rationale)
<populated by 9.55.e — one line per non-generalizable finding + per deduped finding:
- Finding: <id-or-path>; Class: <class>; Reason: <one-line>
>

## Retrospective validation
<2-3 findings replayed against new gate text>
EOF

# Slack summary — post as a reply in the PR's PM transition thread, not as a
# fresh top-level channel message.
CODEX_RETRO_SLACK="/tmp/cleanup-pr-$PR_NUMBER-codex-retro-slack.md"
cat > "$CODEX_RETRO_SLACK" <<EOF
:mag: *Codex retrospective PR #$PR_NUMBER* — $BOT_COUNT bot comments + $COMPANION_COUNT companion runs ($MARKER_COUNT markers) analyzed, $ADDED_COUNT new gates added across ${#SKILLS_TOUCHED[@]} review skills (deduped: $DEDUPED_COUNT; skipped non-generalizable: $SKIPPED_NON_GEN_COUNT). Output: /tmp/pm-codex-meta-analysis-PR-$PR_NUMBER.md.
EOF
~/.claude/scripts/pm-transition-alert.sh \
  --event cleanup-tracker \
  --pr "$PR_NUMBER" \
  --issue "$ISSUE" \
  --state codex-retro \
  --message-file "$CODEX_RETRO_SLACK"
```

**Why this step:**
- Continuous improvement of pre-Codex review gates — every merged PR teaches us what slipped past
- Compounds with the Buddhi-update classifier sweep (Step 9.5 mirrors feedback memos to `21-lessons.md`)
- Closes the loop between Codex finding-classes and the review skills that should preempt them
- Closes the slot-side visibility gap: pre-PR companion iteration cycles are now captured (PR #4585 NEEDS_DATA class)

**Companion memos:**
- Foundation analysis: `/tmp/pm-codex-meta-analysis-2026-05-16.md` (cross-PR prompt additions landed 17:10 IST)
- First-step trigger: PR #4582 (5-cycle iterative pattern, 17:47 IST merge)
- Slot-side gap trigger: PR #4585 (7 companion rounds invisible to bot-only fetch — drove the 9.55.b addition 18:40 IST)

### Step 9.6: Verify shipping communication status is recorded

The customer-originator notification is handled early in Step 6.6, immediately after required prod migrations and Cloudflare production deploy completion. Do not post it here; this step only verifies the status was recorded and makes the final cleanup tracker auditable.

The final tracker reply MUST be in the PR's PM transition thread (not a
top-level `#heydonna-dev` message) and MUST include:

```text
customer_update_status=<posted|not_customer_report|blocked_missing_origin|blocked_deploy|blocked_migration|blocked_diagnosis_mismatch|blocked_missing_status>
customer_update_detail=<Slack channel/thread, blocker summary, or none>
cloudflare_deploy_status=<completed|blocked|failed|timeout>
migration_status=<not_required|completed|blocked>
delivery_retro_status=<complete|blocked>
delivery_retro_path=/tmp/pr-retro-$PR_NUMBER.md
delivery_retro_verdict=<smooth_or_low_signal|delayed_by_ci|delayed_by_dependency|delayed_by_review|scope_risk|delayed_by_cleanup>
delivery_retro_classes=<comma-separated root causes>
```

If `CUSTOMER_UPDATE_STATUS` is unset, treat that as a cleanup failure, post `customer_update_status=blocked_missing_status` to the PR's PM transition thread with `pm-transition-alert.sh --event cleanup-blocked`, and do not claim the cleanup was closed cleanly. Silent absence of the customer-update state is banned because it recreates the Abi follow-up gap.

If `delivery_retro_status` is not `complete`, do not claim `closed-clean`.
Either fix the retro collection failure or post an explicit cleanup blocker with
the failed command and PR number.

**Posting command for terminal cleanup result:**

```bash
CLEANUP_TRACKER="/tmp/cleanup-pr-$PR_NUMBER-tracker.md"
cat > "$CLEANUP_TRACKER" <<EOF
*PR #$PR_NUMBER cleanup tracker*
issue=#$ISSUE
pm_state=closed-clean
customer_update_status=$CUSTOMER_UPDATE_STATUS
customer_update_detail=${CUSTOMER_UPDATE_DETAIL:-none}
cloudflare_deploy_status=$CLOUDFLARE_DEPLOY_STATUS
migration_status=$MIGRATION_STATUS
customer_recovery_status=${RECOVERY_STATUS:-not_required}
delivery_retro_status=complete
delivery_retro_path=/tmp/pr-retro-$PR_NUMBER.md
delivery_retro_verdict=$PR_RETRO_VERDICT
delivery_retro_classes=$PR_RETRO_CLASSES
delivery_review_rounds=$PR_RETRO_REVIEW_ROUNDS
delivery_ci_classes=$PR_RETRO_CI_CLASSES
delivery_missed_pm_transitions=$PR_RETRO_MISSED_TRANSITIONS
codex_threads_resolved=${RESOLVED_COUNT:-0}
dependency_unblock_summary=${DEPENDENCY_UNBLOCK_SUMMARY:-not_run}
EOF

~/.claude/scripts/pm-transition-alert.sh \
  --event cleanup-tracker \
  --pr "$PR_NUMBER" \
  --issue "$ISSUE" \
  --state closed-clean \
  --proof "/tmp/pr-retro-$PR_NUMBER.md" \
  --message-file "$CLEANUP_TRACKER"
```

If cleanup cannot close cleanly, write `/tmp/cleanup-pr-$PR_NUMBER-blocked.md`
with the blocker and post it with:

```bash
~/.claude/scripts/pm-transition-alert.sh \
  --event cleanup-blocked \
  --pr "$PR_NUMBER" \
  --issue "$ISSUE" \
  --state blocked \
  --reason "<blocked_deploy|blocked_migration|blocked_missing_origin|blocked_missing_status|blocked_recovery|retro_failed>" \
  --message-file "/tmp/cleanup-pr-$PR_NUMBER-blocked.md"
```

### Step 10: Restore PM clone to main (MANDATORY — Rajiv directive 2026-05-10 07:35 IST)

cleanup-pr Step 5b (branch cleanup) may leave PM clone on a feature branch when
local-branch deletion is the last branch operation. Per `feedback_pm_clone_branch_leak_after_bg_agent`
(2026-05-10): Rajiv flagged "you're not on main" twice in 17min after
PM-clone bg-agents (PR amend, cleanup-pr) left working tree on feature
branches.

**Final step on every cleanup-pr bg-agent run:**

```bash
cd /Users/rajiv/Downloads/projects/heydonna-app
git checkout main 2>&1 | tail -1
git pull origin main 2>&1 | tail -2
git branch --show-current  # MUST output: main
```

If `git checkout main` fails because of uncommitted changes, surface to PM via Slack #heydonna-dev with the diff summary — do NOT force-discard. PM decides recovery.

**Why:** PM clone is the home base. Rajiv expects it on main with latest. Bg-agents that operate in PM clone (cleanup-pr, infra fix, branch ops) leave the working tree on whatever branch they last touched. PM main thread doesn't auto-revert. This step closes the leak per Rajiv directive 2026-05-10 07:35 IST: *"add switch to main in cleanup pr"*.
