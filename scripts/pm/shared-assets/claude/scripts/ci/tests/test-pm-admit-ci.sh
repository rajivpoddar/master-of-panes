#!/usr/bin/env bash
# Fail-closed tests for pm-admit-ci.sh (2026-10-27 empty-head bug on #11173).
# Stubs gh and git on PATH; never touches GitHub. Pass --merge-only for the focused S166 case.
set -u
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/pm-admit-ci.sh"
SHA=0123456789abcdef0123456789abcdef01234567
export STUB_BASE_SHA=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
export STUB_MAIN_SHA=cccccccccccccccccccccccccccccccccccccccc
export STUB_MERGED_SHA=dddddddddddddddddddddddddddddddddddddddd
export STUB_FRESH_SHA=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
export STUB_MOVED_SHA=9999999999999999999999999999999999999999   # main after it moved on GitHub
export STUB_LAG_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa      # the older main a lagging test merge was built on (an ancestor of STUB_MAIN_SHA)
export STUB_LAGBASE_SHA=7777777777777777777777777777777777777777  # merge-base of that older main and the PR head
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
  *'rev-parse --verify origin/main^{commit}') printf '%s\n' "$STUB_MAIN_SHA" ;;   # the main that passes the overlap check; the stale-ref guard binds to it
  *'merge-base --is-ancestor '*)
    # Lagging-ref guard: "is the test merge's main parent an ancestor of the overlap-checked main?" (the call ends with STUB_MAIN_SHA). Default = NOT an ancestor.
    if [[ "$*" = *" $STUB_MAIN_SHA" ]]; then [ "${LAG_ANCESTOR:-no}" = yes ]; exit $?; fi
    if [ -z "${S166_CASE:-}" ]; then printf '%s\n' "$STUB_SHA"; exit 0; fi
    [ -f "$STUB_DIR/updated" ] && [[ "$*" = *" $STUB_MAIN_SHA $STUB_MERGED_SHA" || "$*" = *" $STUB_SHA $STUB_MERGED_SHA" ]] ;;
  *'merge-base '*)
    # Lagging-ref guard: merge-base of the lagging main parent and the head is a distinct commit, so its diff is a separate, observable read.
    if [ -n "${LAG_CASE:-}" ] && [[ "$*" = *" $STUB_LAG_SHA $STUB_SHA" ]]; then printf '%s\n' "$STUB_LAGBASE_SHA"; exit 0; fi
    if [ -n "${S166_CASE:-}" ]; then printf '%s\n' "$STUB_BASE_SHA"; else printf '%s\n' "$STUB_SHA"; fi ;;
  *'diff --no-renames --name-only '*)
    if [ -n "${LAG_CASE:-}" ]; then   # lagging-ref guard file lists (printf %b turns the \n in LAG_* into newlines); the S166 probe's own reads stay empty
      case "$*" in
        *" $STUB_LAG_SHA $STUB_MAIN_SHA --") [ -z "${LAG_DIFF_FAIL:-}" ] || exit 1; printf '%b' "${LAG_MAIN_FILES:-}" ;;   # files main changed since the lagging parent
        *" $STUB_LAGBASE_SHA $STUB_SHA --") printf '%b' "${LAG_PR_FILES_LAGBASE:-}" ;;                                       # PR files against the lagging merge-base
        *" $STUB_SHA $STUB_SHA --") printf '%b' "${LAG_PR_FILES:-}" ;;                                                       # PR files against the main merge-base
      esac
      exit 0
    fi
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
  # Stale test-merge guard: ONE deadline-bounded read pass, bound to the main that passed the overlap check (STUB_MAIN_SHA). Default = fresh merge ref.
  # STALE_MODE: stale | unreadable | unreadable_main | unreadable_parents | unreadable_confirm_pr | unreadable_confirm_main | no_merge_sha |
  #             parents_mismatch | closed | head_moved | main_moved | main_moved_confirm | pr_changed_confirm | hung | hung_parents | late |
  #             lagging | lagging_head_mismatch | lagging_three_parents   (test merge built on the older main STUB_LAG_SHA; git side: LAG_CASE, LAG_ANCESTOR, LAG_*_FILES)
  "api repos/"*"/git/ref/heads/main"*)
    n=$(cnt main)                                                    # read 1 = first main read, read 2 = confirming main read
    case "${STALE_MODE:-}" in
      unreadable_main) echo "HTTP 502" >&2; exit 1 ;;
      unreadable_confirm_main) [ "$n" -ge 2 ] && { echo "HTTP 502" >&2; exit 1; } ;;
      main_moved) printf '%s\n' "$STUB_MOVED_SHA"; exit 0 ;;                                   # main moved before the guard's first read
      main_moved_confirm) [ "$n" -ge 2 ] && { printf '%s\n' "$STUB_MOVED_SHA"; exit 0; } ;;      # main moved during the pass
    esac
    printf '%s\n' "$STUB_MAIN_SHA" ;;
  "api repos/"*"/pulls/"*".head.sha"*)                               # "<state> <head> <merge_commit_sha>"; read 1 = first, read 2 = confirming
    v=$(cnt verify)
    case "${STALE_MODE:-}" in
      unreadable) echo "HTTP 502" >&2; exit 1 ;;
      unreadable_confirm_pr) [ "$v" -ge 2 ] && { echo "HTTP 502" >&2; exit 1; } ;;
      hung) exec sleep 30 ;;                                          # never answers: only the deadline timeout ends it
      late) sleep 3 ;;                                                # answers correctly but after the deadline (run with a no-op timeout shim)
    esac
    case "${STALE_MODE:-}" in
      stale) printf 'open %s %s\n' "$STUB_SHA" "$STUB_MERGED_SHA" ;;  # GitHub never rebuilt the ref: still on the old main
      closed) printf 'closed %s %s\n' "$STUB_SHA" "$STUB_FRESH_SHA" ;;
      head_moved) printf 'open %s %s\n' ffffffffffffffffffffffffffffffffffffffff "$STUB_FRESH_SHA" ;;
      no_merge_sha) printf 'open %s \n' "$STUB_SHA" ;;
      pr_changed_confirm) if [ "$v" = 1 ]; then printf 'open %s %s\n' "$STUB_SHA" "$STUB_FRESH_SHA"; else printf 'open %s %s\n' "$STUB_SHA" "$STUB_MERGED_SHA"; fi ;;
      *) printf 'open %s %s\n' "$STUB_SHA" "$STUB_FRESH_SHA" ;;       # fresh, parents_mismatch, unreadable_parents, main_moved*, hung_parents
    esac ;;
  "api repos/"*"/commits/"*)                                         # merge commit parents, sorted
    [ "${STALE_MODE:-}" = unreadable_parents ] && { echo "HTTP 502" >&2; exit 1; }
    [ "${STALE_MODE:-}" = hung_parents ] && exec sleep 30
    sha=${args#*commits/}; sha=${sha%% *}
    # Lagging-ref modes: GitHub built the test merge on STUB_LAG_SHA, an older main. lagging = {head, lag}; the others break one condition.
    if [ "$sha" = "$STUB_FRESH_SHA" ] && [ "${STALE_MODE:-}" = lagging ]; then printf '%s %s\n' "$STUB_SHA" "$STUB_LAG_SHA"
    elif [ "$sha" = "$STUB_FRESH_SHA" ] && [ "${STALE_MODE:-}" = lagging_head_mismatch ]; then printf '%s %s\n' "$STUB_LAG_SHA" ffffffffffffffffffffffffffffffffffffffff   # neither parent is the reviewed head
    elif [ "$sha" = "$STUB_FRESH_SHA" ] && [ "${STALE_MODE:-}" = lagging_three_parents ]; then printf '%s %s %s\n' "$STUB_SHA" "$STUB_LAG_SHA" "$STUB_BASE_SHA"
    elif [ "$sha" = "$STUB_FRESH_SHA" ] && [ "${STALE_MODE:-}" != parents_mismatch ]; then printf '%s %s\n' "$STUB_SHA" "$STUB_MAIN_SHA"   # built on the overlap-checked main
    else printf '%s %s\n' "$STUB_SHA" "$STUB_BASE_SHA"; fi ;;                                                                            # still on an old main
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
  *) exit 1 ;;     # includes any "pr close" / "pr reopen": the script must never call them (it is also logged above, and asserted absent)
esac
EOF
  REAL_PYTHON=$(command -v python3); export REAL_PYTHON
  cat > "$T/bin/python3" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" = *pm-ops.py* ]]; then
  printf 'pm-ops %s\n' "$*" >> "$STUB_LOG"
  exit 0
fi
if [[ "$*" = *pm-admit-ci-inputs.py* ]]; then   # the trusted-CI-input helper: changed inputs come from LAG_CI_INPUTS_CHANGED ('%b'), LAG_CI_INPUTS_FAIL=1 is an unreadable discovery
  printf 'ci-inputs %s\n' "$*" >> "$STUB_LOG"
  [ "${LAG_CI_INPUTS_FAIL:-}" != 1 ] || exit 1
  printf '%b' "${LAG_CI_INPUTS_CHANGED:-}"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
EOF
  chmod +x "$T/bin/gh" "$T/bin/git" "$T/bin/python3"
}

run_script() { # name, then script arguments; fixture env pre-set by caller
  local name="$1"; shift
  STUB_DIR="$T/$name"; mkdir -p "$STUB_DIR"; STUB_LOG="$STUB_DIR/gh.log"; : > "$STUB_LOG"
  export STUB_DIR STUB_LOG STUB_SHA="$SHA" PM_ADMIT_RETRY_SLEEP=0 PM_ADMIT_STALE_DEADLINE="${STALE_DL:-4}"
  OUT=$(PATH="${EXTRA_PATH:+$EXTRA_PATH:}$T/bin:$PATH" HOME="$T/home" bash "$SCRIPT" "$@" 2>&1); RC=$?
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

# ---- Stale test-merge ref: ONE deadline-bounded read-only pass, fail closed (CTO REVISE #2 of b6d3f55c, C0ALZJHGE49 thread 1795543629.469469, ts 1795544911.112579) ----
# The PR's test merge (refs/pull/N/merge) is verified in one pass of GitHub reads, every one bounded by the remaining deadline, BEFORE any admission write.
# It is admitted ONLY when the PR is OPEN, the head is unchanged, main still equals the main that passed the overlap check (STUB_MAIN_SHA), the merge
# commit's parents are exactly {head, that main}, and a confirming read agrees. Anything else, including any unreadable, hung or late read, refuses
# stale_merge_ref (exit 3). No polling, no sleeps, no close/reopen, no main merge, no push.
GATE_MODE=none; export GATE_MODE
assert_stale_refused() { # name: exit 3, REFUSE stale_merge_ref BEFORE any admission write, no state change, no close/reopen advice
  check "$1: exit 3 REFUSE stale_merge_ref, nothing admitted" '[ "$RC" = 3 ] && grep -q "^REFUSE stale_merge_ref #11173:" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && ! grep -q MERGE_REF_VERIFIED <<<"$OUT"'
  check "$1: refused before the activation label and before every admission write" '! grep -qE -- "--add-label|--remove-label|label create|pr edit" "$STUB_LOG"'
  check "$1: no PR state change, no main merge, no branch write" '! grep -qE "^gh pr (close|reopen)|update-branch|^git (merge|push|reset|rebase)|--force" "$STUB_LOG"'
  check "$1: the REFUSE text carries no close/reopen advice" '! grep -qiE "reopen|gh pr close" <<<"$OUT"'
}

echo "case: fresh merge ref - verified on the overlap-checked main, admitted after ONE pass (2 PR reads, 2 main reads, 1 parents read), no polling"
run_case stale_fresh --no-12b-gate --reason 'stale ref test'
check 'stale_fresh: exit 0, MERGE_REF_VERIFIED binds head, overlap-checked main and merge sha' '[ "$RC" = 0 ] && grep -qx "MERGE_REF_VERIFIED pr=11173 head=$SHA main=$STUB_MAIN_SHA merge=$STUB_FRESH_SHA" <<<"$OUT"'
check 'stale_fresh: ADMITTED at the reviewed head, ci-head label added after the check' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && grep -q -- "--add-label ci-head:$SHA" "$STUB_LOG"'
check 'stale_fresh: one pass only: 2 PR reads, 2 main reads, 1 parents read of the merge commit' '[ "$(cat "$STUB_DIR/cnt.verify")" = 2 ] && [ "$(cat "$STUB_DIR/cnt.main")" = 2 ] && [ "$(grep -c "^gh api repos/.*/commits/$STUB_FRESH_SHA" "$STUB_LOG")" = 1 ]'
check 'stale_fresh: every guard read precedes the first admission write' '[ "$(grep -n "git/ref/heads/main" "$STUB_LOG" | tail -1 | cut -d: -f1)" -lt "$(grep -nE -- "--add-label|label create" "$STUB_LOG" | head -1 | cut -d: -f1)" ]'
check 'stale_fresh: no PR state change, no main merge, no branch write' '! grep -qE "^gh pr (close|reopen)|update-branch|^git (merge|push|reset|rebase)|--force" "$STUB_LOG" && ! grep -q "main_merge: performed" <<<"$OUT"'

echo "case: stale merge ref - GitHub has not rebuilt it on the overlap-checked main, refused after ONE pass with no polling"
STALE_MODE=stale; export STALE_MODE
run_case stale_still --no-12b-gate --reason 'stale ref test'
assert_stale_refused stale_still
check 'stale_still: REFUSE names the stale merge, its parents, and the exact head and overlap-checked main it needed' 'grep -q "^REFUSE stale_merge_ref #11173: test merge $STUB_MERGED_SHA has parents \[$SHA $STUB_BASE_SHA\] but must be exactly the reviewed head and the overlap-checked main \[$SHA $STUB_MAIN_SHA\]" <<<"$OUT" && grep -q "made NO change to the PR" <<<"$OUT"'
check 'stale_still: read once, never polled (1 PR read, 1 main read, 1 parents read)' '[ "$(cat "$STUB_DIR/cnt.verify")" = 1 ] && [ "$(cat "$STUB_DIR/cnt.main")" = 1 ] && [ "$(grep -c "^gh api repos/.*/commits/" "$STUB_LOG")" = 1 ]'
unset STALE_MODE

echo "case: stale merge ref - EDGE 1: any unreadable PR, main, merge-ref or parents read is UNKNOWN and refuses BEFORE the activation label"
for mode in unreadable unreadable_main unreadable_parents unreadable_confirm_pr unreadable_confirm_main no_merge_sha; do
  STALE_MODE=$mode; export STALE_MODE
  run_case "stale_$mode" --no-12b-gate --reason 'stale ref test'
  assert_stale_refused "stale_$mode"
  case "$mode" in
    unreadable_main) check "stale_$mode: REFUSE says UNKNOWN (read failed, timed out or returned past the deadline), never a guess" 'grep -q "^REFUSE stale_merge_ref #11173: main UNKNOWN (read failed, timed out or returned past the 4s deadline)" <<<"$OUT"' ;;
    no_merge_sha) check "stale_$mode: REFUSE says GitHub has no test merge commit yet" 'grep -q "GitHub has no test merge commit for the PR yet" <<<"$OUT"' ;;
  esac
done
unset STALE_MODE

echo "case: stale merge ref - EDGE 2: a hung GitHub call is cut at the deadline and refuses (no polling, no waiting past it)"
for mode in hung hung_parents; do
  STALE_MODE=$mode; STALE_DL=2; export STALE_MODE STALE_DL
  START=$(date +%s); run_case "stale_$mode" --no-12b-gate --reason 'stale ref test'; ELAPSED=$(( $(date +%s) - START ))
  assert_stale_refused "stale_$mode"
  check "stale_$mode: REFUSE says UNKNOWN past the 2s deadline" 'grep -q "UNKNOWN (read failed, timed out or returned past the 2s deadline)" <<<"$OUT"'
  check "stale_$mode: returned near the 2s deadline, nowhere near the 30s hang" '[ "$ELAPSED" -le 8 ]'
done
unset STALE_MODE STALE_DL

echo "case: stale merge ref - EDGE 2b: a call that SUCCEEDS but returns after the deadline counts as UNKNOWN and refuses"
# A shim `timeout` that ignores its limit models a read that returns a valid answer late; only the post-read deadline check can refuse it.
mkdir -p "$T/notimeout"
cat > "$T/notimeout/timeout" <<'EOF'
#!/usr/bin/env bash
while [ "${1#--}" != "$1" ]; do shift; done   # drop --kill-after=N
shift                                         # drop the duration: never enforced
exec "$@"
EOF
chmod +x "$T/notimeout/timeout"
STALE_MODE=late; STALE_DL=2; EXTRA_PATH="$T/notimeout"; export STALE_MODE STALE_DL EXTRA_PATH
run_case stale_late --no-12b-gate --reason 'stale ref test'
assert_stale_refused stale_late
check 'stale_late: the valid-but-late answer was NOT trusted: REFUSE says UNKNOWN past the 2s deadline' 'grep -q "UNKNOWN (read failed, timed out or returned past the 2s deadline)" <<<"$OUT" && [ "$(cat "$STUB_DIR/cnt.verify")" = 1 ]'
unset STALE_MODE STALE_DL EXTRA_PATH

echo "case: stale merge ref - EDGE 3: main moved after the overlap review, refused for a fresh admission run (never re-read and trusted)"
for mode in main_moved main_moved_confirm; do
  STALE_MODE=$mode; export STALE_MODE
  run_case "stale_$mode" --no-12b-gate --reason 'stale ref test'
  assert_stale_refused "stale_$mode"
  check "stale_$mode: REFUSE names the overlap-checked main, the moved main and asks for a fresh admission run" 'grep -q "overlap checked $STUB_MAIN_SHA, main is now $STUB_MOVED_SHA); a fresh admission run is needed" <<<"$OUT"'
  [ "$mode" != main_moved ] || check 'stale_main_moved: stopped at the first main read, the merge commit parents were never read or trusted' '! grep -q "^gh api repos/.*/commits/" "$STUB_LOG"'
done
unset STALE_MODE

echo "case: stale merge ref - merge parents mismatch, PR closed, head moved, or PR changed during the check, refused"
for mode in parents_mismatch closed head_moved pr_changed_confirm; do
  STALE_MODE=$mode; export STALE_MODE
  run_case "stale_$mode" --no-12b-gate --reason 'stale ref test'
  assert_stale_refused "stale_$mode"
  [ "$mode" != parents_mismatch ] || check 'stale_parents_mismatch: a new merge sha alone is not trusted, its parents were read' 'grep -q "^gh api repos/.*/commits/$STUB_FRESH_SHA" "$STUB_LOG"'
done
unset STALE_MODE

# ---- Lagging test-merge ref (Rajiv rule 62/S166; CTO C0ALZJHGE49 thread 1795567441.556059, ts 1795584167.126789) ----
# GitHub's test merge can sit on an older main M (STUB_LAG_SHA) while main moves on, which refused every admission on 2026-11-25. It is ADMITTED when the
# PR is not CONFLICTING, one parent is exactly the reviewed head, M is a verified ancestor of the overlap-checked main (STUB_MAIN_SHA), and the PR's changed
# files (merge-base..head against BOTH main and M, unioned) do not overlap the files main changed since M. Anything else refuses, with the overlap list.
# Git side of each case is set inline: LAG_ANCESTOR (is M an ancestor of main), LAG_PR_FILES / LAG_PR_FILES_LAGBASE (PR diff vs main-base / vs M-base),
# LAG_MAIN_FILES (main's diff M..main), LAG_DIFF_FAIL (that diff is unreadable). STALE_MODE (via LAG_MODE) sets how the merge commit's parents look.
run_lag() { local name="$1"; shift; STALE_MODE="${LAG_MODE:-lagging}" LAG_CASE=1 run_case "$name" "$@"; }

echo "case: lagging merge ref - built on an older ancestor main, the PR touches none of the files main changed since: ADMITTED, rule logged, one pass"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\nlib/pr2.ts\n' LAG_MAIN_FILES='lib/main.ts\nlib/main2.ts\nlib/main3.ts\n' \
  run_lag lag_admit --no-12b-gate --reason 'lagging ref test'
check 'lag_admit: exit 0, ADMITTED at the reviewed head, ci-head label added after the check' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && grep -q -- "--add-label ci-head:$SHA" "$STUB_LOG"'
check 'lag_admit: MERGE_REF_RULE names the lagging rule, the older main parent, the overlap-checked main, the file counts (union of both PR diffs = 2) and no changed CI input' 'grep -qx "MERGE_REF_RULE rule=lagging_ref_no_overlap pr=11173 head=$SHA merge=$STUB_FRESH_SHA main_parent=$STUB_LAG_SHA main=$STUB_MAIN_SHA pr_files=2 main_files_since_parent=3 overlap=0 ci_inputs_changed=0" <<<"$OUT"'
check 'lag_admit: the MERGE_REF_VERIFIED line is unchanged (head, overlap-checked main, merge sha)' 'grep -qx "MERGE_REF_VERIFIED pr=11173 head=$SHA main=$STUB_MAIN_SHA merge=$STUB_FRESH_SHA" <<<"$OUT"'
check 'lag_admit: the lagging rule is appended to the pm-admit-skip.log audit trail' 'grep -q "rule=lagging_ref_no_overlap pr=11173 head=$SHA merge=$STUB_FRESH_SHA main_parent=$STUB_LAG_SHA" "$T/home/.claude/logs/pm-admit-skip.log"'
check 'lag_admit: ancestry, both merge-bases, both PR diffs and main'"'"'s diff since the lagging parent were all read' 'grep -q "merge-base --is-ancestor $STUB_LAG_SHA $STUB_MAIN_SHA" "$STUB_LOG" && grep -q "merge-base $STUB_MAIN_SHA $SHA" "$STUB_LOG" && grep -q "merge-base $STUB_LAG_SHA $SHA" "$STUB_LOG" && grep -q "diff --no-renames --name-only $SHA $SHA --" "$STUB_LOG" && grep -q "diff --no-renames --name-only $STUB_LAGBASE_SHA $SHA --" "$STUB_LOG" && grep -q "diff --no-renames --name-only $STUB_LAG_SHA $STUB_MAIN_SHA --" "$STUB_LOG"'
check 'lag_admit: still one pass (2 PR reads, 2 main reads, 1 parents read) and no new GitHub read' '[ "$(cat "$STUB_DIR/cnt.verify")" = 2 ] && [ "$(cat "$STUB_DIR/cnt.main")" = 2 ] && [ "$(grep -c "^gh api repos/.*/commits/$STUB_FRESH_SHA" "$STUB_LOG")" = 1 ] && ! grep -q "^gh api repos/.*/compare/" "$STUB_LOG"'
check 'lag_admit: the trusted-CI-input helper ran once, bound to the older parent and the overlap-checked main, read-only, before the first admission write' '[ "$(grep -c "^ci-inputs " "$STUB_LOG")" = 1 ] && grep -q "^ci-inputs -I .*pm-admit-ci-inputs.py --repo .* --base $STUB_LAG_SHA --main $STUB_MAIN_SHA" "$STUB_LOG" && [ "$(grep -n "^ci-inputs " "$STUB_LOG" | head -1 | cut -d: -f1)" -lt "$(grep -nE -- "--add-label|label create" "$STUB_LOG" | head -1 | cut -d: -f1)" ]'
check 'lag_admit: every guard read precedes the first admission write' '[ "$(grep -n "git/ref/heads/main" "$STUB_LOG" | tail -1 | cut -d: -f1)" -lt "$(grep -nE -- "--add-label|label create" "$STUB_LOG" | head -1 | cut -d: -f1)" ]'
check 'lag_admit: no PR state change, no main merge, no branch write' '! grep -qE "^gh pr (close|reopen)|update-branch|^git (merge|push|reset|rebase)|--force" "$STUB_LOG" && ! grep -q "main_merge: performed" <<<"$OUT"'

echo "case: exact main parent - the original rule is unchanged and reported as exact_main_parent"
run_case lag_exact --no-12b-gate --reason 'lagging ref test'
check 'lag_exact: admitted, MERGE_REF_RULE rule=exact_main_parent, MERGE_REF_VERIFIED unchanged, no lagging audit line' '[ "$RC" = 0 ] && grep -qx "MERGE_REF_RULE rule=exact_main_parent pr=11173 head=$SHA merge=$STUB_FRESH_SHA main=$STUB_MAIN_SHA" <<<"$OUT" && grep -qx "MERGE_REF_VERIFIED pr=11173 head=$SHA main=$STUB_MAIN_SHA merge=$STUB_FRESH_SHA" <<<"$OUT" && [ "$(grep -c "rule=lagging_ref_no_overlap" "$T/home/.claude/logs/pm-admit-skip.log")" = 1 ] && ! grep -q "merge-base --is-ancestor $STUB_LAG_SHA" "$STUB_LOG"'

echo "case: lagging merge ref - the PR changes a file main changed since the older parent (overlap vs the main merge-base): REFUSED with the overlap list"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/shared.ts\nlib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\nlib/shared.ts\n' \
  run_lag lag_overlap --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_overlap
check 'lag_overlap: REFUSE says the lagging-ref rule is not met, names the older parent, the count and exactly the overlapping path' 'grep -q "lagging-ref rule not met: overlap: the PR changes 1 file(s) that main also changed since that parent $STUB_LAG_SHA, so CI did not run against main.s version of them: lib/shared.ts\. Admission needs" <<<"$OUT" && grep -q "has parents \[$SHA $STUB_LAG_SHA\] but must be exactly the reviewed head and the overlap-checked main \[$SHA $STUB_MAIN_SHA\]" <<<"$OUT"'
check 'lag_overlap: no MERGE_REF_RULE line, nothing in the lagging audit trail' '! grep -q "^MERGE_REF_RULE" <<<"$OUT" && [ "$(grep -c "rule=lagging_ref_no_overlap" "$T/home/.claude/logs/pm-admit-skip.log")" = 1 ]'

echo "case: lagging merge ref - the overlap is only visible against the older merge-base (PR branched before the older parent): REFUSED"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\nlib/shared.ts\n' LAG_MAIN_FILES='lib/shared.ts\n' \
  run_lag lag_overlap_lagbase --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_overlap_lagbase
check 'lag_overlap_lagbase: REFUSE lists the overlapping path found through the older merge-base' 'grep -q "overlap: the PR changes 1 file(s) that main also changed since that parent $STUB_LAG_SHA.*: lib/shared.ts\. Admission needs" <<<"$OUT"'

echo "case: lagging merge ref - a long overlap list is capped at 20 paths with the remainder counted"
MANY=$(for i in $(seq -w 1 25); do printf 'lib/f%s.ts\\n' "$i"; done)
LAG_ANCESTOR=yes LAG_PR_FILES="$MANY" LAG_PR_FILES_LAGBASE="$MANY" LAG_MAIN_FILES="$MANY" run_lag lag_overlap_many --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_overlap_many
check 'lag_overlap_many: 25 overlapping files reported as the first 20 plus (+5 more)' 'grep -q "the PR changes 25 file(s)" <<<"$OUT" && grep -q "lib/f01.ts " <<<"$OUT" && grep -q "lib/f20.ts \.\.\. (+5 more)" <<<"$OUT" && ! grep -q "lib/f21.ts" <<<"$OUT"'

echo "case: lagging merge ref - neither parent is the reviewed head, or the merge has the wrong parent count: REFUSED"
LAG_MODE=lagging_head_mismatch LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' run_lag lag_head_mismatch --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_head_mismatch
check 'lag_head_mismatch: REFUSE says the test merge was not built on the reviewed head, and the diffs were never read' 'grep -q "the test merge was not built on the reviewed head $SHA" <<<"$OUT" && ! grep -q "merge-base --is-ancestor $STUB_LAG_SHA" "$STUB_LOG"'
LAG_MODE=lagging_three_parents LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' run_lag lag_three_parents --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_three_parents
check 'lag_three_parents: REFUSE says the test merge has 3 parents, not exactly two' 'grep -q "the test merge has 3 parent(s), not exactly two" <<<"$OUT"'

echo "case: lagging merge ref - the older parent is not an ancestor of the overlap-checked main: REFUSED before any file comparison"
LAG_ANCESTOR=no LAG_PR_FILES='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' run_lag lag_not_ancestor --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_not_ancestor
check 'lag_not_ancestor: REFUSE says the main parent is not verified as an ancestor of the overlap-checked main; no merge-base or diff against it was read' 'grep -q "its main parent $STUB_LAG_SHA is not verified as an ancestor of the overlap-checked main $STUB_MAIN_SHA" <<<"$OUT" && ! grep -q "merge-base $STUB_LAG_SHA $SHA" "$STUB_LOG" && ! grep -q "diff --no-renames --name-only $STUB_LAG_SHA " "$STUB_LOG"'

echo "case: lagging merge ref - an unreadable main diff is UNKNOWN and refuses (fail closed), never a guess of no overlap"
LAG_ANCESTOR=yes LAG_DIFF_FAIL=1 LAG_PR_FILES='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' run_lag lag_diff_unreadable --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_diff_unreadable
check 'lag_diff_unreadable: REFUSE says the files main changed since the older parent are UNKNOWN' 'grep -q "the files main changed since $STUB_LAG_SHA are UNKNOWN (read failed, timed out or returned past the 4s deadline)" <<<"$OUT"'

echo "case: lagging merge ref - no PR file overlaps but main changed the shared selector since the older parent (73499dfb..6ee67558 touched scripts/ci/change_scope.py): REFUSED naming the path"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='scripts/ci/change_scope.py\nlib/main.ts\n' LAG_CI_INPUTS_CHANGED='scripts/ci/change_scope.py\n' \
  run_lag lag_selector --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_selector
check 'lag_selector: REFUSE says the lagging-ref rule is not met because a trusted CI input changed, names the older parent, the count and exactly scripts/ci/change_scope.py' 'grep -q "lagging-ref rule not met: trusted CI input changed: main changed 1 shared CI input file(s) that decide what runs since that parent $STUB_LAG_SHA, so CI ran the older selection even though no PR file overlaps: scripts/ci/change_scope.py\. Admission needs" <<<"$OUT"'
check 'lag_selector: the overlap check passed first (no overlap message), nothing admitted, no label or PR write, no lagging rule line or audit entry' '! grep -q "overlap: the PR changes" <<<"$OUT" && ! grep -q "^MERGE_REF_RULE" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT" && ! grep -qE -- "--add-label|--remove-label|label create|pr edit" "$STUB_LOG"'
check 'lag_selector: the helper was asked for the range older parent..overlap-checked main' 'grep -q "^ci-inputs -I .*--base $STUB_LAG_SHA --main $STUB_MAIN_SHA" "$STUB_LOG"'

echo "case: lagging merge ref - several shared CI inputs changed: all counted, listed capped at 20 paths with the remainder counted"
MANYCI=$(for i in $(seq -w 1 25); do printf '.github/workflows/w%s.yml\\n' "$i"; done)
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' LAG_CI_INPUTS_CHANGED="$MANYCI" run_lag lag_selector_many --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_selector_many
check 'lag_selector_many: 25 changed inputs reported as the first 20 plus (+5 more)' 'grep -q "main changed 25 shared CI input file(s)" <<<"$OUT" && grep -q "w01.yml " <<<"$OUT" && grep -q "w20.yml \.\.\. (+5 more)" <<<"$OUT" && ! grep -q "w21.yml" <<<"$OUT"'

echo "case: lagging merge ref - main drifted only in unrelated files (no changed CI input): ADMITTED"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='convex/other.ts\napp/page.tsx\nmodal/x.py\n' LAG_CI_INPUTS_CHANGED='' run_lag lag_unrelated --no-12b-gate --reason 'lagging ref test'
check 'lag_unrelated: exit 0, ADMITTED, MERGE_REF_RULE names the lagging rule with ci_inputs_changed=0, the helper ran once' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && grep -qx "MERGE_REF_RULE rule=lagging_ref_no_overlap pr=11173 head=$SHA merge=$STUB_FRESH_SHA main_parent=$STUB_LAG_SHA main=$STUB_MAIN_SHA pr_files=1 main_files_since_parent=3 overlap=0 ci_inputs_changed=0" <<<"$OUT" && [ "$(grep -c "^ci-inputs " "$STUB_LOG")" = 1 ]'

echo "case: lagging merge ref - the CI-input discovery is unreadable: UNKNOWN refuses (fail closed), never a guess of no change"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' LAG_CI_INPUTS_FAIL=1 run_lag lag_ci_inputs_unreadable --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_ci_inputs_unreadable
check 'lag_ci_inputs_unreadable: REFUSE says the trusted shared CI inputs main changed since the older parent are UNKNOWN; nothing admitted' 'grep -q "the trusted shared CI inputs main changed since $STUB_LAG_SHA are UNKNOWN (read failed, timed out or returned past the 4s deadline)" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT" && ! grep -qE -- "--add-label|label create" "$STUB_LOG"'

echo "case: exact main parent - the CI-input helper is not run (nothing lagged)"
run_case lag_exact_no_helper --no-12b-gate --reason 'lagging ref test'
check 'lag_exact_no_helper: admitted on the exact rule and the helper never ran' '[ "$RC" = 0 ] && grep -q "rule=exact_main_parent" <<<"$OUT" && ! grep -q "^ci-inputs " "$STUB_LOG"'

echo "case: lagging merge ref - an overlap still refuses first (the CI-input helper is not consulted)"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/shared.ts\n' LAG_PR_FILES_LAGBASE='lib/shared.ts\n' LAG_MAIN_FILES='lib/shared.ts\n' LAG_CI_INPUTS_CHANGED='scripts/ci/change_scope.py\n' run_lag lag_overlap_first --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_overlap_first
check 'lag_overlap_first: REFUSE is the overlap message and the helper never ran' 'grep -q "overlap: the PR changes 1 file(s)" <<<"$OUT" && ! grep -q "^ci-inputs " "$STUB_LOG"'

echo "case: lagging merge ref - --dry-run previews the verdict without writing or changing its exit code"
AUDITED_LAG=$(grep -c "rule=lagging_ref_no_overlap" "$T/home/.claude/logs/pm-admit-skip.log")
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' run_lag lag_dry --dry-run --no-12b-gate --reason 'lagging ref test'
check 'lag_dry: DRY RUN merge_ref ELIGIBLE with the lagging rule line, nothing admitted, exit 0' '[ "$RC" = 0 ] && grep -q "^DRY RUN merge_ref: ELIGIBLE" <<<"$OUT" && grep -q "^  MERGE_REF_RULE rule=lagging_ref_no_overlap " <<<"$OUT" && grep -q "nothing admitted" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT"'
check 'lag_dry: no audit line, no label or PR write' '[ "$(grep -c "rule=lagging_ref_no_overlap" "$T/home/.claude/logs/pm-admit-skip.log")" = "$AUDITED_LAG" ] && ! grep -qE -- "--add-label|--remove-label|label create|pr edit|update-branch" "$STUB_LOG"'
LAG_ANCESTOR=yes LAG_PR_FILES='lib/shared.ts\n' LAG_PR_FILES_LAGBASE='lib/shared.ts\n' LAG_MAIN_FILES='lib/shared.ts\n' run_lag lag_dry_overlap --dry-run --no-12b-gate --reason 'lagging ref test'
check 'lag_dry_overlap: DRY RUN merge_ref would REFUSE (rc=3) with the overlap list, dry-run exit code unchanged' '[ "$RC" = 0 ] && grep -q "^DRY RUN merge_ref: would REFUSE (rc=3)" <<<"$OUT" && grep -q "^  REFUSE stale_merge_ref #11173: .*overlap: the PR changes 1 file(s).*: lib/shared.ts\. Admission needs" <<<"$OUT"'
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='scripts/ci/change_scope.py\n' LAG_CI_INPUTS_CHANGED='scripts/ci/change_scope.py\n' run_lag lag_dry_selector --dry-run --no-12b-gate --reason 'lagging ref test'
check 'lag_dry_selector: DRY RUN merge_ref would REFUSE (rc=3) naming the changed selector, dry-run exit code unchanged' '[ "$RC" = 0 ] && grep -q "^DRY RUN merge_ref: would REFUSE (rc=3)" <<<"$OUT" && grep -q "^  REFUSE stale_merge_ref #11173: .*trusted CI input changed: .*: scripts/ci/change_scope.py\. Admission needs" <<<"$OUT"'
run_case lag_dry_exact --dry-run --no-12b-gate --reason 'lagging ref test'
check 'lag_dry_exact: a fresh exact-main-parent ref previews ELIGIBLE with the exact_main_parent rule' '[ "$RC" = 0 ] && grep -q "^DRY RUN merge_ref: ELIGIBLE" <<<"$OUT" && grep -q "^  MERGE_REF_RULE rule=exact_main_parent " <<<"$OUT"'

echo "case: static - the script never contains a PR close or reopen"
check 'pm-admit-ci.sh has no gh pr close/reopen call and no refresh_stale_merge_ref' '! grep -qE "gh pr (close|reopen)|refresh_stale_merge_ref|STALE_MERGE_REF_REFRESHED" "$SCRIPT"'
check 'pm-admit-ci.sh has no stale-ref polling loop, poll sleep or fail-open WARN, and no compare read' '! grep -qE "STALE_POLL_SLEEP|STALE_REFRESH_WAIT|verify_fresh_merge_ref|STALE_MERGE_REF_RECOMPUTED|stale-merge check unavailable|/compare/.*--jq .status" "$SCRIPT"'

finish
