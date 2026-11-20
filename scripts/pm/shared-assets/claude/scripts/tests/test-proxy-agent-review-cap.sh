#!/usr/bin/env bash
# Real launcher path; no agents, notifications, network, or live state.
set -euo pipefail
launcher=${1:-$(cd "$(dirname "$0")/.." && pwd)/proxy-agent.sh}
work=$(mktemp -d "${TMPDIR:-/tmp}/proxy-review-cap-test.XXXXXX")
export PATH="$work/bin:$PATH" HOME="$work/home" TMPDIR="$work"
export PROXY_AGENT_OUT_DIR="$work/out" PROXY_AGENT_STATE_DIR="$work/model-state"
export PROXY_AGENT_NO_SUBSCRIPTION=1 PROXY_BURN_GATE=shadow
export PROXY_AGENT_STUB_POOL_LIVE=0 CODEX_PROXY_CAP=8
unset PROXY_AGENT_REVIEW_CAP_OVERRIDE PROXY_AGENT_DRY_RUN PROXY_AGENT_NAME
export FIXTURE="$work/comments.json" STARTS="$work/starts"
mkdir -p "$work/bin" "$HOME/.claude"/{scripts,agents,state} "$HOME/.config/cliproxyapi" "$work/checkout"
printf 'CLIPROXY_API_KEY=fixture\n' > "$HOME/.config/cliproxyapi/client.env"
printf 'PROXY_AGENT_TOTAL_CAP=8\n' > "$HOME/.claude/state/proxy-caps.env"
for type in pm-admission-reviewer hd-proof-qa; do
  printf 'fixture agent\n' > "$HOME/.claude/agents/$type.md"
done
for helper in proxy-run-state proxy-assignment-notice proxy-agent-notify; do
  printf 'raise SystemExit(0)\n' > "$HOME/.claude/scripts/$helper.py"
done
printf 'print(0)\n' > "$HOME/.claude/scripts/proxy-cap-count.py"
cat > "$work/bin/git" <<'STUB'
#!/usr/bin/env bash
[[ "$1" == -C ]] || exit 98
case "$3" in
  rev-parse) printf '%s\n' "$2";;
  status) :;;
  branch) printf 'fixture\n';;
  *) exit 98;;
esac
STUB
cat > "$work/bin/gh" <<'STUB'
#!/usr/bin/env bash
[[ "$*" == 'api --hostname github.com --paginate --slurp repos/Scribie/heydonna-app/issues/'*'/comments?per_page=100' ]] || exit 98
[[ "${FIXTURE_GH_ERROR:-0}" != 1 ]] || exit 1
exec /bin/cat "$FIXTURE"
STUB
cat > "$work/bin/tmux" <<'STUB'
#!/usr/bin/env python3
import os, pathlib, re, shlex, sys
if sys.argv[1] == 'has-session':
    sys.exit(1)
if sys.argv[1] == 'new-session':
    run = pathlib.Path(shlex.split(sys.argv[-1])[1])
    if os.environ.get('FIXTURE_NO_RECEIPT') == '1':
        # MODE: DEBUG launches take no reservation: no receipt instruction may reach the agent.
        assert b'REVIEW_ROUND_RECEIPT' not in run.read_bytes()
    else:
        assert re.search(rb'REVIEW_ROUND_RECEIPT: [0-9a-f]{32}', run.read_bytes())
    with open(os.environ['STARTS'], 'a') as stream:
        print('START', file=stream)
    run.with_name('rc').write_text('0\n')
else:
    assert sys.argv[1] == 'wait-for'
STUB
printf '#!/usr/bin/env bash\nexit 99\n' > "$work/bin/claude"
chmod +x "$work/bin/"*
: > "$STARTS"

# Execute the proxy-only branch selected by Skill(launch-agent) step 3.
# The skill is prose; the candidate shell launcher and reservation are real.
launch() {
  local type=$1 model=gpt-6.2-sol
  [[ "$type" == pm-admission-reviewer ]] || model=gpt-6.1-terra
  bash "$launcher" "$type" "$work/brief" --cwd "$work/checkout" --model "$model"
}

run() {
  local label=$1 type=$2 expected=$3 marker=$4 rc=0
  launch "$type" >"$work/$label.out" 2>"$work/$label.err" || rc=$?
  if [[ "$rc" != "$expected" ]] || ! grep -Fq "$marker" "$work/$label.err"; then
    printf 'FAIL %s: rc=%s expected=%s\n' "$label" "$rc" "$expected" >&2
    /bin/cat "$work/$label.err" >&2
    exit 1
  fi
  printf 'PASS %s (rc=%s): ' "$label" "$rc"
  grep -F "$marker" "$work/$label.err" | sed -E 's/ receipt=[0-9a-f]{32}$/ receipt=[reserved]/'
}

pm=pm-admission-reviewer
printf 'Related: PR #999\nPR: 321\n' > "$work/brief"
printf '%s\n' '[[]]' > "$FIXTURE"
PROXY_AGENT_DRY_RUN=1 run dry "$pm" 0 'REVIEW_CAP_OK stage=12b pr=321 revise_rounds=0'
[[ ! -s "$STARTS" ]]
run first "$pm" 0 'REVIEW_CAP_OK stage=12b pr=321 revise_rounds=0'
first=$(grep -o 'receipt=[0-9a-f]*' "$work/first.err"); first=${first#receipt=}
python3 -I - "$first" > "$FIXTURE" <<'PY'
import json, sys
receipt = f"PM 12b: PASS at abcdef1\nREVIEW_ROUND_RECEIPT: {sys.argv[1]}"
print(json.dumps([[
    {"id": 1, "body": receipt},
    {"id": 6984189252, "body": "PM 12b: PASS at eaf3885fc9cea53e6a350440d45b4d92d4cdf160\nCARRY_FORWARD: proof only"},
    {"id": 3, "body": "PM 12b: PASS at abcdef2 (S153 disposition)"},
    {"id": 4, "body": "PM 12b delta: PASS at abcdef2\n\nDelta since the PASS: merge-only, no PR-owned change."},
], [{"id": 5, "body": receipt}]]))
PY
run second "$pm" 0 'revise_rounds=1 receipts=2 reservations=1 round=2'
run third "$pm" 76 'REVIEW_CAP_2 stage=12b pr=321 revise_rounds=2'
FIXTURE_GH_ERROR=1 run cached-outage "$pm" 76 'REVIEW_CAP_UNKNOWN stage=12b pr=321 reason=github_history_unavailable'
(umask 077; printf '%s\n' '[{"pr":321,"stage":"12b","round":3,"ts":"1000000000.000002"}]' > "$HOME/.claude/state/proxy-review-cap-approvals.json")
PROXY_AGENT_REVIEW_CAP_OVERRIDE=rajiv:1000000000.000002 run pm-owned-approval "$pm" 76 'REVIEW_CAP_OVERRIDE_DISABLED stage=12b'
[[ $(wc -l < "$STARTS") -eq 2 ]]

printf 'PR: 322\n' > "$work/brief"
FIXTURE_GH_ERROR=1 run no-history "$pm" 76 'REVIEW_CAP_UNKNOWN stage=12b pr=322 reason=github_history_unavailable'
printf '%s\n' '[[{"id":33,"body":null}]]' > "$FIXTURE"
run unreadable-history "$pm" 76 'REVIEW_CAP_UNKNOWN stage=12b pr=322 reason=github_history_unavailable'
[[ $(wc -l < "$STARTS") -eq 2 ]]

# Exact real verdict from PR #14161. The CARRY_FORWARD line is supplementary.
printf 'PR: 14161\n' > "$work/brief"
python3 -I - > "$FIXTURE" <<'PY'
import json
print(json.dumps([[{"id": 7001054897, "body": """PM 12b: REVISE at c924f568239e4364c4398a24df836ace71134899
PR: 14161
HEAD_SHA: c924f568239e4364c4398a24df836ace71134899
VERDICT: REVISE
REVIEWER: pm-admission-reviewer
TIMESTAMP: 2026-11-19T05:53:14Z
behavior: AC-1 billing: the seed repair covers empty databases only. A persisted base AssemblyAI row effective after 2026-11-19T00:00:00Z still wins at $0.21/h. Ensure upgrade seeding selects $0.23/h without rewriting historical pricing, and prove the existing-row case.
other: S133: publish remote RED-on-revert for changed test coverage and remote typecheck on the reviewed/reworked head; rerun overlapping usage-metering.test.ts. Current fresh-seed GREEN and disjoint audio GREEN were inspected.
CARRY_FORWARD: c99cb43e1eb10c8ff4d1ece10b382310a9d4e4c1→c924f568239e4364c4398a24df836ace71134899, diff files=2, audio overlap=none (GREEN only); metering overlap=convex/migrations/seedProviderPricing.ts; TypeScript delta disallows typecheck carry-forward.
"""}, {"id": 7001565584, "body": """PM 12b: PASS at 00ea9edcb551664d010338383e900f91c899efa8 (S153 disposition)
Reviewer verdict at this head was REVISE with REVIEW_CAP_2 (round 2) and only "other" findings: remote typecheck not completed, fresh-seed GREEN/RED and disjoint routing RED not re-run at this head, outdated Codex fresh-seed P2 thread. Per S153 (Rajiv D0AMF0XE6TS ts 1794997144.366759) PM auto-waives "other"-only findings at the cap and admits; the behavior finding (existing-row pricing) is fixed and verified by the reviewer (usage-metering 238 passed; upgrade case RED at c924f568, GREEN here). CI is the type gate (rule 45). Rajiv FYI goes in the daily summary."""}]]))
PY
run real-revise-cap "$pm" 76 'REVIEW_CAP_2 stage=12b pr=14161 revise_rounds=2 receipts=1 reservations=0'
# A separately posted round-two verdict must not make the disposition another review.
python3 -I - "$FIXTURE" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
pages = json.loads(p.read_text())
pages[0].append({"id": 6, "body": "PM 12b: REVISE at 00ea9edcb551664d010338383e900f91c899efa8"})
p.write_text(json.dumps(pages))
PY
run real-revise-reposted "$pm" 76 'REVIEW_CAP_2 stage=12b pr=14161 revise_rounds=2 receipts=2 reservations=0'
[[ $(wc -l < "$STARTS") -eq 2 ]]

# Launch the given agent types at once; leaves $work/<stage>-<n>.{out,err,rc}. Callers assert the outcome.
race_launch() {
  local stage=$1 label type i=0 pids=()
  shift
  for type in "$@"; do
    i=$((i + 1))
    label="$stage-$i"
    (rc=0; launch "$type" >"$work/$label.out" 2>"$work/$label.err" || rc=$?; printf '%s\n' "$rc" > "$work/$label.rc") &
    pids+=("$!")
  done
  wait "${pids[@]}"
}

race() {
  local stage=$1 pr=$2
  shift 2
  race_launch "$stage" "$@"
  [[ $(sort -n "$work/$stage-1.rc" "$work/$stage-2.rc" | tr '\n' ' ') == '0 76 ' ]]
  grep -Fq "REVIEW_CAP_OK stage=$stage pr=$pr revise_rounds=1 receipts=1 reservations=0 round=2" "$work/$stage-1.err" "$work/$stage-2.err"
  grep -Fq "REVIEW_CAP_2 stage=$stage pr=$pr revise_rounds=2 receipts=1 reservations=1" "$work/$stage-1.err" "$work/$stage-2.err"
  printf 'PASS concurrent %s (rc=0,76):\n' "$stage"
  grep -hE 'REVIEW_CAP_(OK|2) ' "$work/$stage-1.err" "$work/$stage-2.err" | sed -E 's/ receipt=[0-9a-f]{32}$/ receipt=[reserved]/'
}

printf 'PR: 323\n' > "$work/brief"
printf '%s\n' '[[{"id":1,"body":"PM 12b: REVISE at abcdef1"}]]' > "$FIXTURE"
race 12b 323 "$pm" "$pm"
[[ $(wc -l < "$STARTS") -eq 3 ]]

token_of() { local t; t=$(grep -o 'receipt=[0-9a-f]\{32\}' "$work/$1.err"); printf '%s' "${t#receipt=}"; }
# Fixture: one POSTED hd-proof-qa verdict comment per argument (a receipt token, or - for none).
verdicts() {
  python3 -I - "$@" > "$FIXTURE" <<'PY'
import json, sys
comments = []
for n, token in enumerate(sys.argv[1:], 1):
    body = f"hd-proof-qa: REVISE at abcdef{n}"
    if token != "-":
        body += f"\nREVIEW_ROUND_RECEIPT: {token}"
    comments.append({"id": 100 + n, "body": body})
print(json.dumps([comments]))
PY
}

# 12b (d): bare reservations with no posted verdict still cap. The QA-stage change must not touch 12b.
printf 'PR: 340\n' > "$work/brief"
printf '%s\n' '[[]]' > "$FIXTURE"
run pm-bare-1 "$pm" 0 'REVIEW_CAP_OK stage=12b pr=340 revise_rounds=0 receipts=0 reservations=0 round=1'
run pm-bare-2 "$pm" 0 'REVIEW_CAP_OK stage=12b pr=340 revise_rounds=1 receipts=0 reservations=1 round=2'
run pm-bare-3 "$pm" 76 'REVIEW_CAP_2 stage=12b pr=340 revise_rounds=2 receipts=0 reservations=2'
# MODE: DEBUG is a QA-stage allowance only; a capped 12b launch stays refused.
printf 'MODE: DEBUG\nPR: 340\n' > "$work/brief"
run pm-debug-ignored "$pm" 76 'REVIEW_CAP_2 stage=12b pr=340 revise_rounds=2 receipts=0 reservations=2'
[[ $(wc -l < "$STARTS") -eq 5 ]]

# QA (Rajiv C0ALZJHGE49 thread 1795010709.991409): only POSTED verdicts occupy QA rounds.
qa=hd-proof-qa
# (a) three launches that never post a verdict: each is allowed, none consumes a round.
printf 'PR: 331\n' > "$work/brief"
printf '%s\n' '[[]]' > "$FIXTURE"
run qa-open-1 "$qa" 0 'REVIEW_CAP_OK stage=qa pr=331 revise_rounds=0 receipts=0 reservations=0 round=1'
run qa-open-2 "$qa" 0 'REVIEW_CAP_OK stage=qa pr=331 revise_rounds=0 receipts=0 reservations=1 round=1'
run qa-open-3 "$qa" 0 'REVIEW_CAP_OK stage=qa pr=331 revise_rounds=0 receipts=0 reservations=2 round=1'
[[ $(wc -l < "$STARTS") -eq 8 ]]
# (b) two posted verdicts cap a normal launch; the three unposted reservations above add nothing to them.
verdicts - -
run qa-two-verdicts "$qa" 76 'REVIEW_CAP_2 stage=qa pr=331 revise_rounds=2 receipts=2 reservations=3'
# (c) the same capped state admits a brief whose first non-empty line is MODE: DEBUG: no reservation, no receipt line.
printf '\n  \nMODE: DEBUG\nPR: 331\n' > "$work/brief"
PROXY_AGENT_DRY_RUN=1 run qa-debug-dry "$qa" 0 'REVIEW_CAP_DEBUG_ALLOWED stage=qa pr=331 revise_rounds=2'
FIXTURE_NO_RECEIPT=1 run qa-debug "$qa" 0 'REVIEW_CAP_DEBUG_ALLOWED stage=qa pr=331 revise_rounds=2'
if grep -Fq 'receipt=' "$work/qa-debug.err"; then echo 'FAIL qa-debug: a receipt token was issued' >&2; exit 1; fi
[[ $(wc -l < "$STARTS") -eq 9 ]]
# Only an exact first-line MODE: DEBUG unlocks the cap. Each refusal also shows reservations=3: the DEBUG launch took none.
for debug_brief in $'PR: 331\nMODE: DEBUG\n' $'MODE: DEBUG now\nPR: 331\n' $'MODE: DEBUGGING\nPR: 331\n' $'MODE: debug\nPR: 331\n' $'  MODE: DEBUG\nPR: 331\n'; do
  printf '%s' "$debug_brief" > "$work/brief"
  run qa-debug-negative "$qa" 76 'REVIEW_CAP_2 stage=qa pr=331 revise_rounds=2 receipts=2 reservations=3'
done
[[ $(wc -l < "$STARTS") -eq 9 ]]
# A posted verdict still pairs with its own launch token (one round, not two); unposted launches stay free.
printf 'PR: 332\n' > "$work/brief"
printf '%s\n' '[[]]' > "$FIXTURE"
run qa-pair-1 "$qa" 0 'REVIEW_CAP_OK stage=qa pr=332 revise_rounds=0 receipts=0 reservations=0 round=1'
t1=$(token_of qa-pair-1)
verdicts "$t1"
run qa-pair-2 "$qa" 0 'REVIEW_CAP_OK stage=qa pr=332 revise_rounds=1 receipts=1 reservations=1 round=2'
t2=$(token_of qa-pair-2)
run qa-pair-3 "$qa" 0 'REVIEW_CAP_OK stage=qa pr=332 revise_rounds=1 receipts=1 reservations=2 round=2'
verdicts "$t1" "$t2"
run qa-pair-capped "$qa" 76 'REVIEW_CAP_2 stage=qa pr=332 revise_rounds=2 receipts=2 reservations=3'
[[ $(wc -l < "$STARTS") -eq 12 ]]

# One posted QA verdict: two concurrent launches (the alias shares the stage) both fit, serialized by the DB transaction,
# because the first one's in-flight reservation consumes nothing.
printf 'for PR #321\n' > "$work/brief"
printf '%s\n' '[[{"id":1,"body":"QA report\n\nQA: NEEDS_REVISION"},{"id":2,"body":"Codex QA: REVISE"},{"id":3,"body":"REVIEW_CAP_2 stage=qa"}]]' > "$FIXTURE"
race_launch qa hd-proof-qa pm-proof-qa
[[ $(sort -n "$work/qa-1.rc" "$work/qa-2.rc" | tr '\n' ' ') == '0 0 ' ]]
grep -Fq 'REVIEW_CAP_OK stage=qa pr=321 revise_rounds=1 receipts=1 reservations=0 round=2' "$work/qa-1.err" "$work/qa-2.err"
grep -Fq 'REVIEW_CAP_OK stage=qa pr=321 revise_rounds=1 receipts=1 reservations=1 round=2' "$work/qa-1.err" "$work/qa-2.err"
printf 'PASS concurrent qa (rc=0,0):\n'
grep -hE 'REVIEW_CAP_(OK|2) ' "$work/qa-1.err" "$work/qa-2.err" | sed -E 's/ receipt=[0-9a-f]{32}$/ receipt=[reserved]/'
[[ $(wc -l < "$STARTS") -eq 14 ]]
