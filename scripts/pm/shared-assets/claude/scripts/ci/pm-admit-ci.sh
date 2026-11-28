#!/usr/bin/env bash
# PM CI admission (Rajiv, C0ALZJHGE49 thread 1795150401.563959, ts 1795161348.814749):
# "main merge is required only on conflict or if the diff overlaps."
# The CI/E2E trigger is the exact `ci-head:<40-hex head>` label (e2e.yml/ci.yml `labeled` gate).
# Related-tests gate (Rajiv "go", DM D0AMF0XE6TS thread 1791298729.215119): vitest related + tsc on the PR head before admission.
# Usage: pm-admit-ci.sh <PR> [--no-update] [--dry-run] [--skip-related-tests "<reason>"] [--force-ci "<reason>"] [--allow-main-red "<reason>"]
# Round-breaker override (only when ADMISSION_ROUND_BREAKER trips): PM_ADMIT_CAP_OVERRIDE=<cto|rajiv>:<slack ts>@<PR>:<full 40-hex head sha> pm-admit-ci.sh <PR> ...
#   The <PR>:<sha> tuple is the PR and FULL head named in the CTO/Rajiv approval handoff, copied from it, never from the live PR. A bare cto:<slack ts> is refused (exit 64); a tuple that differs
#   from the live PR or head is refused (exit 10) before any ledger row, claim or label is written. The grant id stays cto:<slack ts> (ledger and claim file unchanged).
# Pre-review: pm-admit-ci.sh --merge-only <PR> integrates main if S166 requires it; never admits CI/E2E.
# Main merge is SEPARATE from CI admission (MoP S97; Rajiv D0AMF0XE6TS thread 1795811809.236479, ts 1795829276.231109: "merge main should be separated out from ci admit"; S173):
#  - A plain `pm-admit-ci.sh <PR>` NEVER merges main. When rule 62 says a merge is needed (conflict, file overlap, or a changed trusted CI input) it stops BEFORE any dispatch, label or PR write
#    with ONE line `NEEDS_MAIN_MERGE pr=<n> reason=<conflict|overlap|ci_input_changed> files=<n>` and exits 20. With no merge needed it admits exactly as before.
#  - The merge is the separate command `pm-admit-ci.sh --merge-only <PR>`: non-force update-branch pinned to the reviewed head, dispatches NO CI/E2E, verifies the PR's own diff is byte-identical
#    (literal `git diff --no-ext-diff` hunks, before vs after, byte-compared; no whitespace normalization), requires the merged head's parents to be EXACTLY the pinned PR head and the probed origin/main sha (a descendant of it never carries) and its tree to equal `git merge-tree --write-tree <pinned head> <probed main>` (else one `CARRY_REFUSED` line and patch_id_same=no), and prints `MERGED_MAIN pr=<n> head=<sha> pre_head=<sha> main_parent=<sha> patch_id_same=<yes|no>`, binding the receipt to the pinned PR head and the verified main parent. Re-run plain admission on the resulting head afterwards.
#  - Every other gate (12b, admission round breaker, cap override, MERGE_REF_VERIFIED) is unchanged. Exit 20 = needs main merge.
# 12b: --check-12b-only <PR> is read-only; --no-12b-gate --reason "<text>" audits an explicit override.
# Main-red gate REMOVED (Rajiv C0ALZJHGE49 thread 1793958692.281499, ts 1793960417.571149: "remove it outright. note main red flakes have to be rerun. ... the skip pr has to be immediately merged with a followup issue. for affected pr's, merge main and re-admit."). Supersedes DM ts 1791877690.725089. --allow-main-red is still accepted (no-op) for compatibility.
# Scope gate (Rajiv 2026-10-08, DM thread 1791461793.117869): site/docs-only PRs are refused (no CI/E2E); PM may override with --force-ci.
# Admission round breaker (Rajiv C0ALZJHGE49 thread 1795669948.123989, ts 1795676115.108879 "ok"): after 2 admissions on the PR,
# the next admission is refused regardless of cause (ADMISSION_ROUND_BREAKER, exit 10). See admission_cap_gate below for the decision and audited override.
# Stale test-merge ref (CTO REVISE of d644cece, C0ALZJHGE49 thread 1795543629.469469, ts 1795543730.370159; REVISE #2 ts 1795544911.112579): this script never
# changes PR state. No close/reopen (close queues e2e.yml teardown of the PR's Modal apps), no main merge, no push. ONE deadline-bounded read-only pass,
# bound to the SAME main sha that passed the overlap check, runs before any admission write; anything unreadable, late or mismatching is UNKNOWN and
# REFUSEs stale_merge_ref (exit 3). See the guard below.
# Stale-ref WAIVER (PM brief 2026-11-25; deadlock: GitHub does not recompute a lagging refs/pull/N/merge, this guard refused it, and --merge-only skips a PR with no
# conflict and no overlap, so nothing ever rebuilt the ref; rule 62, Rajiv C0ALZJHGE49 thread 1795150401.563959, ts 1795161348.814749: a later main move never
# invalidates green). A test merge whose parents are not exactly {head, overlap-checked main} is WAIVED, not refused, ONLY when the PR is not CONFLICTING and its
# changed files do not overlap the files main changed since the ref's main parent (plus the verified-parent and trusted-CI-input conditions of lagging_ref_verify).
# Receipt: MERGE_REF_RULE rule=stale_merge_ref_waived:no_overlap (also appended to pm-admit-skip.log). Any overlap still REFUSEs stale_merge_ref with the overlap list.
# CI-input deadlock fix (PM brief 2026-11-26, ladder pel-mwx1mo05-a904f753): a lagging test merge whose main parent M lacks a changed trusted CI input is REFUSED by the
# guard (exit 3, "trusted CI input changed"), yet --merge-only skipped the same PR on no_conflict_no_overlap and GitHub never rebuilds a lagging ref, so nothing could
# clear it. --merge-only now runs the guard read-only (ci_input_probe: same deadline-bounded reads, no audit write) on a no-conflict/no-overlap PR and, ONLY when its
# outcome is exactly "the changed-CI-input list is non-empty", treats that as an overlap-class reason (MERGE_REASON=ci_input_changed) and does the usual non-force
# update-branch (pinned to the pre-merge head). Every other outcome (guard passes, any other refusal, unreadable read, timeout, conflict handling) is unchanged:
# a skip with no write. Admission stays the fail-closed gate. `--merge-only <PR> --dry-run` prints the decision and exits before update-branch.
# Audit fixes (PM brief 2026-11-27; Rajiv OK C0ALZJHGE49 thread 1795754821.658339, ts 1795761962.862319 "ok"; PM audit ts 1795761629.894139; rule 62 wins over the CI-input guard):
#  1. verdict_gate: a red on an admitted head needs a ci-verdict PR comment (<!-- ci-verdict: {...} -->) bound to that head. None -> REFUSE ci_verdict_required, "classify first" (exit 11).
#     Classified flaky/infra/inherited-main/out-of-diff -> the S171 admin-ask packet is printed and nothing is re-admitted (exit 12). Logic: pm-admit-verdict.py gate.
#     Rework 2026-11-27 (CTO REVISE, thread 1795754821.658339 ts 1795766409.314279): the gate reads the latest SUBSTANTIVE pull_request run per workflow (label-wrapper gate-only runs are
#     ignored) and accepts only ci-failure-investigation producer-shape verdicts bound to head + run id + CURRENT attempt (no head-only fallback, no earlier-attempt carry).
#  2. Trusted CI inputs are only what ci.yml and e2e.yml execute for a PR run (pm-admit-ci-inputs.py): those two workflows, their reusable workflows/actions and the scripts/ci
#     entrypoints they invoke. perf.yml, deploy-*.yml and scripts/ci/tests/** no longer count, so a stale merge ref whose main delta touches none of them is WAIVED
#     (MERGE_REF_RULE stale_merge_ref_waived:no_overlap), with no main merge. LANES MUST use `--merge-only <PR>` for main integration: it applies the rule 62 overlap check and
#     prints `MAIN_MERGE_REASON: decision=<skip|merge> reason=<...>` for every outcome. Never `gh pr update-branch`, never a hand merge of main.
#  3. Round breaker: when it trips and the latest red is classified flaky/infra/inherited-main/out-of-diff, the S171 admin-ask packet is printed instead of asking for an override
#     (exit 10). One-pair override (rework 2026-11-27, REVISE #2 ts 1795769102.876159): a CTO/Rajiv override funds exactly ONE admission pair at exactly ONE head. A grant names the
#     PR and head it was first issued at and can NEVER fund a different head (automatic carry-forward to a descendant head is PAUSED: no carry at all). It stays available when a
#     preflight refuses before admission; it is claimed ATOMICALLY (O_EXCL file keyed by the grant id, see consume_cap_override) immediately before the first label mutation, so of
#     two concurrent callers exactly one issues the pair and the other refuses; the consumption is recorded in pm-admit-skip.log. Legacy `admission-cap-override=` lines never fund.
#  4. Expected-tuple binding (rework 2026-11-27, CTO REVISE #3, ts 1795771648.671389): a grant's FIRST presentation binds to the PR and FULL head named in the approval handoff, not to whatever the caller
#     presents. PM_ADMIT_CAP_OVERRIDE must carry that tuple (<cto|rajiv>:<slack ts>@<PR>:<full sha>); a bare id is refused (64), and a live PR/head that differs from the tuple is refused (10) with NOTHING
#     written (no granted/consumed ledger row, no claim file, no label), so a head that moved after approval can neither take the grant nor burn it: the same tuple admits once when the live head is the approved one.
# New exit codes: 11 ci_verdict_required (classify first), 12 admin-ask packet (S171); 10 now also carries the packet for a classified-flaky red.
# ONE paid CI/E2E round per PR (Rajiv C0ALZJHGE49 thread 1795841629.732639, ts 1795845160.422969 + ts 1795845739.605399: "yes, admin merge after testing with remote box. the qa videos
# should be posted to slack for review"; PM brief 2026-11-28). When an EARLIER admitted head already had a paid round (a substantive ci.yml/e2e.yml run whose jobs did real work, not a gate-only
# or classifier-skipped run) and this head differs, admission dispatches NOTHING: it prints `BOX_ROUND_REQUIRED pr=<n> head=<sha>` plus the box test list (round-1 failing tests, rule 62
# overlap nearest-owner tests when main was merged, typecheck, the E2E specs for the delta on remote-qa-runner with video ON and the MP4 paths in the receipt) and exits 21. The head is then
# proved on the box and merged by a head-pinned S167 admin merge. Still dispatches a fresh paid round when the PR-authored delta touches CI inputs (.github/workflows/**, non-test scripts/ci/**)
# or PM_ADMIT_PAID_ROUND_OVERRIDE=<cto|rajiv>:<slack ts>@<PR>:<full head sha> is set (logged to pm-admit-skip.log). PM_ADMIT_CAP_OVERRIDE does NOT lift this gate. Exit 21 = box round required.
set -euo pipefail
CHECK_12B_ONLY=0; MERGE_ONLY=0
case "${1:-}" in
  --check-12b-only) CHECK_12B_ONLY=1; shift ;;
  --merge-only) MERGE_ONLY=1; shift ;;
esac
PR="${1:-}"; [ -n "$PR" ] || exit 64
shift; REPO=Scribie/heydonna-app
if [ "$MERGE_ONLY" = 1 ] && { [[ ! "$PR" =~ ^[0-9]+$ ]] || { [ "$#" -ne 0 ] && { [ "$#" -ne 1 ] || [ "$1" != --dry-run ]; }; }; }; then
  echo 'Usage: pm-admit-ci.sh --merge-only <PR number> [--dry-run]'; exit 64
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAIN_REPO=/Users/rajiv/Downloads/projects/heydonna-app
# Crash = non-zero (never 0): any unexpected error/unbound var exits 70 with a REFUSE line.
trap 'rc=$?; echo "REFUSE: script_error line $LINENO rc=$rc"; exit 70' ERR
H="" H0="" HM="" HEAD="" MERGEABLE="" LJ="" L=""
STALE_PROBE_CI=0   # 1 only inside ci_input_probe (--merge-only): lagging_ref_verify reports a changed CI input and exits 4 instead of refusing
# Fail-closed GitHub reads (2026-10-27: a GraphQL HTTP 499 on #11173 yielded an EMPTY head,
# "ADMITTED #11173 at" and an empty `ci-head:` label). Every value admission needs is validated;
# one retry after a short backoff, then REFUSE: github_api_error <step>.
GH_RETRY_SLEEP="${PM_ADMIT_RETRY_SLEEP:-3}"
SHA_RE='^[0-9a-f]{40}$'
api_fail() { echo "REFUSE: github_api_error $1"; exit 7; }
gh_valid() {
  case "$1" in
    sha) printf '%s' "$2" | grep -Eq "$SHA_RE" ;;
    head_mergeable) printf '%s' "$2" | grep -Eq '^[0-9a-f]{40} (MERGEABLE|CONFLICTING|UNKNOWN)$' ;;
    labels_json) printf '%s' "$2" | jq -e '.labels | type == "array"' >/dev/null 2>&1 ;;
    ok) return 0 ;;
    *) return 1 ;;
  esac
}
# gh_get <step> <kind> <cmd...>: prints the validated output; returns 1 after two failed attempts.
gh_get() {
  local step="$1" kind="$2" out attempt; shift 2
  for attempt in 1 2; do
    if out=$("$@" 2>/dev/null) && gh_valid "$kind" "$out"; then printf '%s' "$out"; return 0; fi
    echo "WARN github_api $step: attempt $attempt failed or invalid output" >&2
    [ "$attempt" = 1 ] && sleep "$GH_RETRY_SLEEP"
  done
  return 1
}
# gh_do <step> <cmd...>: run a write; one retry; REFUSE on second failure.
gh_do() {
  local step="$1"; shift
  "$@" >/dev/null 2>&1 && return 0
  echo "WARN github_api $step: attempt 1 failed" >&2; sleep "$GH_RETRY_SLEEP"
  "$@" >/dev/null 2>&1 || api_fail "$step"
}
# Remove any malformed ci-head label (e.g. the empty `ci-head:` left by the 10-27 bug).
clean_bad_ci_head_labels() {
  local lj bad
  lj=$(gh_get pr_labels labels_json gh pr view "$PR" --repo "$REPO" --json labels) || api_fail pr_labels
  for bad in $(printf '%s' "$lj" | jq -r '.labels[].name | select(startswith("ci-head:")) | select(test("^ci-head:[0-9a-f]{40}$") | not)'); do
    echo "removing malformed label '$bad'"
    gh_do remove_bad_ci_head gh pr edit "$PR" --repo "$REPO" --remove-label "$bad"
  done
  return 0
}

# Related-tests gate is OFF by default (Rajiv 2026-10-11, DM ts 1791738750.254179: "why does pm admission have to run tsc or vitest? that's already done by the slots."). Slots run tests; CI is the gate. Opt in with --related-tests.
NO_12B_GATE=0; OVERRIDE_REASON=""
NO_UPDATE=0; DRY=0; SKIP_REASON="default-off: slots ran tests, CI is the gate (Rajiv ts 1791738750.254179)"; FORCE_CI=""; ALLOW_MAIN_RED=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-12b-gate) NO_12B_GATE=1 ;;
    --reason) OVERRIDE_REASON="${2:-}"; [ -n "$OVERRIDE_REASON" ] || exit 64; shift ;;
    --no-update) NO_UPDATE=1 ;;
    --dry-run) DRY=1 ;;
    --related-tests) SKIP_REASON="" ;;
    --skip-related-tests) SKIP_REASON="${2:-}"; [ -n "$SKIP_REASON" ] || { echo "--skip-related-tests requires a reason"; exit 64; }; shift ;;
    --force-ci) FORCE_CI="${2:-}"; [ -n "$FORCE_CI" ] || { echo "--force-ci requires a reason"; exit 64; }; shift ;;
    --allow-main-red) ALLOW_MAIN_RED="${2:-}"; [ -n "$ALLOW_MAIN_RED" ] || { echo "--allow-main-red requires a reason"; exit 64; }; shift ;;
    *) echo "unknown arg: $1"; exit 64 ;;
  esac
  shift
done

if [ "$NO_12B_GATE" = 1 ] && ! printf '%s' "$OVERRIDE_REASON" | grep -q '[^[:space:]]'; then
  echo 'REFUSE: --no-12b-gate requires --reason "<text>"'; exit 64
fi
if [ "$CHECK_12B_ONLY" = 1 ] && { [ "$NO_12B_GATE" = 1 ] || [ "$DRY" = 1 ]; }; then
  echo 'REFUSE: --check-12b-only does not allow overrides or other execution modes'; exit 64
fi

gate_12b() {
  local head="$1" payload
  if [ "$NO_12B_GATE" = 0 ]; then
    python3 -I "$SCRIPT_DIR/pm-admit-12b.py" --repo "$REPO" --pr "$PR" --head "$head"
    return $?
  fi
  echo "12B_GATE: override pr=$PR head=$head reason=$OVERRIDE_REASON"
  [ "$DRY" = 1 ] && return 0
  # Fail closed if either audit sink is unavailable. Never silently bypass auditing.
  printf '%s PR=%s head=%s no-12b-gate reason=%s\n' "$(date -u +%FT%TZ)" "$PR" "$head" "$OVERRIDE_REASON" >> "$HOME/.claude/logs/pm-admit-skip.log" || return 3
  payload=$(python3 -I -c 'import json,sys;print(json.dumps({"reason":sys.argv[1]}))' "$OVERRIDE_REASON") || return 3
  python3 -I "$SCRIPT_DIR/../pm-ops.py" record --source pm-admit-ci --event 12b_gate_override --actor PM --target-type github_pr --target-id "$PR" --pr "$PR" --head-sha "$head" --payload-json "$payload" >/dev/null || return 3
}

# Admission audit/verdict tooling (PM brief 2026-11-27; Rajiv OK C0ALZJHGE49 thread 1795754821.658339, ts 1795761962.862319 "ok", PM audit ts 1795761629.894139).
# Fix 1 verdict_gate: a head that was admitted and is red is NOT re-admitted. Its latest ci-verdict PR comment (<!-- ci-verdict: {...} -->) decides: no comment -> "classify first"
#   (exit 11); flaky/infra/inherited-main/out-of-diff -> the S171 admin-ask packet (exit 12), never a re-admission. The newest verdict comment is the way out (a PR-caused class proceeds).
# Fix 3 admission_cap_gate: when the round breaker trips and the latest red is classified flaky/infra/inherited-main/out-of-diff it prints that same packet instead of asking for an
#   override. (The earlier carry of an override to a merge-only descendant head is PAUSED, REVISE #2: a grant funds only the head it names.)
# All classification and git proof lives in pm-admit-verdict.py (read-only gh GETs and git reads); this script only maps its exit codes. No Slack, no DB write, no label write.
# One-pair override ledger (rework 2026-11-27, CTO REVISE ts 1795766409.314279). One audit line per event in pm-admit-skip.log:
#   "<ts> PR=<n> head=<sha> admission-cap-override-<kind>=<ov> [inherited-from=<sha>]"
#   granted   an explicit PM_ADMIT_CAP_OVERRIDE was applied at the cap (gate time; says nothing about use)
#   carried   LEGACY only (carry-forward is PAUSED, nothing writes it any more): a historical record never funds another head
#   consumed  written once, right after the atomic claim and BEFORE the ci-head label add, so it also covers an admission whose outcome turns uncertain; a failed write refuses (exit 7)
# Head scope: a grant is bound to the PR + head of its FIRST granted|carried record (cap_grant_binding). It can never fund a different head, so an override recorded for head X refuses at
# head Y, and a PR head that moves between the gate and the label add refuses at the claim.
# Atomic claim (REVISE #2): the check above (cap_override_spent) is only an early courtesy. The authority is the O_EXCL claim file "$OVR_CLAIMS/<ov with ':' as '_'>" (macOS has no flock;
# `set -C` opens with O_EXCL, so exactly one creator wins) taken immediately before the first label mutation. The claim file holds "pr=<n> head=<sha>". The claim is released only when
# nothing was admitted and the consumed audit line could not be written. Legacy `admission-cap-override=<ov>` lines (no kind) are not proof of use. DRY writes nothing.
OVR_LOG="$HOME/.claude/logs/pm-admit-skip.log"
OVR_CLAIMS="$HOME/.claude/logs/pm-admit-cap-claims"
OVR_RE='^[^ ]+ PR=([0-9]+) head=([0-9a-f]{40}) admission-cap-override-(granted|carried|consumed)=((cto|rajiv):[0-9]{10}\.[0-9]{6})( |$)'
CAP_OVERRIDE_USED=""   # set by admission_cap_gate when an override funds THIS admission; claimed at the first label mutation
CAP_OVERRIDE_HEAD=""   # the head that grant names (H0 at gate time); the claim refuses when the live head differs
CAP_CLAIMED=0          # 1 once this process holds the claim (the claim is taken once, before the first label mutation)
cap_claim_path() { printf '%s/%s' "$OVR_CLAIMS" "${1//:/_}"; }   # <ov> already matched ^(cto|rajiv):[0-9]{10}\.[0-9]{6}$, so the name is [a-z0-9_.] only
cap_override_spent() {  # <ov>: an atomic claim file exists for that grant id, or this PR has a consumed record for it
  local ov="$1" line
  [ ! -e "$(cap_claim_path "$ov")" ] || return 0
  [ -r "$OVR_LOG" ] || return 1
  while IFS= read -r line; do
    [[ "$line" =~ $OVR_RE ]] || continue
    if [ "${BASH_REMATCH[1]}" = "$PR" ] && [ "${BASH_REMATCH[3]}" = consumed ] && [ "${BASH_REMATCH[4]}" = "$ov" ]; then return 0; fi
  done < <(tail -n 5000 "$OVR_LOG")
  return 1
}
# <ov>: the PR + head the grant was FIRST issued at (earliest granted|carried record of that id in the ledger, any PR). Sets GRANT_PR/GRANT_HEAD; returns 1 when none is recorded.
GRANT_PR=""; GRANT_HEAD=""
cap_grant_binding() {
  local ov="$1" line
  GRANT_PR=""; GRANT_HEAD=""
  [ -r "$OVR_LOG" ] || return 1
  while IFS= read -r line; do
    [[ "$line" =~ $OVR_RE ]] || continue
    [ "${BASH_REMATCH[3]}" != consumed ] || continue
    [ "${BASH_REMATCH[4]}" = "$ov" ] || continue
    GRANT_PR="${BASH_REMATCH[1]}"; GRANT_HEAD="${BASH_REMATCH[2]}"
    return 0
  done < <(tail -n 5000 "$OVR_LOG")
  return 1
}
# Automatic carry-forward is PAUSED (REVISE #2): no grant ever moves to another head. The only thing left is a grant recorded at THIS head (PR + head both bound) that was never claimed:
# an earlier preflight refused before admission, so it still funds exactly one admission pair here. Only the NEWEST grant record of this PR is considered; when it is spent or bound
# elsewhere nothing falls through to an older one. No git read, no helper call, no ledger write.
cap_inherited_override() {
  local line ov=""
  [ -r "$OVR_LOG" ] || return 1
  while IFS= read -r line; do
    [[ "$line" =~ $OVR_RE ]] || continue
    [ "${BASH_REMATCH[1]}" = "$PR" ] || continue
    [ "${BASH_REMATCH[3]}" != consumed ] || continue
    ov="${BASH_REMATCH[4]}"
    break
  done < <(tail -n 5000 "$OVR_LOG" | awk '{l[NR]=$0} END{for(i=NR;i>=1;i--) print l[i]}')
  [ -n "$ov" ] || return 1
  if cap_override_spent "$ov"; then return 1; fi
  cap_grant_binding "$ov" || return 1
  [ "$GRANT_PR" = "$PR" ] && [ "$GRANT_HEAD" = "$H0" ] || return 1   # bound to another head: never funds this one
  CAP_OVERRIDE_USED="$ov"; CAP_OVERRIDE_HEAD="$H0"
  echo "ADMISSION_CAP_OVERRIDE: $ov pr=$PR already recorded at this head and unused (an earlier preflight refused before admission); it still funds exactly one admission pair"
  return 0
}
# Claim the funding override ONCE, immediately before the first label mutation (fail closed). Atomic: of two concurrent callers presenting the same grant exactly one creates the claim
# file; the other refuses and mutates no label. Idempotent within a process (called before the same-head re-arm and again before the add).
cap_override_head_check() {   # no mutation: the PR head moved after the gate, and a grant names its head and can never fund another one
  [ -n "$CAP_OVERRIDE_USED" ] || return 0
  [ "$CAP_OVERRIDE_HEAD" = "$HEAD" ] && return 0
  echo "REFUSE: ADMISSION_ROUND_BREAKER #$PR; override $CAP_OVERRIDE_USED names head $CAP_OVERRIDE_HEAD and cannot fund head $HEAD (a grant never funds a different head); a new CTO/Rajiv override is required; nothing admitted"
  exit 10
}
consume_cap_override() {
  [ -n "$CAP_OVERRIDE_USED" ] || return 0
  [ "$CAP_CLAIMED" = 0 ] || return 0
  local claim
  cap_override_head_check
  if [ "$DRY" = 0 ]; then
    claim=$(cap_claim_path "$CAP_OVERRIDE_USED")
    mkdir -p "$OVR_CLAIMS" 2>/dev/null || { echo 'REFUSE: admission_cap_override_audit_unavailable'; exit 7; }
    if ! ( set -C; printf 'pr=%s head=%s at=%s\n' "$PR" "$HEAD" "$(date -u +%FT%TZ)" > "$claim" ) 2>/dev/null; then
      if [ -e "$claim" ]; then   # somebody else holds the claim: the race loser, or an already spent grant
        echo "REFUSE: ADMISSION_ROUND_BREAKER #$PR; override $CAP_OVERRIDE_USED is already claimed by another admission (one override funds exactly one admission pair); a new CTO/Rajiv override is required; nothing admitted"
        exit 10
      fi
      echo 'REFUSE: admission_cap_override_audit_unavailable'; exit 7   # the claim could not be created at all (unwritable claims dir)
    fi
    if ! printf '%s PR=%s head=%s admission-cap-override-consumed=%s\n' "$(date -u +%FT%TZ)" "$PR" "$HEAD" "$CAP_OVERRIDE_USED" >> "$OVR_LOG"; then
      rm -f -- "$claim"   # exact claim file this process just created; nothing was admitted, so an audit-sink failure must not burn the approval
      echo 'REFUSE: admission_cap_override_audit_unavailable'; exit 7
    fi
  fi
  CAP_CLAIMED=1
  echo "ADMISSION_CAP_OVERRIDE_CONSUMED: $CAP_OVERRIDE_USED pr=$PR head=$HEAD; this override funded one admission pair and is spent"
  return 0
}

# Each exact ci-head label addition is one round, including re-admissions on the same head.
# Apply this before the 12b gate, only in admission mode. A CTO/Rajiv override lifts a known cap, not unreadable history.
admission_cap_gate() {
  local out rc=0 ov="${PM_ADMIT_CAP_OVERRIDE:-}" ov_pr="" ov_head="" vout vrc=0
  if [ -n "$ov" ]; then
    # <cto|rajiv>:<slack ts>@<PR>:<full 40-hex head>: the PR + head the approval names. A bare grant id is refused, so the caller cannot choose what the grant binds to.
    if [[ ! "$ov" =~ ^((cto|rajiv):[0-9]{10}\.[0-9]{6})@([0-9]+):([0-9a-f]{40})$ ]]; then
      echo 'REFUSE: PM_ADMIT_CAP_OVERRIDE must look like cto:<slack ts>@<PR>:<full 40-hex head sha> (or rajiv:<slack ts>@<PR>:<full head sha>), the PR and head named in the approval handoff, e.g. cto:1795324706.834969@14917:<40-hex sha>; a bare cto:<slack ts> is refused; nothing admitted'; exit 64
    fi
    ov="${BASH_REMATCH[1]}"; ov_pr="${BASH_REMATCH[3]}"; ov_head="${BASH_REMATCH[4]}"   # ov is now the grant id (ledger and claim key), unchanged from before
  fi
  out=$(python3 -I "$SCRIPT_DIR/pm-admit-cap.py" --repo "$REPO" --pr "$PR") || rc=$?
  [ -z "$out" ] || printf '%s\n' "$out"
  case "$rc" in
    0) return 0 ;;
    10)
      if [ -n "$ov" ]; then
        # Expected tuple (REVISE #3): the approval names the PR and full head; the live ones must be exactly those. Checked first, before anything can be recorded or claimed, so a mismatch
        # (e.g. the head moved after approval) writes NO ledger row, claim or label and does not take or burn the grant.
        if [ "$ov_pr" != "$PR" ] || [ "$ov_head" != "$H0" ]; then
          echo "REFUSE: ADMISSION_ROUND_BREAKER #$PR; override $ov was approved for PR #$ov_pr head $ov_head but the live PR is #$PR at head $H0 (a grant binds to the PR and full head named in its approval, never to what the caller presents); nothing admitted, nothing recorded; re-run with the approved tuple at that head, or a new CTO/Rajiv override for this head"
          exit 10
        fi
        if cap_override_spent "$ov"; then
          echo "REFUSE: ADMISSION_ROUND_BREAKER #$PR; override $ov was already consumed by an earlier admission (one override funds exactly one admission pair); a new CTO/Rajiv override is required; nothing admitted"
          exit 10
        fi
        # Head scope: a grant already recorded for another PR/head can never fund this head (carry-forward is paused).
        if cap_grant_binding "$ov" && { [ "$GRANT_PR" != "$PR" ] || [ "$GRANT_HEAD" != "$H0" ]; }; then
          echo "REFUSE: ADMISSION_ROUND_BREAKER #$PR; override $ov names PR #$GRANT_PR head $GRANT_HEAD and cannot fund head $H0 (a grant never funds a different head); a new CTO/Rajiv override is required; nothing admitted"
          exit 10
        fi
        if [ "$DRY" = 0 ]; then
          if ! printf '%s PR=%s head=%s admission-cap-override-granted=%s\n' "$(date -u +%FT%TZ)" "$PR" "$H0" "$ov" >> "$OVR_LOG"; then
            echo 'REFUSE: admission_cap_override_audit_unavailable'; exit 7
          fi
          # Two first-time presenters at different heads: the EARLIEST ledger record (O_APPEND order) names the head; the later one refuses.
          if cap_grant_binding "$ov" && { [ "$GRANT_PR" != "$PR" ] || [ "$GRANT_HEAD" != "$H0" ]; }; then
            echo "REFUSE: ADMISSION_ROUND_BREAKER #$PR; override $ov was first granted for PR #$GRANT_PR head $GRANT_HEAD and cannot fund head $H0 (a grant never funds a different head); a new CTO/Rajiv override is required; nothing admitted"
            exit 10
          fi
        fi
        CAP_OVERRIDE_USED="$ov"; CAP_OVERRIDE_HEAD="$H0"
        echo "ADMISSION_CAP_OVERRIDE: $ov pr=$PR; cap reached, re-admission authorized by the override for exactly one admission pair"
        return 0
      fi
      if cap_inherited_override; then return 0; fi
      vout=$(python3 -I "$SCRIPT_DIR/pm-admit-verdict.py" gate --repo "$REPO" --pr "$PR" --head "$H0" --scope latest) || vrc=$?
      echo "REFUSE: ADMISSION_ROUND_BREAKER #$PR; nothing admitted"
      if [ "$vrc" = 21 ]; then
        # The latest red is classified flaky/infra/inherited-main/out-of-diff: an admin-merge case (S171), not a re-admission. No override is requested.
        echo "The latest red on #$PR is classified flaky/infra/inherited-main/out-of-diff, so this is an admin-merge decision for the CTO, not a re-admission and not an override."
        printf '%s\n' "$vout"
        exit 10
      fi
      [ -z "$vout" ] || printf '%s\n' "$vout"   # proceed (PR-caused/unknown class), classify-first or UNAVAILABLE: say why no packet was printed
      echo "Decision required (choose one for #$PR at $H0): S157 admin-merge ask for out-of-diff/flake/infra only with executed-green E2E at this head; fix a PR-caused failure; or slice/freeze."
      echo 'Re-admission requires an audited override: PM_ADMIT_CAP_OVERRIDE=cto:<slack ts>@<PR>:<full head sha> (or rajiv:<slack ts>@<PR>:<full head sha>), the PR and head named in the approval.'
      exit 10 ;;
    *) api_fail admission_cap ;;
  esac
}

# Fix 1 (see above): admission mode only, runs after the cap gate and before the 12b gate. No bypass flag and a cap override does not skip it; a newer ci-verdict comment is the way out.
verdict_gate() {
  local out rc=0
  out=$(python3 -I "$SCRIPT_DIR/pm-admit-verdict.py" gate --repo "$REPO" --pr "$PR" --head "$H0" --scope head) || rc=$?
  [ -z "$out" ] || printf '%s\n' "$out"
  case "$rc" in
    0) return 0 ;;
    20) exit 11 ;;   # red on an admitted head with no ci-verdict comment: classify first (the REFUSE line is the helper's)
    21) exit 12 ;;   # classified flaky/infra/inherited-main/out-of-diff: the helper printed the S171 admin-ask packet; nothing is re-admitted
    11) echo 'REFUSE: github_api_error ci_verdict'; exit 7 ;;   # UNAVAILABLE (read failed/timed out/odd shape), never a guess
    *) api_fail ci_verdict ;;
  esac
}

# The open-state guard applies to both modes; the review gate is for admission only.
if [ "$MERGE_ONLY" = 0 ]; then
  H0=$(gh_get pr_head sha gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid) || api_fail pr_head
fi
PRSTATE=$(gh_get pr_state ok gh pr view "$PR" --repo "$REPO" --json state --jq .state) || api_fail pr_state
[ "$PRSTATE" = OPEN ] || { echo "REFUSE: PR #$PR is $PRSTATE, not OPEN; nothing admitted"; exit 8; }
if [ "$MERGE_ONLY" = 0 ] && [ "$CHECK_12B_ONLY" = 0 ]; then admission_cap_gate; verdict_gate; fi
if [ "$MERGE_ONLY" = 0 ]; then
  gate_12b "$H0" || exit "$?"
  [ "$CHECK_12B_ONLY" = 0 ] || exit 0
fi

related_tests_gate() {
  local head="$1" wt="/tmp/admit-$PR-${1:0:12}" out rc=0 files n
  if [ -n "$SKIP_REASON" ]; then
    echo "related-tests gate SKIPPED (PM): $SKIP_REASON"
    echo "$(date -u +%FT%TZ) PR=$PR head=$head skip-related-tests reason=$SKIP_REASON" >> "$HOME/.claude/logs/pm-admit-skip.log" 2>/dev/null || true
    return 0
  fi
  git -C "$MAIN_REPO" fetch -q origin main "pull/$PR/head" >/dev/null 2>&1 || true
  git -C "$MAIN_REPO" cat-file -e "$head^{commit}" 2>/dev/null || { echo "REFUSE: head $head not fetchable"; return 3; }
  files=$(git -C "$MAIN_REPO" diff --name-only origin/main..."$head" | grep -E '\.(ts|tsx|js)$' || true)
  if [ -z "$files" ]; then echo "related-tests gate: no TS/JS changes, skipped"; return 0; fi
  n=$(echo "$files" | wc -l | tr -d ' ')
  git -C "$MAIN_REPO" worktree remove --force "$wt" >/dev/null 2>&1 || true
  git -C "$MAIN_REPO" worktree add -q --detach "$wt" "$head"
  ln -s "$MAIN_REPO/node_modules" "$wt/node_modules"
# Gitignored build artifacts (support-chat docs snapshots) are absent in a fresh worktree.
[ -f "$wt/scripts/generate-docs-context.mjs" ] && ( cd "$wt" && node scripts/generate-docs-context.mjs >/dev/null 2>&1 ) || true
  out=$(mktemp)
  echo "related-tests gate: $n changed TS/JS files at $head"
  # shellcheck disable=SC2086
  local json="$out.json" vpid waited=0 verdict
  ( cd "$wt" && mkdir -p test-results && exec timeout 900 npx vitest related --run --passWithNoTests --testTimeout=30000 --poolOptions.forks.maxForks=2 --reporter=default --reporter=json --outputFile.json="$json" $files ) >"$out" 2>&1 &
  vpid=$!
  # vitest can hang on exit ("Vite server(s) prevent exit") after finishing; once the JSON result is complete, stop waiting.
  while kill -0 "$vpid" 2>/dev/null; do
    if [ -s "$json" ] && python3 -c 'import json,sys;json.load(open(sys.argv[1]))' "$json" 2>/dev/null; then
      waited=$((waited+1)); [ $waited -ge 10 ] && { echo "  vitest: result written but process hung on exit; killing"; pkill -TERM -P "$vpid" 2>/dev/null; kill -TERM "$vpid" 2>/dev/null; sleep 2; pkill -KILL -P "$vpid" 2>/dev/null; kill -KILL "$vpid" 2>/dev/null; break; }
    fi
    sleep 2
  done
  wait "$vpid" 2>/dev/null; rc=$?
  grep -E '^ *(Test Files|Tests) ' "$out" | sed 's/^/  vitest: /' || true
  # Decide from the parsed result, not the exit code.
  verdict=$(python3 - "$json" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print("NORESULT"); sys.exit()
bad=[]
for f in d.get("testResults",[]):
    if f.get("status")=="failed" and not f.get("assertionResults"): bad.append("SUITE "+f.get("name","?")+" "+(f.get("message") or "")[:200])
    for a in f.get("assertionResults",[]):
        if a.get("status")=="failed": bad.append(a.get("fullName","?"))
if d.get("numFailedTests",0)==0 and d.get("numFailedTestSuites",0)==0 and not bad: print("PASS")
else:
    print("FAIL"); [print("  FAIL "+b) for b in bad[:40]]
PY
)
  case "$verdict" in
    PASS) rc=0; echo "  vitest: parsed result PASS (process exit was irrelevant)";;
    NORESULT) echo "REFUSE: related tests red (no result file written; exit $rc; 124=timeout)"; grep -E '^( FAIL| +×) ' "$out" | sort -u | head -40 || true; [ $rc -eq 0 ] && rc=1;;
    *) echo "REFUSE: related tests red (failing tests)"; echo "$verdict" | tail -n +2; [ $rc -eq 0 ] && rc=1;;
  esac
  if [ $rc -ne 0 ]; then
    :
  else
    # CI also typechecks test files via tsconfig.tests.json; the root tsconfig skips them (#9275/#9276 reds 2026-10-09).
    ( cd "$wt" && timeout 900 npx tsc --noEmit && { [ ! -f tsconfig.tests.json ] || timeout 900 npx tsc --noEmit -p tsconfig.tests.json; } ) >"$out.tsc" 2>&1 || rc=$?
    if [ $rc -ne 0 ]; then
      echo "REFUSE: related tests red (tsc exit $rc)"
      grep -c 'error TS' "$out.tsc" | sed 's/^/  tsc errors: /' || true
      grep 'error TS' "$out.tsc" | head -20 || true
    else
      echo "  tsc: 0 errors"
    fi
  fi
  git -C "$MAIN_REPO" worktree remove --force "$wt" >/dev/null 2>&1 || true
  /Users/rajiv/.local/bin/trash "$out" >/dev/null 2>&1 || true
  [ -e "$out.json" ] && /Users/rajiv/.local/bin/trash "$out.json" >/dev/null 2>&1 || true
  [ -e "$out.tsc" ] && /Users/rajiv/.local/bin/trash "$out.tsc" >/dev/null 2>&1 || true
  return $rc
}

scope_gate() {
  local head="$1" res rc=0
  git -C "$MAIN_REPO" fetch -q origin main >/dev/null 2>&1 || true
  res=$(git -C "$MAIN_REPO" show origin/main:scripts/ci/change_scope.py > "${TMPDIR:-/tmp}/change_scope_$PR.py" 2>/dev/null && \
    git -C "$MAIN_REPO" show origin/main:scripts/ci/change-scope-rules.json > "${TMPDIR:-/tmp}/change_scope_rules_$PR.json" && \
    python3 "${TMPDIR:-/tmp}/change_scope_$PR.py" --rules "${TMPDIR:-/tmp}/change_scope_rules_$PR.json" --repo "$REPO" --pr "$PR" --expected-head "$head") || rc=$?
  [ -e "${TMPDIR:-/tmp}/change_scope_$PR.py" ] && /Users/rajiv/.local/bin/trash "${TMPDIR:-/tmp}/change_scope_$PR.py" "${TMPDIR:-/tmp}/change_scope_rules_$PR.json" >/dev/null 2>&1 || true
  if [ $rc -ne 0 ]; then echo "scope gate: classifier failed (rc=$rc); failing closed to normal admission"; return 0; fi
  local exempt
  exempt=$(printf '%s' "$res" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("yes" if (d["site_scoped"] or d["paid_ci_exempt"] or (not d["ci_required"] and not d["e2e_required"])) else "no", d["scope"])')
  echo "scope gate: scope=${exempt#* } exempt=${exempt%% *}"
  if [ "${exempt%% *}" = yes ]; then
    if [ -n "$FORCE_CI" ]; then
      echo "scope gate OVERRIDDEN (PM --force-ci): $FORCE_CI"
      echo "$(date -u +%FT%TZ) PR=$PR head=$head force-ci reason=$FORCE_CI" >> "$HOME/.claude/logs/pm-admit-skip.log" 2>/dev/null || true
      return 0
    fi
    echo "REFUSE: site/docs-only PR — no CI/E2E admission; send a direct merge ask"
    return 5
  fi
  return 0
}

main_red_gate() {
  local runs rid concl url rsha tip logs tests pf hit="" t b
  runs=$(gh run list --repo "$REPO" --branch main --workflow ci.yml --status completed --limit 5 --json databaseId,conclusion,url,headSha 2>/dev/null) \
    || { echo "WARN main-red gate: run lookup failed; proceeding (fail-open on tooling error)"; return 0; }
  read -r rid concl url rsha <<<"$(printf '%s' "$runs" | python3 -c 'import json,sys
try:
    r=[x for x in json.load(sys.stdin) if x.get("conclusion")!="cancelled"]
    print(r[0]["databaseId"],r[0]["conclusion"],r[0]["url"],r[0]["headSha"]) if r else print("none none none x")
except Exception: print("err err err x")' 2>/dev/null)"
  case "$concl" in
    err|none|"") echo "WARN main-red gate: no usable main ci.yml run; proceeding"; return 0;;
    failure) ;;
    *) echo "main-red gate: latest main ci.yml run $rid = $concl"; return 0;;
  esac
  logs=$(timeout 60 gh run view "$rid" --repo "$REPO" --log-failed 2>/dev/null | cut -f3- | sed -e $'s/\x1b\\[[0-9;]*m//g' -e 's/\^\[\[[0-9;]*m//g' -e 's/^[0-9T:.Z-]* //' | grep -aE '^ *(FAIL|FAILED|✘)( |$)|vitest_parent_runtime_wall|exit code 124' | head -200 || true)
  tests=$(printf '%s' "$logs" | grep -aE '^ *(FAIL|FAILED|✘)( |$)' | sed -e $'s/\x1b\\[[0-9;]*m//g' -e 's/\^\[\[[0-9;]*m//g' -e 's/^[0-9T:.Z-]* //' \
    | grep -E '^ *(FAIL|FAILED|✘)( |$)' | grep -oE '[A-Za-z0-9_./@()-]+\.(test|spec)\.(ts|tsx|js|jsx|py)' \
    | sed -e 's#.*/heydonna-app/heydonna-app/##' -e 's#^\./##' | sort -u | head -20 || true)
  if [ -z "$tests" ] && printf '%s' "$logs" | grep -qaE 'vitest_parent_runtime_wall|exit code 124'; then
    echo "WARN main-red: wall-time/capacity red (no failing tests) — proceeding (run $rid)"; return 0
  fi
  pf=$(gh pr diff "$PR" --repo "$REPO" --name-only 2>/dev/null) \
    || { echo "WARN main-red gate: PR file lookup failed; proceeding"; return 0; }
  for t in $tests; do
    b=$(basename "$t"); b="${b%%.test.*}"; b="${b%%.spec.*}"
    if printf '%s\n' "$pf" | grep -qxF "$t" || printf '%s\n' "$pf" | grep -qE "(^|/)${b}\.[A-Za-z]+$"; then hit="$hit $t"; fi
  done
  if [ -n "$hit" ]; then echo "main-red gate: main red ($rid) but PR touches failing test/subject:$hit; allowed"; return 0; fi
  if [ -n "$ALLOW_MAIN_RED" ]; then
    echo "main-red gate OVERRIDDEN (--allow-main-red): $ALLOW_MAIN_RED (main run $rid)"
    echo "$(date -u +%FT%TZ) PR=$PR main-run=$rid allow-main-red reason=$ALLOW_MAIN_RED" >> "$HOME/.claude/logs/pm-admit-skip.log" 2>/dev/null || true
    return 0
  fi
  tip=$(gh api "repos/$REPO/commits/main" --jq .sha 2>/dev/null || true)
  if [ -n "$tip" ] && [ -n "$rsha" ] && [ "$tip" != "$rsha" ]; then
    echo "WARN main-red: run $rid red at ${rsha:0:10} but main has newer commit ${tip:0:10}; a fix may be pending CI; proceeding (failing: $(echo $tests | tr '\n' ' '))"
    return 0
  fi
  echo "REFUSE main-red: $url failing: ${tests:-<could not extract test files from log>}; PR inherits — wait for main fix" | tr '\n' ' '; echo
  return 6
}

# Overlap report (retro #13357/#13435; CTO C0ALZJHGE49 ts 1794578332.315349). Advisory output only: it names the
# reviewed base, current main, paths changed by both the PR and main, and the proof to re-run. A clean file merge
# is not proof. It never changes an admission decision or exit status, and it never delays one: the report runs in
# the background while the label writes happen, and its wait is bounded by PM_ADMIT_OVERLAP_TIMEOUT seconds.
# A failure is reported with its real reason (timeout vs exit code), never swallowed.
OVERLAP_TIMEOUT="${PM_ADMIT_OVERLAP_TIMEOUT:-25}"
case "$OVERLAP_TIMEOUT" in ''|*[!0-9]*) OVERLAP_TIMEOUT=25 ;; esac
# An environment override may shorten, never unbound, the advisory wait.
if [ "${#OVERLAP_TIMEOUT}" -gt 2 ] || [ "$OVERLAP_TIMEOUT" -gt 25 ] || [ "$OVERLAP_TIMEOUT" -lt 1 ]; then OVERLAP_TIMEOUT=25; fi
OVERLAP_OUT="" OVERLAP_PID=""
overlap_cleanup() { [ -z "$OVERLAP_OUT" ] || rm -f "$OVERLAP_OUT" "$OVERLAP_OUT.rc" 2>/dev/null || true; }
trap overlap_cleanup EXIT
overlap_start() {
  OVERLAP_OUT=$(mktemp /tmp/admit-overlap.XXXXXX 2>/dev/null) || { OVERLAP_OUT=""; return 0; }
  (
    rc=0
    timeout --kill-after=5 "$OVERLAP_TIMEOUT" python3 -I "$SCRIPT_DIR/pm-admit-12b.py" --overlap-only --repo "$REPO" --pr "$PR" --head "$1" \
      >"$OVERLAP_OUT" 2>/dev/null || rc=$?
    echo "$rc" >"$OVERLAP_OUT.rc"
  ) &
  OVERLAP_PID=$!
  return 0
}
overlap_finish() {
  local rc lines
  if [ -z "$OVERLAP_OUT" ]; then echo "12B_OVERLAP: UNKNOWN reason=report_not_started"; return 0; fi
  wait "$OVERLAP_PID" 2>/dev/null || true
  rc=$(cat "$OVERLAP_OUT.rc" 2>/dev/null || echo missing)
  lines=$(cat "$OVERLAP_OUT" 2>/dev/null || true)
  if [ "$rc" = 0 ] && [ -n "$lines" ]; then
    printf '%s\n' "$lines"
  elif [ "$rc" = 124 ] || [ "$rc" = 137 ]; then
    echo "12B_OVERLAP: UNKNOWN reason=report_timeout after=${OVERLAP_TIMEOUT}s"
  else
    echo "12B_OVERLAP: UNKNOWN reason=report_failed rc=$rc"
  fi
  overlap_cleanup; OVERLAP_OUT=""
  return 0
}

read_pr_head() {
  HM=$(gh_get pr_head_mergeable head_mergeable gh pr view "$PR" --repo "$REPO" --json headRefOid,mergeable --jq '.headRefOid+" "+.mergeable') || api_fail pr_head_mergeable
  read -r HEAD MERGEABLE <<<"$HM"
}

main_merge_probe() {
  local head="$1" mergeable="$2" main base pr_files main_files overlap count=0 rc=0 reason
  git -C "$MAIN_REPO" fetch -q --no-tags origin +refs/heads/main:refs/remotes/origin/main "refs/pull/$PR/head" || return 1
  main=$(git -C "$MAIN_REPO" rev-parse --verify 'origin/main^{commit}') || return 1
  base=$(git -C "$MAIN_REPO" merge-base "$main" "$head") || return 1
  # C-quoted names are one path per line even with embedded newlines. Include both sides of renames.
  # Check the sorts directly: a failure inside process substitution is invisible to comm.
  pr_files=$(git -C "$MAIN_REPO" -c core.quotePath=true diff --no-renames --name-only "$base" "$head" -- | LC_ALL=C sort -u) || return 1
  main_files=$(git -C "$MAIN_REPO" -c core.quotePath=true diff --no-renames --name-only "$base" "$main" -- | LC_ALL=C sort -u) || return 1
  overlap=$(LC_ALL=C comm -12 <(printf '%s\n' "$pr_files") <(printf '%s\n' "$main_files")) || return 1
  if [ -n "$overlap" ]; then count=$(printf '%s\n' "$overlap" | wc -l | tr -d ' ') || return 1; fi
  if [ "$mergeable" = CONFLICTING ]; then
    rc=1
  elif [ "$base" != "$head" ] && [ "$base" != "$main" ]; then
    git -C "$MAIN_REPO" merge-tree --write-tree "$head" "$main" >/dev/null || rc=$?
  fi
  case "$rc" in
    1) reason=conflict ;;
    0) if [ "$count" -gt 0 ]; then reason=overlap; else reason=no_conflict_no_overlap; fi ;;
    *) return 1 ;;  # A failed probe is not evidence of a conflict or a clean merge.
  esac
  printf '%s %s %s\n' "$reason" "$count" "$main"
}

plan_main_merge() {
  local plan
  plan=$(main_merge_probe "$HEAD" "$MERGEABLE") || { echo 'REFUSE: main merge probe failed; retry admission'; exit 2; }
  read -r MERGE_REASON MERGE_FILES MAIN_SHA <<<"$plan"
  if [ "$MERGE_ONLY" = 0 ] && [ "$MERGE_REASON" != no_conflict_no_overlap ]; then
    # Admission never merges main (Rajiv D0AMF0XE6TS thread 1795811809.236479, ts 1795829276.231109: "merge main should be separated out from ci admit";
    # S173: a merge-only re-admit runs on a remote box, not CI/E2E). Stop before ANY dispatch, label or PR write with ONE machine-readable line and exit 20.
    # The caller runs the separate main-merge command (`pm-admit-ci.sh --merge-only <PR>`, which prints MERGED_MAIN) and re-admits the resulting head.
    echo "NEEDS_MAIN_MERGE pr=$PR reason=$MERGE_REASON files=$MERGE_FILES"
    exit 20
  fi
}

# Stale test-merge guard (2026-11-21; CTO REVISE #2, C0ALZJHGE49 thread 1795543629.469469, ts 1795544911.112579).
# GitHub can leave refs/pull/N/merge built on an old main for over an hour, so CI tests stale code (#14302/#14420/#14430/#14435/#14436 kept
# failing on a lint already fixed on main). The PR's test merge must be verified built on the main that PASSED THE OVERLAP CHECK above.
# - Fail closed: any unreadable main, PR or merge-ref read is UNKNOWN and REFUSEs here. Nothing reaches the activation label on an UNKNOWN.
# - ONE read pass, no polling and no sleeps (probes found no evidence that waiting rebuilds a ref). Every GitHub read is bounded by the REMAINING
#   deadline (PM_ADMIT_STALE_DEADLINE seconds, default 30, covering the whole pass) with timeout; a result that returns at or after the deadline
#   counts as UNKNOWN (whole-second clock, so it errs early, never late).
# - Bound to OVERLAP_MAIN_SHA, the main that plan_main_merge reviewed for overlap. If main has moved since, REFUSE: the overlap review no longer
#   covers it and a fresh admission run is needed. Never re-read "current main" and trust that instead.
# - No PR state change (a close queues e2e.yml teardown, which stops the PR's six Modal apps and can kill an in-flight E2E run), no main merge,
#   no push (rule 62/S166). The guard runs before every admission write (label create, label cleanup, priority mirror, activation label).
# Lagging-ref WAIVER (2026-11-25; Rajiv rule 62/S166, CTO C0ALZJHGE49 thread 1795567441.556059, ts 1795584167.126789): GitHub's test merge can sit on an older
# main M for minutes to hours while main keeps moving (6 admissions refused on 2026-11-25). A test merge whose parents are NOT exactly {head, overlap-checked
# main} is still ADMITTED when ALL hold, else it REFUSEs with the overlap list: (a) the PR is not CONFLICTING; (b) the PR's changed files (merge-base..head, taken
# against BOTH the overlap-checked main and M, unioned) do not overlap the files main changed since M (M..overlap-checked main); (c) one parent is exactly the
# reviewed head and the other, M, is a verified ancestor of the overlap-checked main; (d) main changed none of the trusted shared CI inputs that decide WHAT
# runs for a PR (NARROWED 2026-11-27, Rajiv ts 1795761962.862319: ONLY ci.yml and e2e.yml, the reusable workflows/actions they use, the scripts/ci entrypoints they
# invoke, the selector/classifier rules and E2E selection config beside them, and EVERY file those workflows read out of a git revision, e.g.
# scripts/ci/unit-shards/durations.json read from WORKFLOW_SHA; perf.yml, deploy-*.yml and scripts/ci/tests/** are NOT inputs) since M,
# because a changed selector means the PR's CI ran the OLD selection even when no PR file overlaps (CTO REVISE of MoP 9518bf9d, same thread, ts 1795586027.634509;
# e.g. 73499dfb..6ee67558 changed scripts/ci/change_scope.py; CTO REVISE #2 of MoP 494a207e, ts 1795587520.404159: a durations-only change must refuse too). The
# inputs are DISCOVERED by pm-admit-ci-inputs.py from the workflows' own references at M and at main (rule 60: no hand-kept list). An unreadable discovery, or a
# recognized trusted read (git show|cat-file|archive|ls-tree, a contents-API ?ref= read, --path with --ref) whose revision or path the helper cannot resolve to a file,
# is UNKNOWN and refuses (fail closed): the helper resolves the FULL operand, including a REV:PATH held in a shell variable or a step env entry, and exits 1 with no
# stdout for anything it cannot resolve (CTO REVISE of MoP ce4f381b, ts 1795629066.866119: an unresolved variable-held read was skipped and the empty result read as eligible). The ancestry, file and input reads are local git reads under the
# same deadline (no new GitHub read). The rule that admitted the PR is printed as MERGE_REF_RULE and, for the waiver, appended to pm-admit-skip.log; the waiver's
# receipt is rule=stale_merge_ref_waived:no_overlap.
stale_refuse() {
  echo "REFUSE stale_merge_ref #$PR: $1. Admission needs the PR's test merge built on the overlap-checked main $OVERLAP_MAIN_SHA, or on an older ancestor of it whose later changes touch none of this PR's files, and made NO change to the PR (no PR state change, no main merge, no push). Re-run admission; if the overlap list above is non-empty, merge main into the PR branch (non-force); if it keeps refusing, report it to the CTO"
  exit 3
}
# stale_read <cmd...>: ONE read bounded by the remaining deadline. Prints stdout; returns 1 (UNKNOWN) when there is no timeout binary, the
# deadline has passed, the call fails or is killed by the timeout, or the result returns at or after the deadline.
stale_read() {
  local remaining out
  [ -n "$STALE_TIMEOUT_BIN" ] || return 1
  remaining=$(( STALE_DEADLINE_AT - $(date +%s) ))
  [ "$remaining" -ge 1 ] || return 1
  out=$("$STALE_TIMEOUT_BIN" --kill-after=2 "$remaining" "$@" 2>/dev/null) || return 1
  [ "$(date +%s)" -lt "$STALE_DEADLINE_AT" ] || return 1
  printf '%s' "$out"
}
is_sha() { printf '%s' "$1" | grep -Eq "$SHA_RE"; }
lag_refuse() {
  stale_refuse "test merge $MERGE_SHA has parents [$STALE_PARENTS] but must be exactly the reviewed head and the overlap-checked main [$STALE_WANTED], so GitHub has not rebuilt it on that main; lagging-ref rule not met: $1"
}
# lagging_ref_verify: called only when the merge commit's parents are not exactly {head, overlap-checked main}. Returns 0 (sets MERGE_REF_RULE_LINE and
# MERGE_REF_LAGGING=1) only when conditions (a)-(d) above hold; every other outcome, including any unreadable or late git read, exits through stale_refuse.
lagging_ref_verify() {
  local p lagp="" nh=0 main_base lag_base d_main d_lag d_since pr_files main_files overlap n_pr=0 n_main=0 n_overlap=0 list ci_changed n_ci
  [ "$MERGEABLE" != CONFLICTING ] || lag_refuse "the PR conflicts with main"
  set -- $STALE_PARENTS
  [ "$#" -eq 2 ] || lag_refuse "the test merge has $# parent(s), not exactly two"
  for p in "$@"; do
    is_sha "$p" || lag_refuse "a test-merge parent is not a full sha"
    if [ "$p" = "$HEAD" ]; then nh=$((nh + 1)); else lagp="$p"; fi
  done
  [ "$nh" -eq 1 ] && [ -n "$lagp" ] || lag_refuse "the test merge was not built on the reviewed head $HEAD"
  stale_read git -C "$MAIN_REPO" merge-base --is-ancestor "$lagp" "$OVERLAP_MAIN_SHA" >/dev/null ||
    lag_refuse "its main parent $lagp is not verified as an ancestor of the overlap-checked main $OVERLAP_MAIN_SHA (not an ancestor, or the git read failed, timed out or returned past the deadline)"
  main_base=$(stale_read git -C "$MAIN_REPO" merge-base "$OVERLAP_MAIN_SHA" "$HEAD") || lag_refuse "the merge-base of the overlap-checked main and the head is $STALE_UNKNOWN"
  lag_base=$(stale_read git -C "$MAIN_REPO" merge-base "$lagp" "$HEAD") || lag_refuse "the merge-base of its main parent and the head is $STALE_UNKNOWN"
  is_sha "$main_base" || lag_refuse "the merge-base of the overlap-checked main and the head is not a full sha"
  is_sha "$lag_base" || lag_refuse "the merge-base of its main parent and the head is not a full sha"
  # C-quoted names are one path per line even with embedded newlines. Both merge-bases are used so a PR branched before M is not under-counted.
  d_main=$(stale_read git -C "$MAIN_REPO" -c core.quotePath=true diff --no-renames --name-only "$main_base" "$HEAD" --) || lag_refuse "the PR's changed files (merge-base $main_base..head) are $STALE_UNKNOWN"
  d_lag=$(stale_read git -C "$MAIN_REPO" -c core.quotePath=true diff --no-renames --name-only "$lag_base" "$HEAD" --) || lag_refuse "the PR's changed files (merge-base $lag_base..head) are $STALE_UNKNOWN"
  d_since=$(stale_read git -C "$MAIN_REPO" -c core.quotePath=true diff --no-renames --name-only "$lagp" "$OVERLAP_MAIN_SHA" --) || lag_refuse "the files main changed since $lagp are $STALE_UNKNOWN"
  pr_files=$(printf '%s\n%s\n' "$d_main" "$d_lag" | sed '/^$/d' | LC_ALL=C sort -u) || lag_refuse "the PR's changed files could not be listed"
  main_files=$(printf '%s\n' "$d_since" | sed '/^$/d' | LC_ALL=C sort -u) || lag_refuse "the files main changed since $lagp could not be listed"
  overlap=$(LC_ALL=C comm -12 <(printf '%s\n' "$pr_files") <(printf '%s\n' "$main_files")) || lag_refuse "the two file lists could not be compared"
  if [ -n "$pr_files" ]; then n_pr=$(printf '%s\n' "$pr_files" | wc -l | tr -d ' '); fi
  if [ -n "$main_files" ]; then n_main=$(printf '%s\n' "$main_files" | wc -l | tr -d ' '); fi
  if [ -n "$overlap" ]; then
    n_overlap=$(printf '%s\n' "$overlap" | wc -l | tr -d ' ')
    list=$(printf '%s\n' "$overlap" | sed -n '1,20p' | tr '\n' ' ' | sed 's/ $//')
    [ "$n_overlap" -le 20 ] || list="$list ... (+$((n_overlap - 20)) more)"
    lag_refuse "overlap: the PR changes $n_overlap file(s) that main also changed since that parent $lagp, so CI did not run against main's version of them: $list"
  fi
  # (d) A changed trusted CI input is refused even with zero file overlap: the PR's CI ran the selection at M, not main's. The helper prints the changed
  # input paths (empty when none) and exits 1 on any unreadable git state; a late or failed run is UNKNOWN through stale_read.
  ci_changed=$(stale_read python3 -I "$SCRIPT_DIR/pm-admit-ci-inputs.py" --repo "$MAIN_REPO" --base "$lagp" --main "$OVERLAP_MAIN_SHA") || lag_refuse "the trusted shared CI inputs main changed since $lagp are $STALE_UNKNOWN (or a workflow reads a trusted file the discovery could not resolve)"
  ci_changed=$(printf '%s\n' "$ci_changed" | sed '/^$/d')
  if [ -n "$ci_changed" ]; then
    n_ci=$(printf '%s\n' "$ci_changed" | wc -l | tr -d ' ')
    list=$(printf '%s\n' "$ci_changed" | sed -n '1,20p' | tr '\n' ' ' | sed 's/ $//')
    [ "$n_ci" -le 20 ] || list="$list ... (+$((n_ci - 20)) more)"
    # --merge-only probe (ci_input_probe, run in a subshell): report exactly this outcome to the caller with a distinct exit code instead of refusing.
    if [ "$STALE_PROBE_CI" = 1 ]; then
      echo "STALE_REF_PROBE ci_inputs_changed n=$n_ci main_parent=$lagp main=$OVERLAP_MAIN_SHA files=$list"
      exit 4
    fi
    # Plain admission never merges main: a changed trusted CI input needs a main merge (rule 62), so stop before any dispatch with the same line and exit as
    # plan_main_merge. Nothing was written (the guard runs before every admission write).
    if [ "$MERGE_ONLY" = 0 ] && [ "$DRY" != 1 ]; then
      echo "NEEDS_MAIN_MERGE pr=$PR reason=ci_input_changed files=$n_ci"
      exit 20
    fi
    lag_refuse "trusted CI input changed: main changed $n_ci shared CI input file(s) that decide what runs since that parent $lagp, so CI ran the older selection even though no PR file overlaps: $list"
  fi
  MERGE_REF_LAGGING=1
  MERGE_REF_RULE_LINE="MERGE_REF_RULE rule=stale_merge_ref_waived:no_overlap pr=$PR head=$HEAD merge=$MERGE_SHA main_parent=$lagp main=$OVERLAP_MAIN_SHA pr_files=$n_pr main_files_since_parent=$n_main overlap=0 ci_inputs_changed=0"
}
merge_ref_guard() {
  OVERLAP_MAIN_SHA="$MAIN_SHA"
  MERGE_REF_LAGGING=0
  STALE_DEADLINE_SECS="${PM_ADMIT_STALE_DEADLINE:-30}"
  case "$STALE_DEADLINE_SECS" in ''|*[!0-9]*) STALE_DEADLINE_SECS=30 ;; esac
  [ "$STALE_DEADLINE_SECS" -ge 1 ] || STALE_DEADLINE_SECS=1
  STALE_DEADLINE_AT=$(( $(date +%s) + STALE_DEADLINE_SECS ))
  STALE_TIMEOUT_BIN=$(command -v timeout || command -v gtimeout || true)
  STALE_UNKNOWN="UNKNOWN (read failed, timed out or returned past the ${STALE_DEADLINE_SECS}s deadline)"
  STALE_PR_JQ='[.state, .head.sha, (.merge_commit_sha // "")] | join(" ")'
  printf '%s' "$OVERLAP_MAIN_SHA" | grep -Eq "$SHA_RE" || stale_refuse "the main sha from the overlap check is unreadable"
  STALE_LINE1=$(stale_read gh api "repos/$REPO/pulls/$PR" --jq "$STALE_PR_JQ") || stale_refuse "PR state, head and merge ref $STALE_UNKNOWN"
  read -r STALE_STATE STALE_HEAD MERGE_SHA <<<"$STALE_LINE1"
  [ "$STALE_STATE" = open ] || stale_refuse "PR is not open (state ${STALE_STATE:-unknown})"
  [ "$STALE_HEAD" = "$HEAD" ] || stale_refuse "PR head moved since review (reviewed $HEAD, GitHub now has ${STALE_HEAD:-unknown}); needs a fresh admission run"
  printf '%s' "$MERGE_SHA" | grep -Eq "$SHA_RE" || stale_refuse "GitHub has no test merge commit for the PR yet (merge_commit_sha empty or invalid)"
  STALE_MAIN_NOW=$(stale_read gh api "repos/$REPO/git/ref/heads/main" --jq .object.sha) || stale_refuse "main $STALE_UNKNOWN"
  printf '%s' "$STALE_MAIN_NOW" | grep -Eq "$SHA_RE" || stale_refuse "main read returned an invalid sha"
  [ "$STALE_MAIN_NOW" = "$OVERLAP_MAIN_SHA" ] || stale_refuse "main moved since the overlap review (overlap checked $OVERLAP_MAIN_SHA, main is now $STALE_MAIN_NOW); a fresh admission run is needed"
  STALE_PARENTS=$(stale_read gh api "repos/$REPO/commits/$MERGE_SHA" --jq '[.parents[].sha] | sort | join(" ")') || stale_refuse "parents of test merge $MERGE_SHA $STALE_UNKNOWN"
  STALE_WANTED=$(printf '%s\n%s\n' "$HEAD" "$OVERLAP_MAIN_SHA" | LC_ALL=C sort | tr '\n' ' ' | sed 's/ $//')
  if [ "$STALE_PARENTS" = "$STALE_WANTED" ]; then
    MERGE_REF_RULE_LINE="MERGE_REF_RULE rule=exact_main_parent pr=$PR head=$HEAD merge=$MERGE_SHA main=$OVERLAP_MAIN_SHA"
  else
    lagging_ref_verify
  fi
  STALE_LINE2=$(stale_read gh api "repos/$REPO/pulls/$PR" --jq "$STALE_PR_JQ") || stale_refuse "confirming PR read $STALE_UNKNOWN"
  [ "$STALE_LINE2" = "$STALE_LINE1" ] || stale_refuse "PR state, head or merge ref changed during the check"
  STALE_MAIN_AGAIN=$(stale_read gh api "repos/$REPO/git/ref/heads/main" --jq .object.sha) || stale_refuse "confirming main read $STALE_UNKNOWN"
  [ "$STALE_MAIN_AGAIN" = "$OVERLAP_MAIN_SHA" ] || stale_refuse "main moved during the check (overlap checked $OVERLAP_MAIN_SHA, main is now $STALE_MAIN_AGAIN); a fresh admission run is needed"
  echo "$MERGE_REF_RULE_LINE"
  if [ "$MERGE_REF_LAGGING" = 1 ] && [ "$DRY" != 1 ] && [ "$STALE_PROBE_CI" != 1 ]; then   # a --merge-only probe admits nothing, so it leaves no waiver receipt
    # Best-effort audit trail beside the other admission overrides; the printed line above is the primary record.
    printf '%s %s\n' "$(date -u +%FT%TZ)" "${MERGE_REF_RULE_LINE#MERGE_REF_RULE }" >> "$HOME/.claude/logs/pm-admit-skip.log" 2>/dev/null || true
  fi
  echo "MERGE_REF_VERIFIED pr=$PR head=$HEAD main=$OVERLAP_MAIN_SHA merge=$MERGE_SHA"
}
# --dry-run only: read-only, informational preview of what the real run's merge-ref guard would decide right now. It never changes the dry run's exit
# code and never writes (the guard's only write is the lagging-rule audit line, skipped when DRY=1).
dry_merge_ref_verdict() {
  local out rc=0
  out=$(
    read_pr_head
    plan=$(main_merge_probe "$HEAD" "$MERGEABLE") || { echo "UNKNOWN: main merge probe failed"; exit 3; }
    read -r MERGE_REASON MERGE_FILES MAIN_SHA <<<"$plan"
    if [ "$MERGE_REASON" != no_conflict_no_overlap ]; then
      echo "main integration required reason=$MERGE_REASON files=$MERGE_FILES; the real run refuses before the merge-ref check"
      exit 2
    fi
    merge_ref_guard
  ) || rc=$?
  if [ "$rc" = 0 ]; then echo "DRY RUN merge_ref: ELIGIBLE (the real run would pass the merge-ref guard now)"
  else echo "DRY RUN merge_ref: would REFUSE (rc=$rc)"; fi
  printf '%s\n' "$out" | sed 's/^/  /'
}

# --merge-only only: is the stale-ref guard's ONLY objection to this no-conflict/no-overlap PR a non-empty changed-trusted-CI-input list? Runs merge_ref_guard
# in a subshell with STALE_PROBE_CI=1 (same deadline-bounded reads, same fail-closed checks, no audit write, no PR/branch/label write). Returns 0 and sets
# CI_PROBE_LINE only when the guard exits 4 AND printed its STALE_REF_PROBE line. The guard passing (exact parents, or the waiver), any other refusal (exit 3), a crash,
# an unreadable or late read and a missing timeout binary all return 1, so the caller keeps its old no-write skip.
CI_PROBE_LINE=""
ci_input_probe() {
  local out rc=0
  CI_PROBE_LINE=""
  out=$(STALE_PROBE_CI=1; merge_ref_guard) || rc=$?
  [ "$rc" = 4 ] || return 1
  CI_PROBE_LINE=$(printf '%s\n' "$out" | grep '^STALE_REF_PROBE ci_inputs_changed ') || return 1
  [ -n "$CI_PROBE_LINE" ] || return 1
  return 0
}

# Integrate main before review, not during admission. Exit before any admission effects.
if [ "$MERGE_ONLY" = 1 ]; then
  read_pr_head
  plan_main_merge
  # One machine-readable decision line for every outcome (rule 62: merge main only on conflict or overlap; lanes go through this overlap check, never merge main by hand).
  # reason= is conflict | overlap | ci_input_changed (main changed a trusted CI input, see the stale-ref guard) | no_conflict_no_overlap (skip). files= is the overlap count
  # (the changed CI input count for ci_input_changed). The existing MAIN_MERGE: lines below are unchanged.
  merge_reason_line() { echo "MAIN_MERGE_REASON: decision=$1 reason=$MERGE_REASON rule=62 head=$HEAD main=$MAIN_SHA files=$MERGE_FILES"; }
  if [ "$MERGE_REASON" = no_conflict_no_overlap ]; then
    if ci_input_probe; then
      # A changed trusted CI input is an overlap-class reason: the PR's lagging test merge ran the older selection, GitHub will not rebuild it, and only a
      # main merge into the PR head clears it. MERGE_FILES = the number of changed CI input files (the probe line's n=).
      echo "$CI_PROBE_LINE"
      MERGE_FILES="${CI_PROBE_LINE#STALE_REF_PROBE ci_inputs_changed n=}"; MERGE_FILES="${MERGE_FILES%% *}"
      MERGE_REASON=ci_input_changed
    else
      merge_reason_line skip
      echo "MAIN_MERGE: skipped head=$HEAD reason=$MERGE_REASON"
      exit 0
    fi
  fi
  merge_reason_line merge

  PRE_MERGE_HEAD="$HEAD"
  PR_BASE=$(gh_get pr_base ok gh pr view "$PR" --repo "$REPO" --json baseRefName --jq .baseRefName) || api_fail pr_base
  [ "$PR_BASE" = main ] || { echo 'REFUSE: update-branch would not merge main; route to slot'; exit 2; }
  if [ "$DRY" = 1 ]; then   # preview only: everything above is a read; no update-branch, no sleep, no push
    echo "MAIN_MERGE: dry-run would attempt update-branch head=$HEAD reason=$MERGE_REASON files=$MERGE_FILES main=$MAIN_SHA (nothing was changed)"
    exit 0
  fi
  if ! gh api -X PUT "repos/$REPO/pulls/$PR/update-branch" -f "expected_head_sha=$PRE_MERGE_HEAD" >/dev/null 2>&1; then
    if [ "$MERGE_REASON" = conflict ]; then echo 'REFUSE: conflict with main; route to slot'
    else echo 'REFUSE: required main update-branch was rejected; retry --merge-only or route to slot'; fi
    exit 2
  fi
  sleep 8
  read_pr_head
  # GitHub accepts the request asynchronously. Confirm the merge, not just a 202 or a different head.
  if [ "$HEAD" = "$PRE_MERGE_HEAD" ] ||
     ! git -C "$MAIN_REPO" fetch -q --no-tags origin "refs/pull/$PR/head" ||
     ! git -C "$MAIN_REPO" merge-base --is-ancestor "$MAIN_SHA" "$HEAD" ||
     ! git -C "$MAIN_REPO" merge-base --is-ancestor "$PRE_MERGE_HEAD" "$HEAD"; then
    echo 'REFUSE: required main merge not confirmed; retry --merge-only or route to slot'; exit 2
  fi
  echo "MAIN_MERGE: performed head=$HEAD reason=$MERGE_REASON files=$MERGE_FILES"
  # Verify the merge did not change the PR's OWN change, byte for byte (CTO REVISE C0ALZJHGE49 thread 1795830648.379839, ts 1795830757.685749: `git patch-id --stable`
  # ignores whitespace, so patches adding "a b" and "ab" got equal ids). The head must be a two-parent merge of the pinned pre-merge head and main; before = the
  # PR's change against its old merge-base, after = the PR's change against the merged-in main parent. Both are `git diff --no-ext-diff` text with NO whitespace
  # option, compared as literal bytes. Only the lines that legitimately move when main lands in the same file are dropped: the `index <blob>..<blob>` line and the
  # `@@ -a,b +c,d @@` hunk headers (-U0 leaves no context lines). Every file header and every +/- line stays exact. Any unreadable state reports "no".
  authored_hunks_of() {
    local out
    out=$(git -C "$MAIN_REPO" diff --no-ext-diff --no-textconv --no-renames --binary -U0 --no-color "$1" "$2" --) || return 1
    printf '%s\n' "$out" | grep -v -e '^index ' -e '^@@ '
  }
  # The carry is allowed ONLY when ALL of these hold (CTO REVISE C0ALZJHGE49 thread 1795830648.379839, ts 1795831628.752089; the literal-hunk check alone is blind to a merge that
  # moves an authored line to another function and leaves the same zero-context hunk):
  #   (a) the merged head has exactly two parents: the pinned PR head and EXACTLY the origin/main sha this merge step probed and recorded (MAIN_SHA). A later commit on top of it, or
  #       any other commit, is not verified and never carries;
  #   (b) the merged head's tree equals the tree of a successful `git merge-tree --write-tree <pinned PR head> <probed main>` (the merge nobody edited);
  #   (c) the literal-byte authored-hunk comparison below (no whitespace option) is identical before and after.
  # Any unreadable state or failed check reports "no" with one CARRY_REFUSED line naming the first failing condition.
  PATCH_ID_SAME=no   # historical receipt key: now "the carry conditions (a)+(b)+(c) all hold"
  CARRY_PRE_HEAD="$PRE_MERGE_HEAD"; CARRY_MAIN_PARENT=none; CARRY_REFUSED=""
  MERGE_PARENTS=$(git -C "$MAIN_REPO" rev-list --parents -n 1 "$HEAD" 2>/dev/null | cut -d' ' -f2- || true)
  set -- $MERGE_PARENTS
  MAIN_PARENT=""
  if [ "$#" -ne 2 ]; then
    CARRY_REFUSED="the merged head is not a two-parent merge"
  else
    if [ "$1" = "$PRE_MERGE_HEAD" ]; then MAIN_PARENT="$2"; elif [ "$2" = "$PRE_MERGE_HEAD" ]; then MAIN_PARENT="$1"; fi
    if [ -z "$MAIN_PARENT" ]; then
      CARRY_REFUSED="the pinned PR head is not a parent of the merged head"
    elif [ "$MAIN_PARENT" != "$MAIN_SHA" ]; then
      CARRY_REFUSED="the main parent $MAIN_PARENT is not exactly the probed main $MAIN_SHA"
    else
      CARRY_MAIN_PARENT="$MAIN_PARENT"
      EXPECT_OUT=$(git -C "$MAIN_REPO" merge-tree --write-tree "$PRE_MERGE_HEAD" "$MAIN_SHA" 2>/dev/null) || EXPECT_OUT=""
      EXPECT_TREE="${EXPECT_OUT%%$'\n'*}"
      ACTUAL_TREE=$(git -C "$MAIN_REPO" rev-parse --verify "$HEAD^{tree}" 2>/dev/null) || ACTUAL_TREE=""
      if ! printf '%s' "$EXPECT_TREE" | grep -Eq "$SHA_RE" || ! printf '%s' "$ACTUAL_TREE" | grep -Eq "$SHA_RE"; then
        CARRY_REFUSED="the clean merge-tree of the pinned head and the probed main is unreadable"
      elif [ "$EXPECT_TREE" != "$ACTUAL_TREE" ]; then
        CARRY_REFUSED="the merged tree $ACTUAL_TREE differs from the clean merge-tree $EXPECT_TREE of the pinned head and the probed main"
      else
        OLD_BASE=$(git -C "$MAIN_REPO" merge-base "$MAIN_PARENT" "$PRE_MERGE_HEAD" 2>/dev/null) || OLD_BASE=""
        if [ -n "$OLD_BASE" ]; then
          HUNKS_BEFORE=$(authored_hunks_of "$OLD_BASE" "$PRE_MERGE_HEAD" 2>/dev/null) || HUNKS_BEFORE=""
          HUNKS_AFTER=$(authored_hunks_of "$MAIN_PARENT" "$HEAD" 2>/dev/null) || HUNKS_AFTER=""
          [ -n "$HUNKS_BEFORE" ] && [ "$HUNKS_BEFORE" = "$HUNKS_AFTER" ] && PATCH_ID_SAME=yes
        fi
        [ "$PATCH_ID_SAME" = yes ] || CARRY_REFUSED="the PR's authored hunks are not byte-identical before and after the merge"
      fi
    fi
  fi
  [ -z "$CARRY_REFUSED" ] || echo "CARRY_REFUSED pr=$PR head=$HEAD reason=$CARRY_REFUSED"
  # No CI/E2E is dispatched here: the merged head gets its own admission afterwards (S173: merge-only work needs no CI round).
  echo "MERGED_MAIN pr=$PR head=$HEAD pre_head=$CARRY_PRE_HEAD main_parent=$CARRY_MAIN_PARENT patch_id_same=$PATCH_ID_SAME"
  exit 0
fi

# ONE paid CI/E2E round per PR (Rajiv C0ALZJHGE49 thread 1795841629.732639, ts 1795845160.422969 + ts 1795845739.605399:
# "yes, admin merge after testing with remote box. the qa videos should be posted to slack for review"). Decision + box list: pm-admit-paid-round.py.
# rc 0 dispatch (no earlier paid, queued or unresolved activation; this head's own activation still in flight; or logged PM_ADMIT_PAID_ROUND_OVERRIDE);
# rc 21 BOX_ROUND_REQUIRED / PAID_ROUND_UNRESOLVED (nothing dispatched or written; a COMPLETED same-head activation is blocked too, so the re-arm below
# can never start a second paid pair without the pinned override). This is the ONLY CI/E2E dispatch path: admit.py refuses (PM owns admission).
# Sets PAID_SAME_HEAD (none|idle|in_flight|completed_paid|unresolved) from the helper's last line for the same-head block below.
PAID_SAME_HEAD=none
paid_round_gate() {
  local head="$1" out rc=0 dry="" state
  [ "$DRY" = 1 ] && dry="--dry-run"
  git -C "$MAIN_REPO" fetch -q --no-tags origin "refs/pull/$PR/head" main >/dev/null 2>&1 || true
  out=$(python3 -I "$SCRIPT_DIR/pm-admit-paid-round.py" gate --repo "$REPO" --pr "$PR" --head "$head" --repo-dir "$MAIN_REPO" $dry) || rc=$?
  [ -z "$out" ] || printf '%s\n' "$out"
  state=$(printf '%s\n' "$out" | sed -n 's/^paid_round_same_head=//p' | tail -n 1)
  [ -z "$state" ] || PAID_SAME_HEAD="$state"
  case "$rc" in
    0) return 0 ;;
    21) return 21 ;;
    64) return 64 ;;
    11) echo 'REFUSE: github_api_error paid_round'; exit 7 ;;
    *) api_fail paid_round ;;
  esac
}

if [ "$DRY" = 1 ]; then
  if [ "$NO_UPDATE" = 1 ]; then
    read_pr_head
    if [ "$HEAD" != "$H0" ]; then gate_12b "$HEAD" || exit "$?"; fi
    plan_main_merge
    H="$HEAD"
  else
    H=$(gh_get pr_head sha gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid) || api_fail pr_head
  fi
  echo "DRY RUN PR #$PR head=$H"
  overlap_start "$H"; overlap_finish
  dry_merge_ref_verdict || true
  rc=0; scope_gate "$H" || rc=$?
  [ $rc -ne 0 ] && { echo "DRY RUN done (scope gate rc=$rc); nothing admitted"; exit $rc; }
  rc=0; paid_round_gate "$H" || rc=$?
  [ $rc -ne 0 ] && { echo "DRY RUN done (paid-round gate rc=$rc); nothing admitted"; exit $rc; }
  rc=0; related_tests_gate "$H" || rc=$?
  echo "DRY RUN done (gate rc=$rc); nothing admitted"; exit $rc
fi

# H0 remains the pre-check's reviewed head; never overwrite it with an unchecked read.
# Closing-reference gate (Rajiv C0ALZJHGE49 thread 1793953549.727149, ts 1793971953.982729 "yes, do it";
# ts 1793972664.618549: "pr admit ci should add the closes #N if it's not already present instead of refusing it").
# Every admitted PR must close its own issue. If closingIssuesReferences is empty, derive exactly one issue number
# (body "Part of #N" > branch "<type>/<N>-..." > title "(#N)"), add "Closes #N" to the PR body, and read it back.
# Refuse only when no single issue number can be derived or the readback still shows no closing reference.
# CTO REVISE (C0ALZJHGE49 thread 1795830648.379839, ts 1795830757.685749): the derivation and its refusals stay here (read-only), but the body WRITE runs only
# after BOTH main-integration checks (plan_main_merge and merge_ref_guard) pass. A PR that needs a main merge exits 20 before any GitHub write, closing reference included.
closes_of() { gh_get pr_closes ok gh pr view "$PR" --repo "$REPO" --json closingIssuesReferences --jq '[.closingIssuesReferences[].number]|map(tostring)|join(",")'; }
CLOSES=$(closes_of) || api_fail pr_closes
CLOSES_PENDING=""
if [ -z "$CLOSES" ]; then
  PRMETA=$(gh_get pr_meta ok gh pr view "$PR" --repo "$REPO" --json body,headRefName,title) || api_fail pr_meta
  # A body saying "Part of #N" declares a partial slice: never rewrite it into "Closes #N" (CTO C0ALZJHGE49
  # thread 1793977537.654329, ts 1793977860.637639: "Do not blindly rewrite Part of #12752 into Closes #12752").
  # The slice needs its own child issue (rule 06), so refuse with that exact remedy.
  SLICE=$(printf '%s' "$PRMETA" | python3 -c '
import json,re,sys
b=json.load(sys.stdin).get("body") or ""
print(",".join(sorted(set(re.findall(r"(?i)\bpart of #(\d+)",b)))))')
  [ -z "$SLICE" ] || { echo "REFUSE: PR #$PR is a partial slice (body says Part of #$SLICE) with no closing reference. File a child issue for this slice, add 'Closes #<child>' and keep 'Part of #$SLICE' (CTO ts 1793977860.637639); nothing admitted"; exit 9; }
  CAND=$(printf '%s' "$PRMETA" | python3 -c '
import json,re,sys
d=json.load(sys.stdin); body=d.get("body") or ""; br=d.get("headRefName") or ""; t=d.get("title") or ""
c=set()
if not c:
    m=re.match(r"^[a-z]+/(?:issue-)?(\d+)[-_](?!\d{2}-\d{2})",br); c={m.group(1)} if m else set()  # dated branches (content/2026-11-06-x) are not issue numbers
if not c:
    c=set(re.findall(r"\(#(\d+)\)",t))
print(next(iter(c)) if len(c)==1 else "")')
  [ -n "$CAND" ] || { echo "REFUSE: PR #$PR has no closing issue reference and no single issue number could be derived (add Closes #N; Rajiv ts 1793971953.982729); nothing admitted"; exit 9; }
  CLOSES_PENDING="$CAND"   # written by add_closing_reference after the main-integration checks
fi
# The deferred closing-reference write (see the CTO REVISE note above): the only GitHub write of the gate, called once after plan_main_merge and merge_ref_guard.
add_closing_reference() {
  [ -n "$CLOSES_PENDING" ] || return 0
  local CAND="$CLOSES_PENDING"
  if [ "$DRY" = 1 ]; then echo "DRY RUN: would add 'Closes #$CAND' to PR #$PR body"; else
    NEWBODY=$(printf '%s' "$PRMETA" | CAND="$CAND" python3 -c '
import json,os,re,sys
b=json.load(sys.stdin).get("body") or ""; n=os.environ["CAND"]
b2=re.sub(r"(?i)\bpart of #"+n+r"\b","Closes #"+n,b)
print(b2 if b2!=b else "Closes #"+n+"\n\n"+b)')
    BF=$(mktemp /tmp/admit-closes-body.XXXXXX); printf '%s' "$NEWBODY" > "$BF"
    gh_do add_closes gh pr edit "$PR" --repo "$REPO" --body-file "$BF"; rm -f "$BF"
    CLOSES=$(closes_of) || api_fail pr_closes_readback
    [ -n "$CLOSES" ] || { echo "REFUSE: added 'Closes #$CAND' to PR #$PR but closingIssuesReferences readback is still empty; nothing admitted"; exit 9; }
    echo "closing-reference gate: ADDED 'Closes #$CAND' to PR #$PR body (readback closes #$CLOSES)"
  fi
}
if [ -n "$CLOSES_PENDING" ]; then echo "closing-reference gate: no closing reference; will add 'Closes #$CLOSES_PENDING' after the main-integration checks"
else echo "closing-reference gate: closes #$CLOSES"; fi
scope_gate "$H0" || exit 5
related_tests_gate "$H0" || exit 4

read_pr_head
# A concurrent push must not carry stale review into dispatch. If main now needs
# integration, refuse rather than moving the head after review.
if [ "$HEAD" != "$H0" ]; then gate_12b "$HEAD" || exit "$?"; fi
plan_main_merge
echo 'MAIN_MERGE: skipped reason=no_conflict_no_overlap'
echo "PR #$PR head=$HEAD mergeable=$MERGEABLE"
[ "$MERGEABLE" = "CONFLICTING" ] && { echo "REFUSE: conflict with main; route to slot"; exit 2; }
# Stale test-merge guard + lagging-ref rule: see merge_ref_guard above. It runs before every admission write (label create, label cleanup,
# priority mirror, activation label) and exits 3 (stale_merge_ref) on anything it cannot verify.
merge_ref_guard
# ONE paid round per PR: a later head with a paid round already behind it dispatches NOTHING (exit 21, BOX_ROUND_REQUIRED + the box test list). After the
# main-integration checks (rule 62 NEEDS_MAIN_MERGE stays first) and before every GitHub write.
paid_round_gate "$HEAD" || exit "$?"
# The helper reports this head's own activation still running (rc 0, paid_round_same_head=in_flight): that is ALREADY_ADMITTED whether or not the ci-head label
# is present. Return BEFORE any label, body or priority write; otherwise an absent label with a live activation falls through to the ci-head label add below
# and re-arms a second activation (CTO REVISE on MoP 6a6f9f1e, fix 3).
if [ "$PAID_SAME_HEAD" = in_flight ]; then
  echo "ALREADY_ADMITTED #$PR at $HEAD (paid-round gate: same-head activation in flight; no label or other write made)"
  exit 0
fi
# Both main-integration checks passed: only now is the closing-reference body write allowed (it is still before every other admission write).
add_closing_reference
# Even malformed-label cleanup is a write; don't do it on a refused head.
clean_bad_ci_head_labels
overlap_start "$HEAD"
# Priority mirroring is best-effort and must never block admission.
mirror_priority() {
  local data existing issues issue labels priorities="" priority
  data=$(gh pr view "$PR" --repo "$REPO" --json labels,closingIssuesReferences) || return 1
  existing=$(printf '%s' "$data" | jq -r '.labels[].name | select(test("^(P[0-5]|priority:.*)$"; "i"))') || return 1
  if [ -n "$existing" ]; then
    echo "priority: preserving existing PR label(s): $existing"
    return 0
  fi
  issues=$(printf '%s' "$data" | jq -r '.closingIssuesReferences[].url') || return 1
  while IFS= read -r issue; do
    [ -n "$issue" ] || continue
    labels=$(gh issue view "$issue" --json labels --jq '.labels[].name') || return 1
    priorities="$priorities
$labels"
  done <<< "$issues"
  priority=$(printf '%s\n' "$priorities" | jq -Rrs 'split("\n") | map(ascii_upcase | sub("^PRIORITY:"; "") | select(test("^P[0-5]$"))) | sort | .[0] // "P2"') || return 1
  if gh pr edit "$PR" --repo "$REPO" --add-label "$priority"; then
    echo "priority: applied $priority to PR #$PR"
  else
    echo "WARN priority: failed to apply $priority to PR #$PR; continuing admission"
  fi
  return 0
}
mirror_priority || echo "WARN priority: lookup or normalization failed for PR #$PR; continuing admission"
printf '%s' "$HEAD" | grep -Eq "$SHA_RE" || api_fail pr_head_invalid
cap_override_head_check   # before ANY ci-head label mutation: a moved head refuses without touching the labels
L="ci-head:$HEAD"
gh label create "$L" --repo "$REPO" --color 0E8A16 >/dev/null 2>&1 || true
LJ=$(gh_get pr_labels labels_json gh pr view "$PR" --repo "$REPO" --json labels) || api_fail pr_labels
# A head with no ci-head label of its own cannot be "already admitted": claim the grant now, so a race loser also never strips another head's label.
printf '%s' "$LJ" | jq -e --arg l "$L" '.labels[].name|select(.==$l)' >/dev/null 2>&1 || consume_cap_override
for old in $(printf '%s' "$LJ" | jq -r '.labels[].name|select(startswith("ci-head:"))'); do
  [ "$old" != "$L" ] && gh_do remove_old_ci_head gh pr edit "$PR" --repo "$REPO" --remove-label "$old"
done
# Same-head re-admission (2026-11-21, #14431): re-adding an existing label fires no "labeled"
# event, so no new CI/E2E runs start. If runs for this head are still queued/in progress,
# report that and stop. Otherwise remove the label first so the add below starts fresh runs.
if printf '%s' "$LJ" | jq -e --arg l "$L" '.labels[].name|select(.==$l)' >/dev/null 2>&1; then
  INFLIGHT=$(gh run list --repo "$REPO" --commit "$HEAD" --limit 30 --json databaseId,workflowName,status \
    --jq '[.[]|select((.workflowName=="CI" or .workflowName=="E2E Smoke Tests") and (.status=="queued" or .status=="in_progress"))|"\(.workflowName):\(.databaseId)"]|join(",")' 2>/dev/null || echo "")
  # The paid-round gate reads every run and job of this head; its in_flight verdict also holds when the run list above is empty or unreadable.
  if [ -z "$INFLIGHT" ] && [ "$PAID_SAME_HEAD" = in_flight ]; then INFLIGHT="paid_round_gate"; fi
  if [ -n "$INFLIGHT" ]; then
    overlap_finish
    echo "ALREADY_ADMITTED #$PR at $HEAD (runs in flight: $INFLIGHT); no new runs started"
    exit 0
  fi
  consume_cap_override   # claim BEFORE the re-arm removes the label, so a race loser never strips the winner's label
  gh_do rearm_same_ci_head gh pr edit "$PR" --repo "$REPO" --remove-label "$L"
  echo "REARM: removed same-head label $L so the re-add starts fresh CI/E2E runs"
fi
consume_cap_override
gh_do add_ci_head_label gh pr edit "$PR" --repo "$REPO" --add-label "$L" --add-label pm-state:qa-passed-awaiting-ci
overlap_finish
echo "ADMITTED #$PR at $HEAD"
echo "VERIFY: confirm new CI and E2E runs exist on $HEAD (gh run list --commit $HEAD) before treating #$PR as in CI"
