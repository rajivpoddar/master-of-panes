---
name: ci-failure-investigation
description: |
  Complete one exact-tuple CI/E2E failure investigation through exactly one
  Sonnet 5 PM-owned read-only agent, then deliver the report to every exact
  originating failure-alert thread with an explicit CTO mention.
  Use when: (1) HeyDonna Alerts posts a CI failure in #heydonna-dev, (2) a PR
  shows red CI checks, or (3) E2E tests fail. PM records and deduplicates the
  terminal event, launches exactly one Sonnet 5 investigator, and waits for its bounded
  report. PM does not relay a raw alert, block a PR, assign a slot, relabel,
  rerun, capture, or merge before the report; CTO selects the next boundary.
  NOT for: (1) production pipeline alerts — see `alert-processing`,
  (2) local test failures during development — slots handle those inline.
metadata:
  author: Dhruva PM
  version: 3.1.0
  date: 2026-08-29
  last-validated: 2026-08-29
  supersedes: []
---

# CI Failure → PM Investigation Report → CTO Decision

## Rajiv Workflow Rule (2026-08-29, thread 1787978187) — SUPERSEDES direct-slot defaults

Rajiv directive: *"does the ci failure investigation skill have a cto relay?
remove that the relay. the pm should run an agent complete the investigation.
then post the report to the pr transistion thread and relay the report to the cto
instead. pm should do the investigation, cto should process it."*

This report-first contract supersedes any earlier direct-slot or raw-alert relay
instruction in this skill.

The earlier direct-slot rule is retained only as history; it is not an action
for this skill.

## What This Skill Does

On a terminal-bad required CI/E2E run, PM:

1. Binds the failed run to the PR's current head.
2. Records/deduplicates the failure through the durable reconcile producer and
   lane-aware `ci_rework` upsert (queue block below).
3. Launches exactly one PM-owned Sonnet 5 read-only investigation agent for the
   exact tuple (`ci-status-investigator`, or `customer-artifact-investigator`
   only when a customer artifact is implicated).
   The agent consumes the relevant run/job logs and Modal cache once, identifies
   the first causal boundary, distinguishes evidence consumption from actual
   E2E execution, and returns a completed report.
4. Posts the complete report to the exact originating failure-alert thread.
   The post must contain the explicit Abhijit CTO mention
   `<@U0BNFGX2UAX>`. A PR transition-thread copy may be retained for
   continuity, but it never substitutes for the originating alert thread.
   A report-delivery receipt is this skill's terminal.
   When CI and E2E alerts are simultaneous for the same PR and exact head,
   one consolidated report is posted once in each originating alert thread;
   both posts contain the same report bytes and the CTO mention.
5. CTO sends the completed report once to PR-merges task
   `01a0324b-68e0-7491-988f-e7e1549f16f7` for evidence verification only; that
   task does not re-consume the failed log and performs evidence verification
   only.
   Confirmed infrastructure/flake permits exactly one unchanged-head rerun
   after duplicate-active and eligibility checks; a verified strict-replay
   fixture miss uses canonical exact-head capture; a production-shaped repro
   returns to PM for numbered-slot assignment; otherwise CTO selects the
   smallest off-slot rescue or hold through the existing release owner. PM
   does not block, assign, relabel, rerun, capture, merge, or create another
   owner from this skill.

## Exact-tuple investigation (all PR classes)

The same sequence applies to general, CTO-rescue, and release-owner/conveyor
PRs. PM never forwards the raw alert. Exactly one Sonnet 5 investigator must
bind repository,
PR (when present), 40-character head, workflow, event, run ID, attempt, failed
job, and authoritative transition thread before reading evidence. Missing,
stale, superseded, zero-job, or head-drifted records refuse before launch.

For E2E failures that need production-shaped reproduction, the report must say
`NUMBERED_SLOT_REQUIRED` and name the exact missing spec/command/evidence; this
skill does not assign a slot. CI diagnosis and local repro may remain off-slot.


## ARGUMENTS

- `run_id` (required) — GitHub Actions run ID (numeric, e.g., `25681285673`).
- `alert_thread_ts` (required when triggered from a Slack alert) — Slack
  `thread_ts` of the originating HeyDonna Alerts message.
- `pr` (optional at invocation) — PR number; mandatory before dispatch whenever
  the run SHA is associated with a PR.
- `alert_channel` (optional, default `C0ALZJHGE49`) — Slack channel for the
  slot handoff/status reply.

## When NOT to Use

- Production pipeline alerts (stuck jobs, error spikes) — see `alert-processing`
- Local test failures during slot development — slots handle inline

## PM report contract (MANDATORY)

The completed report contains the exact tuple, one failed-log/artifact consume,
first causal boundary, evidence-versus-execution distinction, classification
(`product`, `test`, `fixture`, or `infra`), side effects, and one smallest next
action. PM posts the complete report to every required originating alert thread;
each post contains `<@U0BNFGX2UAX>`. For a same-head CI+E2E pair, the required
target set is both exact alert `(channel, thread_ts)` pairs and the report bytes
are identical. A transition-thread copy is optional continuity only. Duplicate
delivery is idempotent by the exact tuple; uncertain investigation or delivery
transport fails closed with one owner and one next wake. No pre-report
GitHub/MoP/slot/label/workflow effect is allowed.

## Required alert-thread delivery boundary

The originating alert thread is a required delivery target, not merely routing
metadata. Before launch, PM must bind one exact alert channel and
`alert_thread_ts` for a single failure, or two complete, distinct alert channel
and thread pairs for a same-PR, same-head CI+E2E pair. Missing, malformed,
ambiguous, or drifted thread identity refuses before reviewer launch. No other
failures may coalesce.

After the investigator returns, PM posts the completed report once to each
required originating alert thread and includes `<@U0BNFGX2UAX>` in every post.
The report-delivery receipt binds the repository/PR/full head, every complete
run/attempt/failed-job tuple, every required alert channel and thread, each
posted message timestamp, and `cto_mention_present=true`. An optional
canonical PR transition-thread copy is not a required target and cannot satisfy
the receipt.

If delivery is partial, PM retries only the missing required thread using the
same completed report and receipt identity. It never relaunches the investigator
or reposts a thread whose exact message receipt already satisfies the tuple.
Missing or uncertain delivery evidence remains one owner/one next wake and
fails closed; it never triggers a raw-alert relay or another reviewer.

The CTO relay is a completed-report handoff only: it contains the same report
bytes and exact tuple, is sent once to PR-merges for verification without a
second failed-log consume or additional review stage.

## Durable failure record

```bash
BODY_FILE="/tmp/slot-rework-<PR>-<RUN_ID>.md"
# <!-- CI_FAILURE_RECONCILE_INGRESS_V1 --> ci_failure_reconcile ingress producer
# Every terminal-bad required pull_request CI/E2E run gets exactly one due
# severity=high ci_failure_reconcile obligation keyed by PR + 40-char head +
# workflow + run/attempt (dedupe-group
# ci_failure_reconcile:<PR>:<HEAD>:<WORKFLOW>:<RUN_ID>:<ATTEMPT>), owner=pm,
# immediate next_review_at. Identical replay creates no duplicate row; head
# drift / closed / merged resolves or supersedes. The producer is NOT a Stop
# hook: enforcement stays on the generic pm-ops-sync-stop-validator.
bash /Users/rajiv/.claude/scripts/ci-failure-verdict-watchdog.sh >> /tmp/ci-failure-reconcile-producer.log 2>&1 || true
# <!-- CI_FAILURE_REWORK_LANE_KEY_V1 --> run-lane-aware ci_rework dedupe key
# A required/blocking run MUST NOT dedupe into the PR-level optional row:
# pm-ops obligation-upsert dedupes on (kind, target_type, target_id, pr,
# issue), so the required lane uses the run-keyed key (ci-run / <RUN_ID>)
# while the optional lane keeps the PR-level key. When the packet does not
# declare the lane, fail closed to the run-keyed high obligation.
LANE_SOURCE="/tmp/ci-verdict-<RUN_ID>.json"
[ -f "$LANE_SOURCE" ] || LANE_SOURCE="/tmp/ci-pm-action-<RUN_ID>.json"
RUN_LANE="$(python3 - "$LANE_SOURCE" <<'PYEOF'
import json
import sys
try:
    packet = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("required")
    raise SystemExit(0)
def flag(key):
    value = packet.get(key)
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value or "").lower()
required = flag("required_check_failure") == "true"
blocking = flag("blocking_for_merge") == "true"
optional = flag("blocking_for_merge") == "false"
print("optional" if (not required and not blocking and optional) else "required")
PYEOF
)"
# The failed-run packet is the immutable authority for continuation identity.
# Refuse the upsert when it has no single validated full head; do not infer a
# head from PR labels, branch text, or a later mutable PR read.
HEAD_SHA="$(python3 - "$LANE_SOURCE" <<'PYEOF'
import json
import re
import sys

try:
    packet = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(1)

values = []
for container in (packet, packet.get("run") if isinstance(packet, dict) else None):
    if not isinstance(container, dict):
        continue
    for key in ("head", "head_sha", "headRefOid", "current_head", "current_head_sha"):
        value = container.get(key)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value, re.I):
                raise SystemExit(1)
            values.append(value.lower())
if not values or len(set(values)) != 1:
    raise SystemExit(1)
print(values[0])
PYEOF
)" || {
  echo "REFUSE ci_rework obligation: validated failed-run head is missing or conflicting" >&2
  exit 1
}
if [ "$RUN_LANE" = "required" ]; then
  python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-upsert \
    --kind ci_rework \
    --severity high \
    --target-type ci-run \
    --target-id "<RUN_ID>" \
    --pr "<PR>" \
    --owner pm \
    --title "Required CI lane rework for PR #<PR> run <RUN_ID>" \
    --action "Await the PM investigation report; CTO selects the next execution boundary for run <RUN_ID>." \
    --blocker "No current live owner slot; required lane must not dedupe into the PR-level optional row" \
    --evidence "run=<RUN_ID>" \
    --evidence "body_file=${BODY_FILE}" \
    --evidence "classification=<CLASSIFICATION>" \
    --evidence "pr_comment_url=<PR_COMMENT_URL>" \
    --evidence "head_sha=${HEAD_SHA}" \
    --evidence "lane=required" \
    --print-id \
    > /tmp/ci-rework-obligation-<RUN_ID>.txt
else
  python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-upsert \
    --kind ci_rework \
    --severity high \
    --target-type pr \
    --target-id "<PR>" \
    --pr "<PR>" \
    --owner pm \
    --title "CI rework pending for PR #<PR>" \
    --action "Await the PM investigation report; CTO selects the next execution boundary for run <RUN_ID>." \
    --blocker "No current live owner slot; prior slot label is stale or slot moved on." \
    --evidence "run=<RUN_ID>" \
    --evidence "body_file=${BODY_FILE}" \
    --evidence "classification=<CLASSIFICATION>" \
    --evidence "pr_comment_url=<PR_COMMENT_URL>" \
    --evidence "head_sha=${HEAD_SHA}" \
    --evidence "lane=optional" \
    --print-id \
    > /tmp/ci-rework-obligation-<PR>-<RUN_ID>.txt
fi

# Accepted queued rework handoff (CTO receipt only)

`ci_rework` above is a diagnostic/report obligation and remains
`PROCESS_LIMBO` to the open-PR reader. Do not rename it. Only after a real
CTO-owned next-step receipt has been accepted, create the reader-supported
`slot_rework` queue row below. Every placeholder must come from that receipt
and the same immutable run/head packet; labels, prose, and a PM-only
"accepted" message are not sufficient.

```bash
NEXT_STEP_HEAD="<same validated full head from the failed-run packet>"
NEXT_OWNER="<owner named by the accepted CTO receipt>"
NEXT_ACTION="<literal executable next action from the accepted receipt>"
NEXT_WAKE="<literal wake condition from the accepted receipt>"
SOURCE_RECEIPT="<accepted CTO next-step receipt key>"

if ! [[ "$NEXT_STEP_HEAD" =~ ^[0-9a-f]{40}$ ]] \
   || [ "$NEXT_STEP_HEAD" != "$HEAD_SHA" ] \
   || [ -z "$NEXT_OWNER" ] || [ -z "$NEXT_ACTION" ] \
   || [ -z "$NEXT_WAKE" ] || [ -z "$SOURCE_RECEIPT" ]; then
  echo "REFUSE slot_rework obligation: accepted exact-head owner/action/wake receipt is incomplete or drifted" >&2
  exit 1
fi

python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-upsert \
  --kind slot_rework \
  --severity high \
  --target-type pr \
  --target-id "<PR>" \
  --pr "<PR>" \
  --owner "$NEXT_OWNER" \
  --title "Accepted exact-head rework for PR #<PR>" \
  --action "$NEXT_ACTION" \
  --blocker "accepted CTO next-step receipt=${SOURCE_RECEIPT}" \
  --dedupe-group "slot_rework:<PR>:${NEXT_STEP_HEAD}:${SOURCE_RECEIPT}" \
  --evidence "head_sha=${NEXT_STEP_HEAD}" \
  --evidence "owner=${NEXT_OWNER}" \
  --evidence "action=${NEXT_ACTION}" \
  --evidence "wake=${NEXT_WAKE}" \
  --evidence "source_receipt=${SOURCE_RECEIPT}" \
  --print-id
```

This is one non-executing queue receipt; it does not assign a slot, start a
workflow, or resolve the generic failure row. If the current PR head or the
accepted receipt changes, stop and reconcile the row rather than rebinding it.

python3 /Users/rajiv/.claude/scripts/pm-ops.py sync --write --no-live --reason ci-rework-pending
```

## Report-delivery terminal

After the investigator returns the completed exact-tuple report, PM posts the
same report once to each required originating failure-alert thread, with the
explicit CTO mention in every post. Delivery receipt is the terminal. PM does
not invoke a slot handoff, block a PR, edit labels, rerun workflows, capture,
merge, or choose the next execution boundary.
