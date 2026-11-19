#!/usr/bin/env bash
# Launcher dry-run contract with paginated GitHub comment fixtures.
set -euo pipefail
launcher=${1:-$(cd "$(dirname "$0")/.." && pwd)/proxy-agent.sh}
work=$(mktemp -d "${TMPDIR:-/tmp}/proxy-review-cap-test.XXXXXX")
mkdir -p "$work/bin" "$work/home"
live_home=$HOME
export PATH="$work/bin:$PATH" HOME="$work/home" PROXY_AGENT_OUT_DIR="$work/out"
export PROXY_AGENT_DRY_RUN=1 PROXY_AGENT_NO_SUBSCRIPTION=1 PROXY_BURN_GATE=shadow
unset PROXY_AGENT_REVIEW_CAP_OVERRIDE
export FIXTURE="$work/comments.json"
cat > "$work/bin/gh" <<'STUB'
#!/usr/bin/env bash
[[ "$*" == 'api --paginate --slurp repos/Scribie/heydonna-app/issues/321/comments?per_page=100' ]] || exit 98
[[ "${FIXTURE_GH_ERROR:-0}" != 1 ]] || exit 1
exec /bin/cat "$FIXTURE"
STUB
chmod +x "$work/bin/gh"

check() {
  local label=$1 expected_rc=$2 expected_line=$3 type=${4:-pm-admission-reviewer} rc=0
  bash "$launcher" "$type" "$work/brief" --model astra >"$work/stdout" 2>"$work/stderr" || rc=$?
  if [[ "$rc" != "$expected_rc" ]] || ! grep -Fxq "$expected_line" "$work/stderr"; then
    printf 'FAIL %s: rc=%s expected=%s\n' "$label" "$rc" "$expected_rc" >&2
    /bin/cat "$work/stderr" >&2
    exit 1
  fi
  if [[ "$rc" == 0 ]]; then
    grep -q '^PROXY_AGENT_PATHS ' "$work/stdout"
  else
    [[ ! -s "$work/stdout" ]]
  fi
  printf 'PASS %s (rc=%s)\n' "$label" "$rc"
}

relay=${2:-$(cd "$(dirname "$0")/../../../../.." && pwd)/hook-relay.sh}
if [[ ! -f "$relay" && $# -lt 2 ]]; then
  relay="$live_home/.local/share/master-of-panes/current/scripts/hook-relay.sh"
fi

check_agent() {
  local label=$1 expected=$2 type=${3:-pm-admission-reviewer}
  printf '{"cwd":"/tmp/heydonna-app","tool_name":"Agent","tool_input":{"subagent_type":"%s","prompt":"for PR #321"}}\n' "$type" \
    | MOP_HOOK_BASE=http://127.0.0.1:1 bash "$relay" PreToolUse > "$work/hook"
  python3 -I - "$work/hook" "$expected" <<'PY'
import json, pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
output = json.loads(text) if text else {}
decision = output.get('hookSpecificOutput', {}).get('permissionDecision')
expected = sys.argv[2]
if expected == 'deny':
    assert decision == 'deny' and 'REVIEW_CAP_2' in output['hookSpecificOutput']['permissionDecisionReason'], output
else:
    assert decision != 'deny', output
    if expected != 'allow':
        assert expected in output.get('systemMessage', ''), output
PY
  printf 'PASS in-process hook: %s\n' "$label"
}

printf 'Work for PR #321\n' > "$work/brief"
printf '%s\n' '[[{"id":1,"body":"Proof\nPM 12b: REVISE at abcdef1"}]]' > "$FIXTURE"
check 'attempt 1; ignore non-first-line PM marker' 0 'proxy-agent: REVIEW_CAP_OK stage=12b pr=321 revise_rounds=0'
check_agent 'attempt 1' allow
printf 'Related: PR #999\nPR: 321\n' > "$work/brief"
printf '%s\n' '[[{"id":1,"body":"## PM 12b: PASS at abcdef1"},{"id":2,"body":"PM 12b delta: REVISE at abcdef2"},{"id":3,"body":"PM 12b: PASS at abcdef2 (S153 disposition)"}],[{"id":1,"body":"## PM 12b: PASS at abcdef1"}]]' > "$FIXTURE"
check 'attempt 2; exclude carry-forwards, S153 and duplicate IDs' 0 'proxy-agent: REVIEW_CAP_OK stage=12b pr=321 revise_rounds=1'
check_agent 'attempt 2' allow
printf '%s\n' '[[{"id":1,"body":"PM 12b: PASS at abcdef1"}],[{"id":2,"body":"PM 12b: REVISE at abcdef2"}]]' > "$FIXTURE"
check 'attempt 3; two full verdicts across heads and pages' 76 'proxy-agent: REVIEW_CAP_2 stage=12b pr=321 revise_rounds=2'
check_agent 'attempt 3' deny
PROXY_AGENT_REVIEW_CAP_OVERRIDE=rajiv:1795072424.329699 check 'authorized override' 0 'proxy-agent: REVIEW_CAP_OVERRIDE stage=12b pr=321 revise_rounds=2 override=rajiv:1795072424.329699'
PROXY_AGENT_REVIEW_CAP_OVERRIDE=rajiv:1795072424.329699 check_agent 'authorized override' REVIEW_CAP_OVERRIDE
PROXY_AGENT_REVIEW_CAP_OVERRIDE=rajiv:invalid check 'malformed override' 76 'proxy-agent: REVIEW_CAP_2 stage=12b pr=321 revise_rounds=2'
FIXTURE_GH_ERROR=1 check 'GitHub error fails open' 0 'proxy-agent: warning: review cap skipped stage=12b pr=321: GitHub comments unavailable'
FIXTURE_GH_ERROR=1 check_agent 'GitHub error fails open' 'GitHub comments unavailable'
printf '%s\n' '[[{"id":1,"body":"Codex QA: NEEDS_REVISION"},{"id":2,"body":"REVIEW_CAP_2 stage=qa head=abcdef1"},{"id":3,"body":"QA: NEEDS_REVISION\nQA: REVISE"}]]' > "$FIXTURE"
check 'one QA round per comment; ignore Codex and cap summaries' 0 'proxy-agent: REVIEW_CAP_OK stage=qa pr=321 revise_rounds=1' hd-proof-qa
check_agent 'QA attempt 2' allow hd-proof-qa
printf '%s\n' '[[{"id":1,"body":"hd-proof-qa: PASS at abcdef1"},{"id":2,"body":"QA report\n\nQA: REVISE at abcdef2\n"}]]' > "$FIXTURE"
check 'two QA rounds including final-line verdict via alias' 76 'proxy-agent: REVIEW_CAP_2 stage=qa pr=321 revise_rounds=2' pm-proof-qa
check_agent 'QA attempt 3' deny hd-proof-qa
