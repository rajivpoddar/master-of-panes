#!/usr/bin/env bash
# PM CI admission (Rajiv 2026-09-28 13:30 IST, thread 1790582222.927069):
# "ci admission should merge main and add the label."
# The CI/E2E trigger is the exact `ci-head:<40-hex head>` label (e2e.yml/ci.yml `labeled` gate).
# Related-tests gate (Rajiv "go", DM D0AMF0XE6TS thread 1791298729.215119): vitest related + tsc on the PR head before admission.
# Usage: pm-admit-ci.sh <PR> [--no-update] [--dry-run] [--skip-related-tests "<reason>"] [--force-ci "<reason>"] [--allow-main-red "<reason>"]
# 12b: --check-12b-only <PR> is read-only; --no-12b-gate --reason "<text>" audits an explicit override.
# Main-red gate REMOVED (Rajiv C0ALZJHGE49 thread 1793958692.281499, ts 1793960417.571149: "remove it outright. note main red flakes have to be rerun. ... the skip pr has to be immediately merged with a followup issue. for affected pr's, merge main and re-admit."). Supersedes DM ts 1791877690.725089. --allow-main-red is still accepted (no-op) for compatibility.
# Scope gate (Rajiv 2026-10-08, DM thread 1791461793.117869): site/docs-only PRs are refused (no CI/E2E); PM may override with --force-ci.
set -euo pipefail
CHECK_12B_ONLY=0
if [ "${1:-}" = --check-12b-only ]; then CHECK_12B_ONLY=1; shift; fi
PR="${1:-}"; [ -n "$PR" ] || exit 64
shift; REPO=Scribie/heydonna-app
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAIN_REPO=/Users/rajiv/Downloads/projects/heydonna-app
# Crash = non-zero (never 0): any unexpected error/unbound var exits 70 with a REFUSE line.
trap 'rc=$?; echo "REFUSE: script_error line $LINENO rc=$rc"; exit 70' ERR
H="" H0="" HM="" HEAD="" MERGEABLE="" LJ="" L=""
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

# This precedes ALL admission effects, including body/label writes and main updates.
H0=$(gh_get pr_head sha gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid) || api_fail pr_head
PRSTATE=$(gh_get pr_state ok gh pr view "$PR" --repo "$REPO" --json state --jq .state) || api_fail pr_state
[ "$PRSTATE" = OPEN ] || { echo "REFUSE: PR #$PR is $PRSTATE, not OPEN; nothing admitted"; exit 8; }
rc=0; gate_12b "$H0" || rc=$?
[ "$rc" = 0 ] || exit "$rc"
[ "$CHECK_12B_ONLY" = 0 ] || exit 0

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
    timeout "$OVERLAP_TIMEOUT" python3 -I "$SCRIPT_DIR/pm-admit-12b.py" --overlap-only --repo "$REPO" --pr "$PR" --head "$1" \
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
  elif [ "$rc" = 124 ]; then
    echo "12B_OVERLAP: UNKNOWN reason=report_timeout after=${OVERLAP_TIMEOUT}s"
  else
    echo "12B_OVERLAP: UNKNOWN reason=report_failed rc=$rc"
  fi
  overlap_cleanup; OVERLAP_OUT=""
  return 0
}

if [ "$DRY" = 1 ]; then
  H=$(gh_get pr_head sha gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid) || api_fail pr_head
  echo "DRY RUN PR #$PR head=$H"
  overlap_start "$H"; overlap_finish
  rc=0; scope_gate "$H" || rc=$?
  [ $rc -ne 0 ] && { echo "DRY RUN done (scope gate rc=$rc); nothing admitted"; exit $rc; }
  rc=0; related_tests_gate "$H" || rc=$?
  echo "DRY RUN done (gate rc=$rc); nothing admitted"; exit $rc
fi

# H0 remains the pre-check's reviewed head; never overwrite it with an unchecked read.
# Closing-reference gate (Rajiv C0ALZJHGE49 thread 1793953549.727149, ts 1793971953.982729 "yes, do it";
# ts 1793972664.618549: "pr admit ci should add the closes #N if it's not already present instead of refusing it").
# Every admitted PR must close its own issue. If closingIssuesReferences is empty, derive exactly one issue number
# (body "Part of #N" > branch "<type>/<N>-..." > title "(#N)"), add "Closes #N" to the PR body, and read it back.
# Refuse only when no single issue number can be derived or the readback still shows no closing reference.
closes_of() { gh_get pr_closes ok gh pr view "$PR" --repo "$REPO" --json closingIssuesReferences --jq '[.closingIssuesReferences[].number]|map(tostring)|join(",")'; }
CLOSES=$(closes_of) || api_fail pr_closes
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
fi
echo "closing-reference gate: closes #${CLOSES:-$CAND}"
clean_bad_ci_head_labels
scope_gate "$H0" || exit 5
related_tests_gate "$H0" || exit 4

if [ "$NO_UPDATE" = 0 ]; then
  gh api -X PUT "repos/$REPO/pulls/$PR/update-branch" >/dev/null 2>&1 || echo "update-branch: already up to date or refused"
  sleep 8
fi
HM=$(gh_get pr_head_mergeable head_mergeable gh pr view "$PR" --repo "$REPO" --json headRefOid,mergeable --jq '.headRefOid+" "+.mergeable') || api_fail pr_head_mergeable
read -r HEAD MERGEABLE <<<"$HM"
# A concurrent push or merge-main update must not carry stale review into dispatch.
if [ "$HEAD" != "$H0" ]; then
  rc=0; gate_12b "$HEAD" || rc=$?
  [ "$rc" = 0 ] || exit "$rc"
fi
echo "PR #$PR head=$HEAD mergeable=$MERGEABLE"
[ "$MERGEABLE" = "CONFLICTING" ] && { echo "REFUSE: conflict with main; route to slot"; exit 2; }
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
L="ci-head:$HEAD"
gh label create "$L" --repo "$REPO" --color 0E8A16 >/dev/null 2>&1 || true
LJ=$(gh_get pr_labels labels_json gh pr view "$PR" --repo "$REPO" --json labels) || api_fail pr_labels
for old in $(printf '%s' "$LJ" | jq -r '.labels[].name|select(startswith("ci-head:"))'); do
  [ "$old" != "$L" ] && gh_do remove_old_ci_head gh pr edit "$PR" --repo "$REPO" --remove-label "$old"
done
gh_do add_ci_head_label gh pr edit "$PR" --repo "$REPO" --add-label "$L" --add-label pm-state:qa-passed-awaiting-ci
overlap_finish
echo "ADMITTED #$PR at $HEAD"
