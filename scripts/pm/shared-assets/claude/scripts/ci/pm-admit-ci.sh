#!/usr/bin/env bash
# PM CI admission (Rajiv 2026-09-28 13:30 IST, thread 1790582222.927069):
# "ci admission should merge main and add the label."
# The CI/E2E trigger is the exact `ci-head:<40-hex head>` label (e2e.yml/ci.yml `labeled` gate).
# Related-tests gate (Rajiv "go", DM D0AMF0XE6TS thread 1791298729.215119): vitest related + tsc on the PR head before admission.
# Usage: pm-admit-ci.sh <PR> [--no-update] [--dry-run] [--skip-related-tests "<reason>"] [--force-ci "<reason>"] [--allow-main-red "<reason>"]
# Main-red gate (Rajiv DM ts 1791877690.725089): refuses admission while the latest completed main ci.yml run is red and the PR does not touch a failing test; override: --allow-main-red "<reason>".
# Scope gate (Rajiv 2026-10-08, DM thread 1791461793.117869): site/docs-only PRs are refused (no CI/E2E); PM may override with --force-ci.
set -euo pipefail
PR="$1"; shift; REPO=Scribie/heydonna-app
MAIN_REPO=/Users/rajiv/Downloads/projects/heydonna-app
# Related-tests gate is OFF by default (Rajiv 2026-10-11, DM ts 1791738750.254179: "why does pm admission have to run tsc or vitest? that's already done by the slots."). Slots run tests; CI is the gate. Opt in with --related-tests.
NO_UPDATE=0; DRY=0; SKIP_REASON="default-off: slots ran tests, CI is the gate (Rajiv ts 1791738750.254179)"; FORCE_CI=""; ALLOW_MAIN_RED=""
while [ $# -gt 0 ]; do
  case "$1" in
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

if [ "$DRY" = 1 ]; then
  H=$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid)
  echo "DRY RUN PR #$PR head=$H"
  rc=0; scope_gate "$H" || rc=$?
  [ $rc -ne 0 ] && { echo "DRY RUN done (scope gate rc=$rc); nothing admitted"; exit $rc; }
  rc=0; main_red_gate || rc=$?
  [ $rc -ne 0 ] && { echo "DRY RUN done (main-red gate rc=$rc); nothing admitted"; exit $rc; }
  rc=0; related_tests_gate "$H" || rc=$?
  echo "DRY RUN done (gate rc=$rc); nothing admitted"; exit $rc
fi

H0=$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid)
scope_gate "$H0" || exit 5
main_red_gate || exit 6
related_tests_gate "$H0" || exit 4

if [ "$NO_UPDATE" = 0 ]; then
  gh api -X PUT "repos/$REPO/pulls/$PR/update-branch" >/dev/null 2>&1 || echo "update-branch: already up to date or refused"
  sleep 8
fi
read -r HEAD MERGEABLE <<<"$(gh pr view "$PR" --repo "$REPO" --json headRefOid,mergeable --jq '.headRefOid+" "+.mergeable')"
echo "PR #$PR head=$HEAD mergeable=$MERGEABLE"
[ "$MERGEABLE" = "CONFLICTING" ] && { echo "REFUSE: conflict with main; route to slot"; exit 2; }
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
L="ci-head:$HEAD"
gh label create "$L" --repo "$REPO" --color 0E8A16 >/dev/null 2>&1 || true
for old in $(gh pr view "$PR" --repo "$REPO" --json labels --jq '.labels[].name|select(startswith("ci-head:"))'); do
  [ "$old" != "$L" ] && gh pr edit "$PR" --repo "$REPO" --remove-label "$old" >/dev/null
done
gh pr edit "$PR" --repo "$REPO" --add-label "$L" --add-label pm-state:qa-passed-awaiting-ci >/dev/null
echo "ADMITTED #$PR at $HEAD"
