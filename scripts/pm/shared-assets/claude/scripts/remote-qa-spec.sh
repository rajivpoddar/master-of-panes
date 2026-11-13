#!/usr/bin/env bash
# Remote single-spec E2E repro lane: ephemeral box at an EXACT head -> canonical local-repro-preflight --spec, strict replay,
# synthetic-JWT auth (rule 41, no live Clerk), LLM proxy on, the box's disposable Modal (-qa-<id>) + the slot's own dev Convex.
# GREEN, or GREEN -> RED (--mutate-patch applied for the RED leg only) -> GREEN on the SAME box. Always destroys the box.
#   remote-browser-box.sh spec --slot N --branch B --head SHA --spec tests/e2e/specs/... --out DIR --allow-seed-replace
#       [--mutate-patch FILE] [--test-line N] [--project core-rest] [--env-file F] [--config JSON] [--provider auto] [--ttl 150m] [--timeout SECS]
#   [--prod-build] [--trace] [--dry-run]: --prod-build forwards to preflight; --trace records Playwright trace (on) + video for every test;
#   --dry-run validates args and prints the resolved lane, no box. Timeouts/retries are never changed by these flags.
#   --disposable-convex also writes the capture-lane ownership receipt (remote-qa-ownership.sh, identical contract) to
#   /root/qa/convex-ownership.json (chmod 444; copy at <out>/convex-ownership.json; /root/qa/run-id == controllerRunId) BEFORE any
#   Convex write; the spec sees QA_CONVEX_OWNERSHIP_PATH. --pre-write-check 'CMD' (needs --disposable-convex): runs CMD on the box
#   (cwd = slot checkout, .env.local exported) after the receipt, before Modal prepare, config apply, seed import or the spec;
#   nonzero -> PRE_WRITE_CHECK_FAILED, run aborts rc 1, box destroyed. Controller-owned anchors (from its own state, NOT the
#   receipt) exported to CMD: QA_CONTROLLER_RUN_ID, QA_EXPECTED_HEAD (--head), QA_BOX_ID, QA_RECEIPT_SHA256 (sha256 of the receipt
#   as written), QA_CONVEX_OWNERSHIP_PATH=/root/qa/convex-ownership.json, QA_RUN_ID_PATH=/root/qa/run-id. Same anchors logged
#   Mac-side as one line: QA_PREWRITE_ANCHORS run=... head=... box=... receipt_sha256=... (stdout/ctl.log + <out>/lane.txt).
#   The same five anchors (+ QA_RUN_ID_PATH) are exported READONLY into the spec runtime -> preflight -> Playwright -> spawned helpers,
#   passed as runtime arguments from controller state (never derived from the receipt or a box file).
#   --timeout SECS: outer watchdog on the box-side lane run only (setup/teardown excluded). Default 3600s per leg (GREEN=3600, GREEN,RED,GREEN=10800),
#   capped to the box TTL minus 600s. It is a harness watchdog, NOT a Playwright/CI budget (rule 03); recorded as timeout_secs in lane.txt.
#   --on-miss (OPT-IN ONLY; Rajiv D0AMF0XE6TS thread 1793524869.957399 ev 1793528200.649109, CTO option A C0ALZJHGE49 1793524845.153109):
#   capture-on-miss through the SAME controller as the capture lane (remote-qa-modal.py register/overlay/prepare/capture-report
#   --on-miss): replay first; ONE eligible priced miss per exact request goes live behind a pre-dispatch reservation in the shared
#   R2 ledgers ($3 per physical box lifetime AND per QA attempt/head, cumulative with capture mode); payloads box-local only, zero
#   canonical fixture writes; UNKNOWN never resent. Needs --disposable-convex; GREEN legs only (no --mutate-patch). Never set by
#   default or by CI. Labels: a run with >=1 live capture ends `REMOTE_SPEC verdict=ON_MISS_CAPTURED` (never lane-ok/strict PASS);
#   all-replayed = `lane-ok-onmiss-replay-only`; a QA_ONMISS_* terminal = lane-fail reason=QA_ONMISS_*. Written to lane.txt + stdout.
#   --stripe-test-env (OPT-IN; needs --disposable-convex; #11552 AC-3 CTO procedure, #11552 comment 6697907189): use a private
#   mode-600 --env-file (full QA base env + the Stripe TEST pair + the subscriptionQuotes.ts:34-42 config). remote-qa-convex.py
#   create --stripe-test-env sets ONLY its allowlisted entries (STRIPE_ENV_ALLOWLIST in remote-qa-convex.py: STRIPE_SECRET_KEY,
#   STRIPE_SUBSCRIPTION_MERCHANT_ACCOUNT, STRIPE_MODE, SUBSCRIPTION_UI_PREVIEW_ENABLED, the four STRIPE_TEST_SUBSCRIPTION_PRICE_
#   {REPORTER,AGENCY}_{MONTHLY,ANNUAL}, STRIPE_TEST_SUBSCRIPTION_CATALOG_VERIFICATION, optional STRIPE_WEBHOOK_SECRET) in the SAME
#   update_environment_variables call as COMMUNITY_SERVER_SECRET on the ownership-verified disposable deployment. Missing required
#   names are listed (names only) and refused; value guards (sk_test_/rk_test_ key, live/pk_ refused, STRIPE_MODE=test, preview flag true|false,
#   price_ ids, whsec_ webhook secret) abort before any write. Names-only receipt: QA_DEPLOYMENT_ENV_SET names=a,b.
#   Runtime secrets (e.g. the whsec printed by `stripe listen --print-secret` on the box): the lane pushes the same helper to
#   /root/qa/convex-lane.py and a read-only ownership copy to /root/qa/convex-root/, and the spec runtime exports READONLY
#   QA_CONVEX_ENV_HELPER + QA_CONVEX_ENV_ROOT (inherited by preflight -> Playwright -> spawned helpers). From the spec:
#     STRIPE_WEBHOOK_SECRET=<whsec> python3 -I "$QA_CONVEX_ENV_HELPER" set-env --root "$QA_CONVEX_ENV_ROOT" \
#       --name STRIPE_WEBHOOK_SECRET --from-env STRIPE_WEBHOOK_SECRET
#     python3 -I "$QA_CONVEX_ENV_HELPER" set-env --root "$QA_CONVEX_ENV_ROOT" \
#       --name SUBSCRIPTION_UI_PREVIEW_SUBJECTS --from-env SYNTH_E2E_SUBJECT   # set-env ONLY; the leg's synthetic subject
#   (SYNTH_E2E_SUBJECT is exported per leg by remote-qa-spec-runtime.sh and re-exported identically by preflight, whose
#   synthetic JWT carries it as `sub`; it is never read from the --env-file.)
#   (value from the process env, never argv; same allowlist; ownership = saved receipt + scoped CONVEX_ADMIN_KEY binding + the
#   controller's box receipt/anchors, checked before the write; prints QA_DEPLOYMENT_ENV_SET names=STRIPE_WEBHOOK_SECRET.)
#   Without --stripe-test-env nothing changes (slot .env.local files already hold Stripe entries; presence is NOT the trigger).
# Never prod, never push: the mutation exists only in the box worktree and is reverse-applied + sha256-verified after RED.
set -euo pipefail
umask 077
# Everything below runs inside ONE brace group: bash parses it whole before executing, so redeploying this file while a lane is running
# cannot shift the read offset and execute a garbled fragment (2026-10-19 #10320: `install --with-deps` ran as coreutils install).
{
rtb="$HOME/.claude/scripts/remote-test-box.sh"
source "$HOME/.claude/scripts/remote-qa-ownership.sh"   # qa_emit_ownership_receipt (shared with capture mode)
llm_onmiss='' stripe_test_env='' pre_write_check='' run_id="qa-spec-$(date -u +%Y%m%dT%H%M%SZ)-$$"
modal_helper="${QA_MODAL_HELPER:-$HOME/.claude/scripts/remote-qa-modal.py}"
runtime="${QA_SPEC_RUNTIME:-$HOME/.claude/scripts/remote-qa-spec-runtime.sh}"
fetch_helper="$HOME/.claude/scripts/remote-qa-spec-fetch.py"
slot='' branch='' head='' out='' env_file='' config='' provider=auto ttl=150m allow_seed='' spec='' patch='' test_line='' project='' with_deps=no legs_override='' timeout_secs='' prod_build=no trace=no dry_run=no
# Pool / disposable Convex reuse the capture lane's implementations (remote-qa-capture.sh): RTB_QA_POOL fenced lease,
# remote-qa-convex.py create/destroy, push-file/run/fetch through the lease generation, quarantine on unresolved teardown.
pool_lane='' pool_priority='' disposable_convex='' managed_dir='' pool_box='' auto_pool=
convex_helper="$HOME/.claude/scripts/remote-qa-convex.py"
while [ $# -gt 0 ]; do
  case "$1" in
    --pool) pool_lane=1; shift;; --priority) pool_priority="$2"; shift 2;; --disposable-convex) disposable_convex=1; shift;;
    --slot) slot="$2"; shift 2;; --branch) branch="$2"; shift 2;; --head) head="$2"; shift 2;;
    --out) out="$2"; shift 2;; --env-file) env_file="$2"; shift 2;; --config) config="$2"; shift 2;;
    --provider) provider="$2"; shift 2;; --ttl) ttl="$2"; shift 2;; --allow-seed-replace) allow_seed=1; shift;;
    --requires-modal) shift;; # This spec lane always deploys disposable Modal with strict replay.
    --spec) spec="$2"; shift 2;; --mutate-patch) patch="$2"; shift 2;; --test-line) test_line="$2"; shift 2;; --project) project="$2"; shift 2;; --with-deps) with_deps=yes; shift;; --legs) legs_override="$2"; shift 2;; --timeout) timeout_secs="$2"; shift 2;;
    --prod-build) prod_build=yes; shift;; # forwarded to local-repro-preflight.sh --prod-build (next build + next start, matches CI)
    --trace) trace=yes; shift;; # box-local playwright.config trace:'on'; trace.zip + video land in <out>/leg*/test-results
    --dry-run) dry_run=yes; shift;;
    --pre-write-check) pre_write_check="${2:?--pre-write-check needs a command}"; shift 2;;
    --on-miss) llm_onmiss=--on-miss; shift;; # opt-in capture-on-miss (see header); never inferred
    --stripe-test-env) stripe_test_env=--stripe-test-env; shift;; # opt-in Stripe TEST env on the disposable deployment (see header)
    *) echo "Unknown spec argument: $1" >&2; exit 2;;
  esac
done
[[ "$slot" =~ ^[1-8]$ && "$head" =~ ^[0-9a-f]{40}$ ]] && [ -n "$branch" ] && [ -n "$out" ] && [ -n "$spec" ] || {
  echo 'spec --slot N --branch B --head SHA40 --spec tests/e2e/specs/...spec.ts --out DIR --allow-seed-replace [--mutate-patch F] [--test-line N] [--project P] [--with-deps] [--legs GREEN,RED,GREEN] [--env-file F] [--config JSON] [--ttl 150m] [--timeout SECS] [--prod-build] [--trace] [--dry-run] [--disposable-convex [--pre-write-check CMD] [--on-miss] [--stripe-test-env]]' >&2; exit 2; }
[[ "$spec" =~ ^tests/e2e/(specs|qa-tests)/[A-Za-z0-9_./-]+\.spec\.ts$ && "$spec" != *..* ]] || { echo '--spec must be a repo-relative tests/e2e/specs/**.spec.ts or tests/e2e/qa-tests/**.spec.ts path' >&2; exit 2; }
case "$spec" in tests/e2e/qa-tests/*|tests/e2e/specs/qa-tests/*) project=qa-tests;; esac
if [ -n "$pool_lane" ]; then
  # Same bounds as the capture pool lane (CTO C0ALZJHGE49 ts 1793272875.383349): disposable dev Convex only, strict replay.
  [ "$disposable_convex" = 1 ] || { echo '--pool requires --disposable-convex (no slot/shared dev backend on pool boxes)' >&2; exit 2; }
  case "$pool_priority" in ""|p0|p1|p2|p3|hotfix) ;; *) echo '--priority must be p0|p1|p2|p3|hotfix' >&2; exit 2;; esac
else
  [ -z "$pool_priority" ] || { echo '--priority requires --pool' >&2; exit 2; }
fi
[ -z "$pre_write_check" ] || [ "$disposable_convex" = 1 ] || { echo '--pre-write-check requires --disposable-convex (it verifies the ownership receipt)' >&2; exit 2; }
[ -z "$stripe_test_env" ] || [ "$disposable_convex" = 1 ] || { echo '--stripe-test-env requires --disposable-convex (never a slot/shared dev backend)' >&2; exit 2; }
if [ -n "$llm_onmiss" ]; then
  # CTO bounds: synthetic data only (throwaway dev Convex seeded from the synthetic preview seed), GREEN-only (RED-on-revert stays strict).
  [ "$disposable_convex" = 1 ] || { echo '--on-miss requires --disposable-convex (synthetic seed in a throwaway dev Convex only)' >&2; exit 2; }
  [ -z "$patch$legs_override" ] || { echo '--on-miss is GREEN-only: RED/mutation proof stays strict replay (no --mutate-patch/--legs)' >&2; exit 2; }
fi
[ "$allow_seed" = 1 ] || { echo 'Canonical preflight replaces seed tables: --allow-seed-replace requires ownership of the selected dev deployment.' >&2; exit 2; }
legs=GREEN
if [ -n "$patch" ]; then
  [ -r "$patch" ] || { echo 'Mutation patch unreadable' >&2; exit 2; }
  # The mutation may only touch E2E test files; never product code, workflows or fixtures.
  bad=$(grep -E '^(\+\+\+|---) ' "$patch" | grep -vE '^(\+\+\+ b|--- a)/tests/e2e/[A-Za-z0-9_./-]+$|^--- /dev/null$|^\+\+\+ /dev/null$' || true)
  grep -qE '^diff --git ' "$patch" && [ -z "$bad" ] || { echo 'Mutation patch must be a git diff touching only tests/e2e/**' >&2; exit 2; }
  legs=GREEN,RED,GREEN
fi
if [ -n "$legs_override" ]; then
  [[ "$legs_override" =~ ^(GREEN|RED)(,(GREEN|RED))*$ ]] || { echo '--legs must be like GREEN,RED,GREEN' >&2; exit 2; }
  [[ "$legs_override" != *RED* || -n "$patch" ]] || { echo 'a RED leg needs --mutate-patch' >&2; exit 2; }
  legs="$legs_override"
fi
case "$ttl" in *m) ttl_seconds=$(( ${ttl%m} * 60 ));; *h) ttl_seconds=$(( ${ttl%h} * 3600 ));; *s) ttl_seconds=${ttl%s};; *) ttl_seconds=$ttl;; esac
[[ "$ttl_seconds" =~ ^[1-9][0-9]*$ ]] || { echo '--ttl must look like 150m, 3h or 9000s' >&2; exit 2; }
n_legs=$(printf '%s' "$legs" | tr ',' '\n' | wc -l | tr -d ' '); n_legs=$((n_legs + 1))
if [ -z "$timeout_secs" ]; then
  timeout_secs=$(( 3600 * n_legs )); [ "$timeout_secs" -le $(( ttl_seconds - 600 )) ] || timeout_secs=$(( ttl_seconds - 600 ))
fi
[[ "$timeout_secs" =~ ^[1-9][0-9]*$ && "$timeout_secs" -ge 60 ]] || { echo '--timeout must be an integer >= 60 (seconds)' >&2; exit 2; }
# The relay lease is registered with the box TTL and must outlive the whole lane run (modal logs are read until the end).
[ "$ttl_seconds" -ge 60 ] && [ "$ttl_seconds" -le 10800 ] || { echo '--ttl must be 60s..3h (QA Modal lease limit)' >&2; exit 2; }
[ "$timeout_secs" -le $(( ttl_seconds - 600 )) ] || { echo '--timeout must leave 600s of --ttl for setup/teardown (lease covers the lane only within --ttl)' >&2; exit 2; }
if [ "$dry_run" = yes ]; then echo "DRY_RUN slot=$slot head=$head spec=$spec legs=$legs test_line=${test_line:-} project=${project:-} with_deps=$with_deps prod_build=$prod_build trace=$trace timeout_secs=$timeout_secs pool=${pool_lane:-0} priority=${pool_priority:-} disposable_convex=${disposable_convex:-0} pre_write_check=$([ -n "$pre_write_check" ] && echo yes || echo no) stripe_test_env=$([ -n "$stripe_test_env" ] && echo yes || echo no) llm_mode=$([ -n "$llm_onmiss" ] && echo capture-on-miss || echo strict)"; exit 0; fi
# Report unexpected failures without exposing command arguments or environment values.
trap 'rc=$?; printf "QA_SPEC_ERROR: command failed at line %s (rc=%s)\n" "$LINENO" "$rc" >&2' ERR
# Shared slot Convex may still use production storage; never start a QA box against it.
[ "$disposable_convex" = 1 ] || { echo 'QA_R2_REFUSED: --disposable-convex required for isolated storage' >&2; exit 2; }
env_file="${env_file:-$HOME/Downloads/projects/heydonna-app-300$slot/.env.local}"
[ -r "$env_file" ] || { echo 'Env file unreadable' >&2; exit 2; }
val() { grep -E "^$1=" "$env_file" | head -1 | cut -d= -f2- | tr -d "\"'" | awk '{print $1}'; }
disposable_cleanup() {
  [ -n "$managed_dir" ] || return 0
  python3 -I "$convex_helper" destroy --root "$managed_dir"
}
if [ "$disposable_convex" = 1 ]; then
  [ -r "$convex_helper" ] || { echo 'Disposable Convex helper missing' >&2; exit 2; }
  pem=$(val SYNTH_JWT_PRIVATE_KEY_PATH || true)
  [ -r "$pem" ] || { echo 'Readable synthetic private key required (SYNTH_JWT_PRIVATE_KEY_PATH missing or unreadable)' >&2; exit 2; }
  mkdir -p "$HOME/.heydonna/qa-convex-leases"
  managed_dir=$(mktemp -d "$HOME/.heydonna/qa-convex-leases/spec-XXXXXXXX")
  # Replaced by cleanup() below once the output dir exists; this covers a failure in between.
  trap 'rc=$?; trap - EXIT; disposable_cleanup || rc=1; exit "$rc"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM HUP
  python3 -I "$convex_helper" create --root "$managed_dir" --base-env "$env_file" $stripe_test_env
  env_file="$managed_dir/dev.env"
fi
dep=$(val CONVEX_DEPLOYMENT)
expiry=$(val QA_CONVEX_DEPLOYMENT_EXPIRES_AT_MS || true)
python3 -I - "$dep" "$expiry" <<'EXPIRY'
import sys, time
name, value = sys.argv[1:]
if value:
    if not value.isascii() or not value.isdigit(): print('QA_DEPLOYMENT_EXPIRY_INVALID', file=sys.stderr); sys.exit(4)
    if int(value) <= int(time.time() * 1000): print(f'QA_DEPLOYMENT_EXPIRED deployment={name}', file=sys.stderr); sys.exit(4)
EXPIRY
key_prefix=$(val CONVEX_ADMIN_KEY); key_prefix="${key_prefix%%|*}"
case "$dep" in dev:beloved-cardinal-*|dev:loyal-lynx-*|dev:prod-*|dev:) echo 'Unsafe deployment' >&2; exit 2;; dev:*) ;; *) echo 'Dev deployment required' >&2; exit 2;; esac
[ "$key_prefix" = "$dep" ] || { echo 'Missing/mismatched dev admin credential' >&2; exit 2; }
unset key_prefix
[ -z "$(val CONVEX_DEPLOY_KEY || true)" ] || { echo 'CONVEX_DEPLOY_KEY must not be in env file (preflight A.5)' >&2; exit 2; }
pem=$(val SYNTH_JWT_PRIVATE_KEY_PATH)
[ -r "$pem" ] || { echo 'Readable provisioned synthetic private key required' >&2; exit 2; }
[ -z "$config" ] || [ -r "$config" ] || { echo 'Config JSON unreadable' >&2; exit 2; }
[ -r "$modal_helper" ] && [ -r "$runtime" ] && [ -r "$fetch_helper" ] || { echo 'QA Modal helper / spec runtime / fetch helper missing' >&2; exit 2; }
python3 -I "$modal_helper" check-credentials   # strict replay needs the box's own disposable Modal proxy
mkdir -p "$out"; out=$(cd "$out" && pwd)
printf 'deployment=%s\nspec=%s\nhead=%s\nlegs=%s\ntimeout_secs=%s\nllm_mode_requested=%s\nstripe_test_env=%s\n' "$dep" "$spec" "$head" "$legs" "$timeout_secs" "$([ -n "$llm_onmiss" ] && echo capture-on-miss || echo strict)" "$([ -n "$stripe_test_env" ] && echo yes || echo no)" >"$out/lane.txt"
[ -z "$patch" ] || { cp "$patch" "$out/mutate.patch"; }
shasum -a 256 "$0" "$runtime" "$modal_helper" "$fetch_helper" "$HOME/.claude/scripts/remote-qa-ownership.sh" "$HOME/.claude/scripts/remote-qa-config.cjs" "$HOME/.claude/scripts/remote-qa-ui-capture.patch" ${patch:+"$patch"} >"$out/tooling.sha256"
[ -z "$disposable_convex" ] || shasum -a 256 "$convex_helper" >>"$out/tooling.sha256"
box='' ip='' known='' remote_ready='' cleanup_running='' modal_registered='' deployment_lease=''
ssho=()
# Transport (same as remote-qa-capture.sh): pool leases go through the generation fence (rtb push-file / run), ephemeral boxes via pinned ssh/scp.
qa_put() {  # LOCAL REMOTE_ABS
  if [ -n "$pool_lane" ]; then "$rtb" push-file "$box" "$1" "$2"; else scp -q "${ssho[@]}" "$1" "root@$ip:$2"; fi
}
qa_rsh() {  # CMD (bash -c); stdin forwarded
  if [ -n "$pool_lane" ]; then "$rtb" run "$box" -- bash -c "$1"; else ssh "${ssho[@]}" "root@$ip" "$1"; fi
}
# onmiss_label OUT RC -> sets ONMISS_{LABEL,VERDICT,REASON,RC,CAPTURES,BOUND}. Pure over <out>/llm-onmiss.txt + <out>/out/llm-ledger.json
# (the controller's capture-report). A run with any live capture is ON_MISS_CAPTURED, never a strict-replay pass; a typed
# QA_ONMISS_* terminal or a missing/unreadable report fails closed. (ONMISS_LABEL_BEGIN/END delimit it for the stub proof.)
# ONMISS_LABEL_BEGIN
onmiss_label() {
  local out="$1"
  ONMISS_RC="$2"; ONMISS_REASON=$(sed -n 's/^QA_ONMISS_TERMINAL reason=//p' "$out/llm-onmiss.txt" 2>/dev/null | head -1)
  ONMISS_CAPTURES=$(python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1])); assert r.get("mode")=="capture-on-miss"; assert r.get("canonical_fixture_keys")==[] and r.get("fixtures_written")==[]; print(len(r.get("attempts") or {}))' "$out/out/llm-ledger.json" 2>/dev/null || echo unknown)
  ONMISS_BOUND=$(python3 -I -c 'import json,sys; print(round(json.load(open(sys.argv[1])).get("charged_upper_bound_usd",0),6))' "$out/out/llm-ledger.json" 2>/dev/null || echo unknown)
  if [ -n "$ONMISS_REASON" ]; then ONMISS_LABEL=ON_MISS_TERMINAL; ONMISS_RC=1
  elif [ "$ONMISS_CAPTURES" = unknown ]; then ONMISS_LABEL=ON_MISS_UNVERIFIED; ONMISS_REASON=QA_ONMISS_REPORT_MISSING; ONMISS_RC=1
  elif [ "$ONMISS_CAPTURES" -gt 0 ]; then ONMISS_LABEL=ON_MISS_CAPTURED
  else ONMISS_LABEL=ON_MISS_REPLAY_ONLY; fi
  if [ "$ONMISS_RC" != 0 ]; then ONMISS_VERDICT=lane-fail
  elif [ "$ONMISS_LABEL" = ON_MISS_CAPTURED ]; then ONMISS_VERDICT=ON_MISS_CAPTURED
  else ONMISS_VERDICT=lane-ok-onmiss-replay-only; fi
}
# ONMISS_LABEL_END
media_status=UNKNOWN
# QA_SPEC_FETCH_BEGIN
# Every remote operation uses the existing lease fence or the pinned ephemeral SSH identity.
# Only these cleanup commands get artifact deadlines; the Playwright run budget is unchanged.
qa_spec_fetch_remote() {
  local limit="$1"; shift
  if [ -n "$pool_lane" ]; then
    timeout --kill-after=5 "$limit" "$rtb" run "$box" -- "$@"
  else
    local command; printf -v command '%q ' "$@"
    timeout --kill-after=5 "$limit" ssh "${ssho[@]}" "root@$ip" "$command"
  fi
}
qa_spec_fetch() {
  local root helper remote_out media_dir
  [[ "$run_id" =~ ^qa-spec-[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || { echo 'QA_FETCH_UNAVAILABLE: invalid run id'; return 1; }
  command -v timeout >/dev/null 2>&1 || { echo 'QA_FETCH_UNAVAILABLE: timeout command missing'; return 1; }
  root="/root/qa/.spec-fetch-$run_id"
  helper="/root/qa/.spec-fetch-$run_id.py"
  media_dir="$out/.spec-media-$run_id"
  if [ -n "$pool_lane" ]; then
    timeout --kill-after=5 300 "$rtb" push-file "$box" "$fetch_helper" "$helper" || { echo 'QA_FETCH_UNAVAILABLE: helper upload failed'; return 1; }
  else
    timeout --kill-after=5 300 scp -q "${ssho[@]}" "$fetch_helper" "root@$ip:$helper" || { echo 'QA_FETCH_UNAVAILABLE: helper upload failed'; return 1; }
  fi
  remote_out="$root/small/out"
  qa_spec_fetch_remote 300 timeout 290 python3 -I "$helper" prepare /root/qa/out "$remote_out" small || { echo 'QA_FETCH_UNAVAILABLE: small snapshot failed'; return 1; }
  RTB_FETCH_TIMEOUT_SECONDS=300 timeout --kill-after=5 330 "$HOME/.claude/scripts/remote-browser-box.sh" fetch "$box" "$remote_out" "$out" || { echo 'QA_FETCH_UNAVAILABLE: small transfer failed'; return 1; }
  echo 'QA_FETCH_SMALL=VERIFIED'
  media_status=PARTIAL
  remote_out="$root/media/out"
  if ! qa_spec_fetch_remote 300 timeout 290 python3 -I "$helper" prepare /root/qa/out "$remote_out" media; then
    echo 'QA_FETCH_MEDIA=PARTIAL reason=media-snapshot-unavailable'; return 3
  fi
  mkdir -p "$media_dir" || { echo 'QA_FETCH_MEDIA=PARTIAL reason=media-directory-unavailable'; return 3; }
  if ! RTB_FETCH_TIMEOUT_SECONDS=900 timeout --kill-after=5 930 "$HOME/.claude/scripts/remote-browser-box.sh" fetch "$box" "$remote_out" "$media_dir"; then
    echo 'QA_FETCH_MEDIA=PARTIAL reason=media-transfer-unavailable'; return 3
  fi
  python3 -I "$fetch_helper" publish "$media_dir/out" "$out/out" || { echo 'QA_FETCH_MEDIA=PARTIAL reason=media-publication-unavailable'; return 3; }
  media_status=COMPLETE
  return 0
}
# QA_SPEC_FETCH_END
cleanup() {
  local rc=$? down_rc=0
  [ -z "$cleanup_running" ] || return
  cleanup_running=1; trap - EXIT; set +e
  if [ -n "$box" ]; then
    if [ "$remote_ready" = 1 ]; then
      "$rtb" run-file "$box" "$runtime" "$slot" "$head" "$spec" cleanup "$legs" "$test_line" "$project" "$with_deps" "$prod_build" "$trace" '' '' '' strict "$run_id" >"$out/final-cleanup.log" 2>&1 || { rc=1; echo 'CONFIG_RESTORE_FAILED: manual dev-config reconciliation required (see final-cleanup.log)' >&2; }
      if [ -n "$llm_onmiss" ] && [ "$modal_registered" = 1 ]; then
        # SAME controller report as the capture lane: lease-scoped view of the durable spend ledgers; on-miss never publishes.
        qa_rsh "set -e; source /root/qa/modal.env; export REMOTE_BOX=1; python3 -I /root/qa/modal-lane.py capture-report --box '$box' --root '$remote' --output /root/qa/out/llm-ledger.json $llm_onmiss" >"$out/llm-report.log" 2>&1 || rc=1
        grep -E '^QA_ONMISS_(REPORT|TERMINAL)' "$out/llm-report.log" >"$out/llm-onmiss.txt" || true
      fi
      local fetch_rc=0
      qa_spec_fetch >>"$out/fetch.log" 2>&1 || fetch_rc=$?
      # Preserve the Playwright lane.rc and any earlier failure; media-only incompleteness is rc 3.
      if [ "$fetch_rc" != 0 ] && [ "$rc" = 0 ]; then rc=$fetch_rc; fi
      [ ! -f "$out/out/legs.tsv" ] || cat "$out/out/legs.tsv"
    fi
    if [ "$modal_registered" = 1 ]; then
      if python3 -I "$modal_helper" stop --box "$box" >"$out/modal-cleanup.log" 2>&1; then python3 -I "$modal_helper" finish --box "$box" || rc=1
      else rc=1; echo 'QA_MODAL_CLEANUP=retry-pending (expiry reaper retains ownership receipt)' >&2; fi
    fi
    if [ -n "$pool_lane" ]; then
      # Owned remote resources first; the pool lease is released only after they are verified gone (capture-lane contract).
      remote_ok=1
      if [ "$modal_registered" = 1 ]; then
        python3 -I -c 'import json,sys;r=json.load(open(sys.argv[1]));sys.exit(0 if r.get("state")=="stopped" else 1)' \
          "$HOME/.heydonna/qa-modal-leases/$box.json" 2>/dev/null || remote_ok=0
      fi
      if [ -n "$managed_dir" ]; then
        disposable_cleanup >"$out/convex-cleanup.log" 2>&1 || rc=1
        if [ -f "$managed_dir/deletion.json" ]; then cp "$managed_dir/deletion.json" "$out/convex-deletion.json" || rc=1; else remote_ok=0; fi
        managed_dir=''
      fi
      printf 'modal_stopped=%s convex_deleted=%s\n' "$([ "$modal_registered" != 1 ] && echo n/a || { [ $remote_ok = 1 ] && echo yes || echo check; })" \
        "$([ -f "$out/convex-deletion.json" ] && echo yes || echo no)" | tee "$out/remote-teardown.txt"
      if [ "$remote_ok" = 1 ]; then
        "$rtb" down "$box" >"$out/down.log" 2>&1; down_rc=$?
        [ "$down_rc" = 0 ] || rc=1
        grep -m1 '^POOL_RESET_WORK_STATUS ' "$out/down.log" >"$out/final-work-status.txt" || :
        echo "POOL_LEASE_RELEASED=$([ "$down_rc" = 0 ] && echo yes || echo no) box=$box pool_box=$pool_box" | tee "$out/teardown.txt"
        [ ! -s "$out/final-work-status.txt" ] || cat "$out/final-work-status.txt" | tee -a "$out/teardown.txt"
      else
        rc=1
        "$rtb" pool-quarantine "$box" "qa spec teardown unresolved (head $head)" >"$out/down.log" 2>&1
        echo "POOL_LEASE_RELEASED=no QUARANTINED box=$box pool_box=$pool_box (remote Modal/Convex teardown unresolved; receipts retained)" | tee "$out/teardown.txt"
      fi
    else
      "$rtb" down "$box" >"$out/down.log" 2>&1; down_rc=$?
      [ "$down_rc" = 0 ] || rc=1
      grep -m1 '^POOL_RESET_WORK_STATUS ' "$out/down.log" >"$out/final-work-status.txt" || :
      echo "BOX_DESTROYED=$([ "$down_rc" = 0 ] && echo yes || echo no)" | tee "$out/teardown.txt"
      [ ! -s "$out/final-work-status.txt" ] || cat "$out/final-work-status.txt" | tee -a "$out/teardown.txt"
    fi
  fi
  if [ -n "$deployment_lease" ]; then
    /Users/rajiv/.local/bin/trash "$deployment_lease" || rc=1
  fi
  if [ -n "$managed_dir" ]; then
    disposable_cleanup >"$out/convex-cleanup.log" 2>&1 || rc=1
    if [ -f "$managed_dir/deletion.json" ]; then cp "$managed_dir/deletion.json" "$out/convex-deletion.json" || rc=1; else rc=1; echo 'QA_CONVEX_DELETE_UNVERIFIED (see convex-cleanup.log)' >&2; fi
  fi
  local verdict reason=''
  verdict=$([ "$rc" = 0 ] && echo lane-ok || echo lane-fail)
  if [ -n "$llm_onmiss" ]; then
    onmiss_label "$out" "$rc"; rc=$ONMISS_RC; verdict=$ONMISS_VERDICT; reason=$ONMISS_REASON
    echo "LLM_MODE=capture-on-miss label=$ONMISS_LABEL live_captures=$ONMISS_CAPTURES charged_upper_bound_usd=$ONMISS_BOUND${reason:+ reason=$reason} payloads=box-local canonical_writes=0" | tee -a "$out/lane.txt"
  fi
  if [ "$media_status" = PARTIAL ]; then
    [ "$rc" != 0 ] || rc=3
    verdict=lane-partial
  fi
  printf 'media_status=%s\nterminal_rc=%s\n' "$media_status" "$rc" >>"$out/lane.txt"
  echo "$rc" >"$out/terminal.rc"
  find "$out" -type f ! -name SHA256SUMS -exec shasum -a 256 {} \; >"$out/SHA256SUMS" 2>/dev/null
  echo "REMOTE_SPEC verdict=$verdict${reason:+ reason=$reason} media_status=$media_status terminal_rc=$rc playwright_rc=${lane_rc:-UNKNOWN} llm_mode=$([ -n "$llm_onmiss" ] && echo capture-on-miss || echo strict) slot=$slot head=$head spec=$spec legs=$legs artifacts=$out"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
# Same deployment mutex as capture: seed replacement/config must have one owner.
[[ "${dep#dev:}" =~ ^[a-z0-9-]+$ ]] || { echo 'Invalid deployment lease identity'; exit 2; }
lease_root="$HOME/.heydonna/qa-deployment-leases"
mkdir -p "$lease_root"
candidate="$lease_root/${dep#dev:}"
if ! mkdir "$candidate" 2>/dev/null; then
  echo "QA_DEPLOYMENT_BUSY deployment=$dep lease=$candidate (wait for owner teardown)" >&2
  exit 4
fi
deployment_lease="$candidate"
printf 'pid=%s\nhead=%s\nout=%s\n' "$$" "$head" "$out" >"$deployment_lease/owner.txt"
echo "QA_DEPLOYMENT_LEASE_ACQUIRED deployment=$dep" | tee "$out/deployment-lease.log"
# Browser warm pool FIRST (Rajiv C0ALZJHGE49 thread 1793299570.404429, ts 1793362672.793199 "browser testing will need
# warm pool. hetzner keeps running out of it"): a run that meets the CTO pool-lane contract (ts 1793272875.383349)
# leases a cx43 browser-pool box; ephemeral cx43 only when no pool box is free. QA_NO_POOL=1 opts out.
if [ -z "$pool_lane" ] && [ "${QA_NO_POOL:-}" != 1 ] && [ "$disposable_convex" = 1 ]; then
  pool_lane=1; auto_pool=1; echo "QA_POOL_FIRST: browser warm pool, ephemeral cx43 only if no pool box is free" >&2
fi
if [ -n "$pool_lane" ]; then
  up_env=(RTB_QA_POOL=1 RTB_QA_HEAD="$head"); up_extra=(${pool_priority:+--priority "$pool_priority"} --wait "${QA_POOL_WAIT_S:-1800}")
  [ -z "${auto_pool:-}" ] || up_extra+=(--allow-ephemeral)
  [ "$provider" != auto ] || provider=hetzner
else
  up_env=(); up_extra=()
fi
qa_spec_owner="qa-spec-s${slot:-x}-${head:0:8}-$$"
env ${up_env[@]+"${up_env[@]}"} "$rtb" up --owner "$qa_spec_owner" --branch "$branch" --provider "$provider" --ttl "$ttl" ${up_extra[@]+"${up_extra[@]}"} >"$out/up.log" 2>&1 || { cat "$out/up.log" >&2; exit 1; }
box=$(sed -n 's/^BOX=//p' "$out/up.log")
# Pool-first fell back to an ephemeral cx43 box (no pool box free): from here on it is an ordinary ephemeral run.
if [ -n "${auto_pool:-}" ] && [[ ! "$box" =~ ^rtbl- ]]; then pool_lane=''; echo "QA_POOL_FIRST: pool full, ephemeral cx43 box $box" >&2; fi
[[ "$box" =~ ^rtbv?-[a-z0-9-]+-[0-9]{6}$ || ( -n "$pool_lane" && "$box" =~ ^rtbl-[0-9a-f]{16}-[0-9]{6}$ ) ]] || { echo 'No valid box receipt' >&2; exit 1; }
pool_box=$(sed -n 's/^POOL_BOX=//p' "$out/up.log")
if [ -n "$pool_lane" ]; then
  got_head=$(sed -n 's/^HEAD=//p' "$out/up.log")
  [ "$got_head" = "$head" ] || { echo "Box head $got_head is not the exact head $head" >&2; exit 1; }
fi
printf 'run_id=%s\nhead=%s\nbox=%s\npool_box=%s\nlease_gen=%s\nllm_mode=%s\n' "$run_id" "$head" "$box" "${pool_box:-}" "$(sed -n 's/^LEASE_GEN=//p' "$out/up.log")" "$([ -n "$llm_onmiss" ] && echo capture-on-miss || echo strict)" >"$out/run-identity.txt"
python3 -I "$modal_helper" register --box "$box" --ttl "$ttl_seconds" $llm_onmiss >"$out/modal-ownership.log"
modal_registered=1
ip=$(sed -n 's/^IP=//p' "$out/up.log")
known="$HOME/.cache/rtb-known-hosts/${pool_box:-$box}"
[ -s "$known" ] && [ ! -L "$known" ] || { echo 'Missing host-key pin; refusing SSH' >&2; exit 1; }
ssho=(-o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known"
  -o GlobalKnownHostsFile=/dev/null -o HostKeyAlgorithms=ssh-ed25519 -o UpdateHostKeys=no -o ConnectTimeout=10 -i "$HOME/.ssh/hetzner-runner-warm")
remote="/work/heydonna-app-300$slot"
if [ -n "$pool_lane" ]; then
  # Pool contract: /work/heydonna-app stays the real repo dir (reset verifies it); the slot path is a symlink. pool_prepare already
  # checked out the exact head and ran deps. The runtime enters the slot path logically so preflight's slot-clone guard sees it.
  "$rtb" run "$box" -- bash -c "set -e; git cat-file -e '$head^{commit}'; test \"\$(git rev-parse HEAD)\" = '$head'; cd /work; ln -sfn heydonna-app heydonna-app-300$slot; mkdir -p /root/qa/out; chmod 700 /root/qa" >"$out/checkout.log" 2>&1
else
ssh "${ssho[@]}" "root@$ip" "set -e; cd /work/heydonna-app; git cat-file -e '$head^{commit}'; git merge-base --is-ancestor '$head' HEAD; git checkout -q --detach '$head'; [ \"\$(git rev-parse HEAD)\" = '$head' ]; cd /work; mv heydonna-app heydonna-app-300$slot; ln -s heydonna-app-300$slot heydonna-app; mkdir -p /root/qa/out; chmod 700 /root/qa" >"$out/checkout.log" 2>&1
# The box was provisioned at the branch tip; the exact head may carry a different lockfile.
"$rtb" run "$box" -- bun install --frozen-lockfile >"$out/deps.log" 2>&1
fi
qa_put "$env_file" "$remote/.env.local.next"
qa_put "$pem" /root/qa/synth-private.pem
qa_put "$HOME/.claude/scripts/remote-qa-config.cjs" /root/qa/config.cjs
[ -z "$patch" ] || qa_put "$patch" /root/qa/mutate.patch
if [ -n "$config" ]; then qa_put "$config" /root/qa/config.json; else qa_rsh "printf '{}' > /root/qa/config.json"; fi
qa_rsh "set -e; chmod 600 '$remote/.env.local.next' /root/qa/synth-private.pem; sed -i 's#^SYNTH_JWT_PRIVATE_KEY_PATH=.*#SYNTH_JWT_PRIVATE_KEY_PATH=/root/qa/synth-private.pem#' '$remote/.env.local.next'; sed -i '/^MODAL_API_KEY=/d; /^MODAL_KEY=/d; /^MODAL_SECRET=/d' '$remote/.env.local.next'; bash -n '$remote/.env.local.next'; mv '$remote/.env.local.next' '$remote/.env.local'; printf '%s\\t%s\\t%s\\n' \"\$(sha256sum <'$remote/.env.local' | cut -c1-64)\" \"\$(readlink -f '$remote/.env.local')\" \"\$(cat /var/lib/rtb/gen 2>/dev/null || echo -)\" >>/root/qa/.rtb-env-copies.tsv; chmod 600 /root/qa/.rtb-env-copies.tsv"
# Ownership receipt (same function/contract as capture mode) BEFORE any Convex write: Modal prepare, runtime config apply,
# preflight seed import and the spec's own setup all come later. The Modal lease was registered above, so v2 binds it.
qa_rsh "printf '%s' '$run_id' > /root/qa/run-id"
if [ -n "$managed_dir" ]; then
  qa_emit_ownership_receipt "$managed_dir" "$run_id" "$box" "$head" "$dep" "$HOME/.heydonna/qa-modal-leases/$box.json" "$out" \
    || { echo 'QA_CONVEX_OWNERSHIP_RECEIPT_FAILED' >&2; exit 1; }
  # Independent identity anchors from the CONTROLLER's own state (never read back from the receipt or any box file). They are
  # passed as arguments to the pre-write-check AND the spec runtime (which exports them readonly to preflight -> Playwright ->
  # spawned seed helpers), so any helper can cross-check receipt fields + on-box file hash right before its own writes.
  receipt_sha=$(shasum -a 256 "$out/convex-ownership.json" | cut -d' ' -f1)
  [[ "$run_id" =~ ^[A-Za-z0-9._-]+$ && "$box" =~ ^[A-Za-z0-9-]+$ && "$receipt_sha" =~ ^[0-9a-f]{64}$ ]] || { echo 'PRE_WRITE_ANCHORS_INVALID' >&2; exit 1; }
  echo "QA_PREWRITE_ANCHORS run=$run_id head=$head box=$box receipt_sha256=$receipt_sha" | tee -a "$out/lane.txt"
  if [ -n "$stripe_test_env" ]; then
    # Box-side allowlisted env setter (see header): helper + non-secret ownership copy, read-only; the runtime exports their paths.
    qa_rsh "set -e; mkdir -p /root/qa/convex-root; chmod 700 /root/qa/convex-root"
    qa_put "$convex_helper" /root/qa/convex-lane.py
    qa_put "$managed_dir/ownership.json" /root/qa/convex-root/ownership.json
    qa_rsh "chmod 444 /root/qa/convex-lane.py /root/qa/convex-root/ownership.json"
    echo "QA_CONVEX_ENV_SETTER pushed helper=/root/qa/convex-lane.py root=/root/qa/convex-root" | tee -a "$out/lane.txt"
  fi
fi
# The Modal overlay's anchors assume the UI-capture preflight patch (same prerequisite as the capture lane); it is inert for --spec runs.
qa_put "$HOME/.claude/scripts/remote-qa-ui-capture.patch" /root/qa/ui-only.patch
qa_rsh "set -e; cd '$remote'; if ! grep -q '^UI_CAPTURE_ONLY=' scripts/e2e/local-repro-preflight.sh; then cp scripts/e2e/local-repro-preflight.sh scripts/e2e/local-repro-preflight.sh.next; git apply --check /root/qa/ui-only.patch; git apply /root/qa/ui-only.patch; bash -n scripts/e2e/local-repro-preflight.sh.next; mv scripts/e2e/local-repro-preflight.sh.next scripts/e2e/local-repro-preflight.sh; fi" >"$out/tooling-overlay.log" 2>&1
qa_put "$modal_helper" /root/qa/modal-lane.py
python3 -I "$modal_helper" credentials --box "$box" | qa_rsh 'umask 077; cat > /root/qa/modal.env'
remote_ready=1
if [ -n "$pre_write_check" ]; then
  # Slot-owned verifier, after the receipt and before ANY Convex write. Nonzero aborts the run (finish 1); cleanup fetches
  # /root/qa/out (incl. pre-write-check.log) and destroys the box.
  # Drop only registrations for missing worktrees before the check creates its baseline.
  "$rtb" run "$box" -- git worktree prune >"$out/worktree-prune.log" 2>&1 || { echo 'WORKTREE_PRUNE_FAILED' >&2; exit 1; }
  pwc_b64=$(printf '%s' "$pre_write_check" | base64 | tr -d '\n')
  printf '%s\n' "$pre_write_check" >"$out/pre-write-check.cmd"
  pwc_rc=0
  "$rtb" run "$box" -- bash -c "cd '$remote' && printf '%s' '$pwc_b64' | base64 -d >/root/qa/pre-write-check.sh && set -a && source .env.local && set +a && unset CONVEX_DEPLOY_KEY && export REMOTE_BOX=1 && declare -rx QA_CONVEX_OWNERSHIP_PATH=/root/qa/convex-ownership.json QA_RUN_ID_PATH=/root/qa/run-id QA_CONTROLLER_RUN_ID='$run_id' QA_EXPECTED_HEAD='$head' QA_BOX_ID='$box' QA_RECEIPT_SHA256='$receipt_sha' && bash /root/qa/pre-write-check.sh >/root/qa/out/pre-write-check.log 2>&1; r=\$?; cat /root/qa/out/pre-write-check.log; exit \$r" >"$out/pre-write-check.log" 2>&1 || pwc_rc=$?
  if [ "$pwc_rc" != 0 ]; then
    echo "PRE_WRITE_CHECK_FAILED rc=$pwc_rc (no Convex write happened; aborting; see pre-write-check.log)" | tee -a "$out/lane.txt" >&2
    echo 1 >"$out/lane.rc"; exit 1
  fi
  echo "PRE_WRITE_CHECK_OK" | tee -a "$out/lane.txt"
fi
qa_rsh "set -e; source /root/qa/modal.env; export REMOTE_BOX=1; python3 -I /root/qa/modal-lane.py overlay --root '$remote' --box '$box' $llm_onmiss; python3 -I /root/qa/modal-lane.py prepare --box '$box' --root '$remote' --config /root/qa/config.json $llm_onmiss" >"$out/modal-prepare.log" 2>&1
if [ -n "$llm_onmiss" ]; then
  # The controller binds lease + exact head + request on the box; if it cannot (e.g. unverified pool hostname) it bakes the
  # strict OFF source and says QA_ONMISS_DISABLED: the run is then plain strict replay and is labelled so.
  if grep -q '^QA_ONMISS_DISABLED' "$out/modal-prepare.log"; then
    grep -m1 '^QA_ONMISS_DISABLED' "$out/modal-prepare.log" | tee -a "$out/lane.txt"; llm_onmiss=''
    echo "llm_mode=strict (on-miss requested, controller disabled it)" | tee -a "$out/lane.txt"
  else
    grep -m1 '^QA_ONMISS_IDENTITY' "$out/modal-prepare.log" | tee -a "$out/lane.txt" || { echo 'QA_ONMISS_IDENTITY missing; refusing an unbound on-miss run' | tee -a "$out/lane.txt" >&2; exit 1; }
  fi
fi
"$rtb" run "$box" -- npx playwright install --with-deps chromium >"$out/browser-install.log" 2>&1
# The overlay edits tooling files only (preflight/deploy/processor secret names); baseline is taken AFTER it so the RED leg's
# restoration check covers tests/** exactly.
# The runtime's exit code IS the lane verdict (GREEN legs must exit 0; RED legs must fail in Playwright). Record it explicitly instead of
# relying on set -e through run-file/ssh, so cleanup() always sees the real code and a wrapper exit 0 can never mask a failed leg.
lane_rc=0
# Outer watchdog (python3, own process group so the ssh child dies too). On expiry: rc 124, best-effort kill of the box-side preflight/playwright so the
# runtime's own trap + the cleanup pass restore config; cleanup() then fetches artifacts and destroys the box as usual.
python3 -I - "$timeout_secs" "$rtb" run-file "$box" "$runtime" "$slot" "$head" "$spec" run "$legs" "$test_line" "$project" "$with_deps" "$prod_build" "$trace" "${managed_dir:+$run_id}" "${managed_dir:+$box}" "${receipt_sha:-}" "$([ -n "$llm_onmiss" ] && echo onmiss || echo strict)" "$run_id" >"$out/lane.log" 2>&1 <<'WATCHDOG' || lane_rc=$?
import os, signal, subprocess, sys
limit, cmd = int(sys.argv[1]), sys.argv[2:]
proc = subprocess.Popen(cmd, start_new_session=True)
def forward(signum, _frame):
    try: os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    sys.exit(128 + signum)
for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT): signal.signal(sig, forward)
try:
    sys.exit(proc.wait(timeout=limit))
except subprocess.TimeoutExpired:
    print(f"LANE_TIMEOUT after {limit}s (outer harness watchdog; Playwright/CI budgets untouched)", flush=True)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try: os.killpg(proc.pid, sig)
        except ProcessLookupError: break
        try: proc.wait(timeout=10); break
        except subprocess.TimeoutExpired: pass
    sys.exit(124)
WATCHDOG
if [ "$lane_rc" = 124 ]; then
  echo "LANE_TIMEOUT timeout_secs=$timeout_secs" >>"$out/lane.txt"
  qa_rsh "pkill -TERM -f 'scripts/e2e/local-repro-[p]reflight.sh' || true; pkill -TERM -f '[p]laywright test' || true; sleep 5" >>"$out/lane.log" 2>&1 || true
fi
echo "$lane_rc" >"$out/lane.rc"
[ "$lane_rc" = 0 ] || echo "REMOTE_SPEC_LEG_FAILURE runtime_rc=$lane_rc (see lane.log / out/legs.tsv)" >&2
exit "$lane_rc"
}
