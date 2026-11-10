#!/usr/bin/env bash
# Fail-closed tests for pm-admit-ci.sh (2026-10-27 empty-head bug on #11173).
# Stubs gh and git on PATH; never touches GitHub.
set -u
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/pm-admit-ci.sh"
SHA=0123456789abcdef0123456789abcdef01234567
PASS=0; FAIL=0
T=$(mktemp -d "${TMPDIR:-/tmp}/pm-admit-test.XXXXXX")

mkstubs() {
  mkdir -p "$T/bin"
  cat > "$T/bin/git" <<'EOF'
#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$STUB_LOG"
exit 1
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
  "api repos/"*"/issues/"*"/comments --paginate"*) printf '%s\n' "${REMOTE_RECEIPTS:-}" ;;
  "api repos/"*"/comments"*)
    if [ "${GATE_MODE:-same}" = none ]; then printf '[[]]\n'; else
      prefix=''; [ "${GATE_MODE:-same}" != heading ] || prefix='## '
      printf '[[{"body":"%sPM 12b: %s at %s"}]]\n' "$prefix" "${VERDICT:-PASS}" "${REVIEW_SHA:-$STUB_SHA}"; fi ;;
  "api repos/"*"/compare/"*)
    changed='[]'; [ "${GATE_MODE:-same}" != changed ] || changed='[{"filename":"lib/a.ts"}]'
    printf '{"status":"ahead","total_commits":1,"commits":[{"sha":"%s","parents":[{},{}],"commit":{"message":"Merge branch '\''main'\'' into topic"}}],"files":%s}\n' "${COMPARE_SHA:-$STUB_SHA}" "$changed" ;;
  "api repos/"*"/commits?"*) printf '[[{"sha":"%s"}]]\n' "${COMPARE_SHA:-$STUB_SHA}" ;;
  "api repos/"*"/files?"*) printf '[[{"filename":"lib/a.ts"}]]\n' ;;
  "api repos/"*"contents/"*)
    [ "${GATE_MODE:-same}" = exempt ] || exit 1
    if [[ "$args" = *change_scope.py* ]]; then
      content=$(printf 'print('\''{"control_plane_only":true,"product_changed":false}'\'')\n' | base64 | tr -d '\n')
    else content=e30=; fi
    printf '{"content":"%s"}\n' "$content" ;;
  "pr view"*"--json state"*) printf 'OPEN\n' ;;
  "pr view"*"--json closingIssuesReferences"*) printf '11173\n' ;;
  "run list"*) exit 1 ;;
  "pr diff"*) printf '%s\n' "${CHANGED_TESTS:-}" ;;
  "pr view"*"--json headRefOid,mergeable"*) emit "$MODE_HM" "$HM_VALUE" hm ;;
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

run_case() { # name; env pre-set by caller
  local name="$1"; shift
  STUB_DIR="$T/$name"; mkdir -p "$STUB_DIR"; STUB_LOG="$STUB_DIR/gh.log"; : > "$STUB_LOG"
  export STUB_DIR STUB_LOG STUB_SHA="$SHA" PM_ADMIT_RETRY_SLEEP=0
  OUT=$(PATH="$T/bin:$PATH" HOME="$T/home" bash "$SCRIPT" 11173 --no-update "$@" 2>&1); RC=$?
  LOG=$(cat "$STUB_LOG")
}
check() { if eval "$2"; then PASS=$((PASS+1)); echo "  ok   $1"; else FAIL=$((FAIL+1)); echo "  FAIL $1"; echo "$OUT" | sed 's/^/       out: /'; fi; }

assert_refused() { # name step
  check "$1: exit non-zero" '[ "$RC" -ne 0 ]'
  check "$1: no ADMITTED line" '! printf "%s" "$OUT" | grep -q ADMITTED'
  check "$1: REFUSE github_api_error $2" "printf '%s' \"\$OUT\" | grep -qx 'REFUSE: github_api_error $2'"
  check "$1: no ci-head label write" '! printf "%s" "$LOG" | grep -qE "add-label ci-head|label create"'
}

mkstubs; mkdir -p "$T/home/.claude/logs"
OKLABELS='{"labels":[]}'
export HM_VALUE="$SHA MERGEABLE" LABELS_JSON="$OKLABELS" MODE_LABELS=ok MODE_HM=ok MODE_WRITE=ok

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
check "head_flaky: no test files passes" 'printf "%s" "$OUT" | grep -q "remote red-green gate: no test files"'
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

CHANGED_TESTS='tests/unit/remote-gate.test.ts'; export CHANGED_TESTS
RECEIPT=$(printf 'REMOTE_BOX_RUN head=%s command_1_rc=0\ntests/unit/remote-gate.test.ts rc=0\nRED-on-revert: rc=0\ntypecheck rc=0' "$SHA"); export REMOTE_RECEIPTS="$RECEIPT"
run_case remote_exact
check 'remote exact-head receipt: pass' '[ "$RC" = 0 ] && printf "%s" "$OUT" | grep -q "remote red-green gate: PASS"'

NEW_HEAD=abcdefabcdefabcdefabcdefabcdefabcdefabcd
COMPARE_SHA="$NEW_HEAD" HM_VALUE="$NEW_HEAD MERGEABLE"; export COMPARE_SHA HM_VALUE
REMOTE_RECEIPTS="$RECEIPT"; export REMOTE_RECEIPTS
run_case remote_main_merge_only
check 'remote pre-admission receipt with main-merge-only head: pass' '[ "$RC" = 0 ] && printf "%s" "$OUT" | grep -q "remote red-green gate: PASS receipt_head=$SHA"'
COMPARE_SHA="$SHA" HM_VALUE="$SHA MERGEABLE"; export COMPARE_SHA HM_VALUE

REMOTE_RECEIPTS=$(printf 'REMOTE_BOX_RUN head=%s command_1_rc=0\ntests/unit/remote-gate.test.ts rc=0\nRED-on-revert: rc=0\ntypecheck rc=0' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa); export REMOTE_RECEIPTS
run_case remote_stale
check 'remote stale head: refuse' '[ "$RC" -ne 0 ] && printf "%s" "$OUT" | grep -q "REFUSE remote_redgreen_missing tests/unit/remote-gate.test.ts"'

REMOTE_RECEIPTS=$(printf 'REMOTE_BOX_RUN head=%s command_1_rc=0\nRED-on-revert: rc=0\ntypecheck rc=0' "$SHA"); export REMOTE_RECEIPTS
run_case remote_missing_file
check 'remote missing file result: refuse' '[ "$RC" -ne 0 ] && printf "%s" "$OUT" | grep -q "REFUSE remote_redgreen_missing tests/unit/remote-gate.test.ts"'

REMOTE_RECEIPTS=$(printf 'REMOTE_BOX_RUN head=%s command_1_rc=0\ntests/unit/remote-gate.test.ts rc=0\ntypecheck rc=0' "$SHA"); export REMOTE_RECEIPTS
run_case remote_missing_red
check 'remote missing RED-on-revert: refuse' '[ "$RC" -ne 0 ] && printf "%s" "$OUT" | grep -q "REFUSE remote_redgreen_missing tests/unit/remote-gate.test.ts"'

echo
echo "RESULT: $PASS passed, $FAIL failed"
/Users/rajiv/.local/bin/trash "$T" >/dev/null 2>&1 || true
[ "$FAIL" -eq 0 ]
