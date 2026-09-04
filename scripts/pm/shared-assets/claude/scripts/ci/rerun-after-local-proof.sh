#!/usr/bin/env bash
# PM-owned entry point for same-head label-gated CI reruns.
#
# A rerun is allowed only after current-head proof exists. This wrapper
# prevents raw gh run rerun from bypassing the local-run gate. Capture-sensitive
# PRs use exact-head remote workflow proof when REMOTE_CAPTURE_ONLY is enabled.
# #7178 flake quarantine (Rajiv directive 2026-08-08, thread 1786113180.659309):
# a run whose ci-verdict carries quarantine_blocked=true (repeated CI-only
# flake with no open follow-up issue, emitted by publish-ci-verdict.py) is
# refused rerun authorization until a fix or quarantine lands; the
# first-occurrence rerun path is unchanged.
# Verdict-lifecycle admission (incident
# control-plane:repair-dispatch-chain:verdict-lifecycle): the investigator's
# pending record (local_repro_result=pending, dispatch-local-repro) plus a
# sealed current-head PASS preflight proof binding the same failed run is the
# executed record for the ordinary_canonical admission class only; all
# exception classes and the quarantine refusal are unchanged.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

REPO="${GH_REPO:-heydonna-app/heydonna-app}"
PM_TRANSITION="${PM_TRANSITION:-/Users/rajiv/.claude/scripts/pm-transition.sh}"
PM_OPS="${PM_OPS:-/Users/rajiv/.claude/scripts/pm-ops.py}"
CAPTURE_REQUIRED="${CAPTURE_REQUIRED:-/Users/rajiv/.claude/scripts/capture-required.py}"
REMOTE_CAPTURE_RUN_VALIDATOR="${REMOTE_CAPTURE_RUN_VALIDATOR:-/Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/remote-capture-run.py}"
LOCAL_PREFLIGHT_VALIDATOR="${LOCAL_PREFLIGHT_VALIDATOR:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/local-preflight-proof.py}"
PM_CAPACITY_CONFIG="${PM_CAPACITY_CONFIG:-/Users/rajiv/.claude/pm-capacity.env}"
# shellcheck source=/Users/rajiv/.claude/pm-capacity.env
# shellcheck disable=SC1091
[ -f "$PM_CAPACITY_CONFIG" ] && . "$PM_CAPACITY_CONFIG"
MIN_HEAD_AGE_SECONDS="${CI_RERUN_MIN_HEAD_AGE_SECONDS:-300}"

usage() {
  cat >&2 <<'EOF'
Usage:
  rerun-after-local-proof.sh --pr <PR> --run <RUN_ID> [--proof <PROOF_FILE (deprecated/ignored)>] [--rebind-checkout <CHECKOUT>]
  rerun-after-local-proof.sh --nonlocal-recovery --pr <PR> --run <RUN_ID>

--rebind-checkout is the one-shot CTO local-preflight-rebind override
(#7308): the sealed proof is validated against a fresh clean detached
checkout at the exact head/tree while the receipt/log final bytes remain
unchanged. It is a validate-time checkout identity override only.

Proof defaults to:
  /tmp/ci-local-preflight-proof-<PR>-<head>.ok

The proof must name the current headRefOid and contain:
  CI_LOCAL_PREFLIGHT: PASS

Exception statuses PASS_WITH_PREEXISTING_FAILURES / NO_LOCAL_EQUIVALENT require
a follow-up issue in the proof.

The PR must also contain a canonical ci-failure-investigation comment for the
same run attempt and head. Capture-sensitive diffs require a successful,
exact-PR/head remote capture when REMOTE_CAPTURE_ONLY=1; the legacy mode
requires current-head CAPTURE_LOCAL: PASS proof.
EOF
}

die() {
  echo "RERUN_AFTER_LOCAL_PROOF_FAILED reason=$*" >&2
  exit 1
}

die_code() {
  # die with an explicit exit code. Used only for the #7178 flake-quarantine
  # refusal, which must exit 2 (DISTINCT from the generic verdict-miss exit 1).
  local code="$1"
  shift
  echo "RERUN_AFTER_LOCAL_PROOF_FAILED reason=$*" >&2
  exit "$code"
}

json_get() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

data = json.loads(sys.argv[1] or "{}")
value = data
for key in sys.argv[2].split("."):
    if isinstance(value, dict):
        value = value.get(key)
    else:
        value = None
        break
print("" if value is None else value)
PY
}

proof_ok() {
  local proof="$1" head="$2" repo="$3" rebind_checkout="${4:-}"
  [ -f "$LOCAL_PREFLIGHT_VALIDATOR" ] || return 1
  local -a validator_args=(--pr "$pr" --head "$head" --proof "$proof")
  if [ -n "$rebind_checkout" ]; then
    validator_args+=(--rebind-checkout "$rebind_checkout")
  fi
  python3 "$LOCAL_PREFLIGHT_VALIDATOR" validate \
    "${validator_args[@]}" >/dev/null 2>&1
}

capture_proof_ok() {
  local proof="$1" head="$2"
  python3 - "$proof" "$head" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
head = sys.argv[2]
if not path.exists():
    raise SystemExit(1)
text = path.read_text(encoding="utf-8", errors="replace")
if f"headRefOid: {head}" not in text:
    raise SystemExit(1)
if not re.search(r"^CAPTURE_LOCAL:\s*PASS(?:\s|$)", text, re.M):
    raise SystemExit(1)
PY
}

remote_capture_run_ok() {
  local pr="$1" head="$2" runs run_id run_json run_json_file rc=0
  [ -r "$REMOTE_CAPTURE_RUN_VALIDATOR" ] || return 1
  runs="$(
    gh run list \
      --repo "$REPO" \
      --workflow "E2E LLM Proxy Capture (manual)" \
      --limit 50 \
      --json databaseId,displayTitle,status,conclusion,createdAt \
      2>/dev/null
  )" || return 1
  run_id="$(python3 - "$runs" "$pr" "$head" <<'PY'
import json
import sys

try:
    runs = json.loads(sys.argv[1])
except (TypeError, ValueError):
    raise SystemExit(1)
expected = f"remote-capture-pr-{sys.argv[2]}-head-{sys.argv[3]}"
matches = [
    run for run in runs
    if run.get("displayTitle") == expected
    and str(run.get("status") or "").lower() == "completed"
    and str(run.get("conclusion") or "").lower() == "success"
]
if not matches:
    raise SystemExit(1)
matches.sort(key=lambda run: str(run.get("createdAt") or ""), reverse=True)
print(matches[0].get("databaseId") or "")
PY
  )" || return 1
  [[ "$run_id" =~ ^[0-9]+$ ]] || return 1
  run_json="$(
    gh run view "$run_id" \
      --repo "$REPO" \
      --json displayTitle,status,conclusion,attempt,event,url,workflowName,jobs \
      2>/dev/null
  )" || return 1
  [ -n "$run_json" ] || return 1
  run_json_file="$(mktemp -t remote-capture-run.XXXXXX)" || return 1
  printf '%s\n' "$run_json" >"$run_json_file"
  python3 "$REMOTE_CAPTURE_RUN_VALIDATOR" validate \
    --run-json "$run_json_file" \
    --pr "$pr" \
    --head "$head" >/dev/null 2>&1 || rc=$?
  rm -f "$run_json_file"
  return "$rc"
}

verdict_comment_ok() {
  local comments_file="$1" run_id="$2" attempt="$3" head="$4" pr="$5" capture_proof="${6:-}" mode="${7:-local}" class_file="${8:-}" sealed_proof="${9:-}"
  python3 - "$comments_file" "$run_id" "$attempt" "$head" "$pr" "$capture_proof" "$mode" "$class_file" "$sealed_proof" <<'PY'
import json
import re
import sys
from pathlib import Path

comments = json.load(open(sys.argv[1], encoding="utf-8"))
run_id, attempt, head, pr, capture_proof, mode, class_file, sealed_proof = sys.argv[2:]
marker = f"ci-failure-investigation:run={run_id} attempt={attempt} head={head}"
json_re = re.compile(r"<!--\s*ci-verdict:\s*(\{.*?\})\s*-->", re.S | re.I)

def truthy(value):
    return value is True or value == "true"

def exact_tuple(verdict, require_schema3=False):
    if require_schema3 and verdict.get("schema_version") != 3:
        return False
    if str(verdict.get("run_id")) != run_id or str(verdict.get("attempt")) != attempt:
        return False
    if str(verdict.get("pr")) != pr or not truthy(verdict.get("current_for_pr")):
        return False
    candidates = [
        verdict.get("run_head_sha"), verdict.get("current_pr_head_sha"),
        verdict.get("head_sha"), verdict.get("sha"),
    ]
    if not any(str(value or "") == head for value in candidates):
        return False
    for value in candidates:
        if value not in (None, "") and str(value) != head:
            return False
    if "run_attempt" in verdict and str(verdict.get("run_attempt")) != attempt:
        return False
    authorization = verdict.get("rerun_authorization")
    if authorization is not None:
        if not isinstance(authorization, dict):
            return False
        if (
            authorization.get("action") != "rerun-after-proof"
            or str(authorization.get("run_id")) != run_id
            or str(authorization.get("attempt")) != attempt
            or str(authorization.get("head_sha")) != head
            or authorization.get("single_use") is not True
        ):
            return False
    return True

def current_attempt_test_authorization(verdict):
    # The package-owned producer emits schema-3 test verdicts.  Unlike legacy
    # admission classes, this class is only rerunnable when the durable
    # current-attempt binding and single-use authorization are both present.
    # Keep this predicate narrow so legacy classes retain their established
    # guards while an unbound producer verdict cannot authorize a rerun.
    if verdict.get("schema_version") != 3 or verdict.get("classification") != "test":
        return False
    run_attempt = verdict.get("run_attempt")
    if isinstance(run_attempt, bool) or str(run_attempt) != attempt:
        return False
    authorization = verdict.get("rerun_authorization")
    if not isinstance(authorization, dict) or set(authorization) != {
        "action", "run_id", "attempt", "head_sha", "single_use"
    }:
        return False
    return (
        authorization.get("action") == "rerun-after-proof"
        and str(authorization.get("run_id")) == run_id
        and str(authorization.get("attempt")) == attempt
        and authorization.get("head_sha") == head
        and authorization.get("single_use") is True
    )

def no_causal_or_breaker(verdict, fingerprint):
    return verdict.get("causal_fingerprint") in (None, "", {}, []) and not fingerprint.get("circuit_breaker")

def normalized(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

def concurrency_signature(value):
    text = normalized(value)
    return (
        "cancel" in text
        and "higher priority" in text
        and "waiting request" in text
    )

def runner_signature(value):
    text = normalized(value)
    return bool(
        re.search(r"self hosted runner.*lost communication", text)
    )

def pool_stall_signature(value):
    # Pool-stall runner-unavailable class (incident
    # rerun-wrapper-pool-stall-admission-gap, 2026-08-06 runner-pool outage):
    # the self-hosted runner pool stalled and the job was never acquired.
    # Anchored on the full mechanism phrase so unrelated runner text cannot
    # admit the class.
    text = normalized(value)
    return bool(
        re.search(
            r"not acquired by runner of type self hosted even after multiple attempts",
            text,
        )
    )

def only_runner_communication_loss(blockers):
    if isinstance(blockers, list):
        if len(blockers) != 1:
            return False
        blocker = blockers[0]
    elif isinstance(blockers, dict):
        blocker = blockers
    else:
        return False
    if isinstance(blocker, dict):
        if set(blocker) - {"reason", "step"}:
            return False
        text = normalized(blocker.get("reason"))
    else:
        text = normalized(blocker)
    return bool(re.search(r"self hosted runner.*lost communication", text))

def numeric_followup(verdict):
    for key in ("followup_issue", "follow_up_issue", "follow-up", "preexisting_followup", "pre_existing_followup"):
        value = verdict.get(key)
        if value is not None and re.fullmatch(r"#?\d+", str(value).strip()):
            return True
    return False

def trusted_author(comment):
    # Authenticated classification provenance for the CTO-authorized
    # closed-informational rerun class (incident
    # control-plane:ci-rerun-wrapper-closed-informational-infra-class:7303).
    # The verdict comment must be authored by a trusted GitHub association
    # (OWNER/MEMBER/COLLABORATOR) and not minimized. Both the camelCase test
    # shape and the real GitHub REST snake_case keys are accepted so the
    # production replay binds the real comment.
    if not isinstance(comment, dict):
        return False
    association = comment.get("authorAssociation") or comment.get("author_association")
    if str(association or "") not in {"OWNER", "MEMBER", "COLLABORATOR"}:
        return False
    minimized = comment.get("isMinimized", comment.get("minimized", False))
    if minimized is True or str(minimized).lower() == "true":
        return False
    return True

def setup_step_infra_signature(value):
    # Concrete infra fingerprint for the setup-step infra class: the emitted
    # causal evidence must name the recorded Convex preview deploy2/start_push
    # 408 mechanism (recurrence class documented with the #7006/#7033 records).
    # A generic setup-step-failure verdict without this concrete mechanism is
    # not an infra exception and must stay blocked pending local-repro proof.
    text = normalized(value)
    return bool(
        ("convex" in text and "start_push" in text)
        or ("convex" in text and "408" in text)
    )

def setup_step_clerk422_signature(value):
    # Concrete infra fingerprint for the Clerk-422 setup-step class: the
    # emitted causal evidence must name the run-scoped E2E Clerk user
    # provisioning HTTP 422 mechanism (recurrence class documented with the
    # #7082 record). The investigation emits category=unknown /
    # needs_log_fallback fingerprints for this class, so admission matches the
    # mechanism text itself. A generic setup-step-failure verdict without this
    # concrete mechanism is not an infra exception and must stay blocked
    # pending local-repro proof.
    text = normalized(value)
    return bool(
        ("clerk" in text and "422" in text)
        or ("provision" in text and "clerk" in text)
    )

def pending_investigator_record(verdict):
    # Verdict-lifecycle recognition (incident
    # control-plane:repair-dispatch-chain:verdict-lifecycle): the
    # ci-status-investigator is read-only BY DESIGN and its Step 8 emission
    # records the failed run with local_repro_result=pending and the
    # dispatch-local-repro instruction. This mirrors the canonical pending
    # recognition shared with ci-fast-triage.pending_action. Nothing in the
    # investigator or packetizer chain advances this record when PM consumes
    # the sealed preflight proof, so the wrapper must accept the combination
    # of the pending record and the sealed proof as the executed record for
    # the ordinary_canonical class only.
    return (
        str(verdict.get("requested_owner_action") or "") == "dispatch-local-repro"
        and str(verdict.get("local_repro_result") or "") == "pending"
        and (
            str(verdict.get("pm_action_status") or "") == "pending"
            or str(verdict.get("terminal_state") or "") == "pending-pm-action"
        )
    )

def sealed_proof_executes_ordinary(proof_path, run_id, head, pr):
    # The sealed current-head preflight proof (validated at the exact head by
    # the wrapper before this gate) is the executed record for a pending
    # investigator verdict. The envelope must bind the exact PR, the exact
    # head, the exact failed run, and a PASS local-repro result.
    # PASS_WITH_PREEXISTING_FAILURES, NO_LOCAL_EQUIVALENT, and DOCS_ONLY
    # envelopes stay on their own admission classes and are never folded into
    # the ordinary class here.
    if not proof_path:
        return False
    try:
        text = Path(proof_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(
        re.search(r"(?m)^CI_LOCAL_PREFLIGHT:\s*PASS(?:\s|$)", text)
        and re.search(rf"(?m)^PR:\s*{re.escape(str(pr))}\s*$", text)
        and re.search(rf"(?m)^headRefOid:\s*{re.escape(head)}\s*$", text)
        and re.search(rf"(?m)^failed_run:\s*{re.escape(run_id)}\s*$", text)
    )

matching_comments = [
    comment for comment in comments
    if marker in str(comment.get("body") or "")
]
if len(matching_comments) > 1:
    raise SystemExit(1)

for comment in reversed(matching_comments):
    body = str(comment.get("body") or "")
    if marker not in body:
        continue
    match = json_re.search(body)
    if not match:
        continue
    try:
        verdict = json.loads(match.group(1))
    except json.JSONDecodeError:
        continue
    fingerprint = verdict.get("fast_fingerprint") or {}
    if not exact_tuple(verdict, require_schema3=(mode == "nonlocal")) or not isinstance(fingerprint, dict):
        continue
    # #7178 flake quarantine: publish-ci-verdict.py emits quarantine_blocked=true
    # when the same CI-only flake fingerprint (local_repro_result=passed)
    # recurred within the rolling 7-day window with no open follow-up issue. A
    # quarantined run must NOT rerun until a fix or quarantine lands. Exit 2 is
    # the DISTINCT quarantine refusal (the wrapper preserves rc and dies with
    # reason=quarantine_blocked_ci_flake via die_code 2, so the wrapper itself
    # exits 2); exit 1 stays the generic verdict-miss. The check sits after the
    # exact-tuple guard, so a quarantined verdict for a DIFFERENT run never
    # refuses this run.
    if truthy(verdict.get("quarantine_blocked")):
        raise SystemExit(2)
    common_grey = (
        verdict.get("verdict") == "GREY"
        and verdict.get("severity") == "GREY"
        and verdict.get("local_repro_result") == "not-applicable"
        and no_causal_or_breaker(verdict, fingerprint)
    )
    concurrency_cancel_non_local = (
        common_grey
        and verdict.get("classification") == "concurrency-cancel"
        and fingerprint.get("category") == "concurrency-cancel"
        and verdict.get("blocking_for_merge") in (False, "false")
        and verdict.get("required_check_failure") in (False, "false")
        and not (verdict.get("terminal_blockers") or [])
        and concurrency_signature(fingerprint.get("signature"))
        and (
            verdict.get("requested_owner_action") == "rerun-after-proof"
            or (
                mode == "nonlocal"
                and "requested_owner_action" not in verdict
                and "no state mutation" in normalized(verdict.get("requested_pr_state"))
                and "typed re arm" in normalized(verdict.get("requested_pr_state"))
            )
        )
    )
    runner_death = (
        common_grey
        and verdict.get("classification") == "runner-death-mid-step"
        and str(verdict.get("attempt")) == "1"
        and verdict.get("requested_owner_action") == "rerun-after-proof"
        and runner_signature(fingerprint.get("signature"))
        and only_runner_communication_loss(verdict.get("terminal_blockers"))
        and numeric_followup(verdict)
        and verdict.get("blocking_for_merge") is True
        and verdict.get("required_check_failure") is True
    )
    # Pool-stall runner-unavailable class (incident
    # rerun-wrapper-pool-stall-admission-gap): the ci-status-investigator
    # emitted classification=runner-death-mid-step GREY verdicts for jobs that
    # were never acquired by the stalled self-hosted runner pool
    # (fast_fingerprint.category runner-unavailable / environment-contract,
    # signature "The job was not acquired by Runner of type self-hosted even
    # after multiple attempts", terminal_blockers [] or a not-acquired reason,
    # requested_owner_action rerun-after-proof, numeric followup). Mirrors
    # runner_death: common_grey, classification, attempt 1, rerun-after-proof,
    # numeric followup, blocking_for_merge, required_check_failure, no circuit
    # breaker. The lost-communication runner_death admission above stays
    # unchanged; the pool-stall class does not require the
    # only_runner_communication_loss terminal-blocker shape because the
    # pool-stall verdicts carry [] or a not-acquired blocker instead.
    pool_stall_runner_unavailable = (
        common_grey
        and verdict.get("classification") == "runner-death-mid-step"
        and str(verdict.get("attempt")) == "1"
        and verdict.get("requested_owner_action") == "rerun-after-proof"
        and (
            fingerprint.get("category") in {"runner-unavailable", "environment-contract"}
            or pool_stall_signature(fingerprint.get("signature"))
        )
        and numeric_followup(verdict)
        and verdict.get("blocking_for_merge") is True
        and verdict.get("required_check_failure") is True
    )
    post_test_tail_all_green = (
        common_grey
        and verdict.get("classification") == "post-test-tail-cancel"
        and verdict.get("cancellation_phase") == "post-test-tail"
        and verdict.get("blocking_for_merge") in (False, "false")
        and verdict.get("required_check_failure") in (False, "false")
        and not (verdict.get("terminal_blockers") or [])
        and verdict.get("requested_owner_action") == "rerun-after-proof"
    )
    apt_lock_grey = (
        verdict.get("verdict") == "GREY"
        and verdict.get("severity") == "GREY"
        and verdict.get("classification") == "setup-step-failure"
        and fingerprint.get("category") == "setup-step-failure-apt-lock"
        and verdict.get("local_repro_result") == "not-applicable"
        and verdict.get("requested_owner_action") == "rerun-after-proof"
        and verdict.get("blocking_for_merge") is True
        and str(verdict.get("attempt")) == "1"
        and not fingerprint.get("circuit_breaker")
    )
    ordinary_canonical = (
        exact_tuple(verdict)
        and verdict.get("requested_owner_action") == "rerun-after-proof"
        and verdict.get("local_repro_result") in {"passed", "skipped", "impossible"}
        and not fingerprint.get("circuit_breaker")
        and (
            not (
                verdict.get("schema_version") == 3
                and verdict.get("classification") == "test"
            )
            or current_attempt_test_authorization(verdict)
        )
    )
    # Verdict-lifecycle admission (incident
    # control-plane:repair-dispatch-chain:verdict-lifecycle): a pending
    # investigator record at the exact tuple, superseded by a sealed
    # current-head PASS envelope binding the same failed run, is admitted as
    # the executed ordinary_canonical record. Schema-3 is required, the exact
    # tuple and current_for_pr checks are unchanged, the circuit breaker still
    # refuses, and a quarantined verdict still exits 2 before this check. All
    # exception classes keep their own (unchanged) guards; a pending record
    # can never satisfy them, so no exception-class record can take this path.
    sealed_proof_executed = (
        exact_tuple(verdict)
        and verdict.get("schema_version") == 3
        and pending_investigator_record(verdict)
        and not fingerprint.get("circuit_breaker")
        and sealed_proof_executes_ordinary(sealed_proof, run_id, head, pr)
    )
    # Dedicated fail-closed guard for the setup-step infra class. Admitted ONLY
    # when ALL THREE hold: (a) a concrete infra fingerprint naming the recorded
    # Convex start_push 408 mechanism in the setup phase, (b)
    # local_repro_result=impossible, and (c) a numeric follow-up issue. A
    # generic setup-step-failure verdict without any one of these stays on the
    # ordinary class and remains blocked for E2E pending canonical local-repro
    # proof.
    setup_step_infra_grey = (
        verdict.get("verdict") == "GREY"
        and verdict.get("severity") == "GREY"
        and verdict.get("classification") == "setup-step-failure"
        and verdict.get("cancellation_phase") == "setup"
        and setup_step_infra_signature(
            fingerprint.get("causal_signature")
            or fingerprint.get("signature")
            or verdict.get("causal_fingerprint")
            or ""
        )
        and verdict.get("local_repro_result") == "impossible"
        and verdict.get("requested_owner_action") == "rerun-after-proof"
        and verdict.get("blocking_for_merge") is True
        and str(verdict.get("attempt")) == "1"
        and numeric_followup(verdict)
        and not fingerprint.get("circuit_breaker")
    )
    # Dedicated fail-closed guard for the Clerk-422 setup-step class (incident
    # rerun-wrapper-clerk422-class-admission-20260805): a real fleet transient
    # where the run-scoped E2E Clerk user provisioning step exited on Clerk
    # Backend API HTTP 422 before any product test ran. Admitted ONLY when ALL
    # hold: GREY verdict/severity, local_repro_result=not-applicable, the
    # concrete clerk/422 mechanism text in the emitted fingerprint,
    # requested_owner_action=rerun-after-proof, a numeric follow-up issue,
    # blocking_for_merge=true, required_check_failure=true, and no circuit
    # breaker. The investigation emits category=unknown/needs_log_fallback
    # fingerprints for this class and records the mechanism in
    # fast_fingerprint.signature/causal_signature as well as the top-level
    # causal_fingerprint, so the mechanism is matched by signature text,
    # mirroring setup_step_infra_signature. A generic setup-step-failure
    # verdict without the clerk-422 mechanism stays blocked pending local-repro
    # proof.
    setup_step_clerk422_grey = (
        verdict.get("verdict") == "GREY"
        and verdict.get("severity") == "GREY"
        and verdict.get("classification") == "setup-step-failure"
        and verdict.get("local_repro_result") == "not-applicable"
        and setup_step_clerk422_signature(
            fingerprint.get("causal_signature")
            or fingerprint.get("signature")
            or verdict.get("causal_fingerprint")
            or ""
        )
        and verdict.get("requested_owner_action") == "rerun-after-proof"
        and numeric_followup(verdict)
        and verdict.get("blocking_for_merge") is True
        and verdict.get("required_check_failure") is True
        and not fingerprint.get("circuit_breaker")
    )
    # YELLOW base-attributed rearm: the ci-status-investigator classifies an
    # E2E failure as non-product/base-attributed (zero diff overlap, main
    # recurrence, same-code local PASS) and the CTO sanctions a same-head
    # rearm. The verdict records local_repro_result=impossible with a
    # follow-up issue, and requested_owner_action=sanctioned-canonical-rearm.
    base_attributed_rearm = (
        exact_tuple(verdict)
        and verdict.get("verdict") == "YELLOW"
        and verdict.get("classification") in {
            "e2e-test-fail", "timeout-or-wall-budget", "save-contract-fail-stop",
        }
        and verdict.get("local_repro_result") in {"impossible", "not-applicable"}
        and verdict.get("requested_owner_action") == "sanctioned-canonical-rearm"
        and verdict.get("blocking_for_merge") is True
        and str(verdict.get("attempt")) == "1"
        and numeric_followup(verdict)
        and not fingerprint.get("circuit_breaker")
    )
    # CTO-authorized rerun of a sealed non-causal closed-informational class
    # (incident
    # control-plane:ci-rerun-wrapper-closed-informational-infra-class:7303):
    # the ci-status-investigator sealed a pre-existing systemic
    # infra/test-flake classification (disposition no-rerun-required,
    # terminal_state closed-informational) and the CTO explicitly authorized
    # exactly ONE proof-gated rerun of the exact failed required run
    # (rerunnable=true, requested_owner_action=proof-authorized-rerun,
    # rerun_authorization binding the CTO decision). The wrapper consumes the
    # class only when every bound holds: schema-3 exact tuple, trusted comment
    # author (typed classification provenance), no circuit breaker, required
    # failure with blocking_for_merge, first attempt, and a non-empty CTO
    # one-rerun authorization. Product-regression verdicts (classification
    # outside the non-causal family set), missing authorization, second
    # retry, stale head, unknown class, and altered proof all stay fail-closed
    # through the unchanged outer wrapper gates (proof validation, live
    # run/attempt binding, workflow allowlist, budget, lease).
    cto_authorized_informational_rerun = (
        exact_tuple(verdict, require_schema3=True)
        and verdict.get("terminal_state") == "closed-informational"
        and verdict.get("disposition") == "no-rerun-required"
        and verdict.get("rerunnable") is True
        and verdict.get("requested_owner_action") == "proof-authorized-rerun"
        and verdict.get("classification") in {
            "e2e-test-fail", "timeout-or-wall-budget", "save-contract-fail-stop",
        }
        and isinstance(verdict.get("rerun_authorization"), str)
        and len(str(verdict.get("rerun_authorization") or "")) >= 20
        and re.search(r"\bCTO\b", str(verdict.get("rerun_authorization") or ""), re.I)
        and re.search(
            r"\b(?:exactly[ _-]?one|one[ _-]?time|single|ONE)\b",
            str(verdict.get("rerun_authorization") or ""),
            re.I,
        )
        and verdict.get("blocking_for_merge") is True
        and verdict.get("required_check_failure") is True
        and str(verdict.get("attempt")) == "1"
        and not fingerprint.get("circuit_breaker")
        and no_causal_or_breaker(verdict, fingerprint)
        and trusted_author(comment)
    )
    accepted_class = concurrency_cancel_non_local or (mode == "nonlocal" and runner_death) or pool_stall_runner_unavailable
    if mode != "nonlocal" and (ordinary_canonical or sealed_proof_executed or post_test_tail_all_green or apt_lock_grey or base_attributed_rearm or cto_authorized_informational_rerun or setup_step_infra_grey or setup_step_clerk422_grey):
        accepted_class = True
    if accepted_class:
        if class_file:
            # Map the exact admitted verdict classes into the sealed classes
            # admitted by local-preflight-proof.py. The setup-step infra and
            # clerk-422 classes are emitted ONLY from the dedicated guards
            # above; the CTO-authorized closed-informational class gets its
            # own typed token so the rerun envelope carries the sealed class
            # provenance end to end.
            if apt_lock_grey:
                class_token = "setup-step-failure-apt-lock"
            elif base_attributed_rearm:
                class_token = "wrapper-rerun-base-attributed"
            elif cto_authorized_informational_rerun:
                class_token = "closed-informational-cto-rerun"
            elif setup_step_infra_grey:
                class_token = "setup-step-failure-infra"
            elif setup_step_clerk422_grey:
                class_token = "setup-step-failure-clerk-422"
            else:
                class_token = str(verdict.get("classification"))
            Path(class_file).write_text(class_token, encoding="utf-8")
        raise SystemExit(0)

    if mode == "nonlocal":
        continue

    # A fixture-miss verdict is necessarily written before capture. Permit the
    # later exact-head capture proof to supersede only that narrow stale premise:
    # same run/head, terminal CAPTURE_LOCAL PASS, failed run bound into the
    # proof, verified fixture presence, and an original verdict that explicitly
    # required capture before rerun. Other verdict classes still require the
    # canonical rerun-after-proof disposition above.
    if not capture_proof:
        continue
    try:
        proof = Path(capture_proof).read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    exact_verdict = (
        exact_tuple(verdict)
        and not fingerprint.get("circuit_breaker")
    )
    exact_capture = all(
        re.search(pattern, proof, re.M | re.I)
        for pattern in (
            r"^CAPTURE_LOCAL:\s*PASS(?:\s|$)",
            rf"^pr:\s*{re.escape(pr)}\s*$",
            rf"^headRefOid:\s*{re.escape(head)}\s*$",
            rf"^failed_remote_run:\s*{re.escape(run_id)}\s*$",
            r"^fixture_verification_status:\s*pass\s*$",
            r"^label_gated_ci_allowed_after_local_capture:\s*true\s*$",
            r"^current_head_status:\s*exact_pr_head\s*$",
        )
    )
    verdict_blob = json.dumps(verdict, sort_keys=True).lower()
    action_blob = " ".join(
        str(verdict.get(key) or "")
        for key in ("required_action", "requested_owner_action", "recommended_next_action", "action")
    ).lower()
    fixture_miss = "fixture_miss" in verdict_blob or "fixture miss" in verdict_blob
    capture_before_rerun = "captur" in action_blob and "rerun" in action_blob
    if exact_verdict and exact_capture and fixture_miss and capture_before_rerun:
        raise SystemExit(0)
raise SystemExit(1)
PY
}

# CI run-count, churn, and spend budgets are retired from this shared rerun
# consumer. Exact-head, causal-classification, stale-head, active-duplicate,
# cleanup, and single-flight fences below remain the safety controls.

pr=""
run_id=""
proof=""
rebind_checkout=""
mode="local"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --nonlocal-recovery) mode="nonlocal"; shift ;;
    --pr) pr="${2:-}"; shift 2 ;;
    --run|--run-id) run_id="${2:-}"; shift 2 ;;
    --proof) proof="${2:-}"; shift 2 ;;
    --rebind-checkout) rebind_checkout="${2:-}"; shift 2 ;;
    --failed-only|--failed) die "failed_only_rerun_banned use_whole_workflow_rerun" ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown_arg=$1" ;;
  esac
done

[ "$mode" = "nonlocal" ] && [ -z "$proof" ] \
  || [ "$mode" != "nonlocal" ] \
  || die "nonlocal_recovery_rejects_hand_authored_proof"

[[ "$pr" =~ ^[0-9]+$ ]] || die "--pr must be numeric"
[[ "$run_id" =~ ^[0-9]+$ ]] || die "--run must be numeric"
[ -x "$PM_TRANSITION" ] || die "pm_transition_missing path=$PM_TRANSITION"

pr_json="$(gh pr view "$pr" --repo "$REPO" --json headRefOid,headRefName 2>/dev/null || true)"
head="$(json_get "$pr_json" headRefOid)"
branch="$(json_get "$pr_json" headRefName)"
[ -n "$head" ] || die "cannot_read_pr_head pr=$pr"
[ -n "$branch" ] || die "cannot_read_pr_branch pr=$pr"

slot_owner="$(gh pr view "$pr" --repo "$REPO" --json labels --jq '[.labels[].name | select(startswith("slot:"))] | join(",")' 2>/dev/null || true)"
[ -z "$slot_owner" ] || die "pr_still_slot_owned pr=$pr owner=$slot_owner release_before_rerun"

head_epoch="$(gh api "repos/${REPO}/commits/${head}" --jq '.commit.committer.date' 2>/dev/null | python3 -c 'import datetime,sys; s=sys.stdin.read().strip(); print(int(datetime.datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()) if s else 0)' 2>/dev/null || echo 0)"
now_epoch="$(date +%s)"
[[ "$head_epoch" =~ ^[0-9]+$ ]] || head_epoch=0
[ "$head_epoch" -gt 0 ] || die "cannot_read_head_age pr=$pr head=$head"
[ $((now_epoch - head_epoch)) -ge "$MIN_HEAD_AGE_SECONDS" ] || die "head_not_stable pr=$pr head=$head min_age_s=$MIN_HEAD_AGE_SECONDS"

if [ -z "$proof" ]; then
  candidates=(
    "/tmp/ci-local-preflight-proof-${pr}-${head}.ok"
    "/tmp/ci-local-preflight-proof-${pr}-${head:0:8}.ok"
  )
  if [ "$mode" = "nonlocal" ]; then
    candidates=(
      "/tmp/affected-test-proof-${pr}-${head}.ok"
      "/tmp/affected-test-proof-${pr}-${head:0:8}.ok"
      "${candidates[@]}"
    )
  fi
  for candidate in "${candidates[@]}"; do
    if [ -f "$candidate" ]; then
      proof="$candidate"
      break
    fi
  done
fi

# Sealed local-preflight envelopes are retired (Rajiv 1786812200.371389):
# the wrapper's raw verdict classification is authoritative for the at-most-
# one same-head infra/flake/shared retry. When a proof file exists it is
# still validated and carried as an optional diagnostic.
if [ -n "$proof" ]; then
  proof_ok "$proof" "$head" "$REPO" "$rebind_checkout" \
    || die "invalid_or_stale_or_unbound_local_proof proof=$proof head=$head"
fi

run_json="$(gh api "repos/${REPO}/actions/runs/${run_id}" 2>/dev/null || true)"
run_head="$(json_get "$run_json" headSha)"
if [ -z "$run_head" ]; then run_head="$(json_get "$run_json" head_sha)"; fi
run_status="$(json_get "$run_json" status)"
run_conclusion="$(json_get "$run_json" conclusion)"
workflow="$(json_get "$run_json" name)"
run_url="$(json_get "$run_json" html_url)"
run_attempt="$(json_get "$run_json" run_attempt)"
[[ "$run_attempt" =~ ^[1-9][0-9]*$ ]] \
  || die "run_attempt_missing_or_malformed run=$run_id"

[ "$run_head" = "$head" ] || die "run_head_mismatch run=$run_id run_head=${run_head:-unknown} pr_head=$head"
[ "$run_status" = "completed" ] || die "run_not_terminal run=$run_id status=${run_status:-unknown}"
case "$run_conclusion" in
  success|skipped|"") die "run_not_bad_terminal run=$run_id conclusion=${run_conclusion:-unknown}" ;;
esac
case "$workflow" in
  CI|"E2E Smoke Tests") ;;
  *) die "workflow_not_label_gated_ci run=$run_id workflow=${workflow:-unknown}" ;;
esac

comments_file="$(mktemp -t ci-rerun-comments.XXXXXX)"
class_file="$(mktemp -t ci-rerun-class.XXXXXX)"
trap 'rm -f "$comments_file" "$class_file"; rmdir "${lease_dir:-}" 2>/dev/null || true' EXIT
gh api "repos/${REPO}/issues/${pr}/comments?per_page=100" >"$comments_file" 2>/dev/null \
  || die "cannot_read_pr_verdict_comments pr=$pr"
capture_proof=""
for candidate in \
  "/tmp/capture-local-proof-${pr}-${head}.ok" \
  "/tmp/capture-local-proof-${pr}-${head:0:8}.ok"; do
  if [ -f "$candidate" ]; then capture_proof="$candidate"; break; fi
done
# Capture verdict_comment_ok's OWN exit status before any shell inversion. The
# `!` negation would rewrite $? to 0 inside the branch and lose the distinct
# rc=2 quarantine signal (Codex BLOCK 8ea59207, FUNCTIONAL_BLOCK
# property=quarantine_refusal_state): rc=2 -> quarantine refusal, exit 2 with
# reason=quarantine_blocked_ci_flake; rc=1 -> generic verdict-miss refusal,
# exit 1.
if verdict_comment_ok "$comments_file" "$run_id" "$run_attempt" "$head" "$pr" "$capture_proof" "$mode" "$class_file" "$proof"; then
  :
else
  rc=$?
  if [ "$rc" -eq 2 ]; then
    die_code 2 "quarantine_blocked_ci_flake pr=$pr run=$run_id attempt=$run_attempt head=$head until_fix_or_quarantine_lands"
  fi
  die "canonical_current_attempt_verdict_missing_or_not_rerunnable pr=$pr run=$run_id attempt=$run_attempt head=$head"
fi

ci_class="wrapper-rerun"
if [ "$mode" = "nonlocal" ]; then
  ci_class="$(cat "$class_file" 2>/dev/null || true)"
  case "$ci_class" in
    concurrency-cancel) ci_class="wrapper-rerun-concurrency-cancel" ;;
    runner-death-mid-step) ci_class="wrapper-rerun-runner-death-mid-step" ;;
    *) die "nonlocal_recovery_unknown_verdict_class class=${ci_class:-unknown}" ;;
  esac
  [ -f "$LOCAL_PREFLIGHT_VALIDATOR" ] || die "local_preflight_validator_missing path=$LOCAL_PREFLIGHT_VALIDATOR"
  nonlocal_validator_args=(--pr "$pr" --head "$head" --proof "$proof" --workflow "$workflow" --ci-class "$ci_class")
  if [ -n "$rebind_checkout" ]; then
    nonlocal_validator_args+=(--rebind-checkout "$rebind_checkout")
  fi
  python3 "$LOCAL_PREFLIGHT_VALIDATOR" validate \
    "${nonlocal_validator_args[@]}" >/dev/null 2>&1 \
    || die "nonlocal_recovery_proof_kind_not_allowed workflow=$workflow class=$ci_class proof=$proof"
elif [ -f "$class_file" ] && [ "$(cat "$class_file" 2>/dev/null || true)" = "setup-step-failure-apt-lock" ]; then
  ci_class="wrapper-rerun-setup-step-apt-lock"
elif [ -f "$class_file" ] && [ "$(cat "$class_file" 2>/dev/null || true)" = "setup-step-failure-infra" ]; then
  # Infra setup-step failure (e.g. Convex preview start_push 408 recurrence,
  # E2E setup step timed out before any product test). The dedicated guard in
  # verdict_comment_ok emits this token ONLY when ALL THREE hold: the concrete
  # infra fingerprint, local_repro_result=impossible, and a numeric follow-up
  # issue. A generic setup-step-failure verdict keeps the ordinary
  # "setup-step-failure" token, is never mapped here, and stays blocked for
  # E2E pending canonical local-repro proof.
  ci_class="wrapper-rerun-setup-step-infra"
elif [ -f "$class_file" ] && [ "$(cat "$class_file" 2>/dev/null || true)" = "setup-step-failure-clerk-422" ]; then
  # Clerk-422 run-scoped E2E user provisioning transient (recurrence class
  # documented with #7082; E2E setup step exited on Clerk Backend API HTTP 422
  # before any product test). The dedicated guard in verdict_comment_ok emits
  # this token ONLY when the concrete clerk/422 mechanism text is present with
  # the full GREY not-applicable conjunction and a numeric follow-up issue. A
  # generic setup-step-failure verdict keeps the ordinary
  # "setup-step-failure" token, is never mapped here, and stays blocked for
  # E2E pending canonical local-repro proof.
  ci_class="wrapper-rerun-setup-step-clerk-422"
elif [ -f "$class_file" ] && [ "$(cat "$class_file" 2>/dev/null || true)" = "wrapper-rerun-base-attributed" ]; then
  # YELLOW base-attributed rearm: the sanctioned canonical rearm already wrote
  # the sealed class token. Preserve it as ci_class so ci-local-preflight-pass
  # seals the envelope with the class the validator admits for E2E and the
  # same-head rerun proceeds.
  ci_class="wrapper-rerun-base-attributed"
elif [ -f "$class_file" ] && [ "$(cat "$class_file" 2>/dev/null || true)" = "closed-informational-cto-rerun" ]; then
  # CTO-authorized closed-informational rerun (incident
  # control-plane:ci-rerun-wrapper-closed-informational-infra-class:7303): the
  # dedicated admission guard wrote the sealed class token. Carry it through
  # ci-local-preflight-pass so the sealed envelope records the typed class
  # provenance and the validator admits the E2E affected-test-plan source.
  ci_class="wrapper-rerun-closed-informational-cto"
fi

if [ -x "$CAPTURE_REQUIRED" ]; then
  set +e
  capture_json="$(python3 "$CAPTURE_REQUIRED" --pr "$pr" --repo "$REPO" --json 2>/dev/null)"
  capture_rc=$?
  set -e
  [ "$capture_rc" -ne 2 ] || die "capture_requirement_classification_failed pr=$pr"
  if [ "$capture_rc" -eq 0 ]; then
    case "${REMOTE_CAPTURE_ONLY:-0}" in
      1|true|enabled)
        remote_capture_run_ok "$pr" "$head" \
          || die "capture_remote_proof_missing_or_invalid pr=$pr head=$head classifier=$capture_json"
        ;;
      *)
        [ -n "$capture_proof" ] || die "capture_local_proof_missing pr=$pr head=$head classifier=$capture_json"
        capture_proof_ok "$capture_proof" "$head" \
          || die "capture_local_proof_invalid_or_stale pr=$pr proof=$capture_proof head=$head"
        ;;
    esac
  fi
fi

running="$(
  gh run list --repo "$REPO" --branch "$branch" --limit 30 \
    --json databaseId,headSha,status,workflowName \
    --jq ".[] | select(.headSha == \"$head\" and (.workflowName == \"CI\" or .workflowName == \"E2E Smoke Tests\") and .status == \"in_progress\") | .databaseId" \
    2>/dev/null | head -n 1
)"
[ -z "$running" ] || die "current_head_ci_already_running pr=$pr head=${head:0:10} run=$running"

lease_dir="/tmp/ci-rerun-lease-${pr}-${head}"
mkdir "$lease_dir" 2>/dev/null || die "rerun_already_claimed pr=$pr head=$head"

# Final live-head pin BEFORE the transition side effect (incident
# review-cap-cb-moving-head-ci, Behavior 2: stop-ci-reruns-on-moving-head).
# Every run binding above was verified against the head read at entry; if
# the head moved since, refuse BEFORE ci-local-preflight-pass records the
# classification against a stale head. The post-transition re-check below
# stays as the second line of defense.
latest_head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq .headRefOid 2>/dev/null || true)"
[ "$latest_head" = "$head" ] || die "head_moved_before_transition pr=$pr expected=$head actual=${latest_head:-unknown}"

preflight_pass_args=(
  --pr "$pr"
  --proof "$proof"
  --failed-run "$run_id"
  --ci-class "$ci_class"
)
if [ -n "$rebind_checkout" ]; then
  preflight_pass_args+=(--rebind-checkout "$rebind_checkout")
fi
"$PM_TRANSITION" ci-local-preflight-pass "${preflight_pass_args[@]}"

latest_head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq .headRefOid 2>/dev/null || true)"
[ "$latest_head" = "$head" ] || die "head_moved_after_transition pr=$pr expected=$head actual=${latest_head:-unknown}"
latest_owner="$(gh pr view "$pr" --repo "$REPO" --json labels --jq '[.labels[].name | select(startswith("slot:"))] | join(",")' 2>/dev/null || true)"
[ -z "$latest_owner" ] || die "pr_reowned_before_rerun pr=$pr owner=$latest_owner"

args=(run rerun "$run_id" --repo "$REPO")

CI_LOCAL_PREFLIGHT_PASSED=1 CI_LOCAL_PREFLIGHT_LOG="$proof" gh "${args[@]}"

if [ -x "$PM_OPS" ]; then
  python3 "$PM_OPS" record \
    --source rerun-after-local-proof \
    --event ci_requested \
    --target-type pr \
    --target-id "$pr" \
    --pr "$pr" \
    --head-sha "$head" \
    --payload "mode=rerun" \
    --payload "failed_run=$run_id" \
    --payload "workflow=$workflow" \
    --payload "proof=$proof" \
    --dedupe-key "ci_requested:${pr}:${head}:rerun:${run_id}" \
    >/dev/null
fi

echo "RERUN_AFTER_LOCAL_PROOF_OK pr=$pr run=$run_id workflow=$workflow head=${head:0:10} proof=$proof url=${run_url:-unknown}"
