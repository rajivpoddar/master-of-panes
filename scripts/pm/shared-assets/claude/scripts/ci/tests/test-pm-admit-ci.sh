#!/usr/bin/env bash
# Fail-closed tests for pm-admit-ci.sh (2026-10-27 empty-head bug on #11173).
# Stubs gh and git on PATH; never touches GitHub. Pass --merge-only for the focused S166 case.
set -u
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/pm-admit-ci.sh"
SHA=0123456789abcdef0123456789abcdef01234567
export STUB_BASE_SHA=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
export STUB_MAIN_SHA=cccccccccccccccccccccccccccccccccccccccc
export STUB_MERGED_SHA=dddddddddddddddddddddddddddddddddddddddd
PASS=0; FAIL=0
T=$(mktemp -d "${TMPDIR:-/tmp}/pm-admit-test.XXXXXX")

mkstubs() {
  mkdir -p "$T/bin"
  cat > "$T/bin/git" <<'EOF'
#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$STUB_LOG"
case "$*" in
  *'fetch -q --no-tags origin +refs/heads/main:'*) exit 0 ;;
  *'fetch -q --no-tags origin refs/pull/'*) [ -f "$STUB_DIR/updated" ] ;;
  *'rev-parse --verify origin/main^{commit}')
    if [ -n "${S166_CASE:-}" ]; then printf '%s\n' "$STUB_MAIN_SHA"; else printf '%s\n' "$STUB_SHA"; fi ;;
  *'merge-base --is-ancestor '*)
    if [ -z "${S166_CASE:-}" ]; then printf '%s\n' "$STUB_SHA"; exit 0; fi
    [ -f "$STUB_DIR/updated" ] && [[ "$*" = *" $STUB_MAIN_SHA $STUB_MERGED_SHA" || "$*" = *" $STUB_SHA $STUB_MERGED_SHA" ]] ;;
  *'merge-base '*)
    if [ -n "${S166_CASE:-}" ]; then printf '%s\n' "$STUB_BASE_SHA"; else printf '%s\n' "$STUB_SHA"; fi ;;
  *'diff --no-renames --name-only '*)
    [ -n "${S166_CASE:-}" ] || exit 0
    if [[ "$*" = *" $STUB_SHA --" ]] || [ "$S166_CASE" = overlap ]; then printf 'lib/pr.ts\n'; else printf 'lib/main.ts\n'; fi ;;
  *'merge-tree --write-tree '*) [ -n "${S166_CASE:-}" ] ;;
  *) exit 1 ;;
esac
EOF
  # gh stub: behaviour from env. MODE_HEAD / MODE_HM / MODE_LABELS = ok|fail|empty|flaky
  cat > "$T/bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$STUB_LOG"
args="$*"
cnt() { local f="$STUB_DIR/cnt.$1"; local n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo $n > "$f"; echo $n; }
emit() { # mode okvalue name
  case "$1" in
    ok) printf '%s\n' "$2" ;;
    fail) echo "HTTP 499: GraphQL" >&2; exit 1 ;;
    empty) printf '\n' ;;
    flaky) [ "$(cnt "$3")" = 1 ] && { echo "TLS handshake timeout" >&2; exit 1; }; printf '%s\n' "$2" ;;
  esac
}
case "$args" in
  "api repos/"*"/comments"*)
    if [ -n "${CAP_COMMENTS_JSON:-}" ]; then printf '%s\n' "$CAP_COMMENTS_JSON"
    elif [ "${GATE_MODE:-same}" = none ]; then printf '[[]]\n'; else
      prefix=''; [ "${GATE_MODE:-same}" != heading ] || prefix='## '
      printf '[[{"body":"%sPM 12b: %s at %s"}]]\n' "$prefix" "${VERDICT:-PASS}" "${REVIEW_SHA:-$STUB_SHA}"; fi ;;
  "api repos/"*"/issues/"*"/events"*)
    [ "${CAP_EVENTS_MODE:-ok}" = fail ] && { echo "HTTP 502" >&2; exit 1; }
    printf '%s\n' "${CAP_EVENTS_JSON:-[[]]}" ;;
  "api repos/"*"/actions/runs?head_sha="*)
    sha=${args#*head_sha=}; sha=${sha%%&*}
    cat "${CAP_FIX:-/nonexistent}/runs.$sha" 2>/dev/null || printf '[{"workflow_runs":[]}]\n' ;;
  "api repos/"*"/actions/runs/"*"/jobs"*)
    printf '[{"jobs":[{"name":"typescript","conclusion":"failure","started_at":"2026-11-20T10:00:00Z","completed_at":"2026-11-20T10:20:00Z"}]}]\n' ;;
  "api repos/"*"/compare/"*"--jq .status"*) printf 'ahead\n' ;;      # stale test-merge guard: current main is contained
  "api repos/"*"/git/ref/heads/main"*) printf '%s\n' "$STUB_MAIN_SHA" ;;
  "api repos/"*"/pulls/"*"merge_commit_sha"*) printf '%s\n' "$STUB_MERGED_SHA" ;;
  "api repos/"*"/compare/"*)
    changed='[]'; [ "${GATE_MODE:-same}" != changed ] || changed='[{"filename":"lib/a.ts"}]'
    printf '{"status":"ahead","total_commits":1,"commits":[{"sha":"%s","parents":[{},{}],"commit":{"message":"Merge branch '\''main'\'' into topic"}}],"files":%s}\n' "$STUB_SHA" "$changed" ;;
  "api repos/"*"/commits?"*) printf '[[{"sha":"%s"}]]\n' "$STUB_SHA" ;;
  "api repos/"*"/files?"*) printf '[[{"filename":"lib/a.ts"}]]\n' ;;
  "api repos/"*"contents/"*)
    [ "${GATE_MODE:-same}" = exempt ] || exit 1
    if [[ "$args" = *change_scope.py* ]]; then
      content=$(printf 'print('\''{"control_plane_only":true,"product_changed":false}'\'')\n' | base64 | tr -d '\n')
    else content=e30=; fi
    printf '{"content":"%s"}\n' "$content" ;;
  "pr view"*"--json state"*) printf 'OPEN\n' ;;
  "pr view"*"--json baseRefName"*) printf 'main\n' ;;
  "pr view"*"--json closingIssuesReferences"*) printf '11173\n' ;;
  "api -X PUT repos/"*"/update-branch -f expected_head_sha=$STUB_SHA")
    [ "${S166_CASE:-}" = overlap ] || exit 1
    : > "$STUB_DIR/updated" ;;
  "run list"*) exit 1 ;;
  "pr diff"*) exit 1 ;;
  "pr view"*"--json headRefOid,mergeable"*)
    if [ -f "$STUB_DIR/updated" ]; then emit "$MODE_HM" "$STUB_MERGED_SHA MERGEABLE" hm
    else emit "$MODE_HM" "$HM_VALUE" hm; fi ;;
  "pr view"*"--json headRefOid"*) emit "$MODE_HEAD" "$STUB_SHA" head ;;
  "pr view"*"--json labels,closingIssuesReferences"*) printf '{"labels":[{"name":"P2"}],"closingIssuesReferences":[]}\n' ;;
  "pr view"*"--json labels"*) emit "$MODE_LABELS" "$LABELS_JSON" labels ;;
  "pr edit"*|"label create"*) [ "${MODE_WRITE:-ok}" = fail ] && exit 1; exit 0 ;;
  *) exit 1 ;;
esac
EOF
  REAL_PYTHON=$(command -v python3); export REAL_PYTHON
  cat > "$T/bin/python3" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" = *pm-ops.py* ]]; then
  printf 'pm-ops %s\n' "$*" >> "$STUB_LOG"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
EOF
  chmod +x "$T/bin/gh" "$T/bin/git" "$T/bin/python3"
}

run_script() { # name, then script arguments; fixture env pre-set by caller
  local name="$1"; shift
  STUB_DIR="$T/$name"; mkdir -p "$STUB_DIR"; STUB_LOG="$STUB_DIR/gh.log"; : > "$STUB_LOG"
  export STUB_DIR STUB_LOG STUB_SHA="$SHA" PM_ADMIT_RETRY_SLEEP=0
  OUT=$(PATH="$T/bin:$PATH" HOME="$T/home" bash "$SCRIPT" "$@" 2>&1); RC=$?
  LOG=$(cat "$STUB_LOG")
}
run_case() { local name="$1"; shift; run_script "$name" 11173 --no-update "$@"; }
check() { if eval "$2"; then PASS=$((PASS+1)); echo "  ok   $1"; else FAIL=$((FAIL+1)); echo "  FAIL $1"; echo "$OUT" | sed 's/^/       out: /'; fi; }

assert_refused() { # name step
  check "$1: exit non-zero" '[ "$RC" -ne 0 ]'
  check "$1: no ADMITTED line" '! printf "%s" "$OUT" | grep -q ADMITTED'
  check "$1: REFUSE github_api_error $2" "printf '%s' \"\$OUT\" | grep -qx 'REFUSE: github_api_error $2'"
  check "$1: no ci-head label write" '! printf "%s" "$LOG" | grep -qE "add-label ci-head|label create"'
}

finish() {
  printf '\nRESULT: %s passed, %s failed\n' "$PASS" "$FAIL"
  /Users/rajiv/.local/bin/trash "$T" >/dev/null 2>&1 || true
  [ "$FAIL" -eq 0 ]; exit "$?"
}

mkstubs; mkdir -p "$T/home/.claude/logs"
OKLABELS='{"labels":[]}'
export HM_VALUE="$SHA MERGEABLE" LABELS_JSON="$OKLABELS" MODE_LABELS=ok MODE_HM=ok MODE_WRITE=ok MODE_HEAD=ok

# Exercise both S166 branches with main and PR diverged. No 12b receipt is available.
echo 'case: merge-only before 12b (no overlap and overlap)'
for fixture in no_overlap overlap; do
  S166_CASE=$fixture GATE_MODE=none run_script "merge_only_$fixture" --merge-only 11173
  if [ "$fixture" = no_overlap ]; then
    check 'merge-only/no overlap: skipped with unchanged head' '[ "$RC" = 0 ] && grep -qx "MAIN_MERGE: skipped head=$SHA reason=no_conflict_no_overlap" <<<"$OUT"'
    check 'merge-only/no overlap: no update-branch call' '! grep -q update-branch "$STUB_LOG"'
  else
    check 'merge-only/overlap: performed with merged head' '[ "$RC" = 0 ] && grep -qx "MAIN_MERGE: performed head=$STUB_MERGED_SHA reason=overlap files=1" <<<"$OUT"'
    check 'merge-only/overlap: one update-branch pinned to original head' '[ "$(grep -c update-branch "$STUB_LOG")" = 1 ] && grep -q "update-branch -f expected_head_sha=$SHA$" "$STUB_LOG"'
  fi
  check "merge-only/$fixture: no 12b, labels or CI/E2E dispatch" '! grep "^gh " "$STUB_LOG" | grep -qvE "^gh (pr view .*--json (state|headRefOid,mergeable|baseRefName) |api -X PUT .*/update-branch )" && ! grep -qE "12B_GATE|ADMITTED" <<<"$OUT"'
done

# If main moved again after review, admission must not do the preflight's write itself.
S166_CASE=overlap GATE_MODE=exempt run_script admission_overlap 11173
check 'admission/overlap: requires a fresh preflight' '[ "$RC" = 2 ] && grep -q "REFUSE: main integration required reason=overlap .*--merge-only 11173" <<<"$OUT"'
check 'admission/overlap: no merge or dispatch' '! grep -qE "update-branch|pr edit|label create|workflow run|run rerun" "$STUB_LOG" && ! grep -q ADMITTED <<<"$OUT"'
[ "${1:-}" != --merge-only ] || finish

echo "case: head lookup fails (HTTP 499) twice"
MODE_HEAD=fail; export MODE_HEAD; run_case head_fail
assert_refused head_fail pr_head
check "head_fail: retried exactly once" '[ "$(grep -c "json headRefOid --jq" "$STUB_LOG")" = 2 ]'

echo "case: head lookup returns empty"
MODE_HEAD=empty; export MODE_HEAD; run_case head_empty
assert_refused head_empty pr_head

echo "case: head lookup returns non-40-hex"
SHA_SAVE=$SHA; SHA=abc123; run_case head_short; SHA=$SHA_SAVE
assert_refused head_short pr_head

echo "case: head+mergeable returns empty head after update"
MODE_HEAD=ok MODE_HM=ok HM_VALUE=" MERGEABLE"; export MODE_HEAD MODE_HM HM_VALUE; run_case hm_empty
assert_refused hm_empty pr_head_mergeable
HM_VALUE="$SHA MERGEABLE"; export HM_VALUE

echo "case: head+mergeable call fails"
MODE_HM=fail; export MODE_HM; run_case hm_fail
assert_refused hm_fail pr_head_mergeable
MODE_HM=ok; export MODE_HM

echo "case: labels lookup fails"
MODE_LABELS=fail; export MODE_LABELS; run_case labels_fail
assert_refused labels_fail pr_labels
MODE_LABELS=ok; export MODE_LABELS

echo "case: label write fails twice"
MODE_WRITE=fail; export MODE_WRITE; run_case write_fail
check "write_fail: exit non-zero" '[ "$RC" -ne 0 ]'
check "write_fail: no ADMITTED line" '! printf "%s" "$OUT" | grep -q ADMITTED'
check "write_fail: REFUSE add_ci_head_label" 'printf "%s" "$OUT" | grep -qx "REFUSE: github_api_error add_ci_head_label"'
MODE_WRITE=ok; export MODE_WRITE

echo "case: transient TLS failure on first head read, then OK"
MODE_HEAD=flaky; export MODE_HEAD; run_case head_flaky
check "head_flaky: exit 0" '[ "$RC" -eq 0 ]'
check "head_flaky: ADMITTED with full sha" 'printf "%s" "$OUT" | grep -qx "ADMITTED #11173 at $SHA"'
MODE_HEAD=ok; export MODE_HEAD

echo "case: pre-existing empty ci-head: label is removed, valid admit"
LABELS_JSON='{"labels":[{"name":"ci-head:"},{"name":"P2"}]}'; export LABELS_JSON; run_case stale_empty
check "stale_empty: exit 0" '[ "$RC" -eq 0 ]'
check "stale_empty: removes empty ci-head: label" 'printf "%s" "$LOG" | grep -qE -- "--remove-label ci-head:$"'
check "stale_empty: adds ci-head:<sha>" 'printf "%s" "$LOG" | grep -q -- "--add-label ci-head:$SHA"'
check "stale_empty: ADMITTED with full sha" 'printf "%s" "$OUT" | grep -qx "ADMITTED #11173 at $SHA"'

LABELS_JSON="$OKLABELS"; export LABELS_JSON
for mode in merge changed none revise heading exempt; do
  echo "case: 12b $mode"
  GATE_MODE=$mode; VERDICT=PASS; REVIEW_SHA=${SHA:0:7}
  case "$mode" in merge|changed) REVIEW_SHA=abcdef0 ;; revise) VERDICT=REVISE ;; esac
  export GATE_MODE VERDICT REVIEW_SHA
  run_case "gate_$mode"
  case "$mode" in
    merge|exempt|heading)
      # heading: "## PM 12b: PASS at <sha>" is the house 12b format (PM 2026-11-09).
      check "$mode: admitted" '[ "$RC" = 0 ] && printf "%s" "$OUT" | grep -q "ADMITTED #11173"' ;;
    *)
      check "$mode: exit 3" '[ "$RC" = 3 ]'
      check "$mode: exact refusal" 'printf "%s" "$OUT" | grep -qx "REFUSE: 12b_missing_or_stale pr=11173 head=$SHA last_12b=.*"'
      check "$mode: no admission writes" '! printf "%s" "$LOG" | grep -qE "pr edit|label create|update-branch"' ;;
  esac
done
GATE_MODE=none; export GATE_MODE
run_case override --no-12b-gate --reason 'authorized test exception'
check 'override: admitted' '[ "$RC" = 0 ] && printf "%s" "$OUT" | grep -q ADMITTED'
check 'override: admit log records reason' 'grep -q "no-12b-gate reason=authorized test exception" "$T/home/.claude/logs/pm-admit-skip.log"'
check 'override: pm-ops event records reason and head' 'printf "%s" "$LOG" | grep -q -- "--event 12b_gate_override.*--head-sha $SHA.*authorized test exception"'
run_case missing_reason --no-12b-gate
check 'override: missing reason rejected' '[ "$RC" = 64 ] && [ ! -s "$STUB_LOG" ]'
# Check-only must not invoke git, audit writes or any GitHub write.
: > "$STUB_LOG"
OUT=$(PATH="$T/bin:$PATH" HOME="$T/home" bash "$SCRIPT" --check-12b-only 11173 2>&1); RC=$?
check 'check-only: refuses missing review' '[ "$RC" = 3 ]'
check 'check-only: no git or audit or GitHub writes' '! grep -qE "^git |pm-ops |pr edit|label create|update-branch" "$STUB_LOG"'
GATE_MODE=same VERDICT=PASS REVIEW_SHA=${SHA:0:7}; export GATE_MODE VERDICT REVIEW_SHA
: > "$STUB_LOG"
OUT=$(PATH="$T/bin:$PATH" HOME="$T/home" bash "$SCRIPT" --check-12b-only 11173 2>&1); RC=$?
check 'check-only: accepts seven-hex PASS' '[ "$RC" = 0 ]'
check 'check-only PASS: no effects' '! grep -qE "^git |pm-ops |pr edit|label create|update-branch" "$STUB_LOG"'

# ---- Admission cap (ADMISSION_CAP_2) -----------------------------------------------------------------------
# Rajiv C0ALZJHGE49 thread 1795324302.697259, ts 1795324706.834969. Fixtures are generated per case. The counting logic is covered
# structurally in test_pm_admit_cap.py; these cases prove the wiring: refusal, exit codes, no writes, ordering, override, fail-closed.
CAP_FIX="$T/capfix"; mkdir -p "$CAP_FIX"; export CAP_FIX
CH1=1111111111111111111111111111111111111111; CH2=2222222222222222222222222222222222222222
OVR=cto:1795324706.834969
cap_events() { # <head>... -> one exact ci-head label event per head, oldest first
  local out='[[' sep='' i=0 s
  for s in "$@"; do i=$((i+1)); out="$out$sep{\"event\":\"labeled\",\"created_at\":\"2026-11-20T1$i:00:00Z\",\"label\":{\"name\":\"ci-head:$s\"}}"; sep=,; done
  printf '%s]]' "$out"
}
cap_red() { # <head> <run id>: one real (30 min) red ci.yml pull_request run at that head
  printf '[{"workflow_runs":[{"id":%s,"head_sha":"%s","event":"pull_request","path":".github/workflows/ci.yml","name":"CI","conclusion":"failure","run_attempt":1,"run_started_at":"2026-11-20T10:00:00Z","updated_at":"2026-11-20T10:30:00Z","pull_requests":[]}]}]\n' "$2" "$1" > "$CAP_FIX/runs.$1"
}
cap_verdict() { # <head> <run id> <classification> -> one ci-failure-investigation verdict comment object
  printf '{"id":%s,"created_at":"2026-11-20T12:00:00Z","body":"## CI failure investigation verdict\\n<!-- ci-failure-investigation:run=%s attempt=1 head=%s -->\\n<!-- ci-verdict: {\\"classification\\":\\"%s\\",\\"run_id\\":%s} -->"}' "$2" "$2" "$1" "$3" "$2"
}
cap_red $CH1 101; cap_red $CH2 102
GATE_MODE=none; export GATE_MODE

echo "case: cap - one admitted head is under the cap and costs no run lookups"
CAP_EVENTS_JSON=$(cap_events $CH1); export CAP_EVENTS_JSON
run_case cap_under --no-12b-gate --reason 'cap test'
check 'cap_under: ok line' 'grep -qx "ADMISSION_CAP: ok pr=11173 admitted_heads=1 pr_caused_red_heads=0 cap=2" <<<"$OUT"'
check 'cap_under: admitted' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT"'
check 'cap_under: no run lookups below the cap' '! grep -q "actions/runs" "$STUB_LOG"'

echo "case: cap - two PR-caused red heads refuse before the 12b gate, with the S131 handoff"
CAP_EVENTS_JSON=$(cap_events $CH1 $CH2); export CAP_EVENTS_JSON
run_case cap_hit
check 'cap_hit: exit 10' '[ "$RC" = 10 ]'
check 'cap_hit: detail names heads, runs and the unrecorded classification' 'grep -q "^ADMISSION_CAP_2: PR #11173 has 2 admitted heads" <<<"$OUT" && grep -q "head ${CH1:0:10} ci run 101 failure classification=unrecorded" <<<"$OUT" && grep -q "head ${CH2:0:10} ci run 102 failure" <<<"$OUT"'
check 'cap_hit: REFUSE, S131 handoff and override hint' 'grep -qx "REFUSE: ADMISSION_CAP_2 #11173; nothing admitted" <<<"$OUT" && grep -q "^S131 handoff: " <<<"$OUT" && grep -q "PM_ADMIT_CAP_OVERRIDE=cto:" <<<"$OUT"'
check 'cap_hit: refused before the 12b gate (no review needed first)' '! grep -q 12b_missing_or_stale <<<"$OUT"'
check 'cap_hit: no admission writes' '! grep -qE "pr edit|label create|update-branch|workflow run" "$STUB_LOG" && ! grep -q ADMITTED <<<"$OUT"'

echo "case: cap - reds recorded as infra / flaky do not count"
CAP_COMMENTS_JSON="[[$(cap_verdict $CH1 101 infra),$(cap_verdict $CH2 102 FLAKY)]]"; export CAP_COMMENTS_JSON
run_case cap_exempt --no-12b-gate --reason 'cap test'
check 'cap_exempt: zero PR-caused heads, admitted' '[ "$RC" = 0 ] && grep -q "pr_caused_red_heads=0" <<<"$OUT" && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT"'
unset CAP_COMMENTS_JSON

echo "case: cap - a CTO-approved override re-admits and is audited; a dry run is not"
PM_ADMIT_CAP_OVERRIDE=$OVR; export PM_ADMIT_CAP_OVERRIDE
run_case cap_override --no-12b-gate --reason 'cap test'
check 'cap_override: override line, admitted' '[ "$RC" = 0 ] && grep -q "^ADMISSION_CAP_OVERRIDE: $OVR pr=11173" <<<"$OUT" && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT"'
check 'cap_override: audited in the skip log' 'grep -q "PR=11173 admission-cap-override=$OVR" "$T/home/.claude/logs/pm-admit-skip.log"'
AUDITED=$(grep -c admission-cap-override "$T/home/.claude/logs/pm-admit-skip.log")
run_case cap_override_dry --dry-run --no-12b-gate --reason 'cap test'
check 'cap_override --dry-run: override line, nothing admitted, not audited' '[ "$RC" = 0 ] && grep -q "^ADMISSION_CAP_OVERRIDE: " <<<"$OUT" && grep -q "nothing admitted" <<<"$OUT" && [ "$(grep -c admission-cap-override "$T/home/.claude/logs/pm-admit-skip.log")" = "$AUDITED" ]'
PM_ADMIT_CAP_OVERRIDE=yes; export PM_ADMIT_CAP_OVERRIDE
run_case cap_override_bad --no-12b-gate --reason 'cap test'
check 'cap_override_bad: a malformed override is refused (64) with no writes' '[ "$RC" = 64 ] && grep -q "must look like cto:<slack ts>" <<<"$OUT" && ! grep -qE "pr edit|label create" "$STUB_LOG"'
unset PM_ADMIT_CAP_OVERRIDE

echo "case: cap - an unreadable record is UNAVAILABLE and fails closed, never a guess"
CAP_EVENTS_MODE=fail; export CAP_EVENTS_MODE
run_case cap_unavailable --no-12b-gate --reason 'cap test'
check 'cap_unavailable: reason and REFUSE github_api_error (7)' '[ "$RC" = 7 ] && grep -qx "ADMISSION_CAP: UNAVAILABLE reason=issues/11173/events: gh_rc_1" <<<"$OUT" && grep -qx "REFUSE: github_api_error admission_cap" <<<"$OUT"'
check 'cap_unavailable: no admission writes' '! grep -qE "pr edit|label create" "$STUB_LOG"'
PM_ADMIT_CAP_OVERRIDE=$OVR; export PM_ADMIT_CAP_OVERRIDE
run_case cap_unavailable_override --no-12b-gate --reason 'cap test'
check 'cap_unavailable: an explicit override may proceed' '[ "$RC" = 0 ] && grep -q "cap lookup unavailable" <<<"$OUT" && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT"'
unset PM_ADMIT_CAP_OVERRIDE CAP_EVENTS_MODE

echo "case: cap - a dry run reports the refusal; merge-only and check-only never consult the cap"
CAP_EVENTS_JSON=$(cap_events $CH1 $CH2); export CAP_EVENTS_JSON
run_case cap_dry --dry-run
check 'cap_dry: exit 10, nothing admitted' '[ "$RC" = 10 ] && grep -q "ADMISSION_CAP_2 #11173; nothing admitted" <<<"$OUT"'
S166_CASE=no_overlap run_script cap_merge_only --merge-only 11173
check 'cap: --merge-only does not read the cap' '[ "$RC" = 0 ] && ! grep -q "/events" "$STUB_LOG" && ! grep -q ADMISSION_CAP <<<"$OUT"'
: > "$STUB_LOG"
GATE_MODE=same VERDICT=PASS REVIEW_SHA=${SHA:0:7}; export GATE_MODE VERDICT REVIEW_SHA
OUT=$(PATH="$T/bin:$PATH" HOME="$T/home" bash "$SCRIPT" --check-12b-only 11173 2>&1); RC=$?
check 'cap: --check-12b-only does not read the cap' '! grep -q "/events" "$STUB_LOG" && ! grep -q ADMISSION_CAP <<<"$OUT"'
unset CAP_EVENTS_JSON

finish
