#!/usr/bin/env bash
# Launcher dry-run contract with paginated GitHub comment fixtures.
set -euo pipefail
launcher=${1:-$(cd "$(dirname "$0")/.." && pwd)/proxy-agent.sh}
work=$(mktemp -d "${TMPDIR:-/tmp}/proxy-review-cap-test.XXXXXX")
mkdir -p "$work/bin" "$work/home"
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

printf 'Work for PR #321\n' > "$work/brief"
printf '%s\n' '[[{"body":"PM 12b: PASS at abcdef0\nPM 12b: REVISE at abcdef1"}]]' > "$FIXTURE"
check 'zero rounds; ignore non-first-line PM marker' 0 'proxy-agent: REVIEW_CAP_OK stage=12b pr=321 revise_rounds=0'
printf 'Related: PR #999\nPR: 321\n' > "$work/brief"
printf '%s\n' '[[{"body":"## PM 12b: REVISE at abcdef1"}]]' > "$FIXTURE"
check 'one round; explicit PR field wins' 0 'proxy-agent: REVIEW_CAP_OK stage=12b pr=321 revise_rounds=1'
printf '%s\n' '[[{"body":"PM 12b: REVISE at abcdef1"}],[{"body":"PM 12b delta: REVISE at abcdef2"}]]' > "$FIXTURE"
check 'two rounds across comment pages' 76 'proxy-agent: REVIEW_CAP_2 stage=12b pr=321 revise_rounds=2'
PROXY_AGENT_REVIEW_CAP_OVERRIDE=rajiv:1795072424.329699 check 'authorized override' 0 'proxy-agent: REVIEW_CAP_OVERRIDE stage=12b pr=321 revise_rounds=2 override=rajiv:1795072424.329699'
PROXY_AGENT_REVIEW_CAP_OVERRIDE=rajiv:invalid check 'malformed override' 76 'proxy-agent: REVIEW_CAP_2 stage=12b pr=321 revise_rounds=2'
FIXTURE_GH_ERROR=1 check 'GitHub error fails open' 0 'proxy-agent: warning: review cap skipped stage=12b pr=321: GitHub comments unavailable'
printf '%s\n' '[[{"body":"Codex QA: NEEDS_REVISION"},{"body":"REVIEW_CAP_2 stage=qa head=abcdef1"},{"body":"QA: NEEDS_REVISION\nQA: REVISE"}]]' > "$FIXTURE"
check 'one QA round per comment; ignore Codex and cap summaries' 0 'proxy-agent: REVIEW_CAP_OK stage=qa pr=321 revise_rounds=1' hd-proof-qa
printf '%s\n' '[[{"body":"QA: NEEDS_REVISION"},{"body":"QA report\n\nQA: REVISE at abcdef2\n"}]]' > "$FIXTURE"
check 'two QA rounds including final-line verdict via alias' 76 'proxy-agent: REVIEW_CAP_2 stage=qa pr=321 revise_rounds=2' pm-proof-qa
