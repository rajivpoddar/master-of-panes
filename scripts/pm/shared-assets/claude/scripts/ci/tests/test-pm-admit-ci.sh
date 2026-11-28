#!/usr/bin/env bash
# Fail-closed tests for pm-admit-ci.sh (2026-10-27 empty-head bug on #11173).
# Stubs gh and git on PATH; never touches GitHub. Pass --merge-only for the focused S166 case.
set -u
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/pm-admit-ci.sh"
SCRIPT_DIR_VERDICT="$(dirname "$SCRIPT")/pm-admit-verdict.py"
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
T=$(cd "$T" && pwd)   # normalize: a TMPDIR ending in "/" (macOS) leaves "//" in $T, and the python3 stub's PATH-strip of its own dir (for the real-helper cases) then never matched

mkstubs() {
  mkdir -p "$T/bin"
  cat > "$T/bin/git" <<'EOF'
#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$STUB_LOG"
case "$*" in
  *'fetch -q --no-tags origin +refs/heads/main:'*) [ -z "${FETCH_FAIL:-}" ] ;;   # FETCH_FAIL=1: the inherited-override fetch fails (no inheritance)
  *'fetch -q --no-tags origin refs/pull/'*) [ -f "$STUB_DIR/updated" ] ;;
  *'rev-parse --verify origin/main^{commit}') printf '%s\n' "$STUB_MAIN_SHA" ;;   # the main that passes the overlap check; the stale-ref guard binds to it
  *'rev-parse --verify '*'^{commit}') v="${*##*--verify }"; printf '%s\n' "${v%\^\{commit\}}" ;;   # pm-admit-12b.py resolve_commit: every sha the fixtures use resolves to itself
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
  *'diff --no-renames '*)   # patch-id input (MERGED_MAIN check): the PR's own diff; PID_CASE=changed makes the diff against the merged head differ from the pre-merge one
    if [ "${PID_CASE:-same}" = changed ] && [[ "$*" = *" $STUB_MERGED_SHA --" ]]; then printf 'diff: the merge altered the PR diff\n'; else printf 'diff: the PR own change\n'; fi ;;
  *'patch-id --stable'*) h=$(cksum | cut -d' ' -f1); printf '%s %s\n' "$h" "$STUB_MERGED_SHA" ;;
  *'rev-list --parents -n 1 '*) all="$*"; printf '%s %s %s\n' "${all##* }" "$STUB_SHA" "$STUB_MAIN_SHA" ;;   # the merged head: parents = the pinned pre-merge head, then main
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
      printf '[[{"user":{"login":"%s"},"body":"%sPM 12b: %s at %s"}]]\n' "${GH_LOGIN:-pm-agent-bot}" "$prefix" "${VERDICT:-PASS}" "${REVIEW_SHA:-$STUB_SHA}"; fi ;;
  "api user"*)   # the account every PM agent posts as: the single trusted login for 12b receipts AND ci-verdict comments (GH_LOGIN=fail: unreadable)
    [ "${GH_LOGIN:-pm-agent-bot}" = fail ] && { echo "HTTP 401" >&2; exit 1; }
    printf '%s\n' "${GH_LOGIN:-pm-agent-bot}" ;;
  "api repos/"*"/issues/"*"/events"*)
    [ "${CAP_EVENTS_MODE:-ok}" = fail ] && { echo "HTTP 502" >&2; exit 1; }
    printf '%s\n' "${CAP_EVENTS_JSON:-[[]]}" ;;
  "api repos/"*"/actions/runs?head_sha="*)
    sha=${args#*head_sha=}; sha=${sha%%&*}
    cat "${CAP_FIX:-/nonexistent}/runs.$sha" 2>/dev/null || printf '[{"workflow_runs":[]}]\n' ;;
  "api repos/"*"/actions/runs/"*"/jobs"*)
    # A run is a label-wrapper gate-only run when its id is in CAP_WRAPPER_RUNS (space separated): its jobs carry the "Require exemption or exact-head real CI success" step.
    rid=${args#*/actions/runs/}; rid=${rid%%/*}
    case " ${CAP_WRAPPER_RUNS:-} " in
      *" $rid "*) printf '[{"jobs":[{"name":"gate","conclusion":"%s","started_at":"2026-11-20T10:00:00Z","completed_at":"2026-11-20T10:01:00Z","steps":[{"name":"Require exemption or exact-head real CI success","conclusion":"%s"}]}]}]\n' "${CAP_WRAPPER_STEP:-failure}" "${CAP_WRAPPER_STEP:-failure}" ;;   # CAP_WRAPPER_STEP=success: a newer GREEN wrapper
      *) printf '[{"jobs":[{"name":"typescript","conclusion":"failure","started_at":"2026-11-20T10:00:00Z","completed_at":"2026-11-20T10:20:00Z","steps":[{"name":"Confirm current paid CI success","conclusion":"failure"}]}]}]\n' ;;
    esac ;;
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
    [ "${S166_CASE:-}" = overlap ] || [ "${UPDATE_OK:-}" = 1 ] || exit 1   # UPDATE_OK=1: GitHub accepts the update (merge-only CI-input and conflict cases)
    : > "$STUB_DIR/updated" ;;
  "run list"*) exit 1 ;;
  "pr diff"*) exit 1 ;;
  "pr view"*"--json headRefOid,mergeable"*)
    if [ -f "$STUB_DIR/updated" ]; then emit "$MODE_HM" "$STUB_MERGED_SHA MERGEABLE" hm
    else emit "$MODE_HM" "$HM_VALUE" hm; fi ;;
  "pr view"*"--json headRefOid"*) emit "$MODE_HEAD" "$STUB_SHA" head ;;
  "pr view"*"--json labels,closingIssuesReferences"*) printf '{"labels":[{"name":"P2"}],"closingIssuesReferences":[]}\n' ;;
  "pr view"*"--json labels"*)
    # Race barrier (BARRIER_DIR + BARRIER_N): the first labels read of each caller drops a marker and waits (bounded) until BARRIER_N callers have reached it, so every caller has passed
    # every gate before ANY of them claims the grant; the claim at the label boundary is then the only thing that can separate them.
    if [ -n "${BARRIER_DIR:-}" ] && [ ! -e "$BARRIER_DIR/m.$(basename "$STUB_DIR")" ]; then
      : > "$BARRIER_DIR/m.$(basename "$STUB_DIR")"
      for _ in $(seq 1 300); do [ "$(ls "$BARRIER_DIR" | grep -c '^m\.')" -ge "${BARRIER_N:-2}" ] && break; /bin/sleep 0.1; done
    fi
    emit "$MODE_LABELS" "$LABELS_JSON" labels ;;
  "pr edit"*|"label create"*)
    # Ordering probe: how many override-consumed records the audit log already held when the ci-head label add (the admission) was issued.
    case "$args" in *"--add-label ci-head:"*) grep -c 'admission-cap-override-consumed=' "$HOME/.claude/logs/pm-admit-skip.log" > "$STUB_DIR/consumed_at_label_add" 2>/dev/null || echo 0 > "$STUB_DIR/consumed_at_label_add" ;; esac
    [ "${MODE_WRITE:-ok}" = fail ] && exit 1; exit 0 ;;
  *) exit 1 ;;     # includes any "pr close" / "pr reopen": the script must never call them (it is also logged above, and asserted absent)
esac
EOF
  REAL_PYTHON=$(command -v python3); export REAL_PYTHON
  cat > "$T/bin/python3" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" = *pm-ops.py* ]]; then
  printf 'pm-ops %s\n' "$*" >> "$STUB_LOG"
  # LOCK_LOG_AT_PMOPS=1: the audit ledger turns read-only right after the 12b-override audit, so only the later override-consumed write can fail.
  [ "${LOCK_LOG_AT_PMOPS:-}" = 1 ] && chmod 444 "$HOME/.claude/logs/pm-admit-skip.log"
  exit 0
fi
if [[ "$*" = *pm-admit-verdict.py*" descendant "* ]]; then   # inherited-override check: DESC_MODE=yes|no|unavailable stubs the helper's answer (its logic has its own test file); anything else runs the real helper
  printf 'verdict-descendant %s\n' "$*" >> "$STUB_LOG"
  case "${DESC_MODE:-real}" in
    yes) echo 'override_inherit: yes merge-only descendant: 3 PR file(s) byte-identical'; exit 0 ;;
    no) echo 'override_inherit: no a PR file differs between the two heads'; exit 1 ;;
    unavailable) echo 'ci_verdict: UNAVAILABLE reason=git rev-list rc=128'; exit 11 ;;
  esac
fi
if [[ "$*" = *pm-admit-ci-inputs.py* ]]; then   # the trusted-CI-input helper: changed inputs come from LAG_CI_INPUTS_CHANGED ('%b'), LAG_CI_INPUTS_FAIL=1 is an unreadable discovery
  printf 'ci-inputs %s\n' "$*" >> "$STUB_LOG"
  if [ -n "${LAG_REAL_REPO:-}" ]; then   # guard-level cases: run the REAL helper on a real temp git repo (the guard's stub shas are swapped for LAG_REAL_*); the stub git is taken off PATH for it
    helper=""; for a in "$@"; do case "$a" in *pm-admit-ci-inputs.py) helper="$a" ;; esac; done
    d=$(cd "$(dirname "$0")" && pwd); p=":$PATH:"; p="${p//:$d:/:}"; p="${p#:}"; p="${p%:}"
    PATH="$p" exec "$REAL_PYTHON" -I "$helper" --repo "$LAG_REAL_REPO" --base "$LAG_REAL_BASE" --main "$LAG_REAL_MAIN"
  fi
  [ "${LAG_CI_INPUTS_FAIL:-}" != 1 ] || exit 1
  printf '%b' "${LAG_CI_INPUTS_CHANGED:-}"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
EOF
  # --merge-only waits 8s for GitHub's asynchronous update-branch; skip exactly that wait. Every other sleep (the gh stub's hung/late modes) stays real.
  cat > "$T/bin/sleep" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" = 8 ] && exit 0
exec /bin/sleep "$@"
EOF
  chmod +x "$T/bin/gh" "$T/bin/git" "$T/bin/python3" "$T/bin/sleep"
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
    check 'merge-only/overlap: MERGED_MAIN with the merged head and an unchanged PR diff (patch-id), exit 0, no CI/E2E dispatch' 'grep -qx "MERGED_MAIN pr=11173 head=$STUB_MERGED_SHA patch_id_same=yes" <<<"$OUT" && ! grep -qE "workflow run|run rerun|--add-label|pr edit" "$STUB_LOG"'
    check 'merge-only/overlap: one update-branch pinned to original head' '[ "$(grep -c update-branch "$STUB_LOG")" = 1 ] && grep -q "update-branch -f expected_head_sha=$SHA$" "$STUB_LOG"'
  fi
  # Fix 2 (Rajiv ts 1795761962.862319): every merge-only outcome prints one MAIN_MERGE_REASON line (rule 62 decision + reason) BEFORE the legacy MAIN_MERGE: line.
  if [ "$fixture" = no_overlap ]; then
    check 'merge-only/no overlap: MAIN_MERGE_REASON decision=skip reason=no_conflict_no_overlap, rule 62, bound to head and main, printed before MAIN_MERGE: skipped' 'grep -qx "MAIN_MERGE_REASON: decision=skip reason=no_conflict_no_overlap rule=62 head=$SHA main=$STUB_MAIN_SHA files=0" <<<"$OUT" && [ "$(grep -n "^MAIN_MERGE_REASON" <<<"$OUT" | head -1 | cut -d: -f1)" -lt "$(grep -n "^MAIN_MERGE: " <<<"$OUT" | head -1 | cut -d: -f1)" ]'
  else
    check 'merge-only/overlap: MAIN_MERGE_REASON decision=merge reason=overlap files=1, printed before MAIN_MERGE: performed' 'grep -qx "MAIN_MERGE_REASON: decision=merge reason=overlap rule=62 head=$SHA main=$STUB_MAIN_SHA files=1" <<<"$OUT" && [ "$(grep -c "^MAIN_MERGE_REASON" <<<"$OUT")" = 1 ] && [ "$(grep -n "^MAIN_MERGE_REASON" <<<"$OUT" | head -1 | cut -d: -f1)" -lt "$(grep -n "^MAIN_MERGE: " <<<"$OUT" | head -1 | cut -d: -f1)" ]'
  fi
  # The merge-only stale-ref probe (CI-input deadlock fix) adds read-only guard reads: pulls/N, git/ref/heads/main and the test merge's commit.
  check "merge-only/$fixture: no 12b, labels or CI/E2E dispatch" '! grep "^gh " "$STUB_LOG" | grep -qvE "^gh (pr view .*--json (state|headRefOid,mergeable|baseRefName) |api -X PUT .*/update-branch |api repos/[^ ]*/(pulls/[0-9]+|git/ref/heads/main|commits/[0-9a-f]{40}) )" && ! grep -qE "12B_GATE|ADMITTED" <<<"$OUT"'
done

# If main moved again after review, admission must not do the preflight's write itself.
S166_CASE=overlap GATE_MODE=exempt run_script admission_overlap 11173
check 'admission/overlap: stops with NEEDS_MAIN_MERGE reason=overlap (exit 20), the merge is the separate --merge-only command' '[ "$RC" = 20 ] && grep -qx "NEEDS_MAIN_MERGE pr=11173 reason=overlap files=1" <<<"$OUT"'
check 'admission/overlap: no merge or dispatch' '! grep -qE "update-branch|pr edit|label create|workflow run|run rerun" "$STUB_LOG" && ! grep -q ADMITTED <<<"$OUT"'

# Rule 62 decision tests (MoP S97, Rajiv ts 1795829276.231109 "merge main should be separated out from ci admit"; ts 1795830085.589869 keeps ci_input_changed as a third trigger).
# Plain admission never merges main: conflict, overlap and ci_input_changed each stop with NEEDS_MAIN_MERGE (exit 20, nothing written); only no-conflict/no-overlap admits.
# conflict -> lag_conflicting, overlap -> admission_overlap (above), ci_input_changed -> the lag_* cases (assert_needs_merge); below: the "none" side and the merge command.
S166_CASE=no_overlap GATE_MODE=exempt run_script admission_no_overlap 11173
check 'rule 62 decision: no conflict and no overlap -> admits (exit 0, ADMITTED, no NEEDS_MAIN_MERGE), and no merge was made' '[ "$RC" = 0 ] && grep -q "^ADMITTED" <<<"$OUT" && ! grep -q NEEDS_MAIN_MERGE <<<"$OUT" && ! grep -q update-branch "$STUB_LOG"'
check 'rule 62 decision: the three triggers share one stop line shape (conflict/overlap/ci_input_changed) and the exit code 20' 'grep -c "NEEDS_MAIN_MERGE pr=\$PR reason=\$MERGE_REASON files=\$MERGE_FILES\|NEEDS_MAIN_MERGE pr=\$PR reason=ci_input_changed files=" "$SCRIPT" | grep -qx 2 && [ "$(grep -c "exit 20" "$SCRIPT")" -ge 2 ]'
# The merge command: merges, never dispatches CI/E2E, and reports when the merge altered the PR's own diff (patch-id) so the caller does not trust it blindly.
S166_CASE=overlap GATE_MODE=none PID_CASE=changed run_script merge_only_pid_changed --merge-only 11173
check 'merge command: when the merge changed the PR diff it prints MERGED_MAIN ... patch_id_same=no, exit 0, still no dispatch' '[ "$RC" = 0 ] && grep -qx "MERGED_MAIN pr=11173 head=$STUB_MERGED_SHA patch_id_same=no" <<<"$OUT" && ! grep -qE "workflow run|run rerun|--add-label|pr edit" "$STUB_LOG"'

# ---- --merge-only and a changed trusted CI input (PM brief 2026-11-26, ladder pel-mwx1mo05-a904f753) ----
# Deadlock: a lagging refs/pull/N/merge whose main parent M lacks a changed shared CI input (scripts/ci/change-scope-rules.json, scripts/ci/e2e-functional-areas.json,
# scripts/ci/tests/*) is REFUSED by admission (stale_merge_ref, exit 3), --merge-only skipped the same PR on no_conflict_no_overlap, and GitHub never rebuilds the ref.
# Now --merge-only runs the guard read-only (ci_input_probe) and treats EXACTLY "the changed-CI-input list is non-empty" as an overlap-class reason (the non-force
# update-branch, pinned to the pre-merge head). Every other guard outcome keeps the old skip with no write. run_mo = a no-conflict/no-overlap PR whose test merge is
# built on the older main STUB_LAG_SHA (lagging); the caller sets UPDATE_OK, LAG_CI_INPUTS_CHANGED / LAG_CI_INPUTS_FAIL and MO_MODE / MO_ANCESTOR / MO_MAIN_FILES / STALE_DL.
run_mo() { local name="$1"; shift
  S166_CASE=no_overlap GATE_MODE=none LAG_CASE=1 STALE_MODE="${MO_MODE:-lagging}" LAG_ANCESTOR="${MO_ANCESTOR:-yes}" \
    LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES="${MO_MAIN_FILES:-lib/main.ts\\n}" \
    run_script "$name" --merge-only 11173 "$@"
}
skiplog_lines() { { cat "$T/home/.claude/logs/pm-admit-skip.log" 2>/dev/null || true; } | wc -l | tr -d ' '; }
CI_WRITES='^gh (pr edit|pr close|pr reopen|label create|workflow run|run rerun)|--add-label|--remove-label|^git (-C [^ ]+ )?(merge |push|reset|rebase)|--force'
CI_TWO='scripts/ci/change-scope-rules.json\nscripts/ci/e2e-functional-areas.json\n'
assert_mo_skipped() { # name: the old skip (exit 0, head unchanged), and nothing was written
  check "$1: skipped at the unchanged head, exit 0, no probe line" '[ "$RC" = 0 ] && grep -qx "MAIN_MERGE: skipped head=$SHA reason=no_conflict_no_overlap" <<<"$OUT" && ! grep -q "STALE_REF_PROBE\|reason=ci_input_changed\|performed" <<<"$OUT"'
  check "$1: no update-branch, no PR/label/branch write, no skip-log audit line" '! grep -q update-branch "$STUB_LOG" && ! grep -qE "$CI_WRITES" "$STUB_LOG" && [ "$(skiplog_lines)" = "$SKIPLOG_N" ]'
}

echo 'case: --merge-only, changed trusted CI inputs on a lagging test merge (the deadlock): non-force main merge pinned to the pre-merge head'
SKIPLOG_N=$(skiplog_lines)
UPDATE_OK=1 LAG_CI_INPUTS_CHANGED="$CI_TWO" run_mo mo_ci_changed
check 'mo_ci_changed: performed at the merged head with reason ci_input_changed and the changed-input count (2)' '[ "$RC" = 0 ] && grep -qx "MAIN_MERGE: performed head=$STUB_MERGED_SHA reason=ci_input_changed files=2" <<<"$OUT"'
check 'mo_ci_changed: the probe line names the count, the older main parent, the overlap-checked main and exactly the changed inputs' 'grep -qx "STALE_REF_PROBE ci_inputs_changed n=2 main_parent=$STUB_LAG_SHA main=$STUB_MAIN_SHA files=scripts/ci/change-scope-rules.json scripts/ci/e2e-functional-areas.json" <<<"$OUT"'
check 'mo_ci_changed: exactly one update-branch, non-force, pinned to the original head, after every guard read' '[ "$(grep -c update-branch "$STUB_LOG")" = 1 ] && grep -q "^gh api -X PUT repos/.*/pulls/11173/update-branch -f expected_head_sha=$SHA$" "$STUB_LOG" && [ "$(grep -n "git/ref/heads/main" "$STUB_LOG" | tail -1 | cut -d: -f1)" -lt "$(grep -n update-branch "$STUB_LOG" | head -1 | cut -d: -f1)" ]'
check 'mo_ci_changed: the helper ran once, read-only, bound to the older parent and the overlap-checked main' '[ "$(grep -c "^ci-inputs " "$STUB_LOG")" = 1 ] && grep -q "^ci-inputs -I .*pm-admit-ci-inputs.py --repo .* --base $STUB_LAG_SHA --main $STUB_MAIN_SHA" "$STUB_LOG"'
check 'mo_ci_changed: the probe wrote nothing else: no label/PR/branch write, no skip-log audit line, no waiver or admission output, no refusal' '! grep -qE "$CI_WRITES" "$STUB_LOG" && [ "$(skiplog_lines)" = "$SKIPLOG_N" ] && ! grep -qE "12B_GATE|ADMITTED|MERGE_REF_RULE|MERGE_REF_VERIFIED|REFUSE" <<<"$OUT"'
check 'mo_ci_changed: exactly one MAIN_MERGE_REASON, decision=merge reason=ci_input_changed files=2 (the CI input count), after the probe line and before MAIN_MERGE: performed' '[ "$(grep -c "^MAIN_MERGE_REASON" <<<"$OUT")" = 1 ] && grep -qx "MAIN_MERGE_REASON: decision=merge reason=ci_input_changed rule=62 head=$SHA main=$STUB_MAIN_SHA files=2" <<<"$OUT" && [ "$(grep -n "^STALE_REF_PROBE" <<<"$OUT" | cut -d: -f1)" -lt "$(grep -n "^MAIN_MERGE_REASON" <<<"$OUT" | cut -d: -f1)" ] && [ "$(grep -n "^MAIN_MERGE_REASON" <<<"$OUT" | cut -d: -f1)" -lt "$(grep -n "^MAIN_MERGE: performed" <<<"$OUT" | cut -d: -f1)" ]'

echo 'case: --merge-only, lagging test merge but no changed trusted CI input (and no overlap): skipped, nothing written'
SKIPLOG_N=$(skiplog_lines)
UPDATE_OK=1 LAG_CI_INPUTS_CHANGED='' run_mo mo_ci_unchanged
assert_mo_skipped mo_ci_unchanged
check 'mo_ci_unchanged: the guard did consult the helper once (the skip is a decision, not a shortcut), and the lagging waiver left no audit line' '[ "$(grep -c "^ci-inputs " "$STUB_LOG")" = 1 ]'
check 'mo_ci_unchanged: MAIN_MERGE_REASON decision=skip (rule 62: no conflict, no overlap, no changed CI input), never decision=merge' 'grep -qx "MAIN_MERGE_REASON: decision=skip reason=no_conflict_no_overlap rule=62 head=$SHA main=$STUB_MAIN_SHA files=0" <<<"$OUT" && ! grep -q "decision=merge" <<<"$OUT"'

echo 'case: --merge-only, conflict with main: still the non-force update-branch (conflict is a merge reason; the CI-input probe is not consulted)'
HM_VALUE="$SHA CONFLICTING" S166_CASE=conflict GATE_MODE=none UPDATE_OK=1 run_script mo_conflict --merge-only 11173
check 'mo_conflict: performed at the merged head with reason conflict' '[ "$RC" = 0 ] && grep -qx "MAIN_MERGE: performed head=$STUB_MERGED_SHA reason=conflict files=0" <<<"$OUT"'
check 'mo_conflict: one update-branch pinned to the original head; no stale-ref guard read, no helper call' '[ "$(grep -c update-branch "$STUB_LOG")" = 1 ] && grep -q "update-branch -f expected_head_sha=$SHA$" "$STUB_LOG" && ! grep -q "^ci-inputs " "$STUB_LOG" && ! grep -q "git/ref/heads/main" "$STUB_LOG"'
check 'mo_conflict: MAIN_MERGE_REASON decision=merge reason=conflict' 'grep -qx "MAIN_MERGE_REASON: decision=merge reason=conflict rule=62 head=$SHA main=$STUB_MAIN_SHA files=0" <<<"$OUT"'
HM_VALUE="$SHA CONFLICTING" S166_CASE=conflict GATE_MODE=none run_script mo_conflict_rejected --merge-only 11173
check 'mo_conflict_rejected: GitHub refuses the update -> REFUSE conflict, route to slot (exit 2), one update-branch attempt, no helper call' '[ "$RC" = 2 ] && grep -qx "REFUSE: conflict with main; route to slot" <<<"$OUT" && [ "$(grep -c update-branch "$STUB_LOG")" = 1 ] && ! grep -q "^ci-inputs " "$STUB_LOG"'

echo 'case: --merge-only, ordinary file overlap is unchanged (merge; the CI-input probe is not consulted)'
S166_CASE=overlap GATE_MODE=none run_script mo_overlap --merge-only 11173
check 'mo_overlap: performed at the merged head with reason overlap, no stale-ref guard read, no helper call' '[ "$RC" = 0 ] && grep -qx "MAIN_MERGE: performed head=$STUB_MERGED_SHA reason=overlap files=1" <<<"$OUT" && ! grep -q "^ci-inputs " "$STUB_LOG" && ! grep -q "git/ref/heads/main" "$STUB_LOG"'

echo 'case: --merge-only, every other guard outcome stays the old no-write skip (fail closed): unreadable helper, overlap with the older parent, older parent not an ancestor, unreadable PR, main moved, timeout'
SKIPLOG_N=$(skiplog_lines)
UPDATE_OK=1 LAG_CI_INPUTS_FAIL=1 LAG_CI_INPUTS_CHANGED="$CI_TWO" run_mo mo_unknown_helper
assert_mo_skipped mo_unknown_helper
UPDATE_OK=1 MO_MAIN_FILES='lib/pr.ts\n' LAG_CI_INPUTS_CHANGED="$CI_TWO" run_mo mo_lag_overlap
assert_mo_skipped mo_lag_overlap
check 'mo_lag_overlap: refused on overlap before the CI-input check, so the helper was never asked' '! grep -q "^ci-inputs " "$STUB_LOG"'
UPDATE_OK=1 MO_ANCESTOR=no LAG_CI_INPUTS_CHANGED="$CI_TWO" run_mo mo_not_ancestor
assert_mo_skipped mo_not_ancestor
check 'mo_not_ancestor: the older parent is unverified, so the helper was never asked' '! grep -q "^ci-inputs " "$STUB_LOG"'
UPDATE_OK=1 MO_MODE=unreadable LAG_CI_INPUTS_CHANGED="$CI_TWO" run_mo mo_unreadable_pr
assert_mo_skipped mo_unreadable_pr
UPDATE_OK=1 MO_MODE=main_moved LAG_CI_INPUTS_CHANGED="$CI_TWO" run_mo mo_main_moved
assert_mo_skipped mo_main_moved
UPDATE_OK=1 MO_MODE=hung STALE_DL=2 LAG_CI_INPUTS_CHANGED="$CI_TWO" run_mo mo_timeout
assert_mo_skipped mo_timeout

echo 'case: --merge-only --dry-run previews the decision and never calls update-branch'
SKIPLOG_N=$(skiplog_lines)
UPDATE_OK=1 LAG_CI_INPUTS_CHANGED="$CI_TWO" run_mo mo_dry_ci --dry-run
check 'mo_dry_ci: would attempt update-branch for ci_input_changed (2 inputs) at the current head and main, exit 0, nothing changed' '[ "$RC" = 0 ] && grep -qx "MAIN_MERGE: dry-run would attempt update-branch head=$SHA reason=ci_input_changed files=2 main=$STUB_MAIN_SHA (nothing was changed)" <<<"$OUT" && grep -q "^STALE_REF_PROBE ci_inputs_changed n=2 " <<<"$OUT"'
check 'mo_dry_ci: the dry run prints the same MAIN_MERGE_REASON (decision=merge reason=ci_input_changed files=2) before the dry-run line' 'grep -qx "MAIN_MERGE_REASON: decision=merge reason=ci_input_changed rule=62 head=$SHA main=$STUB_MAIN_SHA files=2" <<<"$OUT" && [ "$(grep -n "^MAIN_MERGE_REASON" <<<"$OUT" | cut -d: -f1)" -lt "$(grep -n "^MAIN_MERGE: dry-run" <<<"$OUT" | cut -d: -f1)" ]'
check 'mo_dry_ci: no update-branch, no PR/label/branch write, no skip-log audit line, no fetch of the PR head after a merge' '! grep -q update-branch "$STUB_LOG" && ! grep -qE "$CI_WRITES" "$STUB_LOG" && [ "$(skiplog_lines)" = "$SKIPLOG_N" ] && ! grep -q "fetch -q --no-tags origin refs/pull/" "$STUB_LOG"'
UPDATE_OK=1 LAG_CI_INPUTS_CHANGED='' run_mo mo_dry_unchanged --dry-run
assert_mo_skipped mo_dry_unchanged
S166_CASE=overlap GATE_MODE=none run_script mo_dry_overlap --merge-only 11173 --dry-run
check 'mo_dry_overlap: would attempt update-branch for an ordinary overlap, and made no update-branch call' '[ "$RC" = 0 ] && grep -qx "MAIN_MERGE: dry-run would attempt update-branch head=$SHA reason=overlap files=1 main=$STUB_MAIN_SHA (nothing was changed)" <<<"$OUT" && ! grep -q update-branch "$STUB_LOG"'

echo 'case: --merge-only argument errors (usage, exit 64, no GitHub call)'
for bad in 'abc' '11173 --bogus' '11173 --no-update' '11173 --dry-run --dry-run' '11173 --dry-run extra'; do
  # shellcheck disable=SC2086
  S166_CASE=no_overlap GATE_MODE=none run_script mo_usage --merge-only $bad
  check "mo_usage '$bad': usage line, exit 64, nothing called" '[ "$RC" = 64 ] && grep -qx "Usage: pm-admit-ci.sh --merge-only <PR number> \[--dry-run\]" <<<"$OUT" && [ ! -s "$STUB_LOG" ]'
done
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

# ---- Admission round breaker (ADMISSION_ROUND_BREAKER) -----------------------------------------------------
# Rajiv C0ALZJHGE49 thread 1795669948.123989, ts 1795676115.108879. Exercise the real Bash gate and Python helper with stub GitHub history.
CAP_FIX="$T/capfix"; mkdir -p "$CAP_FIX"; export CAP_FIX
CH1=1111111111111111111111111111111111111111; CH2=2222222222222222222222222222222222222222
OVR=cto:1795324706.834969
cap_events() { # <head>... -> one exact ci-head label event per head, oldest first
  local out='[[' sep='' i=0 s
  for s in "$@"; do i=$((i+1)); out="$out$sep{\"id\":$i,\"event\":\"labeled\",\"created_at\":\"2026-11-20T1$i:00:00Z\",\"label\":{\"name\":\"ci-head:$s\"}}"; sep=,; done
  printf '%s]]' "$out"
}
cap_red() { # <head> <run id> [attempt=1]: one real (30 min) red ci.yml pull_request run at that head
  printf '[{"workflow_runs":[{"id":%s,"head_sha":"%s","event":"pull_request","path":".github/workflows/ci.yml","name":"CI","conclusion":"failure","run_attempt":%s,"run_started_at":"2026-11-20T10:00:00Z","updated_at":"2026-11-20T10:30:00Z","pull_requests":[]}]}]\n' "$2" "$1" "${3:-1}" > "$CAP_FIX/runs.$1"
}
cap_red_pair() { # <head> <real run id> <wrapper run id> <wrapper conclusion>: a substantive red ci.yml run AND a higher-id label-wrapper run with the IDENTICAL start time (the #14950 shape)
  printf '[{"workflow_runs":[{"id":%s,"head_sha":"%s","event":"pull_request","path":".github/workflows/ci.yml","name":"CI","conclusion":"failure","run_attempt":1,"run_started_at":"2026-11-20T10:00:00Z","updated_at":"2026-11-20T10:30:00Z","pull_requests":[]},{"id":%s,"head_sha":"%s","event":"pull_request","path":".github/workflows/ci.yml","name":"CI","conclusion":"%s","run_attempt":1,"run_started_at":"2026-11-20T10:00:00Z","updated_at":"2026-11-20T10:01:00Z","pull_requests":[]}]}]\n' "$2" "$1" "$3" "$1" "$4" > "$CAP_FIX/runs.$1"
}
cap_verdict() { # <head> <run id> <classification> [attempt=1] -> one PRODUCER-SHAPE ci-failure-investigation verdict (marker is the FIRST line; verdict JSON names run_id AND the full head)
  printf '{"id":%s,"user":{"login":"%s"},"created_at":"2026-11-20T12:00:00Z","body":"<!-- ci-failure-investigation:run=%s attempt=%s head=%s -->\\n## CI failure investigation verdict\\n<!-- ci-verdict: {\\"classification\\":\\"%s\\",\\"run_id\\":%s,\\"head\\":\\"%s\\"} -->"}' "$2" "${VERDICT_AUTHOR:-pm-agent-bot}" "$2" "${4:-1}" "$1" "$3" "$2" "$1"
}
cap_red $CH1 101; cap_red $CH2 102
GATE_MODE=none; export GATE_MODE

echo "case: breaker - the first and second rounds are allowed"
CAP_EVENTS_JSON=$(cap_events); export CAP_EVENTS_JSON
for prior in 0 1; do
  run_case "cap_under_$prior" --no-12b-gate --reason 'cap test'
  check "cap_under_$prior: count and actual admission" '[ "$RC" = 0 ] && grep -qx "ADMISSION_ROUND_BREAKER: ok pr=11173 rounds=$prior cap=2" <<<"$OUT" && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && grep -q -- "--add-label ci-head:$SHA" "$STUB_LOG"'
  CAP_EVENTS_JSON=$(cap_events "$CH1")
done

cap_verdict_full() { # <head> <run id> <classification> <failing tests> <follow-up> <evidence> -> a verdict with the S171 packet fields
  printf '{"id":%s,"user":{"login":"%s"},"created_at":"2026-11-20T12:00:00Z","html_url":"https://github.example/c/%s","body":"<!-- ci-failure-investigation:run=%s attempt=1 head=%s -->\\n<!-- ci-verdict: {\\"classification\\":\\"%s\\",\\"run_id\\":%s,\\"head\\":\\"%s\\",\\"failing_tests\\":\\"%s\\",\\"follow_up\\":\\"%s\\",\\"evidence\\":\\"%s\\"} -->"}' "$2" "${VERDICT_AUTHOR:-pm-agent-bot}" "$2" "$2" "$1" "$3" "$2" "$1" "$4" "$5" "$6"
}

# Fix 3 (Rajiv ts 1795761962.862319; CTO REVISE ts 1795766409.314279): when the breaker trips and the LIVE head's latest SUBSTANTIVE red is flaky/infra/inherited-main/
# out-of-diff, print the S171 admin-ask packet bound to the LIVE head (exit 10 still, nothing admitted) instead of asking for an override. Earlier heads inform diagnosis
# only and never carry the packet. Fixture: round 1 was admitted at $CH1, round 2 at the live head $SHA (red run 102), so the cap is spent.
echo "case: breaker - two flake-only rounds: the third is the S171 admin-ask packet for the LIVE head, not an override request"
CAP_EVENTS_JSON=$(cap_events $CH1 $SHA); export CAP_EVENTS_JSON
cap_red $CH1 101; cap_red $SHA 102
CAP_COMMENTS_JSON="[[$(cap_verdict $CH1 101 FLAKY),$(cap_verdict $SHA 102 FLAKY)]]"; export CAP_COMMENTS_JSON
run_case cap_hit
check 'cap_hit: exit 10 and two spent rounds' '[ "$RC" = 10 ] && grep -qx "ADMISSION_ROUND_BREAKER: cap_reached pr=11173 rounds=2 cap=2" <<<"$OUT"'
check 'cap_hit: refusal, the packet bound to the LIVE head, and the CTO admin-merge framing' 'grep -qx "REFUSE: ADMISSION_ROUND_BREAKER #11173; nothing admitted" <<<"$OUT" && grep -qx "ADMIN_ASK_PACKET (S171/S157) #11173 head=$SHA" <<<"$OUT" && grep -q "admin-merge decision for the CTO, not a re-admission and not an override" <<<"$OUT"'
check 'cap_hit: no override or Decision-required prompt' '! grep -qE "^Decision required|Re-admission requires|PM_ADMIT_CAP_OVERRIDE=" <<<"$OUT"'
check 'cap_hit: reads history and verdicts but admits, labels and dispatches nothing' 'grep -q "/comments" "$STUB_LOG" && ! grep -qE "pr edit|label create|update-branch|workflow run|add-label" "$STUB_LOG" && ! grep -q ADMITTED <<<"$OUT"'
check 'cap_hit: the packet names the live head run id@attempt, class, and marks missing fields UNAVAILABLE rather than inventing them; the earlier head run is not used' 'grep -q "^  run [^ ]*:102@1 conclusion=failure class=flaky" <<<"$OUT" && grep -q "^  failing_tests: UNAVAILABLE" <<<"$OUT" && grep -q "^  follow_up_issue: UNAVAILABLE" <<<"$OUT" && ! grep -q ":101@" <<<"$OUT"'

echo "case: breaker - the packet carries the verdict failing tests, follow-up issue and evidence"
CAP_COMMENTS_JSON="[[$(cap_verdict $CH1 101 flaky),$(cap_verdict_full $SHA 102 infra 'tests/a.test.ts > saves' '#15001' 'runner lost network mid-job')]]"; export CAP_COMMENTS_JSON
run_case cap_hit_full
check 'cap_hit_full: packet has evidence, failing tests, follow-up and the S167 next step' '[ "$RC" = 10 ] && grep -q "^    evidence: runner lost network mid-job" <<<"$OUT" && grep -qx "  failing_tests: tests/a.test.ts > saves" <<<"$OUT" && grep -qx "  follow_up_issue: #15001" <<<"$OUT" && grep -q "class=infra" <<<"$OUT" && grep -q "verdict comment: https://github.example/c/102" <<<"$OUT" && grep -q "enforce_admins off -> gh pr merge --squash --admin --match-head-commit" <<<"$OUT"'

for cls in flake inherited-main out-of-diff; do
  CAP_COMMENTS_JSON="[[$(cap_verdict $CH1 101 flaky),$(cap_verdict $SHA 102 "$cls")]]"; export CAP_COMMENTS_JSON
  run_case "cap_hit_$cls"
  check "cap_hit_$cls: a $cls red is the packet, exit 10, no override ask" '[ "$RC" = 10 ] && grep -q "^ADMIN_ASK_PACKET (S171/S157)" <<<"$OUT" && ! grep -qE "^Decision required|Re-admission requires" <<<"$OUT"'
done

echo "case: breaker - the #14950 shape at the cap: a higher-id label-wrapper red with the same start time never replaces the substantive red or its verdict"
cap_red_pair $SHA 102 103 failure
CAP_WRAPPER_RUNS=103; export CAP_WRAPPER_RUNS
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 102 flaky)]]"; export CAP_COMMENTS_JSON
run_case cap_hit_wrapper
check 'cap_hit_wrapper: the substantive run 102 is selected with its existing verdict (the packet), the wrapper 103 is ignored and never asked to be classified' '[ "$RC" = 10 ] && grep -q "^ADMIN_ASK_PACKET (S171/S157)" <<<"$OUT" && grep -q "^  run [^ ]*:102@1 conclusion=failure class=flaky" <<<"$OUT" && ! grep -q ":103@" <<<"$OUT" && ! grep -q "^REFUSE: ci_verdict_required" <<<"$OUT"'
unset CAP_WRAPPER_RUNS
cap_red $SHA 102

echo "case: breaker - a verdict from an earlier attempt does not govern a later attempt's failure (classify first, never the packet)"
cap_red $SHA 102 2
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 102 flaky 1)]]"; export CAP_COMMENTS_JSON
run_case cap_hit_stale_attempt
check 'cap_hit_stale_attempt: classify first for run 102 attempt 2, the attempt-1 verdict is not used, no packet' '[ "$RC" = 10 ] && grep -q "^REFUSE: ci_verdict_required #11173 head=$SHA red run(s) [^ ]*:102@2 " <<<"$OUT" && grep -q "classify first" <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET" <<<"$OUT"'
cap_red $SHA 102

echo "case: breaker - a PR-caused red, no verdict, or an unreadable verdict keeps the override ask (never the packet)"
CAP_COMMENTS_JSON="[[$(cap_verdict $CH1 101 flaky),$(cap_verdict $SHA 102 pr-caused)]]"; export CAP_COMMENTS_JSON
run_case cap_hit_prcaused
check 'cap_hit_prcaused: Decision required + override ask, no packet' '[ "$RC" = 10 ] && [ "$(grep -c "^Decision required" <<<"$OUT")" = 1 ] && grep -q "Re-admission requires an audited override: PM_ADMIT_CAP_OVERRIDE=cto:<slack ts>" <<<"$OUT" && grep -q "S157 admin-merge ask for out-of-diff/flake/infra only with executed-green E2E.*fix a PR-caused failure; or slice/freeze" <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET" <<<"$OUT" && grep -q "class=pr-caused" <<<"$OUT"'
CAP_COMMENTS_JSON="[[$(cap_verdict $CH1 101 flaky)]]"; export CAP_COMMENTS_JSON
run_case cap_hit_noverdict
check 'cap_hit_noverdict: the live head red has no verdict -> classify first, then the override ask, no packet' '[ "$RC" = 10 ] && grep -q "^REFUSE: ci_verdict_required #11173 head=$SHA " <<<"$OUT" && grep -q "classify first" <<<"$OUT" && grep -q "Re-admission requires an audited override" <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET" <<<"$OUT"'
CAP_COMMENTS_JSON='this is not json'; export CAP_COMMENTS_JSON
run_case cap_hit_unreadable
check 'cap_hit_unreadable: unreadable verdicts are UNAVAILABLE with a reason, never a classification' '[ "$RC" = 10 ] && grep -q "^ci_verdict: UNAVAILABLE reason=" <<<"$OUT" && grep -q "Re-admission requires an audited override" <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET" <<<"$OUT"'

echo "case: breaker - the live head has no red substantive run; an EARLIER head's flaky red is diagnosis only (no packet, override ask kept)"
CAP_EVENTS_JSON=$(cap_events $CH1 $CH2); export CAP_EVENTS_JSON
CAP_COMMENTS_JSON="[[$(cap_verdict $CH1 101 flaky),$(cap_verdict $CH2 102 FLAKY)]]"; export CAP_COMMENTS_JSON
run_case cap_hit_histonly
check 'cap_hit_histonly: exit 10, an informational historical line for the earlier head, NO packet bound to it, the override ask is kept' '[ "$RC" = 10 ] && grep -qx "ADMISSION_ROUND_BREAKER: cap_reached pr=11173 rounds=2 cap=2" <<<"$OUT" && grep -q "^ci_verdict: info historical head=$CH2 red run(s) [^ ]*:102@1 (diagnosis only" <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET" <<<"$OUT" && grep -q "Re-admission requires an audited override" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT"'
unset CAP_COMMENTS_JSON
printf '[{"workflow_runs":[]}]\n' > "$CAP_FIX/runs.$SHA"   # the live head has no CI run again for the override cases below (events: CH1 + CH2 only)

echo "case: cap - a CTO-approved override re-admits and is audited; a dry run is not"
PM_ADMIT_CAP_OVERRIDE=$OVR@11173:$SHA; export PM_ADMIT_CAP_OVERRIDE
run_case cap_override --no-12b-gate --reason 'cap test'
check 'cap_override: override line, admitted' '[ "$RC" = 0 ] && grep -q "^ADMISSION_CAP_OVERRIDE: $OVR pr=11173" <<<"$OUT" && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT"'
check 'cap_override: granted at gate time AND consumed once before the label add, both bound to the head and approval' 'grep -q "PR=11173 head=$SHA admission-cap-override-granted=$OVR" "$T/home/.claude/logs/pm-admit-skip.log" && [ "$(grep -c "PR=11173 head=$SHA admission-cap-override-consumed=$OVR\$" "$T/home/.claude/logs/pm-admit-skip.log")" = 1 ] && grep -q "^ADMISSION_CAP_OVERRIDE_CONSUMED: $OVR pr=11173 head=$SHA" <<<"$OUT"'
check 'cap_override: the consumed record already existed when the ci-head label add was issued (consumed before admission, not after)' '[ "$(cat "$T/cap_override/consumed_at_label_add")" = 1 ]'
check 'cap_override: one override line pair only, no legacy un-kinded line' '! grep -qE "admission-cap-override=" "$T/home/.claude/logs/pm-admit-skip.log"'
AUDITED=$(grep -c admission-cap-override "$T/home/.claude/logs/pm-admit-skip.log")
PM_ADMIT_CAP_OVERRIDE=rajiv:1795676115.108879@11173:$SHA run_case cap_override_dry --dry-run --no-12b-gate --reason 'cap test'
check 'cap_override --dry-run: Rajiv override allowed, no admission or audit write (no granted, no consumed)' '[ "$RC" = 0 ] && grep -q "^ADMISSION_CAP_OVERRIDE: rajiv:" <<<"$OUT" && grep -q "nothing admitted" <<<"$OUT" && [ "$(grep -c admission-cap-override "$T/home/.claude/logs/pm-admit-skip.log")" = "$AUDITED" ] && ! grep -q "rajiv:1795676115.108879" "$T/home/.claude/logs/pm-admit-skip.log"'
: > "$STUB_LOG"
OUT=$(PATH="$T/bin:$PATH" HOME="$T/no-audit-home" bash "$SCRIPT" 11173 2>&1); RC=$?
check 'cap_override: missing audit sink refuses before any writes' '[ "$RC" = 7 ] && grep -qx "REFUSE: admission_cap_override_audit_unavailable" <<<"$OUT" && ! grep -qE "pr edit|label create" "$STUB_LOG"'
PM_ADMIT_CAP_OVERRIDE=yes; export PM_ADMIT_CAP_OVERRIDE
run_case cap_override_bad --no-12b-gate --reason 'cap test'
check 'cap_override_bad: a malformed override is refused (64) with no writes' '[ "$RC" = 64 ] && grep -q "must look like cto:<slack ts>" <<<"$OUT" && ! grep -qE "pr edit|label create" "$STUB_LOG"'

echo "case: one pair - an override id that was already consumed on this PR is refused when re-presented (a new CTO/Rajiv override is required)"
SKIPN=$(wc -l < "$T/home/.claude/logs/pm-admit-skip.log" | tr -d ' ')
PM_ADMIT_CAP_OVERRIDE=$OVR@11173:$SHA run_case cap_override_reuse --no-12b-gate --reason 'cap test'
check 'cap_override_reuse: exit 10, REFUSE names the spent override, nothing admitted, no label write, no new audit line' '[ "$RC" = 10 ] && grep -q "^REFUSE: ADMISSION_ROUND_BREAKER #11173; override $OVR was already consumed by an earlier admission (one override funds exactly one admission pair)" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && ! grep -qE "pr edit|label create|add-label" "$STUB_LOG" && [ "$(wc -l < "$T/home/.claude/logs/pm-admit-skip.log" | tr -d " ")" = "$SKIPN" ]'
PM_ADMIT_CAP_OVERRIDE=cto:1795766409.314279@11173:$SHA run_case cap_override_fresh --no-12b-gate --reason 'cap test'
check 'cap_override_fresh: a DIFFERENT (new) approval id is accepted at the same cap and funds exactly one more pair' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && grep -q "admission-cap-override-granted=cto:1795766409.314279" "$T/home/.claude/logs/pm-admit-skip.log" && [ "$(grep -c "admission-cap-override-consumed=cto:1795766409.314279" "$T/home/.claude/logs/pm-admit-skip.log")" = 1 ]'
unset PM_ADMIT_CAP_OVERRIDE

echo "case: cap - an unreadable record is UNAVAILABLE and fails closed, never a guess"
CAP_EVENTS_MODE=fail; export CAP_EVENTS_MODE
run_case cap_unavailable --no-12b-gate --reason 'cap test'
check 'cap_unavailable: reason and REFUSE github_api_error (7)' '[ "$RC" = 7 ] && grep -qx "ADMISSION_ROUND_BREAKER: UNAVAILABLE reason=issues/11173/events: gh_rc_1" <<<"$OUT" && grep -qx "REFUSE: github_api_error admission_cap" <<<"$OUT"'
check 'cap_unavailable: no admission writes' '! grep -qE "pr edit|label create" "$STUB_LOG"'
PM_ADMIT_CAP_OVERRIDE=$OVR@11173:$SHA; export PM_ADMIT_CAP_OVERRIDE
run_case cap_unavailable_override --no-12b-gate --reason 'cap test'
check 'cap_unavailable: a cap override never bypasses unreadable history' '[ "$RC" = 7 ] && grep -q "^ADMISSION_ROUND_BREAKER: UNAVAILABLE" <<<"$OUT" && ! grep -qE "pr edit|label create" "$STUB_LOG"'
unset PM_ADMIT_CAP_OVERRIDE CAP_EVENTS_MODE

echo "case: cap - a dry run reports the refusal; merge-only and check-only never consult the cap"
CAP_EVENTS_JSON=$(cap_events $CH1 $CH2); export CAP_EVENTS_JSON
run_case cap_dry --dry-run
check 'cap_dry: exit 10, nothing admitted' '[ "$RC" = 10 ] && grep -q "ADMISSION_ROUND_BREAKER #11173; nothing admitted" <<<"$OUT"'
S166_CASE=no_overlap run_script cap_merge_only --merge-only 11173
check 'cap: --merge-only does not read the cap' '[ "$RC" = 0 ] && ! grep -q "/events" "$STUB_LOG" && ! grep -q ADMISSION_ROUND_BREAKER <<<"$OUT"'
: > "$STUB_LOG"
GATE_MODE=same VERDICT=PASS REVIEW_SHA=${SHA:0:7}; export GATE_MODE VERDICT REVIEW_SHA
OUT=$(PATH="$T/bin:$PATH" HOME="$T/home" bash "$SCRIPT" --check-12b-only 11173 2>&1); RC=$?
check 'cap: --check-12b-only does not read the cap' '[ "$RC" = 0 ] && ! grep -q "/events" "$STUB_LOG" && ! grep -q ADMISSION_ROUND_BREAKER <<<"$OUT"'
unset CAP_EVENTS_JSON

# ---- Fix 1 (Rajiv ts 1795761962.862319): admission REFUSES to re-admit a head whose latest ci-verdict comment classifies its red as flaky/infra/inherited-main/out-of-diff ----
# It prints the S171 admin-ask packet (exit 12) instead; a red on an admitted head with NO ci-verdict comment refuses "classify first" (exit 11). The verdict gate runs after the
# round breaker, in admission mode only. The only way out is a newer verdict comment: --force-ci, a cap override and --dry-run do not bypass it.
cap_verdict_at() { # <head> <run id> <classification> <comment id> <created_at>: like cap_verdict with an explicit id and time, so a newer verdict can supersede an older one
  printf '{"id":%s,"user":{"login":"%s"},"created_at":"%s","body":"<!-- ci-failure-investigation:run=%s attempt=1 head=%s -->\\n<!-- ci-verdict: {\\"classification\\":\\"%s\\",\\"run_id\\":%s,\\"head\\":\\"%s\\"} -->"}' "$4" "${VERDICT_AUTHOR:-pm-agent-bot}" "$5" "$2" "$1" "$3" "$2" "$1"
}
VG_ARGS=(--no-12b-gate --reason 'verdict gate')
GATE_MODE=none; export GATE_MODE
CAP_EVENTS_JSON=$(cap_events $SHA); export CAP_EVENTS_JSON   # $SHA was admitted once (1 round, under the cap): re-admitting it is what the verdict gate judges
cap_red $SHA 201
assert_vg_nothing_admitted() { # name: the gate refused before any admission write
  check "$1: nothing admitted, no label, PR or branch write" '! grep -q ADMITTED <<<"$OUT" && ! grep -qE "$CI_WRITES|pr edit|label create|add-label|update-branch" "$STUB_LOG"'
}

echo "case: verdict gate - a red on an admitted head with NO ci-verdict comment: classify first (exit 11), nothing admitted"
unset CAP_COMMENTS_JSON
run_case vg_none "${VG_ARGS[@]}"
check 'vg_none: exit 11 with REFUSE ci_verdict_required naming the head and the red run, and the classify-first instruction' '[ "$RC" = 11 ] && grep -q "^REFUSE: ci_verdict_required #11173 head=$SHA red run(s) [^ ]*:201@1 have no ci-verdict comment bound to this head + run id + attempt posted by the trusted PM account in the ci-failure-investigation producer shape; classify first" <<<"$OUT"'
assert_vg_nothing_admitted vg_none
check 'vg_none: the breaker ran first and was under the cap (the refusal is the verdict gate, not the cap)' 'grep -qx "ADMISSION_ROUND_BREAKER: ok pr=11173 rounds=1 cap=2" <<<"$OUT" && ! grep -q "ADMISSION_ROUND_BREAKER #" <<<"$OUT"'

echo "case: verdict gate - the latest verdict says flaky/infra/inherited-main/out-of-diff: the S171 admin-ask packet (exit 12), never a re-admission"
for cls in flaky flake infra inherited-main out-of-diff; do
  CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 201 "$cls")]]"; export CAP_COMMENTS_JSON
  run_case "vg_$cls" "${VG_ARGS[@]}"
  check "vg_$cls: exit 12, the packet for the head with the run, class=$cls and the S167 next step" '[ "$RC" = 12 ] && grep -q "^ADMIN_ASK_PACKET (S171/S157) #11173 head=$SHA" <<<"$OUT" && grep -q "^  run [^ ]*:201@1 conclusion=failure class=$cls" <<<"$OUT" && grep -q "enforce_admins off -> gh pr merge --squash --admin --match-head-commit <head>" <<<"$OUT" && ! grep -q "^REFUSE: ci_verdict_required" <<<"$OUT"'
  assert_vg_nothing_admitted "vg_$cls"
done
CAP_COMMENTS_JSON="[[$(cap_verdict_full $SHA 201 infra 'tests/a.test.ts > saves' '#15001' 'runner lost network')]]"; export CAP_COMMENTS_JSON
run_case vg_full "${VG_ARGS[@]}"
check 'vg_full: the packet carries the run id, the failing tests, the follow-up issue, the evidence and the verdict comment link (S171 fields)' '[ "$RC" = 12 ] && grep -qx "  failing_tests: tests/a.test.ts > saves" <<<"$OUT" && grep -qx "  follow_up_issue: #15001" <<<"$OUT" && grep -q "^    evidence: runner lost network" <<<"$OUT" && grep -q "verdict comment: https://github.example/c/201" <<<"$OUT"'
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 201 flaky)]]"; export CAP_COMMENTS_JSON
run_case vg_unavailable_fields "${VG_ARGS[@]}"
check 'vg_unavailable_fields: a verdict with no failing tests / follow-up marks them UNAVAILABLE with a reason, never invented' 'grep -q "^  failing_tests: UNAVAILABLE" <<<"$OUT" && grep -q "^  follow_up_issue: UNAVAILABLE" <<<"$OUT"'

echo "case: verdict gate - a PR-caused verdict proceeds; a newer verdict supersedes an older flaky one (the way out); a verdict for another run does not cover this red"
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 201 pr-caused)]]"; export CAP_COMMENTS_JSON
run_case vg_prcaused "${VG_ARGS[@]}"
check 'vg_prcaused: proceeds and ADMITTED (the red is the PR own fault, fixing and re-admitting is the normal path)' '[ "$RC" = 0 ] && grep -q "^ci_verdict: proceed pr=11173 head=$SHA .*class=pr-caused" <<<"$OUT" && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT"'
CAP_COMMENTS_JSON="[[$(cap_verdict_at $SHA 201 flaky 3001 2026-11-20T12:00:00Z),$(cap_verdict_at $SHA 201 pr-caused 3002 2026-11-20T13:00:00Z)]]"; export CAP_COMMENTS_JSON
run_case vg_newer_prcaused "${VG_ARGS[@]}"
check 'vg_newer_prcaused: the NEWEST verdict decides: flaky then pr-caused proceeds' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET" <<<"$OUT"'
CAP_COMMENTS_JSON="[[$(cap_verdict_at $SHA 201 pr-caused 3001 2026-11-20T12:00:00Z),$(cap_verdict_at $SHA 201 flaky 3002 2026-11-20T13:00:00Z)]]"; export CAP_COMMENTS_JSON
run_case vg_newer_flaky "${VG_ARGS[@]}"
check 'vg_newer_flaky: the NEWEST verdict decides: pr-caused then flaky is the packet' '[ "$RC" = 12 ] && grep -q "^ADMIN_ASK_PACKET (S171/S157) #11173 head=$SHA" <<<"$OUT"'
assert_vg_nothing_admitted vg_newer_flaky
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 999 flaky)]]"; export CAP_COMMENTS_JSON
run_case vg_other_run "${VG_ARGS[@]}"
check 'vg_other_run: a verdict that names a different run does not cover this red: classify first, never the packet' '[ "$RC" = 11 ] && grep -q "^REFUSE: ci_verdict_required #11173" <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET" <<<"$OUT"'

echo "case: verdict gate - no red run at the head (green or none) and a first admission of a head are never gated"
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 201 flaky)]]"; export CAP_COMMENTS_JSON
printf '[{"workflow_runs":[]}]\n' > "$CAP_FIX/runs.$SHA"
run_case vg_no_runs "${VG_ARGS[@]}"
check 'vg_no_runs: no run at the head -> proceeds and ADMITTED even with an old flaky verdict on file' '[ "$RC" = 0 ] && grep -q "^ci_verdict: proceed pr=11173 head=$SHA scope=head" <<<"$OUT" && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT"'
cap_red $SHA 201
CAP_EVENTS_JSON='[[]]'; export CAP_EVENTS_JSON
run_case vg_first_admission "${VG_ARGS[@]}"
check 'vg_first_admission: a head that was never admitted is not judged by the verdict gate (rounds=0) and ADMITTED' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET\|ci_verdict_required" <<<"$OUT"'
CAP_EVENTS_JSON=$(cap_events $SHA); export CAP_EVENTS_JSON

echo "case: verdict gate - an unreadable verdict history is UNAVAILABLE and fails closed (exit 7), never a guess"
CAP_COMMENTS_JSON='this is not json'; export CAP_COMMENTS_JSON
run_case vg_unreadable "${VG_ARGS[@]}"
check 'vg_unreadable: ci_verdict UNAVAILABLE with a reason, REFUSE github_api_error ci_verdict, exit 7' '[ "$RC" = 7 ] && grep -q "^ci_verdict: UNAVAILABLE reason=" <<<"$OUT" && grep -qx "REFUSE: github_api_error ci_verdict" <<<"$OUT"'
assert_vg_nothing_admitted vg_unreadable

echo "case: verdict gate - no bypass: --dry-run and --force-ci still see the packet; a cap override lifts only the cap"
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 201 flaky)]]"; export CAP_COMMENTS_JSON
run_case vg_dry --dry-run "${VG_ARGS[@]}"
check 'vg_dry: --dry-run reports the packet (exit 12), nothing admitted' '[ "$RC" = 12 ] && grep -q "^ADMIN_ASK_PACKET (S171/S157) #11173" <<<"$OUT"'
run_case vg_force_ci --force-ci 'verdict gate test' "${VG_ARGS[@]}"
check 'vg_force_ci: --force-ci overrides only the scope gate, so the packet still stops it (exit 12)' '[ "$RC" = 12 ] && grep -q "^ADMIN_ASK_PACKET (S171/S157) #11173" <<<"$OUT"'
assert_vg_nothing_admitted vg_force_ci
cp -p "$T/home/.claude/logs/pm-admit-skip.log" "$T/skiplog.vg.save"
CAP_EVENTS_JSON=$(cap_events $CH1 $SHA); export CAP_EVENTS_JSON   # 2 rounds = the cap is reached, so the override is what lifts it
OVR_VG=cto:1795700000.000001   # a fresh approval: $OVR was spent by the cap_override cases above and a spent override id is refused
PM_ADMIT_CAP_OVERRIDE=$OVR_VG@11173:$SHA run_case vg_cap_override "${VG_ARGS[@]}"
check 'vg_cap_override: the cap override lifts the cap (granted audit line), but the verdict gate still refuses with the packet (exit 12)' '[ "$RC" = 12 ] && grep -q "^ADMISSION_CAP_OVERRIDE: $OVR_VG pr=11173" <<<"$OUT" && grep -q "^ADMIN_ASK_PACKET (S171/S157) #11173 head=$SHA" <<<"$OUT" && grep -q "admission-cap-override-granted=$OVR_VG" "$T/home/.claude/logs/pm-admit-skip.log"'
assert_vg_nothing_admitted vg_cap_override
check 'vg_cap_override: the packet refusal came BEFORE admission, so the override was granted but NOT consumed (it stays available for the next admission)' '! grep -q "admission-cap-override-consumed=$OVR_VG" "$T/home/.claude/logs/pm-admit-skip.log" && ! grep -q "ADMISSION_CAP_OVERRIDE_CONSUMED" <<<"$OUT"'
cp -p "$T/skiplog.vg.save" "$T/home/.claude/logs/pm-admit-skip.log"
CAP_EVENTS_JSON=$(cap_events $SHA); export CAP_EVENTS_JSON

echo "case: verdict gate - --merge-only and --check-12b-only never run it (no run or verdict read)"
S166_CASE=no_overlap run_script vg_merge_only --merge-only 11173
check 'vg_merge_only: no workflow-run read, no ci_verdict or packet output' '! grep -q "head_sha=" "$STUB_LOG" && ! grep -qE "ci_verdict|ADMIN_ASK_PACKET" <<<"$OUT"'
: > "$STUB_LOG"
OUT=$(PATH="$T/bin:$PATH" HOME="$T/home" bash "$SCRIPT" --check-12b-only 11173 2>&1); RC=$?
check 'vg_check_only: --check-12b-only does not read runs or verdicts (whatever the 12b stub decides)' '! grep -q "head_sha=" "$STUB_LOG" && ! grep -qE "ci_verdict|ADMIN_ASK_PACKET" <<<"$OUT"'
unset CAP_COMMENTS_JSON

# ---- Run selection + verdict binding (CTO REVISE ts 1795766409.314279): the latest SUBSTANTIVE run per workflow, bound to exact head + run id + CURRENT attempt ----
# (a) the #14950 shape: a gate-only label wrapper with the same start time and a HIGHER id than the real red must never be the run that is classified; (b) a verdict from an earlier
# attempt must not govern a later attempt's failure. The selection and binding logic has its own test file; these cases pin the wiring through pm-admit-ci.sh.
echo "case: selection - a newer-id label-wrapper red is ignored; the real red run is classified and its existing verdict is used (the #14950 shape)"
GATE_MODE=none; export GATE_MODE
CAP_EVENTS_JSON=$(cap_events $SHA); export CAP_EVENTS_JSON   # $SHA admitted once: the verdict gate judges it
cap_red_pair $SHA 301 302 failure
CAP_WRAPPER_RUNS=302; export CAP_WRAPPER_RUNS
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 301 flaky)]]"; export CAP_COMMENTS_JSON
run_case sel_wrapper_red "${VG_ARGS[@]}"
check 'sel_wrapper_red: the packet is for the REAL red run 301 with ITS verdict (not "classify first" for wrapper 302), nothing admitted' '[ "$RC" = 12 ] && grep -q "^ADMIN_ASK_PACKET (S171/S157) #11173 head=$SHA" <<<"$OUT" && grep -q "^  run [^ ]*:301@1 conclusion=failure class=flaky" <<<"$OUT" && ! grep -q ":302@" <<<"$OUT" && ! grep -q "ci_verdict_required" <<<"$OUT"'
assert_vg_nothing_admitted sel_wrapper_red
check 'sel_wrapper_red: the wrapper was inspected through its jobs (the discriminator), so it is the step list, not the run id, that excluded it' 'grep -q "actions/runs/302/jobs" "$STUB_LOG" && grep -q "actions/runs/301/jobs" "$STUB_LOG"'
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 301 pr-caused)]]"; export CAP_COMMENTS_JSON
run_case sel_wrapper_prcaused "${VG_ARGS[@]}"
check 'sel_wrapper_prcaused: the real red carries a PR-caused verdict -> proceeds and ADMITTED (the wrapper never forces a re-classification)' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && ! grep -q "ci_verdict_required" <<<"$OUT"'

echo "case: selection - a newer GREEN wrapper must not hide a substantive red; a verdict on the wrapper id does not cover the real red"
cap_red_pair $SHA 301 302 success
CAP_WRAPPER_STEP=success; export CAP_WRAPPER_STEP
CAP_COMMENTS_JSON='[[]]'; export CAP_COMMENTS_JSON
run_case sel_green_wrapper "${VG_ARGS[@]}"
check 'sel_green_wrapper: the substantive red 301 is still the run to classify (exit 11, names :301@1), the green wrapper hides nothing' '[ "$RC" = 11 ] && grep -q "^REFUSE: ci_verdict_required #11173 head=$SHA red run(s) [^ ]*:301@1 " <<<"$OUT" && ! grep -q ":302@" <<<"$OUT"'
assert_vg_nothing_admitted sel_green_wrapper
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 302 flaky)]]"; export CAP_COMMENTS_JSON
run_case sel_verdict_on_wrapper "${VG_ARGS[@]}"
check 'sel_verdict_on_wrapper: a flaky verdict filed against the WRAPPER run id does not cover the real red (classify first, never the packet)' '[ "$RC" = 11 ] && grep -q "^REFUSE: ci_verdict_required #11173" <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET" <<<"$OUT"'
unset CAP_WRAPPER_RUNS CAP_WRAPPER_STEP

echo "case: binding - a verdict for an EARLIER attempt does not govern a later attempt's failure; the current attempt's verdict does"
cap_red $SHA 401 2
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 401 flaky 1)]]"; export CAP_COMMENTS_JSON
run_case bind_stale_attempt "${VG_ARGS[@]}"
check 'bind_stale_attempt: run 401 attempt 2 vs a verdict bound to attempt 1 -> classify first (exit 11, names :401@2), never the packet' '[ "$RC" = 11 ] && grep -q "^REFUSE: ci_verdict_required #11173 head=$SHA red run(s) [^ ]*:401@2 " <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET" <<<"$OUT"'
assert_vg_nothing_admitted bind_stale_attempt
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 401 flaky 2)]]"; export CAP_COMMENTS_JSON
run_case bind_current_attempt "${VG_ARGS[@]}"
check 'bind_current_attempt: a verdict bound to attempt 2 covers it: the packet names :401@2' '[ "$RC" = 12 ] && grep -q "^  run [^ ]*:401@2 conclusion=failure class=flaky" <<<"$OUT"'
CAP_COMMENTS_JSON='[[{"id":9001,"user":{"login":"pm-agent-bot"},"created_at":"2026-11-20T12:00:00Z","body":"## CI failure investigation verdict\n<!-- ci-verdict: {\"classification\":\"flaky\",\"run_id\":401} -->"}]]'; export CAP_COMMENTS_JSON
run_case bind_no_producer_shape "${VG_ARGS[@]}"
check 'bind_no_producer_shape: a head-less, marker-less comment (heading only) is not the producer shape: no head-only fallback, classify first' '[ "$RC" = 11 ] && grep -q "^REFUSE: ci_verdict_required #11173" <<<"$OUT" && ! grep -q "^ADMIN_ASK_PACKET" <<<"$OUT"'
cap_red $SHA 201
unset CAP_COMMENTS_JSON

# ---- One-pair override ledger + atomic claim (CTO REVISE #2 ts 1795769102.876159; ledger design ts 1795766409.314279) ----
# An override funds exactly ONE admission pair AT THE HEAD IT NAMES. pm-admit-skip.log kinds: granted (gate time), carried (LEGACY, read-only: nothing writes it; automatic carry-forward is PAUSED),
# consumed (written once, right after the atomic claim and BEFORE the ci-head label add). The authority against a race is the O_EXCL claim file per grant id (pm-admit-cap-claims/<id>).
echo "case: one-pair override - no carry-forward: a grant recorded for an earlier head never funds a different head; only an unused grant AT this head does"
GATE_MODE=none; export GATE_MODE
CAP_EVENTS_JSON=$(cap_events $CH1 $CH2); export CAP_EVENTS_JSON
SKIPLOG="$T/home/.claude/logs/pm-admit-skip.log"
CLAIMS="$T/home/.claude/logs/pm-admit-cap-claims"
cp -p "$SKIPLOG" "$T/skiplog.inh.save"
CBASE=0
claims_reset() { CBASE=$(find "$CLAIMS" -type f 2>/dev/null | wc -l | tr -d ' '); }   # claim files outlive earlier cases: count only those a case adds
set_skiplog() { printf '%s\n' "$@" > "$SKIPLOG"; chmod 644 "$SKIPLOG"; claims_reset; }
ov() { printf 'cto:1795770%03d.000001' "$1"; }   # a distinct grant id per case (a claim file outlives its case)
gl() { printf '2026-11-27T01:00:00Z PR=%s head=%s admission-cap-override-granted=%s' "$1" "$2" "$3"; }
n_ledger() { grep -c "admission-cap-override-$1=" "$SKIPLOG"; }   # records of one kind, any approval
n_claims() { echo $(( $(find "$CLAIMS" -type f 2>/dev/null | wc -l) - CBASE )); }
no_admission_write() { ! grep -qE -- "--add-label ci-head:|--remove-label" "$STUB_LOG"; }

OVR_A=$(ov 1)
set_skiplog "$(gl 11173 $CH1 "$OVR_A")"
DESC_MODE=yes run_case inh_no_carry "${VG_ARGS[@]}"
check 'inh_no_carry: an unused grant recorded for an EARLIER head is not carried: normal refusal and override ask, exit 10, nothing admitted' '[ "$RC" = 10 ] && grep -q "^Decision required" <<<"$OUT" && grep -q "Re-admission requires an audited override" <<<"$OUT" && ! grep -q "carried from" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT"'
check 'inh_no_carry: no descendant helper call, no git fetch for a carry, no ledger line, no claim, no label write' '! grep -q "^verdict-descendant " "$STUB_LOG" && [ "$(wc -l < "$SKIPLOG" | tr -d " ")" = 1 ] && [ "$(n_claims)" = 0 ] && no_admission_write'

OVR_B=$(ov 2)
set_skiplog "$(gl 11173 $SHA "$OVR_B")"
DESC_MODE=yes run_case inh_same_head "${VG_ARGS[@]}"
check 'inh_same_head: a grant recorded for THIS PR and head that was never claimed (an earlier preflight refused) funds the admission: exit 0, ADMITTED, says it is unused and funds one pair' '[ "$RC" = 0 ] && grep -q "^ADMISSION_CAP_OVERRIDE: $OVR_B pr=11173 already recorded at this head and unused" <<<"$OUT" && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && ! grep -q "^verdict-descendant " "$STUB_LOG"'
check 'inh_same_head: consumed exactly once at the admitted head, the consumed record existed when the ci-head label add was issued, one claim file holds pr and head' '[ "$(grep -c "PR=11173 head=$SHA admission-cap-override-consumed=$OVR_B\$" "$SKIPLOG")" = 1 ] && [ "$(cat "$T/inh_same_head/consumed_at_label_add")" = 1 ] && [ "$(n_claims)" = 1 ] && grep -q "^pr=11173 head=$SHA at=" "$CLAIMS/${OVR_B//:/_}" && grep -q "^ADMISSION_CAP_OVERRIDE_CONSUMED: $OVR_B pr=11173 head=$SHA; this override funded one admission pair and is spent" <<<"$OUT"'
check 'inh_same_head: no legacy un-kinded line and no carried line; one granted, one consumed' '[ "$(n_ledger carried)" = 0 ] && [ "$(n_ledger granted)" = 1 ] && [ "$(n_ledger consumed)" = 1 ] && ! grep -q "admission-cap-override=" "$SKIPLOG"'

echo "case: one-pair override - once claimed/consumed the grant is spent (same head, explicit re-presentation or a newer grant that is itself spent)"
LEDGER_AFTER=$(wc -l < "$SKIPLOG" | tr -d ' ')
DESC_MODE=yes run_case inh_spent "${VG_ARGS[@]}"
check 'inh_spent: the next admission at the cap is the normal refusal, nothing admitted, no new ledger line' '[ "$RC" = 10 ] && grep -q "^Decision required" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && [ "$(wc -l < "$SKIPLOG" | tr -d " ")" = "$LEDGER_AFTER" ] && no_admission_write'
PM_ADMIT_CAP_OVERRIDE=$OVR_B@11173:$SHA run_case inh_spent_explicit "${VG_ARGS[@]}"
check 'inh_spent_explicit: re-presenting the spent override is refused and names it as already consumed (exit 10)' '[ "$RC" = 10 ] && grep -q "^REFUSE: ADMISSION_ROUND_BREAKER #11173; override $OVR_B was already consumed" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && no_admission_write'
OVR_C=$(ov 3)
set_skiplog "$(gl 11173 $SHA "$OVR_C")" "2026-11-27T01:05:00Z PR=11173 head=$SHA admission-cap-override-consumed=$OVR_C"
DESC_MODE=yes run_case inh_consumed_at_grant_head "${VG_ARGS[@]}"
check 'inh_consumed_at_grant_head: granted then consumed at this head -> spent: exit 10, no new line' '[ "$RC" = 10 ] && ! grep -q "carried from" <<<"$OUT" && [ "$(wc -l < "$SKIPLOG" | tr -d " ")" = 2 ] && no_admission_write'
OVR_D=$(ov 4); OVR_D2=$(ov 5)
set_skiplog "$(gl 11173 $SHA "$OVR_D")" "$(gl 11173 $SHA "$OVR_D2")" "2026-11-27T01:05:00Z PR=11173 head=$SHA admission-cap-override-consumed=$OVR_D2"
DESC_MODE=yes run_case inh_no_fallthrough "${VG_ARGS[@]}"
check 'inh_no_fallthrough: only the NEWEST grant is considered; when it is spent an older unused grant is not resurrected: exit 10, nothing admitted' '[ "$RC" = 10 ] && ! grep -q "carried from" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && no_admission_write'
OVR_E=$(ov 6)
set_skiplog "$(gl 11173 $CH1 "$OVR_E")" "2026-11-27T01:02:00Z PR=11173 head=$CH2 admission-cap-override-carried=$OVR_E inherited-from=$CH1"
DESC_MODE=yes run_case inh_legacy_carried "${VG_ARGS[@]}"
check 'inh_legacy_carried: a historical carried record is bound to its own head, never funds this head, and nothing writes a new one: exit 10, ledger unchanged' '[ "$RC" = 10 ] && ! grep -q "^verdict-descendant " "$STUB_LOG" && ! grep -q ADMITTED <<<"$OUT" && [ "$(wc -l < "$SKIPLOG" | tr -d " ")" = 2 ] && no_admission_write'

echo "case: one-pair override - head scope: a grant names its head and can never fund a different head or PR (explicit override, no ledger line and no claim written)"
OVR_F=$(ov 7)
set_skiplog "$(gl 11173 $CH1 "$OVR_F")"
PM_ADMIT_CAP_OVERRIDE=$OVR_F@11173:$SHA run_case head_other "${VG_ARGS[@]}"
check 'head_other: a head-only grant for CH1 presented at head SHA refuses: exit 10, REFUSE names the grant head and the presented head, nothing admitted' '[ "$RC" = 10 ] && grep -q "^REFUSE: ADMISSION_ROUND_BREAKER #11173; override $OVR_F names PR #11173 head $CH1 and cannot fund head $SHA (a grant never funds a different head)" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT"'
check 'head_other: no granted/consumed line appended, no claim file, no ci-head label write' '[ "$(wc -l < "$SKIPLOG" | tr -d " ")" = 1 ] && [ "$(n_claims)" = 0 ] && no_admission_write'
OVR_G=$(ov 8)
set_skiplog "$(gl 99999 $SHA "$OVR_G")"
PM_ADMIT_CAP_OVERRIDE=$OVR_G@11173:$SHA run_case head_other_pr "${VG_ARGS[@]}"
check 'head_other_pr: a grant recorded for another PR is refused even at the same head sha: exit 10, nothing admitted, no claim, no new line' '[ "$RC" = 10 ] && grep -q "names PR #99999 head $SHA and cannot fund" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && [ "$(wc -l < "$SKIPLOG" | tr -d " ")" = 1 ] && [ "$(n_claims)" = 0 ] && no_admission_write'
OVR_H=$(ov 9)
set_skiplog "$(gl 11173 $SHA "$OVR_H")"
HM_VALUE="$CH2 MERGEABLE" PM_ADMIT_CAP_OVERRIDE=$OVR_H@11173:$SHA run_case head_moved "${VG_ARGS[@]}"
check 'head_moved: the PR head moves after the gate (grant names SHA, live head CH2): nothing is admitted, no claim, no consumed record, no ci-head label write (an earlier guard may refuse first; the claim-boundary refusal itself is proven next)' '[ "$RC" -ne 0 ] && ! grep -q ADMITTED <<<"$OUT" && [ "$(n_claims)" = 0 ] && [ "$(n_ledger consumed)" = 0 ] && no_admission_write'
# The claim-boundary head check, run on the script's own function text: a grant naming SHA never funds CH2, and exits 10 before any label or claim write.
HC_FN=$(awk '/^cap_override_head_check\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$SCRIPT")
HC_OUT=$(PR=11173 CAP_OVERRIDE_USED=$OVR_H CAP_OVERRIDE_HEAD=$SHA HEAD=$CH2 bash -c "$HC_FN; cap_override_head_check; echo not-refused" 2>&1); HC_RC=$?
check 'head_moved: cap_override_head_check refuses (exit 10) when the live head differs from the head the grant names, naming both heads' '[ "$HC_RC" = 10 ] && grep -q "override $OVR_H names head $SHA and cannot fund head $CH2" <<<"$HC_OUT" && ! grep -q not-refused <<<"$HC_OUT"'
HC_OUT=$(PR=11173 CAP_OVERRIDE_USED=$OVR_H CAP_OVERRIDE_HEAD=$SHA HEAD=$SHA bash -c "$HC_FN; cap_override_head_check; echo not-refused" 2>&1); HC_RC=$?
check 'head_moved: the same check passes when the live head is the head the grant names' '[ "$HC_RC" = 0 ] && grep -q not-refused <<<"$HC_OUT"'

# ---- CTO REVISE #3 (ts 1795771648.671389): a grant's FIRST presentation binds to the PR + FULL head SHA named in the approval handoff, not to whatever the caller presents ----
# PM_ADMIT_CAP_OVERRIDE=cto:<ts>@<PR>:<full sha>. A bare id is refused (64); a tuple that differs from the live PR/head is refused (10) with NOTHING written (no ledger row, claim or label).
echo "case: expected-tuple binding - a bare id is refused, a drifted head/PR leaves grant + labels untouched, the same authorized tuple then admits once"
OVR_T=$(ov 19)
claims_reset; : > "$SKIPLOG"; : > "$STUB_LOG"
PM_ADMIT_CAP_OVERRIDE=$OVR_T run_case tuple_bare "${VG_ARGS[@]}"
check 'tuple_bare: an override with no PR/head tuple is refused (exit 64), the message names the required form, nothing is written' '[ "$RC" = 64 ] && grep -q "PM_ADMIT_CAP_OVERRIDE must look like cto:<slack ts>@<PR>:<full 40-hex head sha>" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && [ "$(wc -l < "$SKIPLOG" | tr -d " ")" = 0 ] && [ "$(n_claims)" = 0 ] && ! grep -qE "pr edit|label create|add-label" "$STUB_LOG"'
PM_ADMIT_CAP_OVERRIDE=$OVR_T@11173:${SHA:0:12} run_case tuple_short_sha "${VG_ARGS[@]}"
check 'tuple_short_sha: an abbreviated head sha is refused (exit 64): the FULL head is required' '[ "$RC" = 64 ] && ! grep -q ADMITTED <<<"$OUT" && [ "$(wc -l < "$SKIPLOG" | tr -d " ")" = 0 ] && [ "$(n_claims)" = 0 ] && ! grep -qE "pr edit|label create|add-label" "$STUB_LOG"'
PM_ADMIT_CAP_OVERRIDE=$OVR_T@11173:$CH1 run_case tuple_drift "${VG_ARGS[@]}"
check 'tuple_drift: approved for head CH1 but the live head is SHA -> REFUSE names both heads, exit 10, nothing admitted' '[ "$RC" = 10 ] && grep -q "^REFUSE: ADMISSION_ROUND_BREAKER #11173; override $OVR_T was approved for PR #11173 head $CH1 but the live PR is #11173 at head $SHA" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && ! grep -q "^ADMISSION_CAP_OVERRIDE:" <<<"$OUT"'
check 'tuple_drift: first presentation after drift writes NOTHING: no granted row, no consumed row, no claim file, no label write' '[ "$(wc -l < "$SKIPLOG" | tr -d " ")" = 0 ] && [ "$(n_ledger granted)" = 0 ] && [ "$(n_ledger consumed)" = 0 ] && [ "$(n_claims)" = 0 ] && ! grep -qE "pr edit|label create|add-label|--remove-label" "$STUB_LOG"'
PM_ADMIT_CAP_OVERRIDE=$OVR_T@99999:$SHA run_case tuple_other_pr "${VG_ARGS[@]}"
check 'tuple_other_pr: approved for another PR at the same head sha -> refused (exit 10), nothing written' '[ "$RC" = 10 ] && grep -q "was approved for PR #99999 head $SHA but the live PR is #11173 at head $SHA" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && [ "$(wc -l < "$SKIPLOG" | tr -d " ")" = 0 ] && [ "$(n_claims)" = 0 ] && ! grep -qE "pr edit|label create|add-label" "$STUB_LOG"'
DESC_MODE=yes PM_ADMIT_CAP_OVERRIDE=$OVR_T@11173:$SHA run_case tuple_authorized "${VG_ARGS[@]}"
check 'tuple_authorized: the SAME grant id presented with the authorized tuple at the live head admits once (the earlier refusals left it untouched)' '[ "$RC" = 0 ] && grep -q "^ADMISSION_CAP_OVERRIDE: $OVR_T pr=11173" <<<"$OUT" && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && [ "$(n_ledger granted)" = 1 ] && [ "$(grep -c "PR=11173 head=$SHA admission-cap-override-consumed=$OVR_T\$" "$SKIPLOG")" = 1 ] && [ "$(n_claims)" = 1 ] && grep -q -- "--add-label ci-head:$SHA" "$STUB_LOG"'
SKIPN_T=$(wc -l < "$SKIPLOG" | tr -d ' ')
PM_ADMIT_CAP_OVERRIDE=$OVR_T@11173:$SHA run_case tuple_spent "${VG_ARGS[@]}"
check 'tuple_spent: re-presenting the now-spent grant with the same authorized tuple is refused (one pair only), no new row' '[ "$RC" = 10 ] && grep -q "^REFUSE: ADMISSION_ROUND_BREAKER #11173; override $OVR_T was already consumed" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && [ "$(wc -l < "$SKIPLOG" | tr -d " ")" = "$SKIPN_T" ]'

echo "case: one-pair override - ledger lines that are not a well-formed grant for THIS PR are ignored (other PR, malformed approval, legacy un-kinded line)"
OVR_I=$(ov 10)
set_skiplog "2026-11-27T01:00:00Z PR=99999 head=$SHA admission-cap-override-granted=$OVR_I"
DESC_MODE=yes run_case inh_other_pr "${VG_ARGS[@]}"
check 'inh_other_pr: a grant recorded for another PR is ignored: exit 10, nothing admitted' '[ "$RC" = 10 ] && ! grep -q "carried from" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && no_admission_write'
set_skiplog "2026-11-27T01:00:00Z PR=11173 head=$SHA admission-cap-override-granted=yes" "2026-11-27T01:00:00Z PR=11173 head=$SHA no-12b-gate reason=something"
DESC_MODE=yes run_case inh_malformed "${VG_ARGS[@]}"
check 'inh_malformed: a malformed approval and a non-override audit line are ignored: exit 10, nothing admitted' '[ "$RC" = 10 ] && ! grep -q ADMITTED <<<"$OUT" && no_admission_write'
set_skiplog "2026-11-27T01:00:00Z PR=11173 head=$SHA admission-cap-override=$(ov 11)"
DESC_MODE=yes run_case inh_legacy "${VG_ARGS[@]}"
check 'inh_legacy: a legacy un-kinded admission-cap-override= line is not a grant (no proof of non-use): exit 10, nothing admitted' '[ "$RC" = 10 ] && ! grep -q "carried from" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && no_admission_write'

echo "case: one-pair override - a refused preflight keeps the override: an explicit override refused by the verdict gate funds the later admission, and is consumed only then"
CAP_EVENTS_JSON=$(cap_events $CH1 $SHA); export CAP_EVENTS_JSON   # the cap is reached and $SHA was admitted once, so the verdict gate judges it
OVR_RP=$(ov 12)
claims_reset; : > "$SKIPLOG"
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 201 flaky)]]"; export CAP_COMMENTS_JSON
PM_ADMIT_CAP_OVERRIDE=$OVR_RP@11173:$SHA run_case inh_refused_a "${VG_ARGS[@]}"
check 'inh_refused_a: the explicit override lifted the cap but the verdict gate refused with the packet (exit 12): granted, NOT consumed, no claim, nothing admitted' '[ "$RC" = 12 ] && grep -q "admission-cap-override-granted=$OVR_RP" "$SKIPLOG" && [ "$(n_ledger consumed)" = 0 ] && [ "$(n_claims)" = 0 ] && ! grep -q ADMITTED <<<"$OUT"'
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 201 pr-caused)]]"; export CAP_COMMENTS_JSON
DESC_MODE=no run_case inh_refused_b "${VG_ARGS[@]}"
check 'inh_refused_b: with the packet cleared and NO env override, the unused grant at this head funds the admission (no helper call, no second grant or carried line), consumed once before the label add' '[ "$RC" = 0 ] && grep -q "^ADMISSION_CAP_OVERRIDE: $OVR_RP pr=11173 already recorded at this head and unused" <<<"$OUT" && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && ! grep -q "^verdict-descendant " "$STUB_LOG" && [ "$(n_ledger granted)" = 1 ] && [ "$(n_ledger carried)" = 0 ] && [ "$(grep -c "PR=11173 head=$SHA admission-cap-override-consumed=$OVR_RP\$" "$SKIPLOG")" = 1 ] && [ "$(cat "$T/inh_refused_b/consumed_at_label_add")" = 1 ]'
DESC_MODE=yes run_case inh_refused_c "${VG_ARGS[@]}"
check 'inh_refused_c: after that admission the same override is spent: the next attempt at the cap is the normal refusal (exit 10)' '[ "$RC" = 10 ] && grep -q "^Decision required" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT"'
PM_ADMIT_CAP_OVERRIDE=$OVR_RP@11173:$SHA run_case inh_refused_d "${VG_ARGS[@]}"
check 'inh_refused_d: re-presenting the spent override explicitly is refused too (exit 10, REFUSE names it as already consumed)' '[ "$RC" = 10 ] && grep -q "^REFUSE: ADMISSION_ROUND_BREAKER #11173; override $OVR_RP was already consumed" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT"'
unset CAP_COMMENTS_JSON

echo "case: one-pair override - the env override is used as presented (older unused grant stays unused); --dry-run reports without writing a ledger line or claim"
CAP_EVENTS_JSON=$(cap_events $CH1 $CH2); export CAP_EVENTS_JSON   # $SHA was never admitted: the verdict gate does not judge it
OVR_J=$(ov 13)
set_skiplog "$(gl 11173 $CH1 "$OVR_J")"
DESC_MODE=yes PM_ADMIT_CAP_OVERRIDE=rajiv:1795761962.862319@11173:$SHA run_case inh_env_wins "${VG_ARGS[@]}"
check 'inh_env_wins: the explicit override is used and consumed, the older unused grant (other head) is not consumed' '[ "$RC" = 0 ] && grep -q "^ADMISSION_CAP_OVERRIDE: rajiv:1795761962.862319 pr=11173; cap reached" <<<"$OUT" && ! grep -q "^verdict-descendant " "$STUB_LOG" && grep -q "admission-cap-override-consumed=rajiv:1795761962.862319" "$SKIPLOG" && ! grep -q "admission-cap-override-consumed=$OVR_J" "$SKIPLOG"'
OVR_K=$(ov 14)
set_skiplog "$(gl 11173 $SHA "$OVR_K")"
DESC_MODE=yes run_case inh_dry --dry-run "${VG_ARGS[@]}"
check 'inh_dry: --dry-run with a same-head unused grant prints the override line, admits nothing and appends no consumed line and no claim' '[ "$RC" = 0 ] && grep -q "^ADMISSION_CAP_OVERRIDE: $OVR_K pr=11173 already recorded at this head and unused" <<<"$OUT" && grep -q "nothing admitted" <<<"$OUT" && [ "$(grep -c "admission-cap-override" "$SKIPLOG")" = 1 ] && [ "$(n_ledger consumed)" = 0 ] && [ "$(n_claims)" = 0 ] && ! grep -qE "pr edit|label create|add-label" "$STUB_LOG"'

echo "case: one-pair override - an unwritable ledger or claim refuses (exit 7) before any admission write; never an unaudited grant or consumption"
OVR_L=$(ov 15)
claims_reset; : > "$SKIPLOG"; chmod 444 "$SKIPLOG"
PM_ADMIT_CAP_OVERRIDE=$OVR_L@11173:$SHA run_case inh_audit_fail "${VG_ARGS[@]}"
chmod 644 "$SKIPLOG"
check 'inh_audit_fail: the granted line cannot be written: REFUSE admission_cap_override_audit_unavailable, exit 7, nothing admitted' '[ "$RC" = 7 ] && grep -qx "REFUSE: admission_cap_override_audit_unavailable" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && ! grep -qE "pr edit|label create|add-label" "$STUB_LOG"'
CAP_COMMENTS_JSON="[[$(cap_verdict $SHA 201 pr-caused)]]"; export CAP_COMMENTS_JSON
OVR_M=$(ov 16)
set_skiplog "$(gl 11173 $SHA "$OVR_M")"
LOCK_LOG_AT_PMOPS=1 run_case inh_consume_fail "${VG_ARGS[@]}"
chmod 644 "$SKIPLOG"
check 'inh_consume_fail: the consumed record cannot be written (grant at this head; ledger locked after the 12b-override audit): REFUSE admission_cap_override_audit_unavailable, exit 7, the ci-head label is NOT added, the claim is released so the approval is not burned' '[ "$RC" = 7 ] && grep -qx "REFUSE: admission_cap_override_audit_unavailable" <<<"$OUT" && ! grep -q ADMITTED <<<"$OUT" && [ "$(n_ledger consumed)" = 0 ] && [ ! -e "$CLAIMS/${OVR_M//:/_}" ] && ! grep -qE -- "--add-label" "$STUB_LOG"'   # label create only defines the label (admits nothing); the PR --add-label is the admission
OVR_N=$(ov 17)
set_skiplog "$(gl 11173 $SHA "$OVR_N")"
MODE_WRITE=fail run_case inh_label_fail_burns "${VG_ARGS[@]}"
check 'inh_label_fail_burns: the claim and consumed record are written BEFORE the label add, so a label write that fails still burns the override (also covers an uncertain outcome; documented one-pair policy)' '[ "$RC" -ne 0 ] && ! grep -q ADMITTED <<<"$OUT" && [ "$(n_ledger consumed)" = 1 ] && [ -e "$CLAIMS/${OVR_N//:/_}" ] && [ "$(cat "$T/inh_label_fail_burns/consumed_at_label_add")" = 1 ]'
unset CAP_COMMENTS_JSON
CAP_EVENTS_JSON=$(cap_events $CH1 $CH2); export CAP_EVENTS_JSON

echo "case: ATOMIC CLAIM - two concurrent callers race the SAME grant: exactly one issues the admission pair, the other refuses and writes no label"
OVR_R=$(ov 18)
claims_reset; : > "$SKIPLOG"
RACE_BARRIER="$T/race-barrier"; mkdir -p "$RACE_BARRIER"
( BARRIER_DIR="$RACE_BARRIER" BARRIER_N=2 PM_ADMIT_CAP_OVERRIDE=$OVR_R@11173:$SHA run_case race_a "${VG_ARGS[@]}"; printf '%s' "$RC" > "$T/race_a.rc"; printf '%s\n' "$OUT" > "$T/race_a.out" ) &
RACE_PID_A=$!
( BARRIER_DIR="$RACE_BARRIER" BARRIER_N=2 PM_ADMIT_CAP_OVERRIDE=$OVR_R@11173:$SHA run_case race_b "${VG_ARGS[@]}"; printf '%s' "$RC" > "$T/race_b.rc"; printf '%s\n' "$OUT" > "$T/race_b.out" ) &
RACE_PID_B=$!
wait "$RACE_PID_A" "$RACE_PID_B"
RA=$(cat "$T/race_a.rc"); RB=$(cat "$T/race_b.rc")
OUT="$(cat "$T/race_a.out" "$T/race_b.out")"
RACE_ADDS=$(cat "$T/race_a/gh.log" "$T/race_b/gh.log" | grep -c -- "--add-label ci-head:")
check 'race: both callers reached the labels read together (the barrier held both past every gate), so only the claim separates them' '[ -e "$RACE_BARRIER/m.race_a" ] && [ -e "$RACE_BARRIER/m.race_b" ]'
check 'race: exactly one caller exits 0 and the other exits 10' '{ [ "$RA" = 0 ] && [ "$RB" = 10 ]; } || { [ "$RA" = 10 ] && [ "$RB" = 0 ]; }'
check 'race: exactly ONE admission pair was issued (one ADMITTED line, one ci-head label add across both callers)' '[ "$(grep -c "^ADMITTED #11173 at $SHA" <<<"$OUT")" = 1 ] && [ "$RACE_ADDS" = 1 ]'
check 'race: the loser refuses with "already claimed by another admission" naming the grant and writes no ci-head label' '[ "$(grep -c "^REFUSE: ADMISSION_ROUND_BREAKER #11173; override $OVR_R is already claimed by another admission" <<<"$OUT")" = 1 ] && ! grep -q -- "--add-label ci-head:" "$T/race_$( [ "$RA" = 10 ] && echo a || echo b )/gh.log"'
check 'race: one claim file for the grant (pr and head bound) and exactly one consumed record in the ledger' '[ "$(n_claims)" = 1 ] && grep -q "^pr=11173 head=$SHA at=" "$CLAIMS/${OVR_R//:/_}" && [ "$(n_ledger consumed)" = 1 ]'
cp -p "$T/skiplog.inh.save" "$SKIPLOG"
unset CAP_EVENTS_JSON

# ---- Stale test-merge ref: ONE deadline-bounded read-only pass, fail closed (CTO REVISE #2 of b6d3f55c, C0ALZJHGE49 thread 1795543629.469469, ts 1795544911.112579) ----
# The PR's test merge (refs/pull/N/merge) is verified in one pass of GitHub reads, every one bounded by the remaining deadline, BEFORE any admission write.
# It is admitted ONLY when the PR is OPEN, the head is unchanged, main still equals the main that passed the overlap check (STUB_MAIN_SHA), the merge
# commit's parents are exactly {head, that main}, and a confirming read agrees. Anything else, including any unreadable, hung or late read, refuses
# stale_merge_ref (exit 3). No polling, no sleeps, no close/reopen, no main merge, no push.
GATE_MODE=none; export GATE_MODE
assert_needs_merge() { # name: plain admission stops at a changed trusted CI input (rule 62): exit 20 with ONE NEEDS_MAIN_MERGE line, nothing written, no merge
  check "$1: exit 20, one NEEDS_MAIN_MERGE reason=ci_input_changed line, nothing admitted" '[ "$RC" = 20 ] && grep -Eq "^NEEDS_MAIN_MERGE pr=11173 reason=ci_input_changed files=[0-9]+$" <<<"$OUT" && [ "$(grep -c "^NEEDS_MAIN_MERGE" <<<"$OUT")" = 1 ] && ! grep -q ADMITTED <<<"$OUT" && ! grep -q MERGE_REF_VERIFIED <<<"$OUT"'
  check "$1: stopped before the activation label, every admission write, any dispatch and any main merge" '! grep -qE -- "--add-label|--remove-label|label create|pr edit|workflow run|run rerun|update-branch" "$STUB_LOG" && ! grep -qE "^gh pr (close|reopen)|^git (merge|push|reset|rebase)|--force" "$STUB_LOG"'
}
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

# ---- Lagging test-merge ref: the stale-ref WAIVER (Rajiv rule 62/S166; CTO C0ALZJHGE49 thread 1795567441.556059, ts 1795584167.126789; PM brief 2026-11-25) ----
# GitHub's test merge can sit on an older main M (STUB_LAG_SHA) while main moves on, which refused every admission on 2026-11-25, and --merge-only skips a PR with
# no conflict and no overlap, so nothing rebuilt the ref (the deadlock). It is WAIVED (admitted, receipt rule=stale_merge_ref_waived:no_overlap) when the
# PR is not CONFLICTING, one parent is exactly the reviewed head, M is a verified ancestor of the overlap-checked main (STUB_MAIN_SHA), and the PR's changed
# files (merge-base..head against BOTH main and M, unioned) do not overlap the files main changed since M. Anything else refuses, with the overlap list.
# Git side of each case is set inline: LAG_ANCESTOR (is M an ancestor of main), LAG_PR_FILES / LAG_PR_FILES_LAGBASE (PR diff vs main-base / vs M-base),
# LAG_MAIN_FILES (main's diff M..main), LAG_DIFF_FAIL (that diff is unreadable). STALE_MODE (via LAG_MODE) sets how the merge commit's parents look.
run_lag() { local name="$1"; shift; STALE_MODE="${LAG_MODE:-lagging}" LAG_CASE=1 run_case "$name" "$@"; }

echo "case: lagging merge ref - built on an older ancestor main, the PR touches none of the files main changed since: ADMITTED, rule logged, one pass"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\nlib/pr2.ts\n' LAG_MAIN_FILES='lib/main.ts\nlib/main2.ts\nlib/main3.ts\n' \
  run_lag lag_admit --no-12b-gate --reason 'lagging ref test'
check 'lag_admit: exit 0, ADMITTED at the reviewed head, ci-head label added after the check' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && grep -q -- "--add-label ci-head:$SHA" "$STUB_LOG"'
check 'lag_admit: MERGE_REF_RULE names the lagging rule, the older main parent, the overlap-checked main, the file counts (union of both PR diffs = 2) and no changed CI input' 'grep -qx "MERGE_REF_RULE rule=stale_merge_ref_waived:no_overlap pr=11173 head=$SHA merge=$STUB_FRESH_SHA main_parent=$STUB_LAG_SHA main=$STUB_MAIN_SHA pr_files=2 main_files_since_parent=3 overlap=0 ci_inputs_changed=0" <<<"$OUT"'
check 'lag_admit: the MERGE_REF_VERIFIED line is unchanged (head, overlap-checked main, merge sha)' 'grep -qx "MERGE_REF_VERIFIED pr=11173 head=$SHA main=$STUB_MAIN_SHA merge=$STUB_FRESH_SHA" <<<"$OUT"'
check 'lag_admit: the lagging rule is appended to the pm-admit-skip.log audit trail' 'grep -q "rule=stale_merge_ref_waived:no_overlap pr=11173 head=$SHA merge=$STUB_FRESH_SHA main_parent=$STUB_LAG_SHA" "$T/home/.claude/logs/pm-admit-skip.log"'
check 'lag_admit: ancestry, both merge-bases, both PR diffs and main'"'"'s diff since the lagging parent were all read' 'grep -q "merge-base --is-ancestor $STUB_LAG_SHA $STUB_MAIN_SHA" "$STUB_LOG" && grep -q "merge-base $STUB_MAIN_SHA $SHA" "$STUB_LOG" && grep -q "merge-base $STUB_LAG_SHA $SHA" "$STUB_LOG" && grep -q "diff --no-renames --name-only $SHA $SHA --" "$STUB_LOG" && grep -q "diff --no-renames --name-only $STUB_LAGBASE_SHA $SHA --" "$STUB_LOG" && grep -q "diff --no-renames --name-only $STUB_LAG_SHA $STUB_MAIN_SHA --" "$STUB_LOG"'
check 'lag_admit: still one pass (2 PR reads, 2 main reads, 1 parents read) and no new GitHub read' '[ "$(cat "$STUB_DIR/cnt.verify")" = 2 ] && [ "$(cat "$STUB_DIR/cnt.main")" = 2 ] && [ "$(grep -c "^gh api repos/.*/commits/$STUB_FRESH_SHA" "$STUB_LOG")" = 1 ] && ! grep -q "^gh api repos/.*/compare/" "$STUB_LOG"'
check 'lag_admit: the trusted-CI-input helper ran once, bound to the older parent and the overlap-checked main, read-only, before the first admission write' '[ "$(grep -c "^ci-inputs " "$STUB_LOG")" = 1 ] && grep -q "^ci-inputs -I .*pm-admit-ci-inputs.py --repo .* --base $STUB_LAG_SHA --main $STUB_MAIN_SHA" "$STUB_LOG" && [ "$(grep -n "^ci-inputs " "$STUB_LOG" | head -1 | cut -d: -f1)" -lt "$(grep -nE -- "--add-label|label create" "$STUB_LOG" | head -1 | cut -d: -f1)" ]'
check 'lag_admit: every guard read precedes the first admission write' '[ "$(grep -n "git/ref/heads/main" "$STUB_LOG" | tail -1 | cut -d: -f1)" -lt "$(grep -nE -- "--add-label|label create" "$STUB_LOG" | head -1 | cut -d: -f1)" ]'
check 'lag_admit: no PR state change, no main merge, no branch write' '! grep -qE "^gh pr (close|reopen)|update-branch|^git (merge|push|reset|rebase)|--force" "$STUB_LOG" && ! grep -q "main_merge: performed" <<<"$OUT"'

echo "case: exact main parent - the original rule is unchanged and reported as exact_main_parent"
run_case lag_exact --no-12b-gate --reason 'lagging ref test'
check 'lag_exact: admitted, MERGE_REF_RULE rule=exact_main_parent, MERGE_REF_VERIFIED unchanged, no lagging audit line' '[ "$RC" = 0 ] && grep -qx "MERGE_REF_RULE rule=exact_main_parent pr=11173 head=$SHA merge=$STUB_FRESH_SHA main=$STUB_MAIN_SHA" <<<"$OUT" && grep -qx "MERGE_REF_VERIFIED pr=11173 head=$SHA main=$STUB_MAIN_SHA merge=$STUB_FRESH_SHA" <<<"$OUT" && [ "$(grep -c "rule=stale_merge_ref_waived:no_overlap" "$T/home/.claude/logs/pm-admit-skip.log")" = 1 ] && ! grep -q "merge-base --is-ancestor $STUB_LAG_SHA" "$STUB_LOG"'

echo "case: lagging merge ref - the PR changes a file main changed since the older parent (overlap vs the main merge-base): REFUSED with the overlap list"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/shared.ts\nlib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\nlib/shared.ts\n' \
  run_lag lag_overlap --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_overlap
check 'lag_overlap: REFUSE says the lagging-ref rule is not met, names the older parent, the count and exactly the overlapping path' 'grep -q "lagging-ref rule not met: overlap: the PR changes 1 file(s) that main also changed since that parent $STUB_LAG_SHA, so CI did not run against main.s version of them: lib/shared.ts\. Admission needs" <<<"$OUT" && grep -q "has parents \[$SHA $STUB_LAG_SHA\] but must be exactly the reviewed head and the overlap-checked main \[$SHA $STUB_MAIN_SHA\]" <<<"$OUT"'
check 'lag_overlap: no MERGE_REF_RULE line, nothing in the lagging audit trail' '! grep -q "^MERGE_REF_RULE" <<<"$OUT" && [ "$(grep -c "rule=stale_merge_ref_waived:no_overlap" "$T/home/.claude/logs/pm-admit-skip.log")" = 1 ]'

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
assert_needs_merge lag_selector
check 'lag_selector: NEEDS_MAIN_MERGE counts the 1 changed shared CI input (scripts/ci/change_scope.py)' 'grep -qx "NEEDS_MAIN_MERGE pr=11173 reason=ci_input_changed files=1" <<<"$OUT"'
check 'lag_selector: the overlap check passed first (no overlap message), nothing admitted, no label or PR write, no lagging rule line or audit entry' '! grep -q "overlap: the PR changes" <<<"$OUT" && ! grep -q "^MERGE_REF_RULE" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT" && ! grep -qE -- "--add-label|--remove-label|label create|pr edit" "$STUB_LOG"'
check 'lag_selector: the helper was asked for the range older parent..overlap-checked main' 'grep -q "^ci-inputs -I .*--base $STUB_LAG_SHA --main $STUB_MAIN_SHA" "$STUB_LOG"'

echo "case: lagging merge ref - several shared CI inputs changed: all counted, listed capped at 20 paths with the remainder counted"
MANYCI=$(for i in $(seq -w 1 25); do printf '.github/workflows/w%s.yml\\n' "$i"; done)
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' LAG_CI_INPUTS_CHANGED="$MANYCI" run_lag lag_selector_many --no-12b-gate --reason 'lagging ref test'
assert_needs_merge lag_selector_many
check 'lag_selector_many: NEEDS_MAIN_MERGE counts all 25 changed inputs' 'grep -qx "NEEDS_MAIN_MERGE pr=11173 reason=ci_input_changed files=25" <<<"$OUT"'

echo "case: lagging merge ref - no PR file overlaps but main changed ONLY scripts/ci/unit-shards/durations.json, a data file ci.yml reads from the workflow commit in a subdirectory (CTO REVISE #2 of MoP 494a207e, ts 1795587520.404159): REFUSED naming the path"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='scripts/ci/unit-shards/durations.json\n' LAG_CI_INPUTS_CHANGED='scripts/ci/unit-shards/durations.json\n' \
  run_lag lag_durations --no-12b-gate --reason 'lagging ref test'
assert_needs_merge lag_durations
check 'lag_durations: NEEDS_MAIN_MERGE counts the 1 changed shared CI input (scripts/ci/unit-shards/durations.json)' 'grep -qx "NEEDS_MAIN_MERGE pr=11173 reason=ci_input_changed files=1" <<<"$OUT"'
check 'lag_durations: the overlap check passed first, nothing admitted, no label or PR write, no lagging rule line or audit entry' '! grep -q "overlap: the PR changes" <<<"$OUT" && ! grep -q "^MERGE_REF_RULE" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT" && ! grep -qE -- "--add-label|--remove-label|label create|pr edit" "$STUB_LOG"'

echo "case: lagging merge ref - main drifted only in unrelated files (no changed CI input): ADMITTED"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='convex/other.ts\napp/page.tsx\nmodal/x.py\n' LAG_CI_INPUTS_CHANGED='' run_lag lag_unrelated --no-12b-gate --reason 'lagging ref test'
check 'lag_unrelated: exit 0, ADMITTED, MERGE_REF_RULE names the lagging rule with ci_inputs_changed=0, the helper ran once' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && grep -qx "MERGE_REF_RULE rule=stale_merge_ref_waived:no_overlap pr=11173 head=$SHA merge=$STUB_FRESH_SHA main_parent=$STUB_LAG_SHA main=$STUB_MAIN_SHA pr_files=1 main_files_since_parent=3 overlap=0 ci_inputs_changed=0" <<<"$OUT" && [ "$(grep -c "^ci-inputs " "$STUB_LOG")" = 1 ]'

echo "case: lagging merge ref - the CI-input discovery is unreadable: UNKNOWN refuses (fail closed), never a guess of no change"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' LAG_CI_INPUTS_FAIL=1 run_lag lag_ci_inputs_unreadable --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_ci_inputs_unreadable
check 'lag_ci_inputs_unreadable: REFUSE says the trusted shared CI inputs main changed since the older parent are UNKNOWN; nothing admitted' 'grep -q "the trusted shared CI inputs main changed since $STUB_LAG_SHA are UNKNOWN (read failed, timed out or returned past the 4s deadline)" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT" && ! grep -qE -- "--add-label|label create" "$STUB_LOG"'
check 'lag_ci_inputs_unreadable: the UNKNOWN refusal also names the unresolvable trusted-file reference as a cause (the helper exits 1 for it; CTO REVISE #2 "unresolved inputs failing closed")' 'grep -q "or a workflow reads a trusted file the discovery could not resolve" <<<"$OUT"'

# ---- Round 3 (CTO REVISE of MoP ce4f381b, C0ALZJHGE49 thread 1795567441.556059, ts 1795629066.866119): a revision and path held in a shell variable ----
# These cases run the REAL pm-admit-ci-inputs.py through the REAL guard on a real temp git repo whose pull_request workflow reads durations.json out of
# WORKFLOW_SHA through a variable (`SPEC="$WORKFLOW_SHA:scripts/ci/unit-shards/durations.json"; git show "$SPEC"`). The candidate helper silently skipped that read
# (exit 0, no output), which the guard took as "no CI input changed". Expected: a durations-only change REFUSES, unrelated drift is ADMITTED, and a recognized read
# the helper cannot resolve is UNKNOWN and REFUSES.
REAL_GIT=$(command -v git)
rgit() { "$REAL_GIT" -C "$REAL_REPO" -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false "$@"; }
real_commit() { rgit add -A >/dev/null && rgit commit -q -m "$1" --allow-empty && rgit rev-parse HEAD; }
mk_real_repo() { # <name> <run block of the pull_request workflow>; sets REAL_REPO and REAL_BASE (the older main)
  REAL_REPO="$T/real-$1"
  mkdir -p "$REAL_REPO/.github/workflows" "$REAL_REPO/scripts/ci/unit-shards" "$REAL_REPO/lib"
  "$REAL_GIT" -C "$REAL_REPO" init -q -b main
  { printf 'on:\n  pull_request:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - name: s\n        run: |\n'; printf '%s\n' "$2" | sed 's/^/          /'; } > "$REAL_REPO/.github/workflows/ci.yml"
  printf '{"a": 1}\n' > "$REAL_REPO/scripts/ci/unit-shards/durations.json"
  printf 'x = 1\n' > "$REAL_REPO/lib/other.ts"
  REAL_BASE=$(real_commit base)
}
INDIRECT_RUN='SPEC="$WORKFLOW_SHA:scripts/ci/unit-shards/durations.json"
git show "$SPEC" > "$trusted/durations.json"'

echo "case: lagging merge ref (real helper, real repo) - main changed ONLY durations.json, which the workflow reads through a REV:PATH variable: REFUSED at the guard naming the path"
mk_real_repo indirect_durations "$INDIRECT_RUN"
printf '{"a": 2}\n' > "$REAL_REPO/scripts/ci/unit-shards/durations.json"; REAL_MAIN=$(real_commit 'durations only')
LAG_REAL_REPO="$REAL_REPO" LAG_REAL_BASE="$REAL_BASE" LAG_REAL_MAIN="$REAL_MAIN" LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='scripts/ci/unit-shards/durations.json\n' \
  run_lag lag_real_indirect_durations --no-12b-gate --reason 'lagging ref test'
assert_needs_merge lag_real_indirect_durations
check 'lag_real_indirect_durations: NEEDS_MAIN_MERGE counts the 1 changed input (the real helper followed the variable to durations.json)' 'grep -qx "NEEDS_MAIN_MERGE pr=11173 reason=ci_input_changed files=1" <<<"$OUT"'
check 'lag_real_indirect_durations: the real helper ran once, nothing admitted, no lagging rule line or audit entry' '[ "$(grep -c "^ci-inputs " "$STUB_LOG")" = 1 ] && ! grep -q "^MERGE_REF_RULE" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT"'

echo "case: lagging merge ref (real helper, real repo) - the same workflow, main drifted only in an unrelated file: ADMITTED"
mk_real_repo indirect_unrelated "$INDIRECT_RUN"
printf 'x = 2\n' > "$REAL_REPO/lib/other.ts"; REAL_MAIN=$(real_commit 'unrelated drift')
LAG_REAL_REPO="$REAL_REPO" LAG_REAL_BASE="$REAL_BASE" LAG_REAL_MAIN="$REAL_MAIN" LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/other.ts\n' \
  run_lag lag_real_indirect_unrelated --no-12b-gate --reason 'lagging ref test'
check 'lag_real_indirect_unrelated: exit 0, ADMITTED, the lagging rule line reports ci_inputs_changed=0, the real helper ran once' '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && grep -q "^MERGE_REF_RULE rule=stale_merge_ref_waived:no_overlap .* ci_inputs_changed=0$" <<<"$OUT" && [ "$(grep -c "^ci-inputs " "$STUB_LOG")" = 1 ]'

echo "case: lagging merge ref (real helper, real repo) - the workflow has a trusted read the helper cannot resolve (\$SPEC never set), main drifted only in an unrelated file: UNKNOWN refuses, never eligible"
mk_real_repo unresolved_read 'git show "$SPEC" > "$trusted/durations.json"'
printf 'x = 3\n' > "$REAL_REPO/lib/other.ts"; REAL_MAIN=$(real_commit 'unrelated drift')
LAG_REAL_REPO="$REAL_REPO" LAG_REAL_BASE="$REAL_BASE" LAG_REAL_MAIN="$REAL_MAIN" LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/other.ts\n' \
  run_lag lag_real_unresolved_read --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_real_unresolved_read
check 'lag_real_unresolved_read: REFUSE says the trusted shared CI inputs are UNKNOWN and names the unresolvable trusted-file reference as the cause; nothing admitted, no label write' 'grep -q "the trusted shared CI inputs main changed since $STUB_LAG_SHA are UNKNOWN (read failed, timed out or returned past the 4s deadline) (or a workflow reads a trusted file the discovery could not resolve)" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT" && ! grep -q "^MERGE_REF_RULE" <<<"$OUT" && ! grep -qE -- "--add-label|label create" "$STUB_LOG"'

echo "case: exact main parent - the CI-input helper is not run (nothing lagged)"
run_case lag_exact_no_helper --no-12b-gate --reason 'lagging ref test'
check 'lag_exact_no_helper: admitted on the exact rule and the helper never ran' '[ "$RC" = 0 ] && grep -q "rule=exact_main_parent" <<<"$OUT" && ! grep -q "^ci-inputs " "$STUB_LOG"'

echo "case: lagging merge ref - an overlap still refuses first (the CI-input helper is not consulted)"
LAG_ANCESTOR=yes LAG_PR_FILES='lib/shared.ts\n' LAG_PR_FILES_LAGBASE='lib/shared.ts\n' LAG_MAIN_FILES='lib/shared.ts\n' LAG_CI_INPUTS_CHANGED='scripts/ci/change_scope.py\n' run_lag lag_overlap_first --no-12b-gate --reason 'lagging ref test'
assert_stale_refused lag_overlap_first
check 'lag_overlap_first: REFUSE is the overlap message and the helper never ran' 'grep -q "overlap: the PR changes 1 file(s)" <<<"$OUT" && ! grep -q "^ci-inputs " "$STUB_LOG"'

echo "case: lagging merge ref - --dry-run previews the verdict without writing or changing its exit code"
AUDITED_LAG=$(grep -c "rule=stale_merge_ref_waived:no_overlap" "$T/home/.claude/logs/pm-admit-skip.log")
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' run_lag lag_dry --dry-run --no-12b-gate --reason 'lagging ref test'
check 'lag_dry: DRY RUN merge_ref ELIGIBLE with the lagging rule line, nothing admitted, exit 0' '[ "$RC" = 0 ] && grep -q "^DRY RUN merge_ref: ELIGIBLE" <<<"$OUT" && grep -q "^  MERGE_REF_RULE rule=stale_merge_ref_waived:no_overlap " <<<"$OUT" && grep -q "nothing admitted" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT"'
check 'lag_dry: no audit line, no label or PR write' '[ "$(grep -c "rule=stale_merge_ref_waived:no_overlap" "$T/home/.claude/logs/pm-admit-skip.log")" = "$AUDITED_LAG" ] && ! grep -qE -- "--add-label|--remove-label|label create|pr edit|update-branch" "$STUB_LOG"'
LAG_ANCESTOR=yes LAG_PR_FILES='lib/shared.ts\n' LAG_PR_FILES_LAGBASE='lib/shared.ts\n' LAG_MAIN_FILES='lib/shared.ts\n' run_lag lag_dry_overlap --dry-run --no-12b-gate --reason 'lagging ref test'
check 'lag_dry_overlap: DRY RUN merge_ref would REFUSE (rc=3) with the overlap list, dry-run exit code unchanged' '[ "$RC" = 0 ] && grep -q "^DRY RUN merge_ref: would REFUSE (rc=3)" <<<"$OUT" && grep -q "^  REFUSE stale_merge_ref #11173: .*overlap: the PR changes 1 file(s).*: lib/shared.ts\. Admission needs" <<<"$OUT"'
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='scripts/ci/change_scope.py\n' LAG_CI_INPUTS_CHANGED='scripts/ci/change_scope.py\n' run_lag lag_dry_selector --dry-run --no-12b-gate --reason 'lagging ref test'
check 'lag_dry_selector: DRY RUN merge_ref would REFUSE (rc=3) naming the changed selector, dry-run exit code unchanged' '[ "$RC" = 0 ] && grep -q "^DRY RUN merge_ref: would REFUSE (rc=3)" <<<"$OUT" && grep -q "^  REFUSE stale_merge_ref #11173: .*trusted CI input changed: .*: scripts/ci/change_scope.py\. Admission needs" <<<"$OUT"'
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='scripts/ci/unit-shards/durations.json\n' LAG_CI_INPUTS_CHANGED='scripts/ci/unit-shards/durations.json\n' run_lag lag_dry_durations --dry-run --no-12b-gate --reason 'lagging ref test'
check 'lag_dry_durations: DRY RUN merge_ref would REFUSE (rc=3) naming the changed durations file, dry-run exit code unchanged' '[ "$RC" = 0 ] && grep -q "^DRY RUN merge_ref: would REFUSE (rc=3)" <<<"$OUT" && grep -q "^  REFUSE stale_merge_ref #11173: .*trusted CI input changed: .*: scripts/ci/unit-shards/durations.json\. Admission needs" <<<"$OUT"'
run_case lag_dry_exact --dry-run --no-12b-gate --reason 'lagging ref test'
check 'lag_dry_exact: a fresh exact-main-parent ref previews ELIGIBLE with the exact_main_parent rule' '[ "$RC" = 0 ] && grep -q "^DRY RUN merge_ref: ELIGIBLE" <<<"$OUT" && grep -q "^  MERGE_REF_RULE rule=exact_main_parent " <<<"$OUT"'

echo "case: lagging merge ref - a CONFLICTING PR is never waived: refused (rc 2, main integration required) before the guard runs"
HM_VALUE="$SHA CONFLICTING"; export HM_VALUE
LAG_ANCESTOR=yes LAG_PR_FILES='lib/pr.ts\n' LAG_PR_FILES_LAGBASE='lib/pr.ts\n' LAG_MAIN_FILES='lib/main.ts\n' run_lag lag_conflicting --no-12b-gate --reason 'lagging ref test'
HM_VALUE="$SHA MERGEABLE"; export HM_VALUE
check 'lag_conflicting: exit 20 with NEEDS_MAIN_MERGE reason=conflict, no waiver receipt, nothing admitted' '[ "$RC" = 20 ] && grep -qx "NEEDS_MAIN_MERGE pr=11173 reason=conflict files=0" <<<"$OUT" && ! grep -q "stale_merge_ref_waived" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT"'
check 'lag_conflicting: the stale-ref guard never ran (no merge-commit, ancestry or CI-input read) and nothing was written' '! grep -qE "^gh api repos/.*/commits/$STUB_FRESH_SHA|merge-base --is-ancestor $STUB_LAG_SHA|^ci-inputs " "$STUB_LOG" && ! grep -qE -- "--add-label|--remove-label|label create|pr edit|update-branch" "$STUB_LOG"'

# ---- Fix 2 (Rajiv ts 1795761962.862319; rule 62 wins over the CI-input guard): trusted CI inputs = only what ci.yml/e2e.yml execute for a PR run ----
# The REAL helper on a real temp git repo: ci.yml runs scripts/ci/pr-entry.sh; perf.yml, deploy-x.yml, scripts/perf/bench.sh, scripts/deploy/ship.sh and scripts/ci/tests/test-x.sh
# exist but ci.yml/e2e.yml never execute them. A main delta that touches only those is waived (no overlap, no input change) and needs no main merge; a delta that touches
# ci.yml or the entrypoint ci.yml invokes still refuses (and --merge-only merges); an overlap still refuses first.
mk_pr_inputs_repo() { # <name>: sets REAL_REPO and REAL_BASE (the older main the lagging test merge was built on)
  mk_real_repo "$1" 'bash scripts/ci/pr-entry.sh'
  mkdir -p "$REAL_REPO/scripts/ci/tests" "$REAL_REPO/scripts/perf" "$REAL_REPO/scripts/deploy"
  printf 'name: perf\non:\n  schedule:\n    - cron: "0 3 * * *"\njobs:\n  p:\n    runs-on: ubuntu-latest\n    steps:\n      - run: bash scripts/perf/bench.sh\n' > "$REAL_REPO/.github/workflows/perf.yml"
  printf 'name: deploy\non:\n  push:\n    branches: [main]\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps:\n      - run: bash scripts/deploy/ship.sh\n' > "$REAL_REPO/.github/workflows/deploy-x.yml"
  printf 'echo pr entry\n' > "$REAL_REPO/scripts/ci/pr-entry.sh"; printf 'echo bench\n' > "$REAL_REPO/scripts/perf/bench.sh"
  printf 'echo ship\n' > "$REAL_REPO/scripts/deploy/ship.sh"; printf 'echo t\n' > "$REAL_REPO/scripts/ci/tests/test-x.sh"
  REAL_BASE=$(real_commit 'add the entrypoint, perf, deploy and tests files')
}
real_main_change() { # <repo-relative file>...: touch each, commit as the newer main (sets REAL_MAIN)
  local f; for f in "$@"; do mkdir -p "$(dirname "$REAL_REPO/$f")"; printf '# changed on main\n' >> "$REAL_REPO/$f"; done
  REAL_MAIN=$(real_commit "main change: $*")
}
real_lag() { # <name> <main files as a printf %b list> [run args]: the guard with the real helper; PRF overrides the PR's own files
  local name="$1" mf="$2"; shift 2
  LAG_REAL_REPO="$REAL_REPO" LAG_REAL_BASE="$REAL_BASE" LAG_REAL_MAIN="$REAL_MAIN" LAG_ANCESTOR=yes LAG_PR_FILES="${PRF:-lib/pr.ts\\n}" LAG_PR_FILES_LAGBASE="${PRF:-lib/pr.ts\\n}" LAG_MAIN_FILES="$mf" \
    run_lag "$name" --no-12b-gate --reason 'lagging ref test' "$@"
}

echo "case: CI-input guard (real helper) - a main delta that touches ONLY files ci.yml/e2e.yml never execute (perf.yml, deploy-*.yml, their scripts, scripts/ci/tests/**) is waived: no overlap, no input change, no main merge"
for f in .github/workflows/perf.yml .github/workflows/deploy-x.yml scripts/perf/bench.sh scripts/deploy/ship.sh scripts/ci/tests/test-x.sh; do
  tag=$(printf '%s' "$f" | tr -c 'a-zA-Z0-9\n' '_')
  mk_pr_inputs_repo "waive$tag"; real_main_change "$f"
  real_lag "lag_real_waive$tag" "$f\\n"
  check "lag_real_waive$tag: a main change to $f is ADMITTED on the waiver (MERGE_REF_RULE stale_merge_ref_waived:no_overlap, overlap=0 ci_inputs_changed=0), the real helper ran once, no main merge" '[ "$RC" = 0 ] && grep -qx "ADMITTED #11173 at $SHA" <<<"$OUT" && grep -q "^MERGE_REF_RULE rule=stale_merge_ref_waived:no_overlap .* overlap=0 ci_inputs_changed=0$" <<<"$OUT" && [ "$(grep -c "^ci-inputs " "$STUB_LOG")" = 1 ] && ! grep -q update-branch "$STUB_LOG"'
done

echo "case: CI-input guard (real helper) - a main delta that touches ci.yml or the entrypoint ci.yml invokes still REFUSES, naming exactly that path"
for f in .github/workflows/ci.yml scripts/ci/pr-entry.sh; do
  tag=$(printf '%s' "$f" | tr -c 'a-zA-Z0-9\n' '_')
  mk_pr_inputs_repo "refuse$tag"; real_main_change "$f"
  real_lag "lag_real_refuse$tag" "$f\\n"
  assert_needs_merge "lag_real_refuse$tag"
  check "lag_real_refuse$tag: NEEDS_MAIN_MERGE counts the 1 changed trusted CI input ($f); nothing admitted, no waiver receipt" 'grep -qx "NEEDS_MAIN_MERGE pr=11173 reason=ci_input_changed files=1" <<<"$OUT" && ! grep -q "^ADMITTED" <<<"$OUT" && ! grep -q "stale_merge_ref_waived" <<<"$OUT"'
done

echo "case: CI-input guard (real helper) - a mixed delta (perf.yml + the entrypoint) refuses naming ONLY the entrypoint, never the unrelated workflow"
mk_pr_inputs_repo mixed; real_main_change .github/workflows/perf.yml scripts/ci/pr-entry.sh
real_lag lag_real_mixed '.github/workflows/perf.yml\nscripts/ci/pr-entry.sh\n'
assert_needs_merge lag_real_mixed
check 'lag_real_mixed: exactly 1 changed input (the entrypoint); perf.yml is not counted' 'grep -qx "NEEDS_MAIN_MERGE pr=11173 reason=ci_input_changed files=1" <<<"$OUT" && ! grep -q "perf.yml" <<<"$OUT"'

echo "case: CI-input guard (real helper) - the PR overlapping a main file still refuses first (rule 62 overlap), the helper is not consulted"
mk_pr_inputs_repo overlap; real_main_change .github/workflows/perf.yml lib/other.ts
PRF='lib/other.ts\n' real_lag lag_real_overlap '.github/workflows/perf.yml\nlib/other.ts\n'
assert_stale_refused lag_real_overlap
check 'lag_real_overlap: REFUSE is the overlap message naming lib/other.ts, and the helper never ran' 'grep -q "overlap: the PR changes 1 file(s)" <<<"$OUT" && grep -q "lib/other.ts" <<<"$OUT" && ! grep -q "^ci-inputs " "$STUB_LOG"'

echo "case: --merge-only (real helper) - a perf.yml-only main delta is skipped (rule 62), a ci.yml change merges main (the lane path is --merge-only)"
mk_pr_inputs_repo mo_perf; real_main_change .github/workflows/perf.yml
SKIPLOG_N=$(skiplog_lines)
LAG_REAL_REPO="$REAL_REPO" LAG_REAL_BASE="$REAL_BASE" LAG_REAL_MAIN="$REAL_MAIN" MO_MAIN_FILES='.github/workflows/perf.yml\n' run_mo mo_real_perf_only
assert_mo_skipped mo_real_perf_only
check 'mo_real_perf_only: the real helper ran once; the reason line is decision=skip, and the lagging waiver left no audit line' '[ "$(grep -c "^ci-inputs " "$STUB_LOG")" = 1 ] && grep -qx "MAIN_MERGE_REASON: decision=skip reason=no_conflict_no_overlap rule=62 head=$SHA main=$STUB_MAIN_SHA files=0" <<<"$OUT"'
mk_pr_inputs_repo mo_ci; real_main_change .github/workflows/ci.yml
SKIPLOG_N=$(skiplog_lines)
UPDATE_OK=1 LAG_REAL_REPO="$REAL_REPO" LAG_REAL_BASE="$REAL_BASE" LAG_REAL_MAIN="$REAL_MAIN" MO_MAIN_FILES='.github/workflows/ci.yml\n' run_mo mo_real_ci_yml
check 'mo_real_ci_yml: performed with reason ci_input_changed, the probe line names exactly .github/workflows/ci.yml, one update-branch pinned to the original head' '[ "$RC" = 0 ] && grep -qx "MAIN_MERGE: performed head=$STUB_MERGED_SHA reason=ci_input_changed files=1" <<<"$OUT" && grep -qx "STALE_REF_PROBE ci_inputs_changed n=1 main_parent=$STUB_LAG_SHA main=$STUB_MAIN_SHA files=.github/workflows/ci.yml" <<<"$OUT" && [ "$(grep -c update-branch "$STUB_LOG")" = 1 ] && grep -q "update-branch -f expected_head_sha=$SHA\$" "$STUB_LOG"'

echo "case: static - Fix 1/3 are wired in admission mode only, after the round breaker; the lane rule and the merge reason line are documented"
check 'pm-admit-ci.sh: admission_cap_gate then verdict_gate run only when neither --merge-only nor --check-12b-only is set' 'grep -qF "if [ \"\$MERGE_ONLY\" = 0 ] && [ \"\$CHECK_12B_ONLY\" = 0 ]; then admission_cap_gate; verdict_gate; fi" "$SCRIPT"'
check 'pm-admit-ci.sh: verdict_gate maps the helper to exit 11 (classify first), exit 12 (S171 packet) and exit 7 (UNAVAILABLE), and has no bypass flag' 'b=$(awk "/^verdict_gate\(\) \{/{f=1} f{print} f&&/^\}/{exit}" "$SCRIPT"); grep -q "20) exit 11" <<<"$b" && grep -q "21) exit 12" <<<"$b" && grep -q "11) echo .REFUSE: github_api_error ci_verdict.; exit 7" <<<"$b" && ! grep -qE -- "--force-ci|--no-verdict|PM_ADMIT_VERDICT" <<<"$b"'
check 'pm-admit-ci.sh: the header tells lanes to use --merge-only, and --merge-only prints the MAIN_MERGE_REASON line' 'grep -q "LANES MUST use .--merge-only <PR>. for main integration" "$SCRIPT" && grep -q "MAIN_MERGE_REASON: decision=" "$SCRIPT"'
check 'pm-admit-ci.sh: the override ledger writes granted/consumed records only (carried is legacy-read), takes an O_EXCL claim per grant id before the first label mutation, binds a grant to its head, and no carry, descendant call or legacy un-kinded write remains' 'grep -q "admission-cap-override-granted=" "$SCRIPT" && grep -q "admission-cap-override-consumed=" "$SCRIPT" && ! grep -qE "admission-cap-override-carried=[\$]" "$SCRIPT" && grep -q "set -C" "$SCRIPT" && grep -q "pm-admit-cap-claims" "$SCRIPT" && grep -q "cap_override_head_check" "$SCRIPT" && ! grep -q "descendant" <(grep -v "^#" "$SCRIPT") && ! grep -qE "admission-cap-override=[\$]" "$SCRIPT"'
check 'pm-admit-verdict.py is read-only: no gh write verbs, no Slack, no pm-ops or sqlite' '! grep -qE "(-X|--method)[ =]+(POST|PUT|PATCH|DELETE)|slack-send|sqlite3|pm-ops\.py|gh (pr|issue|label) (edit|close|create|comment)" "$SCRIPT_DIR_VERDICT"'

echo "case: static - the waiver is wired into the guard and the receipt is named"
guard_body() { awk '/^merge_ref_guard\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$SCRIPT"; }
check 'pm-admit-ci.sh: merge_ref_guard sends every non-exact tuple to lagging_ref_verify (the waiver) and keeps exact_main_parent' 'g=$(guard_body); grep -q "^    lagging_ref_verify$" <<<"$g" && grep -q "rule=exact_main_parent" <<<"$g"'
check 'pm-admit-ci.sh: no parked refusal remains, and the receipt is stale_merge_ref_waived:no_overlap (the old rule name is gone)' '! grep -qE "lag_parked_refuse|lagging ref \(rule parked\)|lagging_ref_no_overlap" "$SCRIPT" && grep -q "MERGE_REF_RULE rule=stale_merge_ref_waived:no_overlap" "$SCRIPT"'

echo "case: static - the script never contains a PR close or reopen"
check 'pm-admit-ci.sh has no gh pr close/reopen call and no refresh_stale_merge_ref' '! grep -qE "gh pr (close|reopen)|refresh_stale_merge_ref|STALE_MERGE_REF_REFRESHED" "$SCRIPT"'
check 'pm-admit-ci.sh has no stale-ref polling loop, poll sleep or fail-open WARN, and no compare read' '! grep -qE "STALE_POLL_SLEEP|STALE_REFRESH_WAIT|verify_fresh_merge_ref|STALE_MERGE_REF_RECOMPUTED|stale-merge check unavailable|/compare/.*--jq .status" "$SCRIPT"'

finish
