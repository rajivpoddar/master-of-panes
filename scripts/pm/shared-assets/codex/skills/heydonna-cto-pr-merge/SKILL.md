---
name: heydonna-cto-pr-merge
description: Perform one authorized HeyDonna final head-pinned PR merge after verifying genuine exact-head CI and E2E dual-green, no open review comments, matching visual proof, and no overlapping PR/main diff changes. Use for CTO PR merge requests, not admission, reruns, PR-red investigation, or deployment.
---

# HeyDonna CTO PR Merge

This is the dedicated Codex final-merge skill. Check one requested PR; when
all gates pass, execute one squash/delete-branch merge pinned to the checked
head. Verification without the eligible merge is not completion of a merge
request. Do not use `heydonna-open-pr-status`, its sweep, or its merge helper
as a substitute for this skill.

## Authority and scope

- Require an explicit merge request. Bind repository, PR number, authorized
  full 40-character head, source thread, existing owner and supplied dedup
  keys. If the request omits a head, bind the initially observed full head;
  never silently transfer authority to a later head.
- Default repository: `Scribie/heydonna-app` (Rajiv 2026-10-13, C0ALZJHGE49/1791888584.492549; the org moved to Scribie). Read applicable `AGENTS.md`
  and the current `/Users/rajiv/.codex/monitors/heydonna-pm-chat/WAKE_SOP.md`.
  Direct current Rajiv instructions take precedence over stale skill-name or
  release-routing references. Preserve the SOP's single-owner, no-poll,
  PM-owned PR-red, and production boundaries.
- Reconcile existing same-tuple acceptance, command/result receipts and live
  PR disposition. Already merged means report the authoritative existing
  commit/time, without another effect. An uncertain earlier merge is
  reconciliation-only, never another merge attempt.
- Default to the named PR only. An explicit direct request to process all
  dual-greens authorizes one bounded inventory and serial processing of its
  eligible non-draft heads, not an ongoing sweep or polling loop. Bind each
  initially observed full head and coalesce its existing assignment/effect.
  Apply every gate separately, refresh main and the non-overlap comparison
  between merges, preserve concrete holds, and leave red/pending/draft or
  exempt-only PRs untouched. A blocked PR does not stop unrelated eligible
  heads. Report each actual merge or literal blocker; never run admission,
  recovery, or deployment as part of the batch.
- Outside that explicit batch scope, no other-PR sweep. No retired guard,
  PM script/label gate, new reviewer/owner,
  integration/rebase/head push, CI admission/rerun/capture, tests, deployment,
  slot action or production/customer-data operation. Reuse existing evidence.

## One bounded final-action verification

Use `gh` APIs/CLI or an equivalent trusted connector. Pagination to exhaust
one evidence snapshot is allowed; progress polling, watch, sleep and retries
are not. Missing/inaccessible evidence is unknown, not a passed gate.

### 1. Exact head and reviewed scope

Read current PR state, draft flag, head, base, merge disposition and complete
changed-file set. Require open/non-draft, the authorized head and `main` base.
An actual conflict/refusal blocks; `UNKNOWN` or `UNSTABLE` metadata alone is
not proof of a conflict, product defect or failed workflow.

Consume the authoritative shared exact-head classifier from
`scripts/ci/change_scope.py` and `scripts/ci/change-scope-rules.json` or its
genuine workflow receipt. Bind its head, changed paths and rule/classifier
identity. A label, directory name or PM's scope description is not that
receipt. Do not invent another path classifier.

**Dual-green is mandatory in this skill.** Site/control-plane exemptions and
skipped workflows are not dual-green. If the requested PR instead needs an
explicitly authorized exempt lane, report that distinction without launching
paid workflows or merging under an invented dual-green claim.

Consume the existing final functionality-first PM/companion disposition and
any material correction. Verify reviewed authored bytes correspond to this
head using the review's own merge-base diff and commit/path audit. A clean
main integration does not require a repeat review; a product-changing delta
needs an existing applicable disposition. Preserve prior BLOCK/FAIL history;
a resolved reviewer patch is not a fabricated literal PASS.

### Required non-overlap gate

Bind the current remote `main` commit and its actual merge base with the
authorized PR head. Compare **two diffs from that same base**: base-to-PR
authored changes and base-to-current-main changes. Do not use a two-dot
main-to-PR diff that misattributes main's advancement to the PR.

```bash
MAIN_SHA=$(gh api "repos/$REPO/commits/main" --jq .sha) || exit 1
BASE=$(git merge-base "$AUTHORIZED_HEAD" "$MAIN_SHA") || exit 1
git diff --no-ext-diff --find-renames --unified=0 "$BASE" "$AUTHORIZED_HEAD"
git diff --no-ext-diff --find-renames --unified=0 "$BASE" "$MAIN_SHA"
```

Use committed objects only, not the dirty checkout/index. Read-only fetching
of missing exact objects is allowed; never change the branch/head. Require
no overlapping changed base-line ranges, including insertions at the same
base anchor. Track both sides of renames; shared binary changes, conflicting
path additions/deletions, or an indeterminate comparison are blocking.
Touching the same file in disjoint hunks is not itself overlap, unless Rajiv
explicitly requests the stricter any-shared-file rule. Unrelated later-main
changes or evidence age alone do not invalidate the pair.

Record the main SHA, merge base, compared paths/hunks and overlap verdict.
An overlap is `BLOCKED: MAIN_PR_DIFF_OVERLAP` even if GitHub reports CLEAN or
can auto-merge; prior green runs do not prove the overlapping combined bytes.
Return the exact file/base-line ranges to PM's existing integration owner.
Do not integrate, re-admit, rerun or waive this gate in a final-merge request.
Refresh this bounded comparison if main moves before the merge action.

### 2. Both genuine required workflows are green

Resolve the actual workflow identities for `.github/workflows/ci.yml` (`CI`)
and `.github/workflows/e2e.yml` (`E2E Smoke Tests`). Fetch exact-head
`pull_request` runs for each, for example:

```bash
gh api --paginate --slurp \
  "repos/$REPO/actions/workflows/ci.yml/runs?event=pull_request&head_sha=$AUTHORIZED_HEAD&per_page=100"
gh api --paginate --slurp \
  "repos/$REPO/actions/workflows/e2e.yml/runs?event=pull_request&head_sha=$AUTHORIZED_HEAD&per_page=100"
```

Verify workflow ID/path/name, PR/branch association, full head, event, actual
run ID, current attempt, status and conclusion. Require completed success
on both legs. Inspect the newest relevant substantive run/attempt for each;
an older success does not override a newer required red, pending or cancelled
attempt. Ignore a label-event wrapper only after its actual jobs/steps prove
it did not execute the required lane; absence of jobs is not success.

Fetch jobs for the selected attempt explicitly:

```bash
gh api --paginate --slurp \
  "repos/$REPO/actions/runs/$RUN_ID/attempts/$ATTEMPT/jobs?per_page=100"
```

Read the workflow at the authorized head to identify substantive required
jobs/steps. CI must actually execute its required TypeScript/unit-test and
Python phases; E2E must actually execute the required core/critical/QA test
lanes. Where the PR's release-critical proof depends on a named new test,
consume its existing execution/result rather than a wrapper step. Optional
checks are not promoted to requirements. Record job IDs, executed step
conclusions and credible start/completion durations; inspect focused logs
only when needed to distinguish execution from skip/placeholder success.

Not proof: `statusCheckRollup`, generic `test`/`e2e` contexts, labels, notifier
or PM prose, capture-producer success, another head's runs, skipped test jobs
or implausible success with no substantive execution. A real required red
is blocking; this skill grants no red-merge waiver or rerun authority.

### 3. No open review comments

Read **all pages** of current PR review threads with resolution state and
comments (GraphQL `pullRequest.reviewThreads`, `isResolved`, `isOutdated`,
comment author/body/commit/path and `pageInfo`). Also read paginated PR review
comments (`pulls/$PR/comments`) and issue comments (`issues/$PR/comments`),
including `chatgpt-codex-connector[bot]` findings.

Require **zero unresolved review threads**, including human threads and
outdated-but-unresolved threads; do not silently discard them by severity.
For actionable standalone findings without a resolvable thread, require an
actual fix or explicit answered disposition applicable to the current head.
No current P0/P1/P2 may remain unaddressed. Historical discussion, summaries,
approval prose and bot quota/usage notices are not open findings or approval.

Do not resolve, dismiss, answer or hide comments merely to make this gate
pass. Return the concrete thread/comment IDs and existing owning review/PM
edge if an open comment remains. Reuse actual already-resolved dispositions
without launching a new review cycle.

### 4. Visual proof matches the shipped UI

Rajiv 2026-10-13, C0ALZJHGE49/1791888584.492549: visual proof screenshots are
required on Slack. For an actual user-visible UI change, the screenshots must
be image files posted in the Slack thread for this head. Fetch those Slack
files and inspect the bytes. A PR-body path, a local `qa-artifacts` file, a
hash, or the sentence "screenshots posted" is not the gate. A file missing
from `/tmp` is not `VISUAL_PROOF_UNAVAILABLE` when the Slack thread has the
images. Bind the captured revision, reviewed UI bytes, viewport,
settings/configuration and displayed behavior to this authorized head.
Main-only integration or a test-only delta can preserve an existing Slack
screenshot when the authored UI bytes and relevant configuration are unchanged.
If the images are not on Slack, block and return that to the existing PM/UI
owner. Do not take fresh screenshots from a merge-only request.

Verify the changed controls/copy are visible and readable and the evidence
covers the actual UI change. Carry any required recorded Rajiv approval from
its real source; do not invent approval. Missing, stale, inaccessible or
failed applicable visual proof blocks. Do not take fresh screenshots or use
a browser from a merge-only request; return the missing evidence to its
existing PM/UI owner.

**SHELL_CHECK (within this visual verdict):** For an in-app page UI change,
inspect at least one applicable existing screenshot showing page context.
Compare the surrounding shell with the established applicable route/layout and
a sibling page at matching route-family, viewport and state. Bind these
references to the captured revision and exact-head authored bytes as above;
an existing reference may be reused when its applicable layout is unchanged.
For dashboard-family pages, verify the actual Donna branding/logo,
`DashboardSideBar` and `DashboardTopNav` integration, respecting responsive
collapse and visibility. Do not treat a collapsed mobile sidebar as missing.
Auth, editor and other intentionally distinct app layouts use their own
established shell, not dashboard chrome. Marketing/blog/docs/benchmark viewers
are not in-app dashboard pages.

A cropped modal/component image alone does not prove page-shell integration.
Reuse sufficient existing images; not every screenshot must repeat chrome.
If page context or the applicable sibling/layout reference is missing, return
the exact missing context to the existing PM/UI owner, without a new browser
capture or review stage. Missing or replaced required navigation/branding is
a concrete integration defect and blocks merge. Only explicit recorded approval
of an intentional different layout for this page/layout change is an exception;
a mock omitting chrome or approval of inner content is not a shell waiver.
Verify the actual approval source; do not invent it.

Record a concise `SHELL_CHECK` result and applicable reference or approved
exception inside the existing visual verdict, not a new receipt, schema,
status, label, artifact or gate. This extends the same image pass because
content matching alone missed absent app navigation/branding (#9482; Rajiv
C0ALZJHGE49/1792245657.474269, rule 43 #21). Target at most two added minutes
using one reusable sibling/layout comparison, no paid tests or browser session.
Remove this standalone wording if an equivalent canonical visual checklist
fully subsumes it; never run duplicate checks.

**Waiver scope (Rajiv 2026-10-29, C0ALZJHGE49 thread 1793275486.848389,
ts 1793295441.005939 "visual proof can be waived off if integration tests
cover the scenario"; scope confirmed ts 1793295548.599969; extended to this
skill at ts 1793295657.837009; buddhi S106).** The Slack screenshot gate above
applies ONLY to NEW or MOVED UI (placement/layout, shell) or a visual-only
change that no test can assert. For any other behavior change, the PR records
`Visual proof: WAIVED — covered by <test file::cases>`. When exact-head
integration/unit tests exercise that scenario through its real control point,
record `VISUAL_WAIVED_TESTS` with the named tests and do NOT block merge for
missing screenshots or a ProofShot video. Ernest tests it after merge. A
WAIVED line on a change that does add or move UI is not a valid waiver; the
gate above still applies.

When no user-visible UI/layout change exists, record `VISUAL_NOT_APPLICABLE`
with the actual changed-path/behavior reason. Do not require decorative
screenshots for backend, workflow or test-only work. DOCX/Word/PDF artifact
proof, where release-critical, remains product/export evidence and must not
be described as real rendered proof if only OOXML tests were executed.

### 5. No new HIGH/CRITICAL vulnerable dependency

Rajiv, DM D0AMF0XE6TS thread 1793097714.304129, ts 1793104352.768809. Run
`~/.claude/scripts/ci/pr-dependency-review.sh "$PR"` (same Mac host; it reads
the live PR head, which must equal AUTHORIZED_HEAD). Exit 1 FAIL: do not merge;
BLOCKED lists each package@version, GHSA and severity. Exit 3 UNKNOWN: note it in
the receipt, not a block. Exit 0 PASS: quote the PASS line in COMPLETE.
Gate: prevents merging a new HIGH/CRITICAL vulnerable dep (the SOC 2 blocker of
10-12); Dependabot alerts only after merge; cost ~2-25s; review 2027-01-25.

## Merge immediately when all gates pass

Confirm no same-tuple cancellation/supersession or accepted equivalent effect
appeared before mutation. Then freshly read HEAD and perform exactly one
canonical merge, from a neutral directory so local checkout cleanup is not
part of the operation:

```bash
REPO=Scribie/heydonna-app
# PR and AUTHORIZED_HEAD are bound from this request, not a later head.
HEAD=$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid) || exit 1
if [ "$HEAD" != "$AUTHORIZED_HEAD" ]; then
  printf '%s\n' 'BLOCKED: HEAD_CHANGED'
  exit 1
fi
gh pr merge "$PR" --repo "$REPO" --squash --delete-branch --match-head-commit "$HEAD"
```

No `--admin`, auto-merge queue, unpinned merge, alternate head, or blind
second command. Read actual PR merge state/head/commit/time once at the
command boundary. COMPLETE requires authoritative merged readback, not an
exit code or queued acceptance alone. A refusal is a literal blocker;
response loss or an inconclusive effect is `STOPPED-INCOMPLETE` /
`OUTCOME_UNCERTAIN`. Reconcile that exact effect without retry/re-key/fallback.

## Deliver the terminal directly to the source thread

The actual merge executor posts COMPLETE/MERGED, BLOCKED or
STOPPED-INCOMPLETE directly to the ORIGINAL merge-ask Slack channel/thread
using [heydonna-slack-postback](../heydonna-slack-postback/SKILL.md): guarded
Abhijit CTO identity (`U0BNFGX2UAX`) and exact-message readback are required.
This final-merge exception supersedes older briefs reserving the post to
Returns, including in-flight assignments; it does not replay completed outcomes
or change other Returns-owned chains. A local final or queued return to Returns
is NOT source-thread delivery. Post the actual outcome, not a queue ACK:

- Merged: PR, authorized/observed head, actual squash SHA and merge time.
- Blocked: exact failed predicate and ONE actionable PM next step.
- Uncertain: precise known/unknown merge effect and no blind retry.

Bind the destination from the original source channel and thread (source
`thread_ts`, otherwise the original ask's timestamp). If absent or ambiguous,
report `SOURCE_DESTINATION_UNKNOWN` internally; do not guess a channel/thread.
Before posting, use existing effect/delivery records and ONE bounded read of
that source thread to check for an equivalent already-delivered terminal from
the executor or Returns for this exact PR/head/outcome. Reuse its verified
receipt instead of posting twice. Preserve existing dedups and uncertain-send
state; an inconclusive prior delivery is reconciliation-only, not a resend.

Post once through the Slack skill's identity guard. If delivery fails or its
readback is uncertain, retain the actual merge outcome and report
`SOURCE_TERMINAL_DELIVERY_FAILED` or `SOURCE_TERMINAL_DELIVERY_UNCERTAIN` with
the literal error/known receipt. Never issue another merge, retry/re-key the
uncertain post, or fall back to another identity/transport. Direct source
delivery does not depend on Returns accepting or consuming the internal return.

Then return the same outcome internally with source/thread and original
effect identity, approved/observed head, classifier, both actual workflow
IDs/attempts/events/conclusions/jobs/durations, review/comment disposition,
visual proof or applicability, authored correspondence and main/merge-base
non-overlap verdict, command outcome and actual merge commit/time or literal
failed predicate. Include verified Slack channel/thread/message timestamp and
identity/readback receipt, or the typed delivery failure above. Preserve
historical failures and tested/unrun distinctions; one logical terminal has
source delivery plus the internal return, not duplicate source posts.

When the brief supplies `return_task_id` and terminal dedup, use installed
`codex-stdio-send-message` once with that exact routing; otherwise return to
the requesting user. Returns must not relay a terminal already posted by the
executor or retry an uncertain source delivery. No recipient polling or
transport fallback. PM retains PR-red correction, issue cleanup and existing
rollout/canary ownership. Merge is not deployment, migration, published UI or
customer recovery.
