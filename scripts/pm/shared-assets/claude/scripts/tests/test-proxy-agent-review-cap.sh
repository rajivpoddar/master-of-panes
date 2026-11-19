#!/usr/bin/env bash
# Real launcher path; no agents, notifications, network, or live state.
set -euo pipefail
launcher=${1:-$(cd "$(dirname "$0")/.." && pwd)/proxy-agent.sh}
work=$(mktemp -d "${TMPDIR:-/tmp}/proxy-review-cap-test.XXXXXX")
export PATH="$work/bin:$PATH" HOME="$work/home" TMPDIR="$work"
export PROXY_AGENT_OUT_DIR="$work/out" PROXY_AGENT_STATE_DIR="$work/model-state"
export PROXY_AGENT_NO_SUBSCRIPTION=1 PROXY_BURN_GATE=shadow
unset PROXY_AGENT_REVIEW_CAP_OVERRIDE PROXY_AGENT_DRY_RUN PROXY_AGENT_NAME
export FIXTURE="$work/comments.json" STARTS="$work/starts"
mkdir -p "$work/bin" "$HOME/.claude"/{scripts,agents,state} "$HOME/.config/cliproxyapi" "$work/checkout"
printf 'CLIPROXY_API_KEY=fixture\n' > "$HOME/.config/cliproxyapi/client.env"
for type in pm-admission-reviewer hd-proof-qa; do
  printf 'fixture agent\n' > "$HOME/.claude/agents/$type.md"
done
for helper in proxy-run-state proxy-assignment-notice proxy-agent-notify; do
  printf 'raise SystemExit(0)\n' > "$HOME/.claude/scripts/$helper.py"
done
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
    assert re.search(r'REVIEW_ROUND_RECEIPT: [0-9a-f]{32}', run.read_text())
    with open(os.environ['STARTS'], 'a') as stream:
        print('START', file=stream)
    run.with_name('rc').write_text('0\n')
else:
    assert sys.argv[1] == 'wait-for'
STUB
printf '#!/usr/bin/env bash\nexit 99\n' > "$work/bin/claude"
chmod +x "$work/bin/"*
: > "$STARTS"

run() {
  local label=$1 type=${2:-pm-admission-reviewer} expected=${3:-0} marker=$4 rc=0
  bash "$launcher" "$type" "$work/brief" --cwd "$work/checkout" --model astra >"$work/$label.out" 2>"$work/$label.err" || rc=$?
  if [[ "$rc" != "$expected" ]] || ! grep -Fq "$marker" "$work/$label.err"; then
    printf 'FAIL %s: rc=%s expected=%s\n' "$label" "$rc" "$expected" >&2
    /bin/cat "$work/$label.err" >&2
    exit 1
  fi
  printf 'PASS %s (rc=%s)\n' "$label" "$rc"
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
FIXTURE_GH_ERROR=1 run cached-outage "$pm" 76 'warning: GitHub review history unavailable stage=12b pr=321'
grep -Fq 'REVIEW_CAP_2 stage=12b pr=321' "$work/cached-outage.err"
(umask 077; printf '%s\n' '[{"pr":321,"stage":"12b","round":3,"ts":"1000000000.000002"}]' > "$HOME/.claude/state/proxy-review-cap-approvals.json")
PROXY_AGENT_REVIEW_CAP_OVERRIDE=rajiv:1000000000.000002 run pm-owned-approval "$pm" 76 'REVIEW_CAP_OVERRIDE_DISABLED stage=12b'
[[ $(wc -l < "$STARTS") -eq 2 ]]

printf 'PR: 322\n' > "$work/brief"
FIXTURE_GH_ERROR=1 run no-history "$pm" 0 'warning: GitHub review history unavailable stage=12b pr=322'
grep -Fq 'revise_rounds=0 receipts=0 reservations=0 round=1' "$work/no-history.err"
[[ $(wc -l < "$STARTS") -eq 3 ]]

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
"""}]]))
PY
run real-revise "$pm" 0 'stage=12b pr=14161 revise_rounds=1 receipts=1 reservations=0 round=2'
run real-revise-cap "$pm" 76 'REVIEW_CAP_2 stage=12b pr=14161 revise_rounds=2'

printf 'for PR #321\n' > "$work/brief"
printf '%s\n' '[[{"id":1,"body":"QA report\n\nQA: NEEDS_REVISION"},{"id":2,"body":"Codex QA: REVISE"},{"id":3,"body":"REVIEW_CAP_2 stage=qa"}]]' > "$FIXTURE"
# One historical QA verdict leaves one slot. The alias shares the stage.
for label in qa-a qa-b; do
  if [[ "$label" == qa-a ]]; then type=hd-proof-qa; else type=pm-proof-qa; fi
  (rc=0; bash "$launcher" "$type" "$work/brief" --cwd "$work/checkout" --model astra >"$work/$label.out" 2>"$work/$label.err" || rc=$?; printf '%s\n' "$rc" > "$work/$label.rc") &
  if [[ "$label" == qa-a ]]; then a=$!; else b=$!; fi
done
wait "$a" "$b"
[[ $(sort -n "$work/qa-a.rc" "$work/qa-b.rc" | tr '\n' ' ') == '0 76 ' ]]
grep -Fq 'REVIEW_CAP_2 stage=qa pr=321 revise_rounds=2' "$work/qa-a.err" "$work/qa-b.err"
[[ $(wc -l < "$STARTS") -eq 5 ]]
printf 'PASS concurrent canonical QA and alias: exactly one dispatch, one refusal (0, 76)\n'
