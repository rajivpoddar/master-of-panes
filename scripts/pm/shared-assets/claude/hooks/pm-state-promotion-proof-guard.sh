#!/usr/bin/env bash
# PreToolUse Bash hook: block high-trust PM state promotions without explicit
# review/readiness provenance.
#
# Scope is intentionally narrow:
#   pm-state-replace.sh <PR> qa-passed-awaiting-ci
#   pm-state-replace.sh <PR> merge-ready
#
# Demotions, cleanup states, and blocked-rework transitions are never blocked.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

INPUT=$(cat 2>/dev/null || echo "{}")
CMD=$(JSON_INPUT="$INPUT" python3 -c 'import json, os; d=json.loads(os.environ.get("JSON_INPUT", "{}") or "{}"); print(((d.get("tool_input") or {}).get("command")) or "")' 2>/dev/null || true)
[ -n "$CMD" ] || exit 0

emit_block() {
  local message="$1"
  MESSAGE="$message" python3 - <<'PYEOF'
import json
import os

message = os.environ.get("MESSAGE", "")
print(json.dumps({
    "decision": "block",
    "message": message,
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": message,
    },
}))
PYEOF
}

# The canonical proof is a promotion input, not a scratch file. Raw shell
# writes can destroy a previously valid proof before the authoritative
# current-head mutation gets a chance to reject the new candidate. Require the
# operator's merge-ready boundary for canonical-path writes; callers can still
# build an attempt-scoped content file.
PROMOTION_PROOF_PATH_RE='/tmp/pm-state-promotion-proof-[0-9]+-(qa-passed-awaiting-ci|merge-ready)\.ok'
if printf '%s' "$CMD" | grep -qE "$PROMOTION_PROOF_PATH_RE" && \
   printf '%s' "$CMD" | grep -qE 'printf[^;|&>]*>|cat[^;|&>]*>|tee[[:space:]]+|python3[^;|&>]*write_text'; then
  if ! printf '%s' "$CMD" | grep -qE '/(pm-transition|pm-transition\.sh)[[:space:]]+write-promotion-proof([[:space:]]|$)'; then
    emit_block "BLOCKED: raw writes to canonical promotion proofs are disabled; write an attempt-scoped content file, then run /Users/rajiv/.claude/scripts/pm-state-replace.sh <PR> merge-ready. The transition validates the live head and atomically applies the guarded promotion."
    exit 0
  fi
fi

if ! printf '%s' "$CMD" | grep -qE 'pm-state-replace\.sh[[:space:]]+[0-9]+[[:space:]]+(qa-passed-awaiting-ci|merge-ready)($|[[:space:];&|])'; then
  exit 0
fi

PR=$(printf '%s' "$CMD" | sed -nE 's/.*pm-state-replace\.sh[[:space:]]+([0-9]+)[[:space:]]+(qa-passed-awaiting-ci|merge-ready)($|[[:space:];&|]).*/\1/p' | head -1)
STATE=$(printf '%s' "$CMD" | sed -nE 's/.*pm-state-replace\.sh[[:space:]]+[0-9]+[[:space:]]+(qa-passed-awaiting-ci|merge-ready)($|[[:space:];&|]).*/\1/p' | head -1)
[ -n "$PR" ] && [ -n "$STATE" ] || exit 0

# File proof path used by bg-agent/readiness flows.
PROOF_FILE="/tmp/pm-state-promotion-proof-${PR}-${STATE}.ok"
CI_GUARD="/Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/pre-merge-current-head-ci-guard.sh"
CI_GUARD_LOG="/tmp/pm-state-promotion-ci-guard-${PR}.log"

current_head() {
  gh pr view "$1" --repo heydonna-app/heydonna-app --json headRefOid --jq '.headRefOid' 2>/dev/null || true
}

current_is_draft() {
  gh pr view "$1" --repo heydonna-app/heydonna-app --json isDraft --jq '.isDraft' 2>/dev/null || true
}

if [ "$STATE" = "qa-passed-awaiting-ci" ] && [ "$(current_is_draft "$PR")" = "true" ]; then
  emit_block "BLOCKED: PR #${PR} is still draft. pm-state:qa-passed-awaiting-ci starts label-gated CI/E2E, so the PR must be marked ready first. Use the authoritative slot-ready or pm-review-done path, which runs gh pr ready before applying the CI-trigger label."
  exit 0
fi

# Explicit, auditable override. This is allowed for urgent PM repair, but must
# be visible in the shell command. It cannot bypass the draft-before-CI guard
# above.
if printf '%s' "$CMD" | grep -qE 'PM_STATE_PROMOTION_PROOF=(READY_PACKET_PASS|CURRENT_HEAD_REVIEW_OK|PM_OVERRIDE_WITH_PROOF)'; then
  exit 0
fi

proof_head_matches_current() {
  local proof="$1" pr="$2" current proof_head
  current="$(current_head "$pr")"
  [ -n "$current" ] || return 1
  proof_head="$(python3 - "$proof" <<'PYEOF' 2>/dev/null || true
import re
import sys

text = open(sys.argv[1], errors="ignore").read()
for pattern in (r"(?im)^headRefOid:\s*([0-9a-f]{40})\s*$", r"(?im)^head_sha:\s*([0-9a-f]{40})\s*$"):
    m = re.search(pattern, text)
    if m:
        print(m.group(1))
        break
PYEOF
)"
  [ "$proof_head" = "$current" ]
}

latest_named_ci_failure_requirement() {
  local pr="$1"
  python3 - "$pr" <<'PYEOF' 2>/dev/null || true
import json
import re
import subprocess
import sys

proc = subprocess.run(
    ["gh", "pr", "view", sys.argv[1], "--repo", "heydonna-app/heydonna-app", "--json", "comments"],
    text=True,
    capture_output=True,
    check=False,
)
if proc.returncode:
    print("BLOCK\tunknown\tunable-to-read-ci-verdict")
    raise SystemExit

comments = (json.loads(proc.stdout or "{}").get("comments") or [])
for comment in reversed(comments):
    markers = re.findall(r"<!-- ci-verdict:\s*(\{.*?\})\s*-->", str(comment.get("body") or ""))
    for raw in reversed(markers):
        try:
            verdict = json.loads(raw)
        except json.JSONDecodeError:
            continue
        failure = verdict.get("first_real_failure") or {}
        spec = str(failure.get("spec") or "").strip()
        test_name = str(failure.get("test_name") or "").strip()
        named = failure.get("available") in (True, "true") and bool(spec or test_name)
        if not named or verdict.get("current_for_pr") not in (True, "true"):
            continue
        run_id = str(verdict.get("run_id") or "unknown")
        result = str(verdict.get("local_repro_result") or "pending").strip()
        if not spec or result in {"pending", "skipped", "failed-different-error"}:
            print(f"BLOCK\t{run_id}\t{spec or test_name}")
        elif result in {"passed", "failed-same-error"}:
            print(f"REQUIRE\t{run_id}\t{spec}")
        else:
            print(f"BLOCK\t{run_id}\t{spec}")
        raise SystemExit
print("NONE\t\t")
PYEOF
}

proof_matches_ci_failure_requirement() {
  local proof="$1" requirement disposition run_id failed_target
  requirement="$(latest_named_ci_failure_requirement "$PR")"
  IFS=$'\t' read -r disposition run_id failed_target <<< "$requirement"
  case "$disposition" in
    NONE) return 0 ;;
    BLOCK|"") return 1 ;;
    REQUIRE)
      grep -Fqx "source_ci_run: ${run_id}" "$proof" 2>/dev/null || return 1
      grep -Fqx "failed_target: ${failed_target}" "$proof" 2>/dev/null || return 1
      return 0
      ;;
    *) return 1 ;;
  esac
}

affected_test_candidate_ok() {
  local candidate="$1" current="$2"
  [ -f "$candidate" ] &&
    grep -qE '^AFFECTED_TESTS:[[:space:]]*(PASS|PASS_WITH_PREEXISTING_FAILURES|DOCS_ONLY|TARGETED_CI_PASS|NO_LOCAL_EQUIVALENT)($|[[:space:]])' "$candidate" 2>/dev/null &&
    grep -q "headRefOid: ${current}" "$candidate" 2>/dev/null &&
    grep -qE '^no_full_suite:[[:space:]]*true($|[[:space:]])' "$candidate" 2>/dev/null &&
    proof_matches_ci_failure_requirement "$candidate" &&
    targeted_ci_candidate_ok "$candidate" "$current"
}

targeted_ci_candidate_ok() {
  local candidate="$1" current="$2" run_id run_json
  grep -qE '^AFFECTED_TESTS:[[:space:]]*TARGETED_CI_PASS($|[[:space:]])' "$candidate" 2>/dev/null || return 0
  run_id="$(sed -n 's/^targeted_ci_run:[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1/p' "$candidate" | head -1)"
  [[ "$run_id" =~ ^[0-9]+$ ]] || return 1
  grep -Fqx "targeted_ci_head: $current" "$candidate" 2>/dev/null || return 1
  grep -Fqx 'targeted_ci_conclusion: success' "$candidate" 2>/dev/null || return 1
  grep -Fqx 'targeted_ci_event: pull_request' "$candidate" 2>/dev/null || return 1
  run_json="$(gh run view "$run_id" --repo "$REPO" --json headSha,conclusion,event,jobs 2>/dev/null || true)"
  [ -n "$run_json" ] || return 1
  python3 - "$current" "$run_json" <<'PYEOF' >/dev/null 2>&1
import json, sys
head, raw = sys.argv[1:]
data = json.loads(raw)
jobs = data.get("jobs") or []
ok = (
    data.get("headSha") == head
    and str(data.get("conclusion") or "").lower() == "success"
    and data.get("event") == "pull_request"
    and any(str(job.get("conclusion") or "").lower() == "success" for job in jobs)
)
raise SystemExit(0 if ok else 1)
PYEOF
}

affected_test_proof_ok() {
  local pr="$1" promotion_proof="${2:-}" current candidate
  current="$(current_head "$pr")"
  [ -n "$current" ] || return 1
  if [ -n "$promotion_proof" ] && [ -f "$promotion_proof" ]; then
    candidate="$(python3 - "$promotion_proof" <<'PYEOF' 2>/dev/null || true
import re
import sys
text = open(sys.argv[1], errors="ignore").read()
m = re.search(r"(?im)^affected_test_proof:\s*(\S+)\s*$", text)
if m:
    print(m.group(1))
PYEOF
)"
    if [ -n "$candidate" ] && affected_test_candidate_ok "$candidate" "$current"; then
      return 0
    fi
  fi
  for candidate in \
    "/tmp/affected-test-proof-${pr}-${current}.ok" \
    "/tmp/affected-test-proof-${pr}-${current:0:8}.ok"; do
    if affected_test_candidate_ok "$candidate" "$current"; then
      return 0
    fi
  done
  return 1
}

merge_ready_ci_guard_ok() {
  local pr="$1"
  [ -f "$CI_GUARD" ] || return 1
  bash "$CI_GUARD" "$pr" >"$CI_GUARD_LOG" 2>&1
}

proof_requires_pm_review() {
  local proof="$1"
  python3 - "$proof" <<'PYEOF' 2>/dev/null
import re
import sys

text = open(sys.argv[1], errors="ignore").read()
if re.search(r"^(PM_CLAUDE_REQUIRED|PM_OPUS_REQUIRED)\b", text, re.M):
    print("yes")
    raise SystemExit
for line in text.splitlines():
    if line.lower().startswith("latest_rework_sha:"):
        value = line.split(":", 1)[1].strip().lower()
        if value and value not in {"none", "null", "n/a", "na", "unknown"}:
            print("yes")
            raise SystemExit
print("no")
PYEOF
}

pm_review_marker_ok() {
  local pr="$1"
  local head marker legacy_marker age_s
  head=$(gh pr view "$pr" --repo heydonna-app/heydonna-app --json headRefOid --jq '.headRefOid' 2>/dev/null || true)
  [ -n "$head" ] || return 1

  marker="/tmp/pm-claude-code-review-${pr}-${head}.md"
  if [ -f "$marker" ]; then
    age_s=$(python3 -c 'import os,time,sys; print(int(time.time() - os.path.getmtime(sys.argv[1])))' "$marker" 2>/dev/null || echo 999999)
    if [ "$age_s" -le 86400 ] &&
       grep -q 'PM_CLAUDE_REVIEW: PASS' "$marker" 2>/dev/null &&
       grep -q "headRefOid: ${head}" "$marker" 2>/dev/null &&
       grep -qE '^review_model:[[:space:]]*(sonnet|opus|fable|kimi3)($|[[:space:]])' "$marker" 2>/dev/null &&
       grep -qE '^model_reason:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null &&
       grep -qE '^runtime_control_point:[[:space:]]*.+' "$marker" 2>/dev/null &&
       grep -qE '^pass_scope:[[:space:]]*(blocker-clear|phase-a|merge-ready)($|[[:space:]])' "$marker" 2>/dev/null &&
       grep -qE '^readiness_ceiling:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null; then
      return 0
    fi
  fi

  legacy_marker="/tmp/pm-opus-code-review-${pr}-${head}.md"
  [ -f "$legacy_marker" ] || return 1
  age_s=$(python3 -c 'import os,time,sys; print(int(time.time() - os.path.getmtime(sys.argv[1])))' "$legacy_marker" 2>/dev/null || echo 999999)
  [ "$age_s" -le 86400 ] || return 1
  grep -q 'PM_OPUS_REVIEW: PASS' "$legacy_marker" 2>/dev/null || return 1
  grep -q "headRefOid: ${head}" "$legacy_marker" 2>/dev/null || return 1
  grep -qE '^runtime_control_point:[[:space:]]*.+' "$legacy_marker" 2>/dev/null || return 1
  grep -qE '^pass_scope:[[:space:]]*(blocker-clear|phase-a|merge-ready)($|[[:space:]])' "$legacy_marker" 2>/dev/null || return 1
  grep -qE '^readiness_ceiling:[[:space:]]*[^[:space:]].+' "$legacy_marker" 2>/dev/null || return 1
  return 0
}

CORE_E2E_CHANGED_FILES=""
CORE_E2E_ALLOWED_RE='CORE_E2E_CLASSIFICATION:[[:space:]]*(project_creation|proofreading|formatting|auto_process|rajiv_override|cto_override)($|[^[:alnum:]_])'

core_e2e_scope_ok() {
  local pr="$1"
  local proof_text
  CORE_E2E_CHANGED_FILES="$(gh pr view "$pr" --repo heydonna-app/heydonna-app --json files --jq '[.files[].path | select(test("^tests/e2e/specs/core/.*\\.spec\\.ts$"))] | join("\n")' 2>/dev/null || true)"
  [ -z "$CORE_E2E_CHANGED_FILES" ] && return 0

  proof_text="$(gh pr view "$pr" --repo heydonna-app/heydonna-app --json body,comments --jq '[.body, (.comments[]?.body // empty)] | join("\n")' 2>/dev/null || true)"
  printf '%s\n' "$proof_text" | grep -qE "$CORE_E2E_ALLOWED_RE"
}

# Allow a single ordered shell command that writes the exact proof file and then
# runs the state helper. This keeps documented command snippets usable while
# still requiring visible current-head provenance in the command text.
if printf '%s' "$CMD" | grep -q "$PROOF_FILE" && \
   printf '%s' "$CMD" | grep -qE 'READY_PACKET: PASS|CURRENT_HEAD_REVIEW_OK|review_provenance:ok' && \
   printf '%s' "$CMD" | grep -qE 'printf|tee|cat[[:space:]]*>'; then
  if [ "$STATE" = "merge-ready" ]; then
    emit_block "BLOCKED: PR #${PR} -> merge-ready cannot be promoted by an inline self-authored proof command. Run the project readiness contract, write its result to an attempt-scoped file, then invoke /Users/rajiv/.claude/scripts/pm-state-replace.sh ${PR} merge-ready. The transition and guard verify the live head and scripts/ci/pre-merge-current-head-ci-guard.sh before the label flip."
    exit 0
  fi
  if [ "$STATE" = "qa-passed-awaiting-ci" ] && ! affected_test_proof_ok "$PR"; then
    emit_block "BLOCKED: PR #${PR} -> qa-passed-awaiting-ci requires current-head affected-test proof before starting label-gated CI. If this follows a named CI failure, first complete the exact local reproduction, then run affected-test-plan.py with --source-ci-run <RUN_ID> --failed <EXACT_SPEC> --write --verify so the new-head proof is bound to that failure."
    exit 0
  fi
  exit 0
fi

if ! core_e2e_scope_ok "$PR"; then
  core_files_excerpt="$(printf '%s\n' "$CORE_E2E_CHANGED_FILES" | tr '\n' ' ' | sed 's/[[:space:]]\{1,\}/ /g' | head -c 700)"
  emit_block "BLOCKED: pm-state promotion PR #${PR} -> ${STATE} changes core E2E specs without valid CORE_E2E_CLASSIFICATION proof. Core is only for project_creation, proofreading, formatting, auto_process, or explicit Rajiv/CTO override. Move non-core specs to tests/e2e/specs/qa-tests/ or add the explicit classification/override. Core files: ${core_files_excerpt}"
  exit 0
fi

if [ -f "$PROOF_FILE" ]; then
  age_s=$(python3 -c 'import os,time,sys; print(int(time.time() - os.path.getmtime(sys.argv[1])))' "$PROOF_FILE" 2>/dev/null || echo 999999)
  if [ "$age_s" -le 7200 ] && grep -qE 'READY_PACKET: PASS|CURRENT_HEAD_REVIEW_OK|review_provenance:ok' "$PROOF_FILE" 2>/dev/null; then
    if ! proof_head_matches_current "$PROOF_FILE" "$PR"; then
      emit_block "BLOCKED: pm-state promotion PR #${PR} -> ${STATE} proof file is missing current headRefOid or is stale for another head. Refresh ${PROOF_FILE} from live gh pr view current head before applying the label."
      exit 0
    fi
    if [ "$STATE" = "merge-ready" ] && ! merge_ready_ci_guard_ok "$PR"; then
      excerpt="$(tr '\n' ' ' < "$CI_GUARD_LOG" 2>/dev/null | sed 's/[[:space:]]\{1,\}/ /g' | head -c 900)"
      emit_block "BLOCKED: pm-state promotion PR #${PR} -> merge-ready requires real latest-head CI/E2E workflow proof. scripts/ci/pre-merge-current-head-ci-guard.sh failed. Log: ${CI_GUARD_LOG}. Excerpt: ${excerpt}"
      exit 0
    fi
    if [ "$STATE" = "qa-passed-awaiting-ci" ] && ! affected_test_proof_ok "$PR" "$PROOF_FILE"; then
      emit_block "BLOCKED: pm-state promotion PR #${PR} -> qa-passed-awaiting-ci requires current-head affected-test proof before starting label-gated CI. If this follows a named CI failure, the proof must include source_ci_run and the exact failed_target and must actually run that target locally. The promotion proof must cite affected_test_proof: /tmp/affected-test-proof-${PR}-<head>.ok."
      exit 0
    fi
    if [ "$(proof_requires_pm_review "$PROOF_FILE")" = "yes" ] && ! pm_review_marker_ok "$PR"; then
      emit_block "BLOCKED: pm-state promotion PR #${PR} -> ${STATE} has latest_rework_sha proof, so it requires an existing current-head PM Claude PASS from the post-rework Phase A gate. Do not run a second PM Claude review just for merge-ready; cite or recreate /tmp/pm-claude-code-review-${PR}-<head>.md only if the Phase A marker is missing/stale, or use PM_STATE_PROMOTION_PROOF=PM_OVERRIDE_WITH_PROOF with explicit evidence."
      exit 0
    fi
    exit 0
  fi
fi

if [ "$STATE" = "merge-ready" ]; then
  required="READY_PACKET: PASS on current head plus live CI/Codex/mergeability proof; after rework, cite the existing current-head PM Claude Phase A PASS"
else
  required="current-head QA/Codex review provenance, not a hand-authored marker; after rework, include latest_rework_sha and current-head PM Claude review PASS"
fi

emit_block "BLOCKED: pm-state promotion PR #${PR} -> ${STATE} requires ${required}. Run the matching readiness/review contract, write its result to an attempt-scoped file, then invoke the authoritative current-head operator path. Do not write ${PROOF_FILE} directly. PM_STATE_PROMOTION_PROOF=PM_OVERRIDE_WITH_PROOF remains available only with explicit current-head evidence in the command/comment."
exit 0
