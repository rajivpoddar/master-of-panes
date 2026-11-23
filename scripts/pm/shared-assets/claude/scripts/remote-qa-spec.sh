#!/usr/bin/env bash
# Remote single-spec E2E repro lane: ephemeral box at an EXACT head -> canonical local-repro-preflight --spec, strict replay,
# synthetic-JWT auth (rule 41, no live Clerk), LLM proxy on, the box's disposable Modal (-qa-<id>) + the slot's own dev Convex.
# GREEN, or GREEN -> RED (--mutate-patch applied for the RED leg only) -> GREEN on the SAME box. Always destroys the box.
# --env-file F (optional): base env for the mandatory --disposable-convex. Default = slot 5's dev env
# ($HOME/Downloads/projects/heydonna-app-3005/.env.local) whatever --slot is; --slot never selects the env file.
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
#   --extra-secret SUPPORTING_DOCUMENT_RECEIPT_SECRET=generate (OPT-IN; needs --requires-modal --disposable-convex; same contract as
#   the capture lane): the box generates one 64-hex dev-only secret, sets it on the disposable dev Convex via the ownership-verified
#   helper, then writes it to the leased per-box Modal secret. Any other name or value is refused before any write; the value is
#   never supplied by the caller, printed or logged.
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
#   --on-miss-exact MANIFEST --canary-receipt FILE (OPT-IN, M15; CTO GO C0ALZJHGE49 thread 1794677380.515009 ts 1795301055.769519,
#   Option A custody ts 1795301727.470839): SCOPED exact-hash capture, REPLACING stock --on-miss (never both, never a default).
#   MANIFEST = {"version":1,"head":"<40hex = --head>","approval_ref":"...","hashes":[{"hash":"<64hex>","provider","model","operation",
#   "canonical_key"}]} (1-4 full hashes, no prefixes). Only those exact request hashes may dispatch live (checked after an AUTHORITATIVE
#   replay miss and before pricing or any reservation; a lookup error or 403 is never a miss); every other miss is a typed refusal
#   (QA_ONMISS_NOT_APPROVED, with its full hash) and zero provider I/O. Spend: stock $3 box-lifetime + per-attempt ledgers, both CAS
#   reservations before the single send, zero retries, no reclaim of an earlier lease's completed claim, UNKNOWN stays consumed at its
#   bound. Storage: the proxy's fixture/staging/ledger clients and the report container get ONLY prefix-scoped 5-minute sessions minted
#   on this host (re-minted every 100 s during the leg; the report session is minted after the leg), no shared secret and no fallback;
#   the app-data runtimes keep their existing binding. The existing guarded publisher runs unchanged (PUT-if-absent), then each canonical
#   object is read back (bytes, status, content type) and the dispatch id + LangSmith ids are joined by ingress request id. The
#   run-local spec writes /root/qa/out/langsmith-trace.json {request_id, run_id, trace_id, project, transcript_id}. --canary-receipt = the
#   passed scoped-report-canary receipt of THIS deployed controller (<=48 h old). Label: ON_MISS_CAPTURED only with readback_match>=1 and
#   join COMPLETE; else lane-fail reason=QA_ONMISS_*.
#   CTO REVISE 1795306033.832689 (pel-mwr051ua-91fb8062): (1) the trusted host DERIVES every scoped path (lease, ledger objects, staging
#   prefix) from its own box receipt, the physical-box identity (POOL_BOX, or the box itself) and --head before minting; the box-written
#   ids file is only checked against it (DENIED on any difference). Exact mode also requires an EXPLICIT scoped app-data selection
#   (QA_R2_SCOPED=1 = Run 9 scoped heydonna-qa; QA_R2_SCOPED=0/unset = CI-parity heydonna-uploads with the CI key, Rajiv option A C0ALZJHGE49
#   ts 1795311474.240329, recorded in lane.txt/the report; the mount replacement is then skipped), --grant-receipt (the Run 9 object-only grant,
#   which backs the prefix-scoped sessions in BOTH modes), and on the launch path: the box env readback (scoped: bucket heydonna-qa,
#   signer 9b8c9f309b38da1f), every shared R2 mount replaced by a lease-only QA secret, and the approved product pin (manifest
#   product_pin; the launch head may differ from it only by the run-local spec). (2) Before any session exists the trusted host reconciles
#   the LEGACY (heydonna-fixtures) and QA ledgers for the physical box AND the exact head (prior claims for the approved hash, halts, UNKNOWN
#   exposure, combined allowance >= manifest max_reserve_usd) and binds the result into the QA ledgers (carry rows + custody marker); the
#   proxy refuses every reservation without it and any priced bound above max_reserve_usd. (3) The join verifies the native LangSmith
#   project, transcript and aligned draft the approval pinned. --exact-dry-check bakes an authoritative-miss dry-check: the proxy proves
#   the miss on the scoped path and never prices, claims, reserves or dispatches (custody reconcile is then read-only).
#   PROFILES (CTO GO C0ALZJHGE49 thread 1795302375.270589, ts 1795310865.915209): every manifest names one explicit profile; a missing or
#   unknown profile is refused. m15-langsmith keeps the mandatory verified LangSmith join. pr14414-headerless-capture is for ONE pinned spec
#   (blob-pinned and verified on the box), head 6edba2fa... and approved hash e5c629a1... only; its completion is exactly one settled
#   request with ingress + provider response identity and a verified PUT-if-absent readback. It certifies the fixture capture only and is
#   never that spec's AC1 PASS (AC1 needs a full strict replay on the private template). There is no switch that disables provenance.
#   M15 SPEC INPUTS (exact mode; CTO-brief add-on, coordinator 2026-11-22): the controller records the app-data binding it selected and the
#   readback verified, and exports it to the spec env as QA_APP_DATA=<scoped|ci-parity> and QA_APP_DATA_BUCKET=<heydonna-qa|heydonna-uploads>
#   (derived from the selected mode, never echoed back from R2_BUCKET_NAME; the box readback must report the same bucket or the run aborts).
#   --m15-draft FILE --m15-observer FILE (m15-langsmith profile only, both together): the aligned ASR draft must hash to the manifest's pinned
#   langsmith.draft_sha256, the observer script is recorded by digest; both are checked on this host before any lease/box/spend, pushed to the
#   fixed box paths /root/qa/m15/draft.txt (mode 444) and /root/qa/m15/observe.sh (mode 555, bash -n), re-verified by digest on the box,
#   and exported as QA_M15_DRAFT_PATH, QA_M15_OBSERVER_PATH and QA_M15_OBSERVER_LOG=/root/qa/m15/modal.raw (the log the observer writes).
#   The spec env travels in .env.local (preflight sources it into Playwright); the lines are written to the .env.local.next copy BEFORE the
#   install, so the byte-hash pool reset verifies already covers them. The launcher stages files only: it does not run the observer.
# Never prod, never push: the mutation exists only in the box worktree and is reverse-applied + sha256-verified after RED.
set -euo pipefail
umask 077
# Everything below runs inside ONE brace group: bash parses it whole before executing, so redeploying this file while a lane is running
# cannot shift the read offset and execute a garbled fragment (2026-10-19 #10320: `install --with-deps` ran as coreutils install).
{
rtb="$HOME/.claude/scripts/remote-test-box.sh"
source "$HOME/.claude/scripts/remote-qa-ownership.sh"   # qa_emit_ownership_receipt (shared with capture mode)
llm_onmiss='' stripe_test_env='' pre_write_check='' extra_secrets=() extra_args_remote='' extra_box_env='' requires_modal='' run_id="qa-spec-$(date -u +%Y%m%dT%H%M%SZ)-$$"
exact_manifest='' canary_receipt='' exact_digest='' arm_pid='' exact_args='' exact_overlay_args='' onmiss_stock=''
grant_receipt='' exact_dry='' product_pin='' exact_spend_box='' exact_profile='' spec_blob='' app_data=''
app_data_bucket='' m15_draft='' m15_observer='' m15_draft_sha='' m15_observer_sha=''
strict_manifest='' strict_digest='' strict_args='' strict_pid='' strict_preflight_line=''
# Label of the requested LLM mode (follows llm_onmiss, which the controller clears if it disables on-miss).
llm_label() { if [ -n "$strict_manifest" ]; then echo strict-replay; elif [ -z "$llm_onmiss" ]; then echo strict; elif [ -n "$exact_manifest" ]; then echo capture-on-miss-exact; else echo capture-on-miss; fi; }
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
    --requires-modal) requires_modal=1; shift;; # This spec lane always deploys disposable Modal with strict replay; --extra-secret needs this flag.
    --spec) spec="$2"; shift 2;; --mutate-patch) patch="$2"; shift 2;; --test-line) test_line="$2"; shift 2;; --project) project="$2"; shift 2;; --with-deps) with_deps=yes; shift;; --legs) legs_override="$2"; shift 2;; --timeout) timeout_secs="$2"; shift 2;;
    --prod-build) prod_build=yes; shift;; # forwarded to local-repro-preflight.sh --prod-build (next build + next start, matches CI)
    --trace) trace=yes; shift;; # box-local playwright.config trace:'on'; trace.zip + video land in <out>/leg*/test-results
    --dry-run) dry_run=yes; shift;;
    --pre-write-check) pre_write_check="${2:?--pre-write-check needs a command}"; shift 2;;
    --on-miss) llm_onmiss=--on-miss; onmiss_stock=1; shift;; # opt-in capture-on-miss (see header); never inferred
    --on-miss-exact) exact_manifest="${2:?--on-miss-exact needs an approval manifest}"; llm_onmiss=--on-miss; shift 2;; # M15 scoped exact-hash capture (see header)
    --canary-receipt) canary_receipt="${2:?--canary-receipt needs a receipt file}"; shift 2;; # passed scoped publish/readback canary (see header)
    --grant-receipt) grant_receipt="${2:?--grant-receipt needs the Run 9 parent grant receipt}"; shift 2;; # verified object-only grant (see header)
    --m15-draft) m15_draft="${2:?--m15-draft needs the aligned ASR draft file}"; shift 2;; # M15 spec inputs staged by the controller (see header)
    --m15-observer) m15_observer="${2:?--m15-observer needs the observer script}"; shift 2;;
    --strict-replay) strict_manifest="${2:?--strict-replay needs the strict-replay manifest}"; shift 2;; # opt-in fail-closed M15 strict replay (2 pinned fixtures, forwarding off)
    --exact-dry-check) exact_dry=1; shift;; # authoritative-miss dry-check: the proxy never prices, claims, reserves or dispatches (see header)
    --stripe-test-env) stripe_test_env=--stripe-test-env; shift;; # opt-in Stripe TEST env on the disposable deployment (see header)
    --extra-secret) extra_secrets+=("${2:?--extra-secret needs NAME=generate}"); shift 2;; # opt-in box-generated dev-only secret (see header)
    *) echo "Unknown spec argument: $1" >&2; exit 2;;
  esac
done
[[ "$slot" =~ ^[1-8]$ && "$head" =~ ^[0-9a-f]{40}$ ]] && [ -n "$branch" ] && [ -n "$out" ] && [ -n "$spec" ] || {
  echo 'spec --slot N --branch B --head SHA40 --spec tests/e2e/specs/...spec.ts --out DIR --allow-seed-replace [--mutate-patch F] [--test-line N] [--project P] [--with-deps] [--legs GREEN,RED,GREEN] [--env-file F] [--config JSON] [--ttl 150m] [--timeout SECS] [--prod-build] [--trace] [--dry-run] [--disposable-convex [--pre-write-check CMD] [--on-miss] [--stripe-test-env]] [--requires-modal --extra-secret SUPPORTING_DOCUMENT_RECEIPT_SECRET=generate]' >&2; exit 2; }
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
# The M15 spec cannot attach an ASR draft without the receipt signer/verifier pair. Use the same
# box-generated secret path as an explicit opt-in, so an omitted flag cannot produce a false QA failure.
if [ "$spec" = tests/e2e/specs/qa-tests/m15-asr-draft-langsmith.spec.ts ] && [ ${#extra_secrets[@]} -eq 0 ]; then
  extra_secrets+=(SUPPORTING_DOCUMENT_RECEIPT_SECRET=generate)
fi
# --extra-secret fails closed BEFORE any credential, lease, box or Convex deployment exists. Refusal text never echoes the spec
# (a pasted secret would otherwise land in the log). modal-lane.py prepare re-validates the same allowlist on the box.
extra_args_remote=''
if [ ${#extra_secrets[@]} -gt 0 ]; then
  [ "$requires_modal" = 1 ] && [ "$disposable_convex" = 1 ] || {
    echo 'QA_EXTRA_SECRET_REFUSED: --extra-secret requires --requires-modal and --disposable-convex (disposable dev Convex + leased per-box Modal app only)' >&2; exit 2; }
  for _es in "${extra_secrets[@]}"; do
    [ "$_es" = SUPPORTING_DOCUMENT_RECEIPT_SECRET=generate ] || {
      echo 'QA_EXTRA_SECRET_REFUSED: only SUPPORTING_DOCUMENT_RECEIPT_SECRET=generate is accepted (value is generated on the box, never supplied)' >&2; exit 2; }
    extra_args_remote="$extra_args_remote --extra-secret $_es"
  done
  unset _es
  extra_args_remote="$extra_args_remote --convex-helper /root/qa/convex-lane.py --convex-root /root/qa/convex-root"
fi
if [ -n "$llm_onmiss" ]; then
  # CTO bounds: synthetic data only (throwaway dev Convex seeded from the synthetic preview seed), GREEN-only (RED-on-revert stays strict).
  [ "$disposable_convex" = 1 ] || { echo '--on-miss requires --disposable-convex (synthetic seed in a throwaway dev Convex only)' >&2; exit 2; }
  [ -z "$patch$legs_override" ] || { echo '--on-miss is GREEN-only: RED/mutation proof stays strict replay (no --mutate-patch/--legs)' >&2; exit 2; }
fi
if [ -n "$strict_manifest" ]; then
  # Opt-in strict replay (CTO GO C0ALZJHGE49 thread 1794677380.515009 ts 1795401768.237669): replay ONLY the manifest's two canonical
  # objects read from heydonna-qa through a controller-minted GetObject-only session, provider forwarding off, app data CI-parity
  # (heydonna-uploads). Never combined with capture/on-miss; every refusal names its predicate and exits before any lease or spend.
  sr() { echo "QA_STRICT_REPLAY_REFUSED: $1" >&2; exit 2; }
  [ -z "$llm_onmiss$onmiss_stock" ] || sr 'predicate=on_miss (strict replay never captures)'
  [ -z "$exact_manifest" ] || sr 'predicate=on_miss_exact (strict replay never captures)'
  [ -z "$canary_receipt" ] || sr 'predicate=canary_receipt (capture-only input)'
  [ -z "$grant_receipt" ] || sr 'predicate=grant_receipt (capture-only input)'
  [ -z "$exact_dry" ] || sr 'predicate=exact_dry_check (capture-only input)'
  [ "$disposable_convex" = 1 ] || sr 'predicate=disposable_convex_required'
  [ "$requires_modal" = 1 ] || sr 'predicate=requires_modal_required'
  [ -z "$patch$legs_override" ] || sr 'predicate=mutate_or_legs (strict replay is GREEN-only over the two pinned fixtures)'
  [ "${QA_R2_SCOPED:-0}" != 1 ] || sr 'predicate=app_data_scoped (app data stays CI-parity heydonna-uploads)'
  [ -r "$strict_manifest" ] && [ ! -L "$strict_manifest" ] || sr 'predicate=manifest_unreadable'
  strict_line=$(python3 -I "$modal_helper" check-strict-manifest --strict-manifest "$strict_manifest" --head "$head") || sr 'predicate=manifest_invalid'
  strict_digest=$(printf '%s' "$strict_line" | sed -n 's/^QA_STRICT_MANIFEST ok digest=\([0-9a-f]\{64\}\) .*/\1/p')
  product_pin=$(printf '%s' "$strict_line" | sed -n 's/.* product_pin=\([0-9a-f]\{40\}\) .*/\1/p')
  [[ "$strict_digest" =~ ^[0-9a-f]{64}$ && "$product_pin" =~ ^[0-9a-f]{40}$ ]] || sr 'predicate=manifest_digest_unparsed'
  if [ -n "$m15_draft$m15_observer" ]; then
    [ -n "$m15_draft" ] && [ -n "$m15_observer" ] || sr 'predicate=m15_inputs_incomplete (--m15-draft and --m15-observer go together)'
    m15_line=$(python3 -I "$modal_helper" check-m15-stage --strict-manifest "$strict_manifest" --head "$head" --draft "$m15_draft" --observer "$m15_observer") || sr 'predicate=m15_stage_digest'
    m15_draft_sha=$(printf '%s' "$m15_line" | sed -n 's/.* draft_sha256=\([0-9a-f]\{64\}\).*/\1/p')
    m15_observer_sha=$(printf '%s' "$m15_line" | sed -n 's/.* observer_sha256=\([0-9a-f]\{64\}\).*/\1/p')
    [[ "$m15_draft_sha" =~ ^[0-9a-f]{64}$ && "$m15_observer_sha" =~ ^[0-9a-f]{64}$ ]] || sr 'predicate=m15_stage_digest_unparsed'
    # Run 11 attempt 2 (CTO ts 1795408974.778089): the observer's six flags need the approved capture lane's setup. Convex flag comes
    # from --config (applied to the disposable deployment by prepare); the Modal readiness signal from the overlay pre-write-check.
    [ -n "$config" ] && python3 -I -c 'import json,sys; sys.exit(json.load(open(sys.argv[1])).get("SUPPORTING_DOCUMENT_UPLOADS_ENABLED")!="true")' "$config" 2>/dev/null \
      || sr 'predicate=m15_convex_flag_config (--config must set SUPPORTING_DOCUMENT_UPLOADS_ENABLED=true)'
    [ -n "$pre_write_check" ] || sr 'predicate=m15_overlay_pre_write_check (--pre-write-check carries the pinned observation overlay)'
    # Pinned observation overlay = the approved Run 11 capture's pre-write-check (<out>/pre-write-check.cmd bytes, CTO GO ts 1795408974.778089).
    m15_overlay_sha=$(printf '%s\n' "$pre_write_check" | shasum -a 256 | cut -c1-64)
    [ "$m15_overlay_sha" = "${QA_M15_OVERLAY_SHA256:-dabd39664f79a2a0a5a9aabfb35580b1009eed451760dd2c5144b498125c45ae}" ] \
      || sr "predicate=m15_overlay_digest (overlay sha256=$m15_overlay_sha is not the pinned observation overlay)"
  fi
  strict_args=' --strict-manifest /root/qa/strict-manifest.json'
  app_data=ci-parity app_data_bucket=heydonna-uploads
fi
if [ -n "$exact_manifest" ]; then
  # Scoped exact-hash mode: never default, never combined with stock --on-miss, never without the canary receipt. The manifest is
  # validated by the SAME helper code the box and the proxy use (full 64-hex hashes only, exact head, canonical key shape).
  [ -z "$onmiss_stock" ] || { echo '--on-miss-exact replaces --on-miss; do not pass both (stock on-miss is not scoped)' >&2; exit 2; }
  [ -r "$exact_manifest" ] && [ ! -L "$exact_manifest" ] || { echo '--on-miss-exact manifest unreadable' >&2; exit 2; }
  [ -n "$canary_receipt" ] || { echo '--on-miss-exact requires --canary-receipt (passed scoped publish/readback canary of the deployed controller)' >&2; exit 2; }
  [ -n "$grant_receipt" ] || { echo '--on-miss-exact requires --grant-receipt (the verified Run 9 object-only grant on heydonna-qa)' >&2; exit 2; }
  exact_line=$(python3 -I "$modal_helper" check-exact-manifest --exact-manifest "$exact_manifest" --head "$head" --spec "$spec") || { echo 'QA_EXACT_MANIFEST_REFUSED' >&2; exit 2; }
  exact_profile=$(printf '%s' "$exact_line" | tr ' ' '\n' | sed -n 's/^profile=//p')
  [[ "$exact_profile" =~ ^[a-z0-9-]+$ ]] || { echo 'QA_EXACT_MANIFEST_REFUSED (profile)' >&2; exit 2; }
  spec_blob=$(printf '%s' "$exact_line" | tr ' ' '\n' | sed -n 's/^spec_blob=//p')
  [[ "$spec_blob" =~ ^([0-9a-f]{40}|none)$ ]] || { echo 'QA_EXACT_MANIFEST_REFUSED (spec blob)' >&2; exit 2; }
  exact_digest=$(printf '%s' "$exact_line" | sed -n 's/^QA_EXACT_MANIFEST ok digest=\([0-9a-f]\{64\}\) .*/\1/p')
  [[ "$exact_digest" =~ ^[0-9a-f]{64}$ ]] || { echo 'QA_EXACT_MANIFEST_REFUSED (digest)' >&2; exit 2; }
  product_pin=$(printf '%s' "$exact_line" | sed -n 's/.* product_pin=\([0-9a-f]\{40\}\) .*/\1/p')
  [[ "$product_pin" =~ ^[0-9a-f]{40}$ ]] || { echo 'QA_EXACT_MANIFEST_REFUSED (product pin)' >&2; exit 2; }
  exact_args=' --exact-manifest /root/qa/exact-manifest.json'
  exact_overlay_args="$exact_args --ids-out /root/qa/onmiss-ids.json${exact_dry:+ --dry-check}"
  if [ -n "$m15_draft$m15_observer" ]; then
    # M15 inputs staged by the CONTROLLER at fixed box paths (never a box-side prewrite): the aligned ASR draft must be the exact bytes the
    # approved manifest pins (langsmith.draft_sha256); the observer script is the operator's file, recorded by digest. Checked here, before
    # any lease, box or spend, and re-checked by digest on the box after the push.
    [ "$exact_profile" = m15-langsmith ] || { echo '--m15-draft/--m15-observer are only valid with the m15-langsmith profile' >&2; exit 2; }
    [ -n "$m15_draft" ] && [ -n "$m15_observer" ] || { echo '--m15-draft and --m15-observer go together' >&2; exit 2; }
    m15_line=$(python3 -I "$modal_helper" check-m15-stage --exact-manifest "$exact_manifest" --head "$head" --draft "$m15_draft" --observer "$m15_observer") || { echo 'QA_M15_STAGE_REFUSED' >&2; exit 2; }
    m15_draft_sha=$(printf '%s' "$m15_line" | sed -n 's/.* draft_sha256=\([0-9a-f]\{64\}\) .*/\1/p')
    m15_observer_sha=$(printf '%s' "$m15_line" | sed -n 's/.* observer_sha256=\([0-9a-f]\{64\}\) .*/\1/p')
    [[ "$m15_draft_sha" =~ ^[0-9a-f]{64}$ && "$m15_observer_sha" =~ ^[0-9a-f]{64}$ ]] || { echo 'QA_M15_STAGE_REFUSED (digests)' >&2; exit 2; }
  fi
else
  [ -z "$canary_receipt$grant_receipt$exact_dry" ] && { [ -n "$strict_manifest" ] || [ -z "$m15_draft$m15_observer" ]; } || { echo '--canary-receipt, --grant-receipt, --exact-dry-check are only valid with --on-miss-exact; --m15-draft/--m15-observer need --on-miss-exact or --strict-replay' >&2; exit 2; }
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
if [ "$dry_run" = yes ]; then echo "DRY_RUN slot=$slot head=$head spec=$spec legs=$legs test_line=${test_line:-} project=${project:-} with_deps=$with_deps prod_build=$prod_build trace=$trace timeout_secs=$timeout_secs pool=${pool_lane:-0} priority=${pool_priority:-} disposable_convex=${disposable_convex:-0} pre_write_check=$([ -n "$pre_write_check" ] && echo yes || echo no) stripe_test_env=$([ -n "$stripe_test_env" ] && echo yes || echo no) llm_mode=$(llm_label)${strict_manifest:+ strict_digest=$strict_digest strict_fixtures=3 strict_bucket=heydonna-qa forwarding=off product_pin=$product_pin app_data=$app_data app_data_bucket=$app_data_bucket m15_staged=$([ -n "$m15_draft" ] && echo yes || echo no)}${m15_overlay_sha:+ m15_overlay_sha256=$m15_overlay_sha m15_convex_flag=config m15_next_flags=spec_env m15_observer_gate=before_browser}"; exit 0; fi
# Report unexpected failures without exposing command arguments or environment values.
trap 'rc=$?; printf "QA_SPEC_ERROR: command failed at line %s (rc=%s)\n" "$LINENO" "$rc" >&2' ERR
# Shared slot Convex may still use production storage; never start a QA box against it.
[ "$disposable_convex" = 1 ] || { echo 'QA_R2_REFUSED: --disposable-convex required for isolated storage' >&2; exit 2; }
if [ -n "$strict_manifest" ]; then
  # Trusted-host readback BEFORE any Convex, lease or box: both canonical objects through a 60 s GetObject-only session on exactly
  # those keys plus one known-absent key (sha256 + size must match; the absent key must be NoSuchKey). Denial = stop, exact reason.
  strict_preflight_line=$("${QA_UV:-$HOME/.local/bin/uv}" run --no-project --python 3.13 --with boto3==1.35.99 python -I "$modal_helper" strict-preflight --strict-manifest "$strict_manifest" --head "$head" 2>&1) \
    || { printf '%s\n' "$strict_preflight_line" | grep -E '^(QA_STRICT_|ValueError|RuntimeError)' | tail -3 >&2; echo 'QA_STRICT_PREFLIGHT_FAILED (Run 11 stays pending)' >&2; exit 2; }
  strict_preflight_line=$(printf '%s\n' "$strict_preflight_line" | grep '^QA_STRICT_PREFLIGHT ok ' | tail -1)
  [ -n "$strict_preflight_line" ] || { echo 'QA_STRICT_PREFLIGHT_FAILED predicate=no_receipt' >&2; exit 2; }
  echo "$strict_preflight_line" >&2
fi
# Default BASE env when --env-file is omitted: slot 5's dev env (CTO OK C0ALZJHGE49 ts 1795034415.355669; slot 2 is retired).
# Deliberately NOT derived from --slot: --slot stays the run's assigned slot (box checkout name), the env file is only the
# --disposable-convex base template. An explicit --env-file overrides. An unreadable default keeps the existing refusal (no fallback
# to another slot's file); remote-qa-convex.py create still refuses a production base env before any request.
env_file="${env_file:-$HOME/Downloads/projects/heydonna-app-3005/.env.local}"
[ -r "$env_file" ] || { echo 'Env file unreadable' >&2; exit 2; }
val() { grep -E "^$1=" "$env_file" | head -1 | cut -d= -f2- | tr -d "\"'" | awk '{print $1}'; }
if [ -n "$exact_manifest" ]; then
  # App-data binding, explicit and recorded on every run. remote-qa-r2.py selects the Run 9 scoped heydonna-qa storage ONLY when QA_R2_SCOPED=1;
  # any other value (0 or unset) is the CI-parity base env: heydonna-uploads with the CI key, like CI. Rajiv chose that (option A, C0ALZJHGE49
  # thread 1794677380.515009 ts 1795311474.240329: "outcome takes precdence"). Either way the fixture, staging, ledger and report clients stay
  # on prefix-scoped sessions; only the application's own data runtimes follow the selected binding.
  if [ "${QA_R2_SCOPED:-$(val QA_R2_SCOPED || true)}" = 1 ]; then app_data=scoped app_data_bucket=heydonna-qa; else app_data=ci-parity app_data_bucket=heydonna-uploads; fi
  exact_args="$exact_args --app-data $app_data"; exact_overlay_args="$exact_overlay_args --app-data $app_data"
  echo "QA_APP_DATA mode=$app_data" >&2
fi
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
if [ -n "$exact_manifest" ]; then
  # Arming prerequisites, before any lease, box or spend: a passed publish/readback canary of THIS controller file, and a working
  # trusted-host session mint (a missing mint would otherwise surface only as a proxy that waits and fails closed).
  python3 -I "$modal_helper" check-grant-receipt --receipt "$grant_receipt" || { echo 'QA_GRANT_RECEIPT_REFUSED' >&2; exit 2; }
  python3 -I "$modal_helper" check-canary-receipt --receipt "$canary_receipt" || { echo 'QA_SCOPED_CANARY_REQUIRED: run scoped-report-canary on the deployed controller first' >&2; exit 2; }
  python3 -I "$modal_helper" mint-session --kind check || { echo 'QA_SCOPED_MINT_UNAVAILABLE' >&2; exit 2; }
fi
mkdir -p "$out"; out=$(cd "$out" && pwd)
printf 'deployment=%s\nspec=%s\nhead=%s\nlegs=%s\ntimeout_secs=%s\nllm_mode_requested=%s\nstripe_test_env=%s\n' "$dep" "$spec" "$head" "$legs" "$timeout_secs" "$(llm_label)" "$([ -n "$stripe_test_env" ] && echo yes || echo no)" >"$out/lane.txt"
[ -z "$patch" ] || { cp "$patch" "$out/mutate.patch"; }
[ -z "$strict_manifest" ] || { printf '%s\n' "${strict_line:-}" "$strict_preflight_line" ${m15_line:+"$m15_line"} >>"$out/lane.txt"; cp "$strict_manifest" "$out/strict-manifest.json"; }
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
qa_ssh_retry_used=0
# One bounded retry of a refused SSH/scp (sshd not yet stable after cloud-init). Not a loop; budget is per run.
# Same helper as remote-qa-capture.sh. --extra-secret setup calls it; without this definition bash exits 127
# (qa_net: command not found) before Playwright. Does not change what qa_put/qa_rsh send.
qa_net() {
  local rc
  "$@" 2>"$out/ssh-retry.err" && rc=0 || rc=$?
  if [ "$rc" = 255 ] && [ "$qa_ssh_retry_used" = 0 ] && grep -q 'Connection refused' "$out/ssh-retry.err"; then
    qa_ssh_retry_used=1
    echo "QA_SSH_RETRY reason=connection_refused backoff_seconds=30 command=$1" | tee -a "$out/retries.log" >&2
    sleep 30
    "$@" 2>"$out/ssh-retry.err" && rc=0 || rc=$?
  fi
  [ ! -s "$out/ssh-retry.err" ] || cat "$out/ssh-retry.err" >&2
  return "$rc"
}
# onmiss_label OUT RC -> sets ONMISS_{LABEL,VERDICT,REASON,RC,CAPTURES,BOUND}. Pure over <out>/llm-onmiss.txt + <out>/out/llm-ledger.json
# (the controller's capture-report). A run with any live capture is ON_MISS_CAPTURED, never a strict-replay pass; a typed
# QA_ONMISS_* terminal or a missing/unreadable report fails closed. (ONMISS_LABEL_BEGIN/END delimit it for the stub proof.)
# ONMISS_LABEL_BEGIN
onmiss_label() {
  local out="$1"
  ONMISS_RC="$2"; ONMISS_REASON=$(sed -n 's/^QA_ONMISS_TERMINAL reason=//p' "$out/llm-onmiss.txt" 2>/dev/null | head -1)
  ONMISS_CAPTURES=$(python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1])); assert r.get("mode")=="capture-on-miss"; print(len(r.get("attempts") or {}))' "$out/out/llm-ledger.json" 2>/dev/null || echo unknown)
  # S161: valid on-miss replies are published canonical PUT-if-absent; report how many keys were written.
  ONMISS_PUBLISHED=$(python3 -I -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("fixtures_written") or []))' "$out/out/llm-ledger.json" 2>/dev/null || echo unknown)
  ONMISS_BOUND=$(python3 -I -c 'import json,sys; print(round(json.load(open(sys.argv[1])).get("charged_upper_bound_usd",0),6))' "$out/out/llm-ledger.json" 2>/dev/null || echo unknown)
  ONMISS_JOIN=$(sed -n 's/^QA_ONMISS_JOIN_RESULT status=\([A-Z_]*\).*/\1/p' "$out/llm-join.txt" 2>/dev/null | head -1)
  ONMISS_MATCH=$(python3 -I -c 'import json,sys; print(int((json.load(open(sys.argv[1])).get("exact") or {}).get("readback_match", 0)))' "$out/out/llm-ledger.json" 2>/dev/null || echo 0)
  if [ -n "$ONMISS_REASON" ]; then ONMISS_LABEL=ON_MISS_TERMINAL; ONMISS_RC=1
  elif [ "$ONMISS_CAPTURES" = unknown ]; then ONMISS_LABEL=ON_MISS_UNVERIFIED; ONMISS_REASON=QA_ONMISS_REPORT_MISSING; ONMISS_RC=1
  elif [ "$ONMISS_CAPTURES" -gt 0 ]; then ONMISS_LABEL=ON_MISS_CAPTURED
  else ONMISS_LABEL=ON_MISS_REPLAY_ONLY; fi
  # Exact mode: a live capture counts only with a matching canonical readback and a complete dispatch/trace join.
  if [ -n "${exact_manifest:-}" ] && [ "$ONMISS_LABEL" = ON_MISS_CAPTURED ]; then
    if [ "$ONMISS_MATCH" -lt 1 ] 2>/dev/null; then ONMISS_LABEL=ON_MISS_TERMINAL; ONMISS_REASON=QA_ONMISS_CAPTURE_UNPROVEN; ONMISS_RC=1
    elif [ "${ONMISS_JOIN:-}" != COMPLETE ]; then ONMISS_LABEL=ON_MISS_TERMINAL; ONMISS_REASON=QA_ONMISS_JOIN_UNPROVEN; ONMISS_RC=1; fi
  fi
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
  if [ -n "$arm_pid" ]; then pkill -P "$arm_pid" 2>/dev/null; kill "$arm_pid" 2>/dev/null; wait "$arm_pid" 2>/dev/null; arm_pid=''; echo 'QA_SCOPED_ARM_STOPPED' >>"$out/lane.txt"; fi
  if [ -n "$box" ]; then
    if [ "$remote_ready" = 1 ]; then
      "$rtb" run-file "$box" "$runtime" "$slot" "$head" "$spec" cleanup "$legs" "$test_line" "$project" "$with_deps" "$prod_build" "$trace" '' '' '' strict "$run_id" >"$out/final-cleanup.log" 2>&1 || { rc=1; echo 'CONFIG_RESTORE_FAILED: manual dev-config reconciliation required (see final-cleanup.log)' >&2; }
      if [ -n "$llm_onmiss" ] && [ "$modal_registered" = 1 ]; then
        # SAME controller report as the capture lane: lease-scoped view of the durable spend ledgers; on-miss never publishes.
        if [ -n "$exact_manifest" ]; then
          # Report container: ONLY a freshly minted prefix-scoped five-minute session, piped to a mode-600 box file that the
          # controller deletes on read (no shared secret mount, no fallback). Minted now, after the leg, never at launcher startup.
          python3 -I "$modal_helper" mint-session --kind report --box "$box" --head "$head" --spend-box "$exact_spend_box" --ids "$out/onmiss-ids.json" --exact-manifest "$exact_manifest" \
            | qa_rsh "umask 077; cat > /root/qa/scoped-report-session.json" >"$out/llm-report-session.log" 2>&1 || { rc=1; echo 'QA_SCOPED_REPORT_SESSION_FAILED' >>"$out/llm-report-session.log"; }
          qa_rsh "set -e; source /root/qa/modal.env; export REMOTE_BOX=1; python3 -I /root/qa/modal-lane.py capture-report --box '$box' --root '$remote' --output /root/qa/out/llm-ledger.json $llm_onmiss$exact_args --session-file /root/qa/scoped-report-session.json" >"$out/llm-report.log" 2>&1 || rc=1
          qa_rsh "rm -f /root/qa/scoped-report-session.json" >/dev/null 2>&1
        else
        qa_rsh "set -e; source /root/qa/modal.env; export REMOTE_BOX=1; python3 -I /root/qa/modal-lane.py capture-report --box '$box' --root '$remote' --output /root/qa/out/llm-ledger.json $llm_onmiss" >"$out/llm-report.log" 2>&1 || rc=1
        fi
        grep -E '^QA_ONMISS_(REPORT|TERMINAL)' "$out/llm-report.log" >"$out/llm-onmiss.txt" || true
      fi
      local fetch_rc=0
      qa_spec_fetch >>"$out/fetch.log" 2>&1 || fetch_rc=$?
      if [ -n "$exact_manifest" ]; then
        # Join, VERIFIED (not merely present): the proxy's ingress request id + provider dispatch id (durable claim) against the run-local
        # spec's native LangSmith evidence (<out>/out/langsmith-trace.json). The helper checks the project (qa-m15-<this run id>), that the
        # LangSmith transcript is the one the spec created, and that the persisted draft and the aligned alternate block equal the digests
        # the approval pinned. Anything missing or different is a named status, never a pass.
        python3 -I "$modal_helper" verify-join --report "$out/out/llm-ledger.json" --trace "$out/out/langsmith-trace.json" --run-id "$run_id" --exact-manifest "$exact_manifest" --output "$out/out/llm-join.json" >"$out/llm-join.txt" 2>&1
        cat "$out/llm-join.txt"
        if [ "$exact_profile" != m15-langsmith ]; then
          # Fixture capture is SEPARATE from the spec's own PASS: this verdict certifies the one approved capture only.
          echo "EXACT_PROFILE name=$exact_profile fixture_capture=$(sed -n 's/^QA_ONMISS_JOIN_RESULT status=\([A-Z_]*\).*/\1/p' "$out/llm-join.txt" | head -1) spec_ac_pass=NOT_ASSESSED (the spec's AC1 needs its own full strict replay on the private template; a fixture capture is never an AC1 PASS)" | tee -a "$out/lane.txt"
        fi
      fi
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
    echo "LLM_MODE=$(llm_label) label=$ONMISS_LABEL live_captures=$ONMISS_CAPTURES charged_upper_bound_usd=$ONMISS_BOUND${exact_manifest:+ exact_manifest_sha256=$exact_digest readback_match=$ONMISS_MATCH join=$ONMISS_JOIN}${reason:+ reason=$reason} payloads=staged+canonical-put-if-absent canonical_writes=$ONMISS_PUBLISHED" | tee -a "$out/lane.txt"
  fi
  if [ "$media_status" = PARTIAL ]; then
    [ "$rc" != 0 ] || rc=3
    verdict=lane-partial
  fi
  printf 'media_status=%s\nterminal_rc=%s\n' "$media_status" "$rc" >>"$out/lane.txt"
  echo "$rc" >"$out/terminal.rc"
  find "$out" -type f ! -name SHA256SUMS -exec shasum -a 256 {} \; >"$out/SHA256SUMS" 2>/dev/null
  echo "REMOTE_SPEC verdict=$verdict${reason:+ reason=$reason} media_status=$media_status terminal_rc=$rc playwright_rc=${lane_rc:-UNKNOWN} llm_mode=$(llm_label) slot=$slot head=$head spec=$spec legs=$legs artifacts=$out"
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
printf 'run_id=%s\nhead=%s\nbox=%s\npool_box=%s\nlease_gen=%s\nllm_mode=%s\n' "$run_id" "$head" "$box" "${pool_box:-}" "$(sed -n 's/^LEASE_GEN=//p' "$out/up.log")" "$(llm_label)" >"$out/run-identity.txt"
python3 -I "$modal_helper" register --box "$box" --ttl "$ttl_seconds" $llm_onmiss ${exact_manifest:+--exact-manifest "$exact_manifest"} >"$out/modal-ownership.log"
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
if [ -n "$exact_manifest$strict_manifest" ]; then
  # Spec env (controller-selected, NOT echoed back from R2_BUCKET_NAME): preflight sources .env.local into Playwright and prepare keeps
  # unknown lines. Written to the .env.local.next copy BEFORE the install below, so the byte-hash pool reset verifies already covers it.
  # The box readback below must report this same bucket for the selected mode, else the run aborts before Playwright.
  spec_env="QA_APP_DATA=$app_data QA_APP_DATA_BUCKET=$app_data_bucket"
  [ -z "$m15_draft" ] || spec_env="$spec_env QA_M15_DRAFT_PATH=/root/qa/m15/draft.txt QA_M15_OBSERVER_PATH=/root/qa/m15/observe.sh QA_M15_OBSERVER_LOG=/root/qa/m15/modal.raw"
  # Strict M15: both Next supporting-document flags land in .env.local BEFORE preflight builds/starts Next (observer nextServer/nextBuild).
  m15_next_check=''
  if [ -n "$strict_manifest" ] && [ -n "$m15_draft" ]; then
    spec_env="$spec_env SUPPORTING_DOCUMENT_UPLOADS_ENABLED=true NEXT_PUBLIC_SUPPORTING_DOCUMENT_UPLOADS=true"
    m15_next_check="; grep -qx 'SUPPORTING_DOCUMENT_UPLOADS_ENABLED=true' \"\$f\"; grep -qx 'NEXT_PUBLIC_SUPPORTING_DOCUMENT_UPLOADS=true' \"\$f\""
  fi
  qa_rsh "set -e; f='$remote/.env.local.next'; sed -i '/^QA_APP_DATA=/d; /^QA_APP_DATA_BUCKET=/d; /^QA_M15_DRAFT_PATH=/d; /^QA_M15_OBSERVER_PATH=/d; /^QA_M15_OBSERVER_LOG=/d${m15_next_check:+; /^SUPPORTING_DOCUMENT_UPLOADS_ENABLED=/d; /^NEXT_PUBLIC_SUPPORTING_DOCUMENT_UPLOADS=/d}' \"\$f\"; [ -z \"\$(tail -c1 \"\$f\")\" ] || echo >>\"\$f\"; printf '%s\\n' $spec_env >>\"\$f\"; grep -qx 'QA_APP_DATA=$app_data' \"\$f\"; grep -qx 'QA_APP_DATA_BUCKET=$app_data_bucket' \"\$f\"$m15_next_check" \
    || { echo 'QA_SPEC_ENV_FAILED: could not record the controller-selected spec env' | tee -a "$out/lane.txt" >&2; exit 1; }
  echo "QA_SPEC_ENV names=$(printf '%s' "$spec_env" | tr ' ' '\n' | cut -d= -f1 | paste -sd, -) app_data=$app_data bucket=$app_data_bucket (controller-selected)" | tee -a "$out/lane.txt"
fi
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
  # --extra-secret: push the allowlisted Convex env setter + the NON-secret ownership copy (read-only), same as spec --stripe-test-env.
  # The box-side setter verifies the hash-anchored controller receipt (QA_* anchors below) and the scoped dev key before ONE write.
  extra_box_env=''
  if [ -n "$extra_args_remote" ]; then
    [ -n "$managed_dir" ] && [ -n "${receipt_sha:-}" ] && [ -r "$managed_dir/ownership.json" ] \
      || { echo 'QA_EXTRA_SECRET_REFUSED: disposable Convex ownership receipt unavailable' >&2; exit 2; }
    qa_net qa_rsh "set -e; mkdir -p /root/qa/convex-root; chmod 700 /root/qa/convex-root"
    qa_net qa_put "$convex_helper" /root/qa/convex-lane.py
    qa_net qa_put "$managed_dir/ownership.json" /root/qa/convex-root/ownership.json
    qa_net qa_rsh "chmod 444 /root/qa/convex-lane.py /root/qa/convex-root/ownership.json"
    # CONVEX_ADMIN_KEY is read from the box env file into the prepare process only: never echoed, never argv, no xtrace.
    extra_box_env="export QA_CONVEX_OWNERSHIP_PATH=/root/qa/convex-ownership.json QA_RECEIPT_SHA256='$receipt_sha' QA_CONTROLLER_RUN_ID='$run_id'; CONVEX_ADMIN_KEY=\$(grep -E '^CONVEX_ADMIN_KEY=' '$remote/.env.local' | head -1 | cut -d= -f2- | tr -d \"\\\"'\" | awk '{print \$1}'); export CONVEX_ADMIN_KEY; "
    echo "QA_EXTRA_SECRET_REQUESTED names=SUPPORTING_DOCUMENT_RECEIPT_SECRET mode=generate (value generated on the box; never printed)" | tee -a "$out/lane.txt"
  fi
fi
# The Modal overlay's anchors assume the UI-capture preflight patch (same prerequisite as the capture lane); it is inert for --spec runs.
qa_put "$HOME/.claude/scripts/remote-qa-ui-capture.patch" /root/qa/ui-only.patch
qa_rsh "set -e; cd '$remote'; if ! grep -q '^UI_CAPTURE_ONLY=' scripts/e2e/local-repro-preflight.sh; then cp scripts/e2e/local-repro-preflight.sh scripts/e2e/local-repro-preflight.sh.next; git apply --check /root/qa/ui-only.patch; git apply /root/qa/ui-only.patch; bash -n scripts/e2e/local-repro-preflight.sh.next; mv scripts/e2e/local-repro-preflight.sh.next scripts/e2e/local-repro-preflight.sh; fi" >"$out/tooling-overlay.log" 2>&1
qa_put "$modal_helper" /root/qa/modal-lane.py
if [ -n "$exact_manifest" ]; then
  qa_put "$exact_manifest" /root/qa/exact-manifest.json; qa_rsh "chmod 444 /root/qa/exact-manifest.json"
  # The approved PRODUCT pin is preserved: the launch head may differ from it only by the run-local spec file.
  qa_rsh "set -e; cd '$remote'; git cat-file -e '$product_pin^{commit}'; git merge-base --is-ancestor '$product_pin' HEAD; [ -z \"\$(git diff --name-only '$product_pin' HEAD | grep -v -x -F '$spec' || true)\" ]" >"$out/product-pin.log" 2>&1 || { echo "QA_PRODUCT_PIN_REFUSED: head $head differs from product pin $product_pin by more than the run-local spec" | tee -a "$out/lane.txt" >&2; exit 1; }
  echo "QA_PRODUCT_PIN ok product_pin=$product_pin head=$head spec_only=true profile=$exact_profile" | tee -a "$out/lane.txt"
  # A profile that pins its spec also pins the spec BLOB: the exact bytes approved, not just a path.
  if [ "$spec_blob" != none ]; then
    qa_rsh "set -e; cd '$remote'; [ \"\$(git rev-parse 'HEAD:$spec')\" = '$spec_blob' ]" >"$out/spec-blob.log" 2>&1 || { echo "QA_SPEC_BLOB_REFUSED: $spec is not the approved blob $spec_blob" | tee -a "$out/lane.txt" >&2; exit 1; }
    echo "QA_SPEC_BLOB ok spec=$spec blob=$spec_blob" | tee -a "$out/lane.txt"
  fi
  # Run 9 effective-storage readback on the exact launch path: the box/Next env must be the verified scoped heydonna-qa identity.
  qa_rsh "python3 -I /root/qa/modal-lane.py check-app-data --env '$remote/.env.local' --app-data '$app_data'" >"$out/app-data-readback.log" 2>&1 || { echo "QA_APP_DATA_UNEXPECTED: box app-data storage does not match the selected mode ($app_data)" | tee -a "$out/lane.txt" >&2; exit 1; }
  echo "QA_APP_DATA mode=$app_data$([ "$app_data" = ci-parity ] && echo ' by Rajiv decision (option A, C0ALZJHGE49 1794677380.515009 ts 1795311474.240329): app data is CI-parity (heydonna-uploads, CI key); fixture/staging/ledger/report clients stay prefix-scoped')" | tee -a "$out/lane.txt"
  cat "$out/app-data-readback.log" | tee -a "$out/lane.txt"
  # The bucket the controller selected for this mode (exported to the spec as QA_APP_DATA_BUCKET) must be what the box readback verified.
  grep -q "^QA_APP_DATA_READBACK mode=$app_data bucket=$app_data_bucket " "$out/app-data-readback.log" || { echo "QA_APP_DATA_UNEXPECTED: the box readback does not report the controller-selected $app_data bucket $app_data_bucket" | tee -a "$out/lane.txt" >&2; exit 1; }
  if [ -n "$m15_draft" ]; then
    # Controller-staged M15 inputs at the fixed paths the spec reads (QA_M15_DRAFT_PATH / QA_M15_OBSERVER_PATH, exported through .env.local above).
    qa_rsh "set -e; umask 077; mkdir -p /root/qa/m15; chmod 700 /root/qa/m15"
    qa_put "$m15_draft" /root/qa/m15/draft.txt
    qa_put "$m15_observer" /root/qa/m15/observe.sh
    qa_rsh "set -e; [ \"\$(sha256sum </root/qa/m15/draft.txt | cut -c1-64)\" = '$m15_draft_sha' ]; [ \"\$(sha256sum </root/qa/m15/observe.sh | cut -c1-64)\" = '$m15_observer_sha' ]; bash -n /root/qa/m15/observe.sh; chmod 444 /root/qa/m15/draft.txt; chmod 555 /root/qa/m15/observe.sh" >"$out/m15-stage.log" 2>&1 \
      || { echo 'QA_M15_STAGE_FAILED: the staged bytes differ from the checked sources or the observer does not parse (see m15-stage.log)' | tee -a "$out/lane.txt" >&2; exit 1; }
    echo "QA_M15_STAGE ok draft=/root/qa/m15/draft.txt draft_sha256=$m15_draft_sha observer=/root/qa/m15/observe.sh observer_sha256=$m15_observer_sha (the launcher stages files only; it does not run the observer)" | tee -a "$out/lane.txt"
  fi
  exact_spend_box="${pool_box:-$box}"
fi
if [ -n "$strict_manifest" ]; then
  qa_put "$strict_manifest" /root/qa/strict-manifest.json; qa_rsh "chmod 444 /root/qa/strict-manifest.json"
  qa_rsh "set -e; cd '$remote'; git cat-file -e '$product_pin^{commit}'; git merge-base --is-ancestor '$product_pin' HEAD; [ -z \"\$(git diff --name-only '$product_pin' HEAD | grep -v -x -F '$spec' || true)\" ]" >"$out/product-pin.log" 2>&1 || { echo "QA_STRICT_REPLAY_STOPPED predicate=product_pin head=$head product_pin=$product_pin" | tee -a "$out/lane.txt" >&2; exit 1; }
  echo "QA_PRODUCT_PIN ok product_pin=$product_pin head=$head spec_only=true mode=strict-replay" | tee -a "$out/lane.txt"
  qa_rsh "python3 -I /root/qa/modal-lane.py check-app-data --env '$remote/.env.local' --app-data ci-parity" >"$out/app-data-readback.log" 2>&1 \
    && grep -q "^QA_APP_DATA_READBACK mode=ci-parity bucket=heydonna-uploads " "$out/app-data-readback.log" \
    || { echo 'QA_STRICT_REPLAY_STOPPED predicate=app_data_not_ci_parity' | tee -a "$out/lane.txt" >&2; exit 1; }
  cat "$out/app-data-readback.log" | tee -a "$out/lane.txt"
  if [ -n "$m15_draft" ]; then
    qa_rsh "set -e; umask 077; mkdir -p /root/qa/m15; chmod 700 /root/qa/m15"
    qa_put "$m15_draft" /root/qa/m15/draft.txt
    qa_put "$m15_observer" /root/qa/m15/observe.sh
    qa_rsh "set -e; [ \"\$(sha256sum </root/qa/m15/draft.txt | cut -c1-64)\" = '$m15_draft_sha' ]; [ \"\$(sha256sum </root/qa/m15/observe.sh | cut -c1-64)\" = '$m15_observer_sha' ]; bash -n /root/qa/m15/observe.sh; chmod 444 /root/qa/m15/draft.txt; chmod 555 /root/qa/m15/observe.sh" >"$out/m15-stage.log" 2>&1 \
      || { echo 'QA_STRICT_REPLAY_STOPPED predicate=m15_staged_bytes (see m15-stage.log)' | tee -a "$out/lane.txt" >&2; exit 1; }
    echo "QA_M15_STAGE ok mode=strict-replay draft=/root/qa/m15/draft.txt draft_sha256=$m15_draft_sha observer=/root/qa/m15/observe.sh observer_sha256=$m15_observer_sha (staged only; not run by the launcher)" | tee -a "$out/lane.txt"
  fi
fi
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
  if [ -n "$strict_manifest" ] && [ -n "${m15_overlay_sha:-}" ]; then
    # Bind the overlay to the product pin: every anchor matched exactly once, and its diff is taken against product_pin itself.
    grep -q '^QA_M15_PREWRITE_LEASE_OK ' "$out/pre-write-check.log" && ! grep -q 'count=[^1]' <(grep '^QA_M15_ANCHOR ' "$out/pre-write-check.log") \
      && [ "$(grep -c '^QA_M15_ANCHOR .* count=1$' "$out/pre-write-check.log")" -ge 1 ] \
      || { echo 'QA_STRICT_REPLAY_STOPPED predicate=m15_overlay_anchors (see pre-write-check.log)' | tee -a "$out/lane.txt" >&2; echo 1 >"$out/lane.rc"; exit 1; }
    qa_rsh "set -e; cd '$remote'; git diff --binary '$product_pin' -- . ':(exclude)$spec'" >"$out/m15-overlay.diff" 2>"$out/m15-overlay-diff.err" \
      || { echo 'QA_STRICT_REPLAY_STOPPED predicate=m15_overlay_diff' | tee -a "$out/lane.txt" >&2; echo 1 >"$out/lane.rc"; exit 1; }
    echo "QA_M15_OVERLAY ok overlay_sha256=$m15_overlay_sha product_pin=$product_pin overlay_diff_sha256=$(shasum -a 256 <"$out/m15-overlay.diff" | cut -c1-64) files=$(grep -c '^diff --git ' "$out/m15-overlay.diff" || true) anchors=$(grep -c '^QA_M15_ANCHOR ' "$out/pre-write-check.log")" | tee -a "$out/lane.txt"
  fi
fi
qa_rsh "set -e; source /root/qa/modal.env; export REMOTE_BOX=1; python3 -I /root/qa/modal-lane.py overlay --root '$remote' --box '$box' $llm_onmiss$exact_overlay_args$strict_args; ${extra_box_env}python3 -I /root/qa/modal-lane.py prepare --box '$box' --root '$remote' --config /root/qa/config.json $llm_onmiss$exact_args$strict_args$extra_args_remote" >"$out/modal-prepare.log" 2>&1
if [ -n "$strict_manifest" ]; then
  grep -q "^QA_STRICT_REPLAY digest=$strict_digest fixtures=3 bucket=heydonna-qa forwarding=off " "$out/modal-prepare.log" \
    && grep -q "^LLM_MODE=strict-replay strict_fixtures=true forwarding=off fixtures=3 bucket=heydonna-qa app_data=ci-parity digest=$strict_digest\$" "$out/modal-prepare.log" \
    || { echo 'QA_STRICT_REPLAY_STOPPED predicate=overlay_unbound (overlay/prepare did not bake this manifest digest)' | tee -a "$out/lane.txt" >&2; exit 1; }
  grep -m1 '^QA_STRICT_REPLAY digest=' "$out/modal-prepare.log" | tee -a "$out/lane.txt"
fi
if [ -n "$llm_onmiss" ]; then
  # The controller binds lease + exact head + request on the box; if it cannot (e.g. unverified pool hostname) it bakes the
  # strict OFF source and says QA_ONMISS_DISABLED: the run is then plain strict replay and is labelled so.
  if grep -q '^QA_ONMISS_DISABLED' "$out/modal-prepare.log"; then
    [ -z "$exact_manifest" ] || { echo 'QA_ONMISS_EXACT_DISABLED: an exact run never degrades to strict replay' | tee -a "$out/lane.txt" >&2; exit 1; }
    grep -m1 '^QA_ONMISS_DISABLED' "$out/modal-prepare.log" | tee -a "$out/lane.txt"; llm_onmiss=''
    echo "llm_mode=strict (on-miss requested, controller disabled it)" | tee -a "$out/lane.txt"
  else
    grep -m1 '^QA_ONMISS_IDENTITY' "$out/modal-prepare.log" | tee -a "$out/lane.txt" || { echo 'QA_ONMISS_IDENTITY missing; refusing an unbound on-miss run' | tee -a "$out/lane.txt" >&2; exit 1; }
    if [ -n "$exact_manifest" ]; then
      # The proxy was baked with THIS manifest digest and the box wrote its non-secret run identities; bind both before any session exists.
      grep -q "^QA_ONMISS_EXACT_PREPARED digest=$exact_digest approved=" "$out/modal-prepare.log" || { echo 'QA_ONMISS_EXACT_UNBOUND: prepare did not bind this manifest digest' | tee -a "$out/lane.txt" >&2; exit 1; }
      qa_rsh "cat /root/qa/onmiss-ids.json" >"$out/onmiss-ids.json" 2>/dev/null || { echo 'QA_ONMISS_IDS_MISSING' | tee -a "$out/lane.txt" >&2; exit 1; }
      # The box-written identities are only CHECKED against the trusted derivation (lease, physical box, exact head); DENIED on any difference.
      python3 -I "$modal_helper" check-exact-ids --box "$box" --head "$head" --spend-box "$exact_spend_box" --ids "$out/onmiss-ids.json" --exact-manifest "$exact_manifest" | tee -a "$out/lane.txt" || { echo 'QA_ONMISS_IDS_DENIED: box-written identities differ from the trusted derivation' | tee -a "$out/lane.txt" >&2; exit 1; }
      # Run 9 mount readback: no leased runtime may still mount the shared R2 secret.
      if [ "$app_data" = scoped ]; then
        qa_rsh "python3 -I /root/qa/modal-lane.py check-app-mounts --root '$remote'" 2>&1 | tee -a "$out/lane.txt"
        [ "$(grep -c 'shared_mount_remaining=0' "$out/lane.txt")" -ge 1 ] || { echo 'QA_APP_MOUNTS_SHARED_REMAIN' | tee -a "$out/lane.txt" >&2; exit 1; }
      else
        echo 'QA_APP_MOUNTS mode=ci-parity shared_mount=kept (CI parity by Rajiv decision; the proxy fixture/staging/ledger clients never use it)' | tee -a "$out/lane.txt"
      fi
      # Spend custody BEFORE any session exists: legacy (heydonna-fixtures) + QA (heydonna-qa) ledgers for the physical box AND the exact head,
      # prior claims for the approved hashes, halts and the combined allowance, bound into the QA ledgers (carry rows + custody marker) so the
      # proxy refuses every reservation without it. Trusted host only: the legacy read key never reaches a box.
      "${QA_UV:-$HOME/.local/bin/uv}" run --no-project --python 3.13 --with boto3==1.35.99 python "$modal_helper" reconcile-custody --box "$box" --head "$head" --spend-box "$exact_spend_box" --exact-manifest "$exact_manifest" --output "$out/custody-reconcile.json" ${exact_dry:+--read-only} 2>&1 | tee -a "$out/lane.txt"
      [ "$(grep -c '^QA_CUSTODY status=RECONCILED' "$out/lane.txt")" -ge 1 ] || { echo 'QA_CUSTODY_REFUSED: see custody-reconcile.json' | tee -a "$out/lane.txt" >&2; exit 1; }
      echo "QA_ONMISS_EXACT_BOUND manifest_sha256=$exact_digest ids=$out/onmiss-ids.json fixture_session_ttl_s=300 refresh_s=100 no_shared_secret=true" | tee -a "$out/lane.txt"
    fi
  fi
fi
"$rtb" run "$box" -- npx playwright install --with-deps chromium >"$out/browser-install.log" 2>&1
# The overlay edits tooling files only (preflight/deploy/processor secret names); baseline is taken AFTER it so the RED leg's
# restoration check covers tests/** exactly.
# The runtime's exit code IS the lane verdict (GREEN legs must exit 0; RED legs must fail in Playwright). Record it explicitly instead of
# relying on set -e through run-file/ssh, so cleanup() always sees the real code and a wrapper exit 0 can never mask a failed leg.
lane_rc=0
# Exact mode: the proxy's fixture clients get ONLY prefix-scoped five-minute sessions, minted on this trusted host and handed to the
# box's own Modal Dict. They are minted from the start of the leg (after the launcher setup: box, overlay, prepare), re-minted every
# 100 s, never at launcher startup and never persisted; if this loop stops, every session expires inside 5 minutes and the proxy's
# fixture clients fail closed (no fallback to the application signer). Nothing secret is placed in argv, receipts or artifacts.
scoped_arm_loop() {
  local n=0
  while :; do
    if python3 -I "$modal_helper" mint-session --kind proxy --box "$box" --head "$head" --spend-box "$exact_spend_box" --ids "$out/onmiss-ids.json" --exact-manifest "$exact_manifest" \
        | qa_rsh "set -e; source /root/qa/modal.env; export REMOTE_BOX=1; python3 -I /root/qa/modal-lane.py arm-session --box '$box' --exact-manifest /root/qa/exact-manifest.json" >>"$out/scoped-arm.log" 2>&1; then
      n=$((n + 1)); echo "QA_SCOPED_ARM n=$n at=$(date -u +%FT%TZ)" >>"$out/scoped-arm.log"
    else
      echo "QA_SCOPED_ARM_FAILED at=$(date -u +%FT%TZ)" >>"$out/scoped-arm.log"
    fi
    sleep 100
  done
}
stop_scoped_arm() {
  # `wait` returns 143 for the TERM'd loop; under `set -e` that exited the lane before lane.rc (M15 Run 11 strict4: playwright_rc=0, verdict lane-fail).
  [ -z "$arm_pid" ] || { pkill -P "$arm_pid" 2>/dev/null; kill "$arm_pid" 2>/dev/null; wait "$arm_pid" 2>/dev/null || :; arm_pid=''; echo 'QA_SCOPED_ARM_STOPPED' >>"$out/lane.txt"; }
  return 0
}
strict_arm_once() {
  python3 -I "$modal_helper" mint-session --kind strict-read --box "$box" --head "$head" --strict-manifest "$strict_manifest" \
    | qa_rsh "set -e; source /root/qa/modal.env; export REMOTE_BOX=1; python3 -I /root/qa/modal-lane.py arm-session --box '$box' --strict-manifest /root/qa/strict-manifest.json" >>"$out/strict-arm.log" 2>&1
}
strict_arm_loop() {
  local n=1
  while :; do
    sleep 100
    if strict_arm_once; then n=$((n + 1)); echo "QA_STRICT_ARM n=$n at=$(date -u +%FT%TZ)" >>"$out/strict-arm.log"; else echo "QA_STRICT_ARM_FAILED at=$(date -u +%FT%TZ)" >>"$out/strict-arm.log"; fi
  done
}
if [ -n "$strict_manifest" ]; then
  # GetObject-only session on exactly the two manifest keys, minted on this trusted host, re-armed every 100 s (TTL 300 s). The first
  # arm must succeed before the browser; if the loop stops, reads fail closed as misses (424), never as provider calls.
  strict_arm_once || { echo 'QA_STRICT_REPLAY_STOPPED predicate=strict_read_session_unarmed (see strict-arm.log)' | tee -a "$out/lane.txt" >&2; exit 1; }
  strict_arm_loop & arm_pid=$!
  echo "QA_STRICT_ARM_STARTED refresh_s=100 ttl_s=300 actions=GetObject keys=2 bucket=heydonna-qa forwarding=off" >>"$out/lane.txt"
fi
if [ -n "$exact_manifest" ]; then
  scoped_arm_loop & arm_pid=$!
  echo "QA_SCOPED_ARM_STARTED refresh_s=100 ttl_s=300 prefixes=fixtures/llm/v2/(read),staging+ledgers(write) shared_secret=none" >>"$out/lane.txt"
fi
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
stop_scoped_arm
if [ "$lane_rc" = 124 ]; then
  echo "LANE_TIMEOUT timeout_secs=$timeout_secs" >>"$out/lane.txt"
  qa_rsh "pkill -TERM -f 'scripts/e2e/local-repro-[p]reflight.sh' || true; pkill -TERM -f '[p]laywright test' || true; sleep 5" >>"$out/lane.log" 2>&1 || true
fi
echo "$lane_rc" >"$out/lane.rc"
[ "$lane_rc" = 0 ] || echo "REMOTE_SPEC_LEG_FAILURE runtime_rc=$lane_rc (see lane.log / out/legs.tsv)" >&2
exit "$lane_rc"
}
