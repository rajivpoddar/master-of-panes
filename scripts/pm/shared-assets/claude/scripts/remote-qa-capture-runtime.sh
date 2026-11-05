#!/usr/bin/env bash
# Runs only on an ephemeral remote-test-box, with /work/heydonna-app as cwd.
main() {
set -euo pipefail
umask 077
if [ "${7:-}" = anonymous ]; then exec bash /root/qa/anonymous.sh --runtime "$1" "$2" "$3"; fi
cd -P .
# Slot-clone identity (pool boxes): /work/heydonna-app is the real repo and /work/heydonna-app-300N a symlink to it, so `cd -P`
# lands on a name preflight's slot-clone guard rejects. Enter the slot path LOGICALLY only when it is the very same physical
# checkout (inode-equal) on this isolated box; the guard's intent (never deploy from a non-slot/PM checkout) is unchanged.
slot_link="/work/heydonna-app-300$1"
if [[ "$1" =~ ^[1-8]$ ]] && [ "$(basename "$PWD")" != "heydonna-app-300$1" ] && [ -L "$slot_link" ] && [ "$slot_link" -ef "$PWD" ]; then
  cd "$slot_link" || exit 2
  echo "SLOT_CLONE_PATH=logical $PWD -> $(pwd -P)"
fi
slot="$1"; expected_head="$2"; page_path="$3"
cleanup_only="${4:-}"
persona="${5:-admin}"
capture_mode="${6:-ui-only}"
seed_project="${8:-none}"
browser_lane="${9:-agent-browser}"
interactive="${10:-0}"
next_mode="${11:-${QA_NEXT_MODE:-dev}}"
[[ "$next_mode" = dev || "$next_mode" = prod ]] || { echo 'Invalid Next mode'; exit 2; }
signed_out_auth="${12:-0}"
# ProofShot recording (Rajiv D0AMF0XE6TS thread 1793252228.425639). Best-effort: never fails the QA run.
record="${13:-0}"
[[ "$record" = 0 || "$record" = 1 ]] || record=0
# Seeded-test arm (PM 2026-10-30, #12031 AC-1): base64 of ONE vitest command run against the canonical preview seed in
# this run's disposable dev Convex, right after canonical setup. No Next, no browser, no ProofShot.
seeded_test_b64="${14:-}"
seeded_test=''
if [ -n "$seeded_test_b64" ]; then
  seeded_test=$(printf '%s' "$seeded_test_b64" | base64 -d) || { echo 'Invalid seeded-test encoding'; exit 2; }
  [[ "$seeded_test" =~ ^SEEDED_SCRIPT(\ [A-Za-z0-9._/:=@-]+)*$ ]] || [[ "$seeded_test" =~ ^(VITEST_INTEGRATION=true\ )?npx\ vitest\ run\ [A-Za-z0-9_./@:=\ -]+$ ]] || { echo 'Seeded test must be: [VITEST_INTEGRATION=true ]npx vitest run <files/flags>'; exit 2; }
  [ "$record" = 0 ] && [ "$interactive" = 0 ] || { echo 'Seeded test runs without recording or an interactive browser'; exit 2; }
fi
[[ "$signed_out_auth" = 0 || "$signed_out_auth" = 1 ]] || exit 2
if [ "$signed_out_auth" = 1 ]; then
  [[ "$next_mode" = dev && ( "$page_path" = /sign-in || "$page_path" = /sign-up || "$page_path" = /community ) && "$browser_lane" = agent-browser && "$interactive" = 1 && "$seed_project" = none && "$persona" = admin ]] || { echo 'Invalid signed-out capture tuple'; exit 2; }
fi
export QA_NEXT_MODE="$next_mode"
[[ "$browser_lane" = agent-browser || "$browser_lane" = playwright ]] || exit 2
[[ "$interactive" = 0 || "$interactive" = 1 ]] || exit 2
[[ "$seed_project" = none || "$seed_project" = medium || "$seed_project" = long ]] || { echo 'Invalid seed project'; exit 2; }
[[ "$capture_mode" = ui-only || "$capture_mode" = modal ]] || { echo 'Invalid capture mode'; exit 2; }
export QA_CAPTURE_MODE="$capture_mode"
if [ "$capture_mode" = modal ]; then
  source /root/qa/modal.env
  export MODAL_CONFIG_PATH=/dev/null
fi
qa=/root/qa
out="$qa/out"
mkdir -p "$out"
# Recorder truncation root cause (2026-10-30, proved on rtb-qa-recfix): agent-browser 0.26.0 spawns
# `ffmpeg -f image2pipe ... -i pipe:0 ... session.webm` with stderr on a pipe it never reads. ffmpeg's progress line fills
# the 64 KiB pipe at ~330s; ffmpeg blocks in pipe_write(stderr), stops muxing (file frozen on a 256 KiB write boundary,
# no Cues/trailer), its stdin fills and the daemon's frame writer blocks too (stop -> EAGAIN). This shim, first on PATH for
# the daemon and ProofShot, sends the recorder's stderr to a file with -nostats; every other ffmpeg call passes through.
mkdir -p "$qa/recorder-bin"
cat >"$qa/recorder-bin/ffmpeg.tmp" <<'FFMPEG_SHIM'
#!/bin/sh
for a in "$@"; do
  if [ "$a" = pipe:0 ]; then exec /usr/bin/ffmpeg -nostats -loglevel warning "$@" 2>>/root/qa/out/recorder-ffmpeg.log; fi
done
exec /usr/bin/ffmpeg "$@"
FFMPEG_SHIM
chmod 755 "$qa/recorder-bin/ffmpeg.tmp"; mv -f "$qa/recorder-bin/ffmpeg.tmp" "$qa/recorder-bin/ffmpeg"
case ":$PATH:" in *":$qa/recorder-bin:"*) ;; *) export PATH="$qa/recorder-bin:$PATH";; esac
# Box-side QA session log (Rajiv C0ALZJHGE49 thread 1793299570.404429, 2026-10-30): bounded JSONL, box clock, no args/secrets/seed text.
QA_SESSION_LOG="$qa/session-log.jsonl"; QA_SESSION_LOG_MAX=4194304
qa_sl() {  # qa_sl EVENT [key=value ...] (values: [A-Za-z0-9._:-] only; numbers stay numbers)
  [ "$(stat -c %s "$QA_SESSION_LOG" 2>/dev/null || echo 0)" -lt "$QA_SESSION_LOG_MAX" ] || return 0
  local l kv k v; l="{\"t_ms\":$(date +%s%3N),\"ev\":\"$1\""; shift
  for kv in "$@"; do k=${kv%%=*}; v=${kv#*=}; v=$(printf '%s' "$v" | tr -c 'A-Za-z0-9._:-' '_' | cut -c1-48)
    if [[ "$v" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then l="$l,\"$k\":$v"; else l="$l,\"$k\":\"$v\""; fi; done
  printf '%s}\n' "$l" >>"$QA_SESSION_LOG" 2>/dev/null || true
}
if [ "$cleanup_only" != cleanup ]; then
  : >"$QA_SESSION_LOG"; chmod 600 "$QA_SESSION_LOG"; rm -f "$qa"/ab-inflight.* "$qa/ab-watchdog-restarted" "$qa/record.busy"
  qa_sl run_start mode="$capture_mode" interactive="$interactive" record="$record"
  # /root/qa/sl-ext start|end SRC ID [RC]: called by remote-test-box.sh around every non-browser remote command while this
  # run is live, so the analyser can attribute idle video spans to non-browser work (and trim them). No command text.
  cat >"$qa/sl-ext" <<'SL_EXT'
#!/bin/bash
f=/root/qa/session-log.jsonl
[ -f "$f" ] && [ ! -e /root/qa/runtime.done ] || exit 0
[ "$(stat -c %s "$f" 2>/dev/null || echo 0)" -lt 4194304 ] || exit 0
src=$(printf '%s' "${2:-driver}" | tr -c 'A-Za-z0-9_-' '_' | cut -c1-24); id=$(printf '%s' "${3:-0}" | tr -dc '0-9a-f' | cut -c1-16)
case "$1" in
  start) printf '{"t_ms":%s,"ev":"ext_start","src":"%s","id":"%s"}\n' "$(date +%s%3N)" "$src" "$id" >>"$f";;
  end) rc=$(printf '%s' "${4:-0}" | tr -dc '0-9' | cut -c1-3); printf '{"t_ms":%s,"ev":"ext_end","src":"%s","id":"%s","rc":%s}\n' "$(date +%s%3N)" "$src" "$id" "${rc:-0}" >>"$f";;
esac
exit 0
SL_EXT
  chmod 700 "$qa/sl-ext"
fi
# Health sampler + daemon watchdog: every 10s pid/socket/RSS/CPU/load; when no driver command is in flight, a cheap
# `get url` probe every 30s. Daemon gone, or 2 consecutive failed probes -> the deployed one-restart path at once.
qa_health_loop() {
  local d=/root/.agent-browser pid sock alive rss cpu prev=0 now tick=0 fails=0 hz load mem prc inflight
  hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
  while [ ! -f "$qa/browser-finish" ] && [ ! -f "$qa/runtime.done" ]; do
    pid=$(cat "$d/$AGENT_BROWSER_SESSION.pid" 2>/dev/null || true); alive=0; rss=0; cpu=0
    sock=0; [ -S "$d/$AGENT_BROWSER_SESSION.sock" ] && sock=1
    if [[ "$pid" =~ ^[0-9]+$ ]] && [ -d "/proc/$pid" ]; then alive=1
      rss=$(awk '/^VmRSS:/{print $2}' "/proc/$pid/status" 2>/dev/null || echo 0)
      now=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null || echo 0); cpu=$(( (now-prev)*100/(hz*10) )); [ "$prev" -gt 0 ] || cpu=0; prev=$now
    fi
    load=$(cut -d' ' -f1 /proc/loadavg); mem=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
    inflight=0; for f in "$qa"/ab-inflight.*; do [ -e "$f" ] && kill -0 "${f##*.}" 2>/dev/null && inflight=$((inflight+1)); done
    qa_sl health pid_alive=$alive sock=$sock rss_kb="${rss:-0}" cpu_pct=$cpu load1="$load" mem_avail_mb="${mem:-0}" inflight=$inflight
    tick=$((tick+1))
    if [ -f "$qa/browser-ready.json" ] && [ ! -f "$qa/record.busy" ] && [ "$inflight" = 0 ] && [ ! -f "$qa/ab-daemon-unresponsive" ]; then
      if [ "$alive" = 0 ] && [ "$tick" -gt 1 ]; then fails=2
      elif [ $((tick % 3)) = 0 ]; then
        prc=0; QA_AB_INTERNAL=1 QA_AB_PROBE=1 timeout 30 bash "$qa/agent-browser.sh" __probe >/dev/null 2>&1 || prc=$?
        if [ "$prc" = 0 ]; then fails=0; else fails=$((fails+1)); fi
        qa_sl watchdog_probe rc=$prc fails=$fails
      fi
      if [ "$fails" -ge 2 ]; then
        qa_sl watchdog_restart reason="$([ "$alive" = 0 ] && echo daemon_gone || echo probe_failed)"
        bash "$qa/agent-browser.sh" __watchdog-restart >>"$out/browser.log" 2>&1 || true
        fails=0
      fi
    fi
    sleep 10
  done
}
set -a
source .env.local
set +a
unset CONVEX_DEPLOY_KEY ANTHROPIC_BASE_URL ANTHROPIC_API_KEY MODAL_KEY MODAL_SECRET
unset LLM_PROXY_CAPTURE_BYPASS_HASHES LLM_PROXY_CAPTURE_BYPASS_PROFILE
if [ "$capture_mode" = ui-only ]; then unset MODAL_API_KEY; fi
[[ "${PORT:-}" =~ ^[0-9]+$ ]] || { echo 'PORT missing'; exit 2; }
[[ "$persona" = admin || "$persona" = unadmitted ]] || { echo 'Unknown capture persona'; exit 2; }
# Unset preserves the legacy identity and provenance bytes.
capture_persona="$persona"
if [ "${QA_PERSONA_SUBJECT+x}" = x ]; then
  [[ "$QA_PERSONA_SUBJECT" =~ ^synth-workspace-qa-[a-f0-9]{32}-(owner|admin|member|viewer|billing_admin)$ ]] || { echo 'Invalid QA_PERSONA_SUBJECT'; exit 2; }
  [ "$persona" = admin ] || { echo 'QA_PERSONA_SUBJECT cannot use unadmitted persona'; exit 2; }
  capture_persona="$persona:$QA_PERSONA_SUBJECT"
fi
[[ "$page_path" = /* && "$page_path" != //* ]] || { echo 'Page must be an origin-relative path'; exit 2; }
case "$page_path" in *'{SEEDED_TRANSCRIPT_ID}'*) [ "$seed_project" != none ] && [ "$persona" = admin ] || { echo '{SEEDED_TRANSCRIPT_ID} requires --seed-project and the admin persona'; exit 2; };; esac
[[ "$(git rev-parse HEAD)" = "$expected_head" ]] || { echo 'Wrong head'; exit 2; }
[[ "${CONVEX_DEPLOYMENT:-}" = dev:* ]] || { echo 'Dev deployment required'; exit 2; }
dep="${CONVEX_DEPLOYMENT#dev:}"
case "$dep" in beloved-cardinal-*|loyal-lynx-*|prod-*|'') echo 'Unsafe deployment'; exit 2;; esac
[[ "${CONVEX_ADMIN_KEY%%|*}" = "dev:$dep" ]] || { echo 'Scoped dev admin key required'; exit 2; }
[[ "$NEXT_PUBLIC_CONVEX_URL" = "https://$dep.convex.cloud" ]] || { echo 'Deployment URL mismatch'; exit 2; }
export REMOTE_BOX=1 SYNTH_E2E_AUTH=1 NEXT_PUBLIC_SYNTH_E2E_AUTH=1 SYNTH_E2E_NO_CLERK=1 SYNTH_CONVEX_PREVIEW=1
# Never inherit enablement into normal captures. Setup may still provision its
# canonical synthetic verifier; the signed-out browser never receives an identity.
unset QA_SIGNED_OUT_AUTH
if [ "$signed_out_auth" = 1 ]; then
  [ -z "${QA_PERSONA_SUBJECT:-}${QA_WORKSPACE_TEMPLATE_POOL:-}" ] && [ "${LLM_PROXY_FIXTURE_CAPTURE:-false}" != true ] || { echo 'Signed-out capture conflicts with identity/processing overrides'; exit 2; }
  export QA_SIGNED_OUT_AUTH=1
  export NODE_ENV=development
  export CLERK_SECRET_KEY=sk_test_remote_qa_synthetic_not_a_credential # secret-scan:allow
fi
# Varlock requires this app field even though synthetic QA never authenticates with Clerk.
# An inert value satisfies that schema without copying a slot/live Clerk credential.
if [ -z "${CLERK_SECRET_KEY:-}" ]; then
  export CLERK_SECRET_KEY=sk_test_remote_qa_synthetic_not_a_credential # secret-scan:allow
  echo 'QA_APP_ENV_CLERK_SECRET_KEY=synthetic-placeholder (no live credential)' >"$out/app-env-compatibility.log"
fi
export LLM_PROXY_ENABLED=true
if [ "${LLM_PROXY_FIXTURE_CAPTURE:-false}" = true ] && [ "$capture_mode" = modal ]; then
  export STRICT_FIXTURES=false
else
  export STRICT_FIXTURES=true
fi
# Public listener health is not authentication proof. Use a fixed, persona-reachable auth gate.
export QA_LIVENESS_PATH=/api/release QA_CAPTURE_PERSONA="$persona"
if [ "$persona" = unadmitted ]; then
  # app/page.tsx sends signed-out users to /sign-in; an unadmitted signed-in user stays here.
  export QA_AUTH_PROOF_PATH=/
else
  export QA_AUTH_PROOF_PATH=/admin
fi
if [ "${QA_PERSONA_SUBJECT+x}" = x ]; then export QA_AUTH_PROOF_PATH=/dashboard; fi
export QA_READINESS_PATH="$QA_AUTH_PROOF_PATH"
if [ "$capture_mode" = ui-only ]; then
  echo 'CAPTURE_MODE=ui-only MODAL_READINESS=NOT_APPLICABLE (no Modal/LLM steps permitted)' >"$out/capture-mode.log"
else
  echo "CAPTURE_MODE=$([ "${LLM_PROXY_FIXTURE_CAPTURE:-false}" = true ] && echo on || echo off) QA_MODAL_BOX_ID=$QA_MODAL_BOX_ID run_id=${LLM_PROXY_CAPTURE_RUN_ID:-} strict=$STRICT_FIXTURES cap_usd=3" >"$out/capture-mode.log"
fi
echo "CAPTURE_RUNTIME_REVISION=agent-browser-v3-single-state persona=$persona liveness=$QA_LIVENESS_PATH auth_proof=$QA_AUTH_PROOF_PATH" | tee -a "$out/capture-mode.log"
export QA_URL="http://localhost:$PORT" AUTH_STATE="$qa/auth-state.json"
# Controller-written disposable-Convex ownership binding (read-only, non-secret); absent on non-disposable runs.
if [ -f "$qa/convex-ownership.json" ]; then
  export QA_CONVEX_OWNERSHIP_PATH="$qa/convex-ownership.json"
  echo "QA_CONVEX_OWNERSHIP path=$QA_CONVEX_OWNERSHIP_PATH mode=$(stat -c %a "$QA_CONVEX_OWNERSHIP_PATH") sha256=$(sha256sum "$QA_CONVEX_OWNERSHIP_PATH" | cut -c1-64) deployment_match=$([ "$(node -p "require('$QA_CONVEX_OWNERSHIP_PATH').deployment")" = "dev:$dep" ] && echo yes || echo no)" >>"$out/capture-mode.log"
fi
export QA_HEAD="$expected_head" QA_CAPTURE_PATH="$page_path" QA_CAPTURE_OUT="$out" AGENT_BROWSER_SESSION="qa-capture-$slot" AGENT_BROWSER_HEADED=false
if [ "$signed_out_auth" = 1 ]; then export AGENT_BROWSER_SESSION="qa-signed-out-$slot-$$-$(date +%s)"; fi
qa_config() (
  unset NODE_OPTIONS
  export CONVEX_DEPLOY_KEY="$CONVEX_ADMIN_KEY"
  node "$qa/config.cjs" "$1" "$qa/config.json" "$qa/config-before.json"
)
qa_persona() (
  unset NODE_OPTIONS
  export CONVEX_DEPLOY_KEY="$CONVEX_ADMIN_KEY"
  node "$qa/persona.cjs" "$1" "$qa/persona-before.json"
)
qa_seed() (
  unset NODE_OPTIONS
  export CONVEX_DEPLOY_KEY="$CONVEX_ADMIN_KEY"
  # General-project canary (pool QA): the controller drops /root/qa/seed-project-type ("general"); clone-only patch.
  if [ -f "$qa/seed-project-type" ]; then export QA_SEED_PROJECT_TYPE="$(cat "$qa/seed-project-type")"; fi
  node "$qa/seed.cjs" "$1" "$slot" "$seed_project" "$qa/seed-project.json"
)
qa_workspace() (
  unset NODE_OPTIONS
  export CONVEX_DEPLOY_KEY="$CONVEX_ADMIN_KEY"
  node "$qa/workspace.cjs" "$1"
)
listener() { ss -ltnH "sport = :$PORT" | grep -q .; }
# The controller launches this runtime in its own timeout-owned process group.
# Its PID receipt lets EXIT/TERM cleanup stop preflight descendants before restoration.
ps -o pgid= -p $$ | tr -d ' ' >"$qa/runtime.pgid"
owned_pid='' observer_pid=''
qa_egress_assert() {
  curl --noproxy '*' -fsS --max-time 10 -H 'content-type: application/json' \
    -d '{"phase":"signed-out-auth"}' http://127.0.0.1:18477/assert >"$out/signed-out-egress.json"
}
# Remote-only, noncredential diagnostics for browser startup failures.
qa_browser_resources() {
  { date -u; free -m; printf '\nKernel tail\n'; dmesg | tail -80; } >"$out/browser-resources-$1.log" 2>&1 || true
}
# ProofShot wraps the SAME agent-browser session (env session + config). Any failure only warns.
qa_media() {  # after qa_record_stop: H.264 transcode only. AC frames are extracted on the Mac from this MP4
  # (qa-video-frames.sh, Rajiv D0AMF0XE6TS thread 1793252228.425639 ts 1793291004.677549) using recording/ac-marks.tsv.
  local src="$out/recording/session.webm" mp4="$out/recording/session.mp4"
  [ -s "$src" ] || { echo 'QA_MEDIA_WARN no source video'; return 1; }
  [ ! -f "$out/recording/TRUNCATED" ] || echo "QA_MEDIA_WARN QA_RECORDING_TRUNCATED source; MP4 is a PARTIAL video ($(cat "$out/recording/TRUNCATED"))"
  command -v ffmpeg >/dev/null || { echo 'QA_MEDIA_WARN ffmpeg missing on box'; return 1; }
  ffmpeg -nostdin -loglevel error -y -i "$src" -vf "scale=1440:900:force_original_aspect_ratio=decrease,pad=1440:900:(ow-iw)/2:(oh-ih)/2,format=yuv420p" \
    -c:v libx264 -preset veryfast -crf 26 -movflags +faststart -an "$mp4" || { echo 'QA_MEDIA_WARN H.264 encode failed'; return 1; }
  python3 -I - "$out" "$QA_HEAD" "$(cat "$qa/run-id" 2>/dev/null)" <<'MEDIA_MANIFEST'
import hashlib, json, os, struct, sys
out, head, run = sys.argv[1:]
def h(p):
    d = hashlib.sha256()
    with open(p, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''): d.update(c)
    return d.hexdigest()
m = {'head': head, 'run_id': run, 'source': {'file': 'recording/session.webm', 'sha256': h(os.path.join(out, 'recording/session.webm'))},
     'mp4': {'file': 'recording/session.mp4', 'sha256': h(os.path.join(out, 'recording/session.mp4')), 'codec': 'h264'},
     'marks': 'recording/ac-marks.tsv', 'frames_extracted_by': 'qa-video-frames.sh (Mac, after fetch)'}
tp = os.path.join(out, 'recording/TRUNCATED')
m['truncated'] = open(tp).read().strip() if os.path.exists(tp) else False
json.dump(m, open(os.path.join(out, 'media-manifest.json'), 'w'), indent=2)
print('QA_MEDIA_OK mp4=%s' % m['mp4']['sha256'][:12])
MEDIA_MANIFEST
}
qa_proofshot() ( cd "$qa/proofshot-cwd" && env -i HOME=/root PATH="$PATH" AGENT_BROWSER_SESSION="$AGENT_BROWSER_SESSION" \
  AGENT_BROWSER_CONFIG="$qa/agent-browser.json" AGENT_BROWSER_DEFAULT_TIMEOUT=180000 timeout "$1" proofshot "${@:2}" )
qa_record_start() {
  command -v proofshot >/dev/null && command -v ffmpeg >/dev/null || { echo 'QA_RECORDING_WARN proofshot/ffmpeg missing on box'; return 1; }
  mkdir -p "$qa/proofshot-cwd" "$qa/proofshot"
  printf '{"output":"%s","viewport":{"width":1440,"height":900},"devServer":{"port":%s},"headless":true}\n' "$qa/proofshot" "$PORT" >"$qa/proofshot-cwd/proofshot.config.json"
  local url; url=$(bash "$qa/agent-browser.sh" get url | tail -1)
  [[ "$url" = "$QA_URL"/* ]] || { echo "QA_RECORDING_WARN unexpected browser url"; return 1; }
  touch "$qa/record.busy"
  qa_proofshot 150 start --force --url "$url" --description "remote QA $QA_HEAD" || { rm -f "$qa/record.busy"; qa_sl record_start_failed; return 1; }
  rm -f "$qa/record.busy"
  rm -f "$qa/recording.stop_ms"
  touch "$qa/recording.active"; date +%s%3N >"$qa/recording.started_ms"; echo "QA_RECORDING_STARTED epoch=$(date +%s)"
  qa_sl record_start
  qa_record_single_writer
}
# proofshot start runs `agent-browser open` again, and with a recording active agent-browser opens that URL in a NEW
# tab with fresh localStorage (new deviceId). The first tab still holds and heartbeats the editor writer lease,
# so the recorded tab claims as a second writer and rule 48 makes it read-only ("This transcript is being edited").
# Keep only the recorded tab, clear the stale lease on the disposable deployment, and reload so it claims cleanly.
qa_record_single_writer() {
  case "$QA_CAPTURE_PATH" in /dashboard/editor/*) ;; *) return 0;; esac
  local tabs t closed=0 waited=0
  tabs=$(bash "$qa/agent-browser.sh" --json tab | node -e 'let s="";process.stdin.on("data",d=>s+=d);process.stdin.on("end",()=>{const j=JSON.parse(s);for(const t of (j.data&&j.data.tabs)||[])if(!t.active&&t.type==="page")console.log(t.tabId);})') || { echo 'QA_RECORDING_WARN tab listing failed'; return 1; }
  for t in $tabs; do
    [[ "$t" =~ ^t[0-9]+$ ]] || continue
    bash "$qa/agent-browser.sh" tab close "$t" >/dev/null && closed=$((closed+1))
  done
  echo "QA_RECORDING_EXTRA_TABS_CLOSED=$closed"
  [ "$closed" -gt 0 ] || return 0
  if [ -n "${QA_CONVEX_DEPLOYMENT_EXPIRES_AT_MS:-}" ]; then
    qa_seed clear-leases || { echo 'QA_RECORDING_WARN lease clear failed'; return 1; }
  else
    # Not disposable: never delete rows; let the closed tab's lease reach its 60s server TTL instead.
    echo 'QA_SEED_EDITOR_SESSIONS_CLEARED=skipped reason=not-disposable-convex wait=65s'; sleep 65; waited=65
  fi
  bash "$qa/agent-browser.sh" reload >/dev/null || { echo 'QA_RECORDING_WARN reload failed'; return 1; }
  bash "$qa/agent-browser.sh" wait --fn '[...document.querySelectorAll("[data-editor-ready=true]:not([inert]) .ProseMirror")].some(e=>e.textContent.trim().length>0)' >/dev/null || true
  echo "QA_EDITOR_WRITABLE=$(bash "$qa/agent-browser.sh" eval 'document.querySelector("[data-editor-ready=true]:not([inert]) .ProseMirror")?.isContentEditable===true && !document.body.innerText.includes("This transcript is being edited")' | tail -1) lease_wait_s=$waited"
}
# Recorder finalize (2026-10-30, 7/16 videos truncated on a 256 KiB boundary): the webm was copied while agent-browser's
# ffmpeg was still writing (stop never reached a wedged daemon, or the daemon tree was killed). Ask any live recorder to
# finish (SIGINT = flush + trailer), wait up to 20s, and only then copy. Logged as recording_finalize in the session log.
qa_record_finalize() {
  local pids p n=0 waited=0
  pids=$(pgrep -f 'ffmpeg.*(image2pipe|session[.]webm)' 2>/dev/null || true)
  [ -n "$pids" ] || { qa_sl recording_finalize live_recorders=0; return 0; }
  for p in $pids; do kill -INT "$p" 2>/dev/null && n=$((n+1)); done
  while [ $waited -lt 20 ] && pgrep -f 'ffmpeg.*(image2pipe|session[.]webm)' >/dev/null 2>&1; do sleep 1; waited=$((waited+1)); done
  pids=$(pgrep -f 'ffmpeg.*(image2pipe|session[.]webm)' 2>/dev/null || true)
  echo "QA_RECORDING_FINALIZED signalled=$n waited_s=$waited still_running=$([ -n "$pids" ] && echo 1 || echo 0)"
  qa_sl recording_finalize live_recorders=$n waited_s=$waited still_running="$([ -n "$pids" ] && echo 1 || echo 0)"
}
qa_record_stop() {
  [ -f "$qa/recording.active" ] || return 0
  rm -f "$qa/recording.active"
  touch "$qa/record.busy"
  date +%s%3N >"$qa/recording.stop_ms"
  local stop_rc=0; qa_proofshot 90 stop --no-close || { stop_rc=$?; echo 'QA_RECORDING_WARN proofshot stop failed'; }
  rm -f "$qa/record.busy"
  qa_record_finalize
  qa_sl record_stop rc=$stop_rc
  local s; s=$(ls -dt "$qa/proofshot"/*/ 2>/dev/null | head -1)
  [ -n "$s" ] && [ ! -e "$out/recording" ] || { echo 'QA_RECORDING_WARN no proofshot session dir'; return 1; }
  cp -r "$s" "$out/recording"; rm -f "$out/recording/viewer.html"
  # AC markers as ms offsets into the recording (offset_ms<TAB>ac<TAB>slug); negative = before recording start.
  local t0; t0=$(cat "$qa/recording.started_ms" 2>/dev/null)
  if [ -s "$qa/ac-marks.tsv" ] && [[ "$t0" =~ ^[0-9]+$ ]]; then
    awk -F'\t' -v OFS='\t' -v t0="$t0" '$1 ~ /^[0-9]+$/ {print $1 - t0, $2, $3}' "$qa/ac-marks.tsv" >"$out/recording/ac-marks.tsv"
    echo "QA_AC_MARKS count=$(wc -l <"$out/recording/ac-marks.tsv" | tr -d ' ')"
  else echo 'QA_AC_MARKS count=0'; fi
  if [ -s "$out/recording/session.webm" ]; then
    (cd "$out/recording" && sha256sum session.webm >session.webm.sha256)
    echo "QA_RECORDING_SAVED path=$out/recording/session.webm sha256=$(cut -d' ' -f1 "$out/recording/session.webm.sha256") bytes=$(stat -c %s "$out/recording/session.webm")"
    qa_sl record_saved bytes="$(stat -c %s "$out/recording/session.webm")" video_s="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$out/recording/session.webm" 2>/dev/null | head -1)"
    qa_record_check "$t0" "$stop_rc"
  else echo 'QA_RECORDING_WARN no video produced (screenshots unaffected)'; fi
}
# Truncation verdict (2026-10-30): a video is COMPLETE only with an end marker, a real duration, and >=90% of the
# recorded session. Otherwise recording/TRUNCATED + QA_RECORDING_TRUNCATED; the video still posts, labelled partial.
qa_record_check() {
  local f="$out/recording/session.webm" t0="$1" now sess_s dur errs reasons=''
  # Session ends at the stop request (2026-11-03): stop + finalize took ~17s and alone pushed short sessions under 90%.
  now=$(cat "$qa/recording.stop_ms" 2>/dev/null); [[ "$now" =~ ^[0-9]+$ ]] || now=$(date +%s%3N)
  sess_s=$([[ "$t0" =~ ^[0-9]+$ ]] && awk -v a="$t0" -v b="$now" 'BEGIN{printf "%.1f", (b-a)/1000}' || echo 0)
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null | head -1)
  errs=$(timeout 120 ffmpeg -nostdin -v error -i "$f" -f null - 2>&1 | head -20 || true)
  printf '%s' "$errs" | grep -q 'File ended prematurely' && reasons="${reasons}no_end_marker,"
  [[ "$dur" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { reasons="${reasons}duration_NA,"; dur=NA; }
  # The recorder stamps CDP screencast frames at a fixed rate, and Chrome only emits frames on repaint, so idle or
  # navigating stretches are compressed (2026-11-03 smoke: 38s video for an 89s session whose last frame is the final page).
  # A short video is therefore a truncation ONLY when the stop was unclean or the daemon wedged/restarted (12608: stop rc 124).
  if [ "$dur" != NA ] && awk -v d="$dur" -v s="$sess_s" 'BEGIN{exit !(s>0 && d<0.9*s)}'; then
    if [ "${2:-0}" != 0 ] || [ -e "$qa/ab-daemon-restarted" ] || [ -e "$qa/ab-daemon-unresponsive" ]; then reasons="${reasons}short_duration,"
    else echo "QA_RECORDING_TIMELINE_COMPRESSED video_s=$dur session_s=$sess_s (clean stop; idle/navigation time has no frames)"; fi
  fi
  reasons=${reasons%,}
  if [ -n "$reasons" ]; then
    printf 'reason=%s video_s=%s session_s=%s\n' "$reasons" "$dur" "$sess_s" >"$out/recording/TRUNCATED"
    echo "QA_RECORDING_TRUNCATED reason=$reasons video_s=$dur session_s=$sess_s (video posts labelled partial; never complete)"
    qa_sl record_truncated reason="$reasons" video_s="$dur" session_s="$sess_s"
  else
    rm -f "$out/recording/TRUNCATED"
    echo "QA_RECORDING_COMPLETE video_s=$dur session_s=$sess_s"
    qa_sl record_complete video_s="$dur" session_s="$sess_s"
  fi
}
cleanup() {
  local rc=$? restore_rc=0 persona_restore_rc=0 seed_restore_rc=0
  trap - EXIT
  set +e
  [ -z "${health_pid:-}" ] || { kill "$health_pid" 2>/dev/null; wait "$health_pid" 2>/dev/null; }
  qa_sl cleanup_start rc=$rc
  if [ "$browser_lane" = agent-browser ] && [ -f "$qa/agent-browser.sh" ] && [ ! -f "$qa/browser.closed" ]; then
    qa_record_stop >>"$out/recording.log" 2>&1
    if [ -f "$qa/ab-daemon-unresponsive" ]; then
      echo 'AGENT_BROWSER_DAEMON_UNRESPONSIVE: skipped console/errors/network collection' | tee "$out/browser-console.log" "$out/browser-errors.log" >"$out/browser-network.log"
    else
    bash "$qa/agent-browser.sh" console >"$out/browser-console.log" 2>&1
    bash "$qa/agent-browser.sh" errors >"$out/browser-errors.log" 2>&1
    bash "$qa/agent-browser.sh" network requests >"$out/browser-network.log" 2>&1
    fi
    [ "$record" != 1 ] || qa_media >>"$out/recording.log" 2>&1 || true
    bash "$qa/agent-browser.sh" close >>"$out/browser.log" 2>&1
    touch "$qa/browser.closed"
  fi
  if [ "$signed_out_auth" = 1 ] && [ "$cleanup_only" != cleanup ]; then
    qa_egress_assert || rc=1
    curl --noproxy '*' -fsS --max-time 10 http://127.0.0.1:18477/events >"$out/signed-out-egress-events.json" || rc=1
  fi
  [ -z "$observer_pid" ] || kill -- "-$observer_pid" 2>/dev/null
  qa_browser_resources cleanup
  if [ -f "$qa/seed-project.json" ]; then
    qa_seed delete >"$out/restore-seed.log" 2>&1; seed_restore_rc=$?
    echo "$seed_restore_rc" >"$out/restore-seed.rc"
  fi
  if [ -f "$qa/persona-before.json" ]; then
    qa_persona restore >"$out/restore-persona.log" 2>&1; persona_restore_rc=$?
    echo "$persona_restore_rc" >"$out/restore-persona.rc"
  fi
  if [ -f "$qa/config-before.json" ]; then
    qa_config restore >"$out/restore-config.log" 2>&1; restore_rc=$?
    echo "$restore_rc" >"$out/restore-config.rc"
  fi
  if [ -n "$owned_pid" ]; then
    kill -- "-$owned_pid" 2>/dev/null
    wait "$owned_pid" 2>/dev/null
  fi
  for f in /tmp/preflight-*-slot"$slot"*.log /tmp/preflight-setup-receipt-slot"$slot".json; do
    [ ! -f "$f" ] || cp "$f" "$out/"
  done
  [ "$restore_rc" = 0 ] && [ "$persona_restore_rc" = 0 ] && [ "$seed_restore_rc" = 0 ] || rc=1
  [ "$cleanup_only" = cleanup ] || echo "$rc" >"$out/run.rc"
  qa_sl cleanup_end rc=$rc
  [ ! -s "$QA_SESSION_LOG" ] || cp -f "$QA_SESSION_LOG" "$out/session-log.jsonl"
  printf '%s\n' "$rc" >"$qa/runtime.done"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
if [ "$cleanup_only" = cleanup ]; then exit 0; fi
if listener; then
  [ "$signed_out_auth" = 0 ] || { echo 'Signed-out capture requires a fresh Next/browser lifecycle'; exit 3; }
  [ -s "$qa/app.env" ] && [ "$(cat "$qa/app.head")" = "$expected_head" ] &&
    [ "$(cat "$qa/app.persona" 2>/dev/null || echo admin)" = "$capture_persona" ] || {
    echo "Listener already exists on :$PORT without matching capture provenance/persona; refusing a second server"; exit 3;
  }
  source "$qa/app.env"
  [ "${QA_PERSONA_SUBJECT+x}" != x ] || [ "$SYNTH_E2E_SUBJECT" = "$QA_PERSONA_SUBJECT" ] || { echo 'Stored subject mismatch'; exit 3; }
  echo 'SERVER=reused' | tee "$out/server-lifecycle.log"
else
  export E2E_RUN_NAMESPACE="local-repro-slot${slot}-foreground-$(date +%s)-$$"
  export SYNTH_E2E_SUBJECT="synth-local-${E2E_RUN_NAMESPACE#local-repro-slot${slot}-}"
  if [ "${QA_PERSONA_SUBJECT+x}" = x ]; then export SYNTH_E2E_SUBJECT="$QA_PERSONA_SUBJECT"; fi
  qa_config snapshot >"$out/setup-config.log" 2>&1
  qa_config apply >>"$out/setup-config.log" 2>&1
  echo 'PREFLIGHT_CONVEX_DEPLOY_KEY=unset' >"$out/credential-scope.log"
  preflight_args=(--setup-only)
  [ "$capture_mode" = modal ] || preflight_args+=(--ui-capture-only)
  setup_started=$SECONDS
  echo "QA_SETUP_STARTED epoch=$(date +%s)" | tee -a "$out/timing.log"
  if [ "${QA_WORKSPACE_TEMPLATE_POOL:-0}" = 1 ]; then
    qa_workspace overlay >"$out/workspace-overlay.log" 2>&1
  fi
  # Canonical preflight configures the synthetic verifier before its first push.
  # Do not warm-push a fresh deployment before that configuration exists.
  if [ -n "${QA_CONVEX_DEPLOYMENT_EXPIRES_AT_MS:-}" ]; then
    echo 'QA_CONVEX_WARM_PUSH=skipped reason=synthetic-verifier-not-configured' | tee -a "$out/timing.log"
  fi
  env -u CONVEX_DEPLOY_KEY bash scripts/e2e/local-repro-preflight.sh "${preflight_args[@]}" >"$out/setup.log" 2>&1
  echo "QA_SETUP_SECONDS=$((SECONDS-setup_started))" | tee -a "$out/timing.log"
  grep -q '^SETUP_COMPLETE ' "$out/setup.log"
  if [ "$capture_mode" = modal ]; then
    [ -s "$qa/preflight-auth.env" ] || { echo 'Canonical synthetic verifier handoff missing'; exit 4; }
    source "$qa/preflight-auth.env"
    if [ "${QA_PERSONA_SUBJECT+x}" = x ]; then export SYNTH_E2E_SUBJECT="$QA_PERSONA_SUBJECT"; fi
    echo 'SYNTH_VERIFIER_HANDOFF=canonical-preflight' >>"$out/credential-scope.log"
  fi
  # setup-only imports preview-seed but never runs Playwright globalSetup.
  # Restore its targeted secondary edition from the TESTED HEAD, never copied tariffs.
  env -u NODE_OPTIONS bun - <<'PRICING' >"$out/setup-pricing.log" 2>&1
const fs = require('node:fs');
const { execFileSync } = require('node:child_process');
const dep = process.env.CONVEX_DEPLOYMENT;
if (!/^dev:[a-z0-9-]+$/.test(dep || '') || /^dev:(beloved-cardinal-|loyal-lynx-|prod-)/.test(dep) ||
    process.env.CONVEX_ADMIN_KEY?.split('|')[0] !== dep ||
    process.env.NEXT_PUBLIC_CONVEX_URL !== `https://${dep.slice(4)}.convex.cloud`) {
  throw new Error('Targeted pricing requires the guarded box dev deployment');
}
if (process.env.SYNTH_E2E_AUTH !== '1' || !process.env.SYNTH_E2E_SUBJECT?.startsWith('synth-') ||
    !process.env.SYNTH_JWT_ISSUER || !process.env.SYNTH_JWT_AUDIENCE) {
  throw new Error('Canonical synthetic identity setup must precede targeted pricing');
}
const file = './convex/migrations/seedProviderPricing.ts';
if (!fs.existsSync(file)) {
  console.log('TARGETED_PRICING=skipped reason=older-head-no-seeder');
} else {
  const seed = require(file);
  if (!seed.seedPricingRowsByVersion && !seed.SECONDARY_SEARCH_EDITION && !seed.SECONDARY_SEARCH_PRICING_ROWS) {
    console.log('TARGETED_PRICING=skipped reason=older-head-no-targeted-edition');
  } else if (!seed.SECONDARY_SEARCH_EDITION && !seed.SECONDARY_SEARCH_PRICING_ROWS) {
    console.log('TARGETED_PRICING=skipped reason=older-head-no-secondary-edition');
  } else {
    const version = seed.SECONDARY_SEARCH_EDITION, rows = seed.SECONDARY_SEARCH_PRICING_ROWS;
    if (!seed.seedPricingRowsByVersion || typeof version !== 'string' || !version ||
        !Array.isArray(rows) || !rows.length || rows.some(r => r.version !== version || typeof r.model !== 'string' || !r.model)) {
      throw new Error('Incomplete tested-head targeted pricing contract');
    }
    const args = { version, models: [...new Set(rows.map(r => r.model))] };
    const result = JSON.parse(execFileSync('npx', ['convex', 'run',
      'migrations/seedProviderPricing:seedPricingRowsByVersion', JSON.stringify(args)], {
      encoding: 'utf8', timeout: 90000, stdio: ['ignore', 'pipe', 'inherit'],
      env: { ...process.env, CONVEX_DEPLOY_KEY: process.env.CONVEX_ADMIN_KEY },
    }));
    if (!Number.isInteger(result.inserted) || result.inserted < 0 || !Number.isInteger(result.skipped) ||
        result.skipped < 0 || result.inserted + result.skipped !== rows.length) {
      throw new Error('Targeted pricing seeder did not restore all required rows');
    }
    console.log('TARGETED_PRICING=restored ' + JSON.stringify({ ...args, ...result }));
  }
}
PRICING
  cat "$out/setup-pricing.log"
  if [ "$capture_mode" = modal ]; then
    # Canonical preflight resolved and baked these URLs in its child shell.
    # Import the same authoritative pair for readiness, Next and browser routing.
    source "$qa/endpoints.env"
    cp "$qa/endpoints.json" "$out/modal-endpoints.json"
    # qaspeed 2026-10-30 (Rajiv C0ALZJHGE49 thread 1793299570.404429 "2-7 min of setup? that's too long"): the readiness
    # probe is ~157s (terms cold start). Run it alongside seed clone + Next boot; it is JOINED (and must pass) before the
    # browser starts, so readiness semantics are unchanged. Set QA_MODAL_READY_SERIAL=1 to restore the serial wait.
    if [ "${QA_MODAL_READY_SERIAL:-0}" = 1 ]; then
      python3 -I /root/qa/modal-lane.py ready >"$out/modal-readiness.log" 2>&1
    else
      ( python3 -I /root/qa/modal-lane.py ready >"$out/modal-readiness.log" 2>&1; echo $? >"$qa/modal-ready.rc" ) &
      modal_ready_pid=$!
      echo "QA_MODAL_READY_BACKGROUND pid=$modal_ready_pid epoch=$(date +%s)" >>"$out/timing.log"
    fi
  fi
  if [ "$seed_project" != none ]; then
    # Canonical preview-seed clone for the SAME synthetic subject the browser will use; deployment-scoped key only.
    qa_seed clone | tee "$out/seed-project.log"
    cp "$qa/seed-project.json" "$out/seed-project.json"
    # Start the editor with no writer lease on the disposable deployment (rule 48: one writer only).
    if [ -n "${QA_CONVEX_DEPLOYMENT_EXPIRES_AT_MS:-}" ]; then
      qa_seed clear-leases | tee -a "$out/seed-project.log"
    else
      echo 'QA_SEED_EDITOR_SESSIONS_CLEARED=skipped reason=not-disposable-convex' | tee -a "$out/seed-project.log"
    fi
    seeded_transcript=$(node -e 'process.stdout.write(JSON.parse(require("fs").readFileSync("/root/qa/seed-project.json","utf8")).transcriptId)')
    [[ "$seeded_transcript" =~ ^[a-z0-9]+$ ]] || { echo 'Seed transcript id invalid'; exit 4; }
    page_path="${page_path//\{SEEDED_TRANSCRIPT_ID\}/$seeded_transcript}"
    echo "$page_path" >"$qa/page-path"
  fi
  if [ "$persona" = unadmitted ]; then
    # A distinct, unseeded subject has no users row. Paused mode is the real production refusal boundary.
    qa_persona prepare | tee "$out/persona.log"
    export SYNTH_E2E_SUBJECT="${SYNTH_E2E_SUBJECT}-unadmitted"
  fi
  {
    printf 'export E2E_RUN_NAMESPACE=%q\n' "$E2E_RUN_NAMESPACE"
    printf 'export SYNTH_E2E_SUBJECT=%q\n' "$SYNTH_E2E_SUBJECT"
  } >"$qa/app.env"
  printf '%s\n' "$expected_head" >"$qa/app.head"
  printf '%s\n' "$capture_persona" >"$qa/app.persona"
fi
if [ "${QA_PERSONA_SUBJECT+x}" = x ]; then
  printf 'QA_PERSONA_SUBJECT=%s\n' "$SYNTH_E2E_SUBJECT" | tee "$out/persona-subject.log"
  cp "$qa/app.persona" "$out/app.persona"
fi
if [ "${QA_WORKSPACE_TEMPLATE_POOL:-0}" = 1 ] || [ "${QA_PERSONA_SUBJECT+x}" = x ]; then
  # After replace-all AND canonical persona provisioning; readback fails before Next/browser.
  # 2026-11-04: every workspace persona needs its users row + membership (preflight seeds only synth-local-*).
  node "$qa/workspace.test.cjs" | tee "$out/workspace-regression.log"
  qa_workspace provision | tee "$out/workspace-provision.log"
  # A second pass proves retries accept existing identities and do not duplicate the pool.
  qa_workspace provision | tee "$out/workspace-idempotency.log"
fi
if [ -n "$seeded_test" ]; then
  # Real canonical seed + real deployment: setup above ran the canonical preflight (seed import, Convex deploy, Modal
  # readiness when --requires-modal). The test gets the deployment env (.env.local + preflight handoffs); no stubs added.
  if [ -n "${modal_ready_pid:-}" ]; then
    wait "$modal_ready_pid" 2>/dev/null || true
    [ "$(cat "$qa/modal-ready.rc" 2>/dev/null)" = 0 ] || { echo 'QA Modal readiness failed; see modal-readiness.log'; exit 4; }
  fi
  grep -q '^SETUP_COMPLETE ' "$out/setup.log" || { echo 'Seeded test refused: canonical setup incomplete'; exit 4; }
  echo "QA_SEEDED_TEST_START epoch=$(date +%s) deployment=$CONVEX_DEPLOYMENT head=$expected_head cmd=$seeded_test" | tee "$out/seeded-test.log"
  st_rc=0
  if [[ "$seeded_test" = SEEDED_SCRIPT* ]]; then
    # Controller-copied local script (~/.heydonna/, bash -n checked): repo checkout cwd, .env.local (already exported
    # above) + preview-seed project ids + CONVEX_ADMIN_KEY in its environment.
    [ -s "$qa/seeded-script.sh" ] && bash -n "$qa/seeded-script.sh" || { echo 'Seeded script missing or fails bash -n'; exit 4; }
    read -r -a st_args <<<"${seeded_test#SEEDED_SCRIPT}"
    ( set -a; [ ! -f tests/e2e/fixtures/preview-seed-projects.sh ] || source tests/e2e/fixtures/preview-seed-projects.sh; set +a
      export CONVEX_ADMIN_KEY REMOTE_BOX=1 QA_SEEDED_TEST=1
      bash "$qa/seeded-script.sh" ${st_args[@]+"${st_args[@]}"} ) >>"$out/seeded-test.log" 2>&1 || st_rc=$?
  else
    env CONVEX_DEPLOY_KEY="$CONVEX_ADMIN_KEY" REMOTE_BOX=1 QA_SEEDED_TEST=1 bash -c "$seeded_test" >>"$out/seeded-test.log" 2>&1 || st_rc=$?
  fi
  grep -E 'Test Files|Tests +[0-9]|FAIL|✓|×|skipped|SEED_|TEST_EXIT' "$out/seeded-test.log" | sed 's/\x1b\[[0-9;]*m//g' | tail -40 >"$out/seeded-test-summary.txt"
  echo "QA_SEEDED_TEST_RC=$st_rc" | tee -a "$out/seeded-test.log" "$out/seeded-test-summary.txt"
  exit "$st_rc"
fi
[ ! -f "$qa/page-path" ] || export QA_CAPTURE_PATH="$(cat "$qa/page-path")"
case "$QA_CAPTURE_PATH" in *'{SEEDED_TRANSCRIPT_ID}'*) echo 'Seeded transcript placeholder was not resolved'; exit 4;; esac
# Deployment credentials are not available to Next or the browser callback, including server reuse.
if [ "$capture_mode" = modal ]; then unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET; fi
if [ "$signed_out_auth" = 1 ]; then
  # Use the existing persona snapshot/restore format for the isolated signup policy.
  env -u NODE_OPTIONS CONVEX_DEPLOY_KEY="$CONVEX_ADMIN_KEY" node <<'SIGNUP_OPEN'
const fs = require('node:fs'), {execFileSync} = require('node:child_process');
const run = (path, args = {}) => JSON.parse(execFileSync(process.execPath,
  ['node_modules/convex/bin/main.js', 'run', path, JSON.stringify(args)], {encoding:'utf8', timeout:60000}));
const key=process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY || '';
if (!/^pk_test_[A-Za-z0-9_-]+$/.test(key)) throw new Error('Test publishable key required');
const frontend=Buffer.from(key.slice(8),'base64url').toString().replace(/\$$/,'').toLowerCase();
if (!/(^|\.)(clerk\.com|clerk\.dev|clerk\.accounts\.dev|clerkstage\.dev|clerk\.heydonna\.law)$/.test(frontend)) throw new Error('Decoded frontend host not covered by canonical no-Clerk proxy');
const previous = run('config:querySignupMode');
if (!['open','paused'].includes(previous)) throw new Error('Unknown signup mode');
fs.writeFileSync('/root/qa/persona-before.json', JSON.stringify({signupMode:previous}), {flag:'wx',mode:0o600});
console.log('PERSONA_SIGNUP_MODE_BEFORE=' + previous);
run('config:setSignupModeInternal', {mode:'open'});
if (run('config:querySignupMode') !== 'open') throw new Error('Signup policy did not open');
SIGNUP_OPEN
  # No mint, curl auth config, cookies or storageState for this browser.
  unset AUTH_STATE TOKEN SYNTH_JWT_PRIVATE_KEY SYNTH_CONVEX_TOKEN
  export SYNTH_E2E_EGRESS_PROXY_HOST=127.0.0.1 SYNTH_E2E_EGRESS_PROXY_PORT=18477
  export SYNTH_E2E_EGRESS_COLLECTOR_URL=http://127.0.0.1:18477/event
  ss -ltnH 'sport = :18477' | grep -q . && { echo 'Observer port already owned'; exit 4; }
  setsid node scripts/ci/no-clerk-egress-proxy.mjs >"$out/egress-observer.log" 2>&1 & observer_pid=$!
  for ((i=0;i<30;i++)); do
    kill -0 "$observer_pid" 2>/dev/null || { echo 'Egress observer exited'; exit 4; }
    curl --noproxy '*' -fsS --max-time 1 http://127.0.0.1:18477/health >/dev/null 2>&1 && break
    sleep 1
  done
  curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:18477/health >/dev/null
else
# Mint the actual capture persona before readiness; verifier tuple and expected subject stay aligned.
export SYNTH_JWT_PRIVATE_KEY="$(cat "$SYNTH_JWT_PRIVATE_KEY_PATH")"
TOKEN=$(node scripts/synth-auth-mint.mjs --sub "$SYNTH_E2E_SUBJECT" --issuer "$SYNTH_JWT_ISSUER" \
  --audience "$SYNTH_JWT_AUDIENCE" --kid "$SYNTH_JWT_KID" --expires-in 3000)
unset SYNTH_JWT_PRIVATE_KEY
TOKEN="$TOKEN" node <<'STATE'
const fs = require('node:fs');
(async()=>{
 const t=process.env.TOKEN,s=process.env.SYNTH_E2E_SUBJECT;
 if(process.env.QA_CAPTURE_PERSONA==='unadmitted') {
  const response=await fetch(`${process.env.NEXT_PUBLIC_CONVEX_URL}/api/query`,{
   method:'POST',headers:{'Content-Type':'application/json',Authorization:`Bearer ${t}`},
   body:JSON.stringify({path:'users:ensureWorkspaceAdmission',args:{},format:'json'})
  });
  const result=await response.json();
  if(!response.ok || result.status!=='success' || result.value?.admitted!==false) throw new Error('Unadmitted persona did not reach the real admission-refused state');
  fs.writeFileSync(`${process.env.QA_CAPTURE_OUT}/persona-result.json`,JSON.stringify({persona:'unadmitted',admitted:false},null,2));
  console.log('PERSONA=unadmitted admitted=false (production query)');
 }
 const c=(name,value,httpOnly)=>({name,value,domain:'localhost',path:'/',expires:-1,httpOnly,secure:false,sameSite:'Lax'});
 fs.writeFileSync(process.env.AUTH_STATE,JSON.stringify({cookies:[c('synth_session',t,true),c('synth_identity',s,false)],
  origins:[{origin:process.env.QA_URL,localStorage:[{name:'synth_convex_token',value:t}]}]}),{mode:0o600});
 fs.writeFileSync('/root/qa/curl.conf',`cookie = "synth_identity=${s}; synth_session=${t}"\n`,{mode:0o600});
})().catch(()=>{console.error('Capture persona/auth-state preparation failed; see private setup and admission configuration');process.exitCode=1;});
STATE
unset TOKEN
fi
# The app config has no source-map env toggle. Override the resolved Next config
# only in this remote QA process; do not rewrite the tested app/config on disk.
if [ "$next_mode" = prod ]; then
  cat >"$qa/next-prod-config.cjs" <<'NEXT_PROD_CONFIG'
const { createRequire } = require('node:module');
const appRequire = createRequire(process.cwd() + '/package.json');
const configPath = appRequire.resolve('next/dist/server/config');
const original = appRequire(configPath);
require.cache[configPath].exports = {
  ...original,
  __esModule: true,
  default: async (...args) => ({
    ...await original.default(...args),
    productionBrowserSourceMaps: true,
  }),
};
NEXT_PROD_CONFIG
fi
ensure_next() {
if ! listener; then
  if [ "$next_mode" = prod ]; then : >"$qa/next-build.rc"; fi
  setsid env QA_NEXT_MODE="$next_mode" PATH="$PWD/node_modules/.bin:$PATH" node scripts/env.mjs run bash -c '
    if [ "$QA_CAPTURE_MODE" = modal ]; then source /root/qa/preflight-auth.env; fi
    source /root/qa/app.env
    # Keep Next 15 unchanged; Next 16 defaults to Turbopack despite webpack config.
    next_major=$(node -p '\''require("./node_modules/next/package.json").version.split(".")[0]'\'') || exit
    if [ "$QA_NEXT_MODE" = prod ]; then
      export NODE_ENV=production
      export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--require=/root/qa/next-prod-config.cjs"
      build_args=(build)
      if [ "$next_major" -ge 16 ]; then build_args+=(--webpack); fi
      build_rc=0
      next "${build_args[@]}" >"$QA_CAPTURE_OUT/next-build.log" 2>&1 || build_rc=$?
      printf "%s\n" "$build_rc" >/root/qa/next-build.rc
      [ "$build_rc" = 0 ] || exit 4
      exec next start -p "$PORT"
    fi
    next_args=(dev)
    if [ "$next_major" -ge 16 ]; then next_args+=(--webpack); fi
    exec next "${next_args[@]}" -p "$PORT"
  ' >"$out/next-server.log" 2>&1 &
  owned_pid=$!
  if [ "$next_mode" = prod ]; then
    echo "SERVER=started mode=prod pid=$owned_pid port=$PORT" | tee -a "$out/server-lifecycle.log"
    # Build time must not consume the unchanged listener-readiness window.
    while [ ! -s "$qa/next-build.rc" ]; do
      kill -0 "$owned_pid" 2>/dev/null || { echo 'Next exited before build completed'; exit 4; }
      sleep 1
    done
    [ "$(cat "$qa/next-build.rc")" = 0 ] || { echo 'Next build failed; see next-build.log'; exit 4; }
  else
    echo "SERVER=started pid=$owned_pid port=$PORT" | tee -a "$out/server-lifecycle.log"
  fi
else
  echo "SERVER=reused port=$PORT" | tee -a "$out/server-lifecycle.log"
fi
}
ensure_next
for ((i=0;i<90;i++)); do
  listener && break
  if [ -n "$owned_pid" ] && ! kill -0 "$owned_pid" 2>/dev/null; then echo 'Next exited before binding'; exit 4; fi
  sleep 1
done
listener || { echo 'Listener timeout'; exit 4; }
# Liveness is public. Auth proof is a separate fixed route, never selected by --page.
code=$(curl --noproxy '*' -s -o "$qa/liveness-body" -w '%{http_code}' --max-time 180 "$QA_URL$QA_LIVENESS_PATH")
echo "LIVENESS_HTTP=$code route=$QA_LIVENESS_PATH (public; not auth proof)" | tee "$out/readiness.log"
[ "$code" = 200 ] || { echo 'Public listener health failed'; exit 4; }
node -e 'const j=JSON.parse(require("fs").readFileSync("/root/qa/liveness-body","utf8")); if(!Object.hasOwn(j,"version")) process.exit(1)'
code=$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 180 "$QA_URL$QA_AUTH_PROOF_PATH")
echo "UNAUTHENTICATED_HTTP=$code auth_proof=$QA_AUTH_PROOF_PATH" | tee -a "$out/readiness.log"
case "$code" in
  401|403|302|303|307|308) ;;
  *) echo 'Auth-proof route did not deny the unauthenticated request'; exit 4;;
esac
if [ "$signed_out_auth" = 0 ]; then
code=$(curl --noproxy '*' -s -D "$qa/auth-proof-headers" -o "$qa/auth-proof-body" -w '%{http_code}' --max-time 180 \
  --config "$qa/curl.conf" "$QA_URL$QA_AUTH_PROOF_PATH")
echo "AUTHENTICATED_HTTP=$code auth_proof=$QA_AUTH_PROOF_PATH persona=$persona" | tee -a "$out/readiness.log"
# Keep raw headers/body private (they can contain session credentials); publish only bounded redacted diagnostics.
node <<'AUTH_DIAGNOSTIC'
const fs=require('node:fs');
const headers=fs.readFileSync('/root/qa/auth-proof-headers','utf8');
const location=headers.match(/^location:\s*(.+)$/im)?.[1]?.trim();
const result={status:Number(headers.match(/^HTTP\/\S+\s+(\d+)/m)?.[1]||0)};
if(location){const u=new URL(location,process.env.QA_URL);result.locationOrigin=u.origin;result.locationPath=u.pathname;result.queryOmitted=!!u.search;}
if(result.status===401){const body=fs.readFileSync('/root/qa/auth-proof-body','utf8');result.body=body==='Unauthorized'||body==='Unauthorized\n'?'Unauthorized':'<redacted nonstandard response body>';}
fs.writeFileSync(process.env.QA_CAPTURE_OUT+'/auth-readiness-diagnostic.json',JSON.stringify(result,null,2));
console.log('AUTH_READINESS_DIAGNOSTIC '+JSON.stringify(result));
AUTH_DIAGNOSTIC
[ "$code" = 200 ] || { echo 'Persona authenticated readiness failed'; exit 4; }
else
  code=$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 180 "$QA_URL$QA_CAPTURE_PATH")
  echo "SIGNED_OUT_HTTP=$code route=$QA_CAPTURE_PATH" | tee -a "$out/readiness.log"
  [ "$code" = 200 ] || { echo 'Signed-out auth route did not return 200'; exit 4; }
fi
ensure_next
if [ -n "${modal_ready_pid:-}" ]; then
  wait "$modal_ready_pid" 2>/dev/null || true
  echo "QA_MODAL_READY_JOINED epoch=$(date +%s) rc=$(cat "$qa/modal-ready.rc" 2>/dev/null || echo missing)" >>"$out/timing.log"
  [ "$(cat "$qa/modal-ready.rc" 2>/dev/null)" = 0 ] || { echo 'QA Modal readiness failed; see modal-readiness.log'; cat "$out/modal-readiness.log"; exit 4; }
fi
ss -ltnpH "sport = :$PORT" >"$out/listener.log"
qa_browser_resources before
if [ "$browser_lane" = agent-browser ]; then
  # Warm the exact authenticated browser target before native Page.navigate.
  # Keep the body/headers private: Next responses may contain session material.
  if [[ "$QA_CAPTURE_PATH" = /dashboard/editor/* ]]; then
    warm_started=$(date +%s)
    warm_code=$(curl --noproxy '*' -sS --max-time 180 --config "$qa/curl.conf" \
      -D "$qa/editor-warm-headers" -o "$qa/editor-warm-body" -w '%{http_code}' "$QA_URL$QA_CAPTURE_PATH")
    QA_WARM_STATUS="$warm_code" QA_WARM_STARTED="$warm_started" node <<'AB_WARM'
const fs=require('node:fs');
const url=new URL(process.env.QA_CAPTURE_PATH,process.env.QA_URL);
const body=fs.readFileSync('/root/qa/editor-warm-body');
const result={url:url.href,status:Number(process.env.QA_WARM_STATUS),bodyBytes:body.length,containsHtml:/<html[\s>]/i.test(body.toString('utf8')),startedAtEpoch:Number(process.env.QA_WARM_STARTED),finishedAtEpoch:Math.floor(Date.now()/1000)};
fs.writeFileSync(process.env.QA_CAPTURE_OUT+'/editor-warm.json',JSON.stringify(result,null,2));
if(url.hostname!=='localhost' || url.port!==process.env.PORT || result.status!==200 || !result.bodyBytes || !result.containsHtml) throw new Error('Exact editor URL did not warm with authenticated HTTP 200 HTML');
console.log('EDITOR_WARM '+JSON.stringify(result));
AB_WARM
    tail -n 30 "$out/next-server.log" >"$out/next-before-navigation.log"
  fi
  # Seeded editor warm (2026-11-03, PR #12608): --seed-project runs usually start on /dashboard/projects and then open
  # the cloned editor; compile that route now so the driver's Page.navigate does not hit a 60s cold compile.
  if [ "$signed_out_auth" = 0 ] && [ -s "$qa/seed-project.json" ]; then
    seed_tid=$(node -e 'const r=JSON.parse(require("fs").readFileSync("/root/qa/seed-project.json","utf8"));if(/^[a-z0-9]+$/.test(r.transcriptId||""))console.log(r.transcriptId)' || true)
    if [ -n "$seed_tid" ] && [ "/dashboard/editor/$seed_tid" != "$QA_CAPTURE_PATH" ]; then
      seed_warm_t0=$SECONDS
      seed_warm=$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 180 --config "$qa/curl.conf" "$QA_URL/dashboard/editor/$seed_tid" || true)
      echo "SEED_EDITOR_WARM status=${seed_warm:-000} seconds=$((SECONDS-seed_warm_t0))" | tee -a "$out/timing.log"
    fi
  fi
  # CLI-native domain policy covers navigation, subresources and WebSocket/EventSource.
  # Only the owned Convex/Modal targets and static assets are allowed; never Clerk.
  node <<'AB_CONFIG'
const fs=require('node:fs');
const deployment=process.env.CONVEX_DEPLOYMENT;
if(!/^dev:[a-z0-9-]+$/.test(deployment||'') || process.env.NEXT_PUBLIC_CONVEX_URL!==`https://${deployment.slice(4)}.convex.cloud`) throw new Error('Missing or mismatched owned Convex URL');
const hosts=new Set(['localhost','fonts.googleapis.com','fonts.gstatic.com','heydonna.law',`${deployment.slice(4)}.convex.cloud`,`${deployment.slice(4)}.convex.site`]);
for(const key of ['NEXT_PUBLIC_CONVEX_URL','NEXT_PUBLIC_CONVEX_SITE_URL','CONVEX_SITE_URL','NEXT_PUBLIC_R2_PUBLIC_URL','R2_PUBLIC_URL']) {
 const value=process.env[key]; if(!value) continue;
 const u=new URL(value); if(!['http:','https:'].includes(u.protocol)) throw new Error('Invalid QA asset URL');
 hosts.add(u.hostname);
}
if(process.env.QA_CAPTURE_MODE==='modal') {
 const identity=process.env.QA_MODAL_BOX_ID;
 if(!/^[a-f0-9]{12}$/.test(identity||'')) throw new Error('Missing Modal box identity');
 const endpoints=JSON.parse(fs.readFileSync('/root/qa/endpoints.json','utf8'));
 if(!Object.keys(endpoints).length) throw new Error('Empty owned Modal receipt');
 for(const [key,value] of Object.entries(endpoints)) {
  const u=new URL(value);
  if(!/^MODAL_[A-Z_]+_(ENDPOINT_)?URL$/.test(key) || u.protocol!=='https:' || u.username || u.password || u.port || u.search || u.hash || !u.hostname.endsWith('.modal.run') || !u.hostname.includes('-qa-'+identity+'-')) throw new Error('Invalid owned Modal endpoint');
  hosts.add(u.hostname);
 }
}
if([...hosts].some(h=>/(^|\.)(clerk\.com|clerk\.accounts\.dev|clerk\.heydonna\.(law|chat))$/.test(h))) throw new Error('Clerk domain forbidden');
// 0.26.0 treats storageState on ANY command as a clean-browser relaunch.
// Keep the shared config state-free; pass --state only on the initial open.
const signedOut=process.env.QA_SIGNED_OUT_AUTH==='1';
// Proxy sees EVERY attempted host (including redirects) before blocking Clerk;
// domain allowlisting would suppress attempted-request evidence upstream.
const config=signedOut
 ? {headed:false,proxy:'http://127.0.0.1:18477',proxyBypass:'<-loopback>'}
 // agent-browser 0.26.0 allowedDomains = CDP Fetch interception of every request; with --state loaded, disk-backed
 // File/Blob request bodies (wizard multipart PUT to R2) stall forever after a 204 preflight (status 0 @120s, 2026-10-27
 // PR #11420). Enforce the same host allowlist at the network layer instead: dead proxy for everything, direct only for hosts.
 : {headed:false,proxy:'http://127.0.0.1:9',proxyBypass:[...hosts].join(';')};
const allowedHosts=[...hosts];
fs.writeFileSync('/root/qa/agent-browser.json',JSON.stringify(config,null,2));
fs.writeFileSync(process.env.QA_CAPTURE_OUT+'/browser-allowed-domains.json',JSON.stringify(signedOut ? {policy:'no-Clerk observer proxy; zero attempted Clerk calls required'} : allowedHosts,null,2));
console.log('AGENT_BROWSER_ALLOWED_HOSTS '+JSON.stringify(signedOut ? null : allowedHosts)+(signedOut?'':' enforcement=proxy-bypass-list'));
fs.writeFileSync('/root/qa/browser.env',`export QA_URL=${JSON.stringify(process.env.QA_URL)}\nexport QA_CAPTURE_PATH=${JSON.stringify(process.env.QA_CAPTURE_PATH)}\nexport QA_SIGNED_OUT_AUTH=${JSON.stringify(process.env.QA_SIGNED_OUT_AUTH || '0')}\nexport AGENT_BROWSER_SESSION=${JSON.stringify(process.env.AGENT_BROWSER_SESSION)}\nexport PATH=${JSON.stringify(process.env.PATH)}\n`);
AB_CONFIG
  # Opt-in loading-view capture kind (CTO C0ALZJHGE49 thread 1793295948.815209, ts 1793308360.130339). Inert unless ONE
  # screenshot names `--capture-kind editor-processing-overlay`; the default editor guard in the wrapper is unchanged.
  # Expected identity comes only from the seed-clone receipt (owned by this run's synthetic subject), never from the page.
  rm -f "$qa/overlay-capture.json"
  if [ -f "$qa/seed-project.json" ] && [ "$signed_out_auth" = 0 ] && [ "$persona" = admin ]; then
    node <<'OVERLAY_CONFIG'
const fs=require('node:fs');
const r=JSON.parse(fs.readFileSync('/root/qa/seed-project.json','utf8'));
const subject=process.env.SYNTH_E2E_SUBJECT||'';
if(!/^[a-z0-9]+$/.test(r.transcriptId||'') || !/^synth-[A-Za-z0-9_-]+$/.test(subject) || r.subject!==subject) throw new Error('Overlay capture config: seed receipt subject/transcript mismatch');
const cfg={qaUrl:process.env.QA_URL,convexUrl:process.env.NEXT_PUBLIC_CONVEX_URL,head:process.env.QA_HEAD,subject,transcriptId:r.transcriptId};
fs.writeFileSync('/root/qa/overlay-capture.json',JSON.stringify(cfg,null,2),{mode:0o600});
console.log('OVERLAY_CAPTURE_KIND_AVAILABLE kind=editor-processing-overlay transcript='+cfg.transcriptId);
OVERLAY_CONFIG
  fi
  cat >"$qa/overlay-capture.cjs" <<'OVERLAY_CAPTURE'
// editor-processing-overlay capture kind: assertions only (read-only DOM probe + read-only owner query). No DOM writes.
'use strict';
const fs=require('node:fs'), crypto=require('node:crypto');
const dir=process.env.QA_OVERLAY_DIR||'/root/qa';
const KIND='editor-processing-overlay', STEPS=['Proofread','Speakers','Format'];
// Server stage -> stepper index; mirrors lib/editor/overlay-progress.ts selectPipelineOverlay + STAGE_STEPS.
const serverStageIndex=(status,stage)=>status==='proofreading'&&stage==='v0'?0:status==='proofreading'&&stage==='speaker_correcting'?1:status==='formatting'&&stage==='v1'?2:null;
function config(){
  const c=JSON.parse(fs.readFileSync(dir+'/overlay-capture.json','utf8'));
  if(!/^http:\/\/localhost:\d+$/.test(c.qaUrl||'') || !/^https:\/\/[a-z0-9-]+\.convex\.cloud$/.test(c.convexUrl||'') ||
     !/^[a-z0-9]+$/.test(c.transcriptId||'') || !/^synth-[A-Za-z0-9_-]+$/.test(c.subject||'')) throw new Error('Invalid overlay capture config');
  return c;
}
// Single source of truth for the DOM assertions; evaluated in the page, returns observations + failures.
const OBSERVE=String.raw`(expected) => {
  const failures=[];
  const vis=e=>!!e&&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
  const cookies=Object.fromEntries(document.cookie.split(/;\s*/).filter(Boolean).map(c=>{const i=c.indexOf('=');return [c.slice(0,i),decodeURIComponent(c.slice(i+1))];}));
  const route={url:location.href,origin:location.origin,path:location.pathname,search:location.search,expectedPath:'/dashboard/editor/'+expected.transcriptId};
  if(route.origin!==expected.qaUrl) failures.push('a:origin');
  if(route.path!==route.expectedPath) failures.push('a:route');
  const persona={identityCookie:cookies.synth_identity||null,tokenPresent:!!localStorage.getItem('synth_convex_token')};
  if(persona.identityCookie!==expected.subject) failures.push('a:persona');
  if(!persona.tokenPresent) failures.push('a:token');
  const skeletons=[...document.querySelectorAll('#editor-skeleton-layout, [data-panel-group-id="editor-skeleton-layout"]')];
  const revealed=[...document.querySelectorAll('[data-editor-ready="true"] .ProseMirror')].filter(vis);
  const skeleton={visible:skeletons.some(vis),revealedEditors:revealed.length};
  if(!skeleton.visible) failures.push('b:skeleton-not-visible');
  if(revealed.length) failures.push('b:editor-revealed');
  const overlays=[...document.querySelectorAll('[data-testid="processing-overlay"]')].filter(vis);
  const card=overlays.length===1?overlays[0].querySelector('[data-testid="processing-overlay-card"][role="dialog"][aria-modal="true"]'):null;
  const message=card?.querySelector('[data-testid="processing-message"]')?.textContent.trim()||null;
  const stepper=card?.querySelector('ol[data-testid="processing-stepper"]')||null;
  const items=stepper?[...stepper.querySelectorAll(':scope > li')]:[];
  const steps=items.map(li=>{const label=li.querySelectorAll(':scope > span')[1];return {id:li.getAttribute('data-testid'),state:li.getAttribute('data-state'),ariaCurrent:li.getAttribute('aria-current'),label:label?[...label.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim():null};});
  const currentIndex=steps.findIndex(s=>s.state==='current');
  const overlay={count:overlays.length,cardVisible:vis(card),message,compact:!!card&&(!!card.querySelector('[data-testid="processing-spinner"]')||/editing paused/.test(message||'')),stepperVisible:vis(stepper),steps,currentIndex,currentLabel:currentIndex>=0?steps[currentIndex].label:null};
  if(overlays.length!==1) failures.push('c:overlay-count-'+overlays.length);
  if(!overlay.cardVisible) failures.push('c:overlay-card');
  if(overlay.compact) failures.push('c:overlay-compact');
  if(!overlay.stepperVisible) failures.push('c:stepper-missing');
  if(steps.length!==3 || steps.some((s,i)=>s.id!=='processing-step-'+(i+1)||s.label!==expected.steps[i])) failures.push('c:not-three-step');
  if(steps.filter(s=>s.state==='current').length!==1 || currentIndex<0 || steps[currentIndex].ariaCurrent!=='step') failures.push('c:no-current-step');
  else if(steps.some((s,i)=>s.state!==(i<currentIndex?'done':i===currentIndex?'current':'upcoming'))) failures.push('c:step-states');
  const sidebar=document.querySelector('[data-testid="sidebar-container"]');
  const main=skeletons.find(vis)?.closest('main')||null;
  const shell={sidebarVisible:vis(sidebar),skeletonInsideMain:!!main,viewport:{width:innerWidth,height:innerHeight}};
  if(!shell.sidebarVisible) failures.push('d:sidebar');
  if(!shell.skeletonInsideMain) failures.push('d:main');
  return {route,persona,skeleton,overlay,shell,failures};
}`;
const expected=c=>({qaUrl:c.qaUrl,transcriptId:c.transcriptId,subject:c.subject,steps:STEPS});
async function server(){
  const c=config();
  const st=JSON.parse(fs.readFileSync(dir+'/auth-state.json','utf8'));
  const token=(st.origins||[]).flatMap(o=>o.localStorage||[]).find(e=>e.name==='synth_convex_token')?.value||'';
  let sub=null; try{sub=JSON.parse(Buffer.from(token.split('.')[1]||'','base64url').toString()).sub;}catch{}
  const res=await fetch(c.convexUrl+'/api/query',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},
    body:JSON.stringify({path:'editor:getEditorData',args:{transcriptId:c.transcriptId},format:'json'})});
  const j=await res.json().catch(()=>({}));
  const v=j.status==='success'?j.value:null, t=v?.transcript;
  const e={httpStatus:res.status,tokenSubjectMatches:sub===c.subject,accessLevel:v?.accessLevel??null,transcriptIdMatches:t?._id===c.transcriptId,
    status:t?.status??null,pipelineStage:t?.pipelineStage??null,serverStageIndex:t?serverStageIndex(t.status,t.pipelineStage):null};
  e.ok=res.ok&&e.tokenSubjectMatches&&e.accessLevel==='owner'&&e.transcriptIdMatches&&e.serverStageIndex!==null;
  return e;
}
function readJson(p){try{return JSON.parse(fs.readFileSync(p,'utf8'));}catch{return null;}}
function verify(shot,ev){
  const c=config(), failures=[];
  const pre=readJson(ev+'/pre.json'), post=readJson(ev+'/post.json'), sb=readJson(ev+'/server-before.json'), sa=readJson(ev+'/server-after.json');
  const obs={};
  for(const [phase,raw] of [['pre',pre],['post',post]]){
    const r=raw?.success?raw.data?.result:null;
    if(!r){failures.push(phase+':probe-missing');continue;}
    obs[phase]=r; for(const f of r.failures||[]) failures.push(phase+':'+f);
    if(r.shell?.viewport?.width!==1440||r.shell?.viewport?.height!==900) failures.push(phase+':viewport');
  }
  for(const [phase,s] of [['server-before',sb],['server-after',sa]]) if(!s?.ok) failures.push(phase+':owner-or-stage');
  const idx=[obs.pre?.overlay?.currentIndex,obs.post?.overlay?.currentIndex,sb?.serverStageIndex,sa?.serverStageIndex];
  if(!idx.every(i=>Number.isInteger(i)&&i===idx[0])) failures.push('c:stage-mismatch '+JSON.stringify(idx));
  let png=null;
  try{const b=fs.readFileSync(shot);if(b.subarray(0,8).equals(Buffer.from([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a])))png={width:b.readUInt32BE(16),height:b.readUInt32BE(20),sha256:crypto.createHash('sha256').update(b).digest('hex')};}catch{}
  if(!png||png.width!==1440||png.height!==900) failures.push('png');
  const p=obs.pre||{};
  const record={kind:KIND,head:c.head,shot,ok:!failures.length,failures,png,
    assertions:{
      a_identity_route:{expectedSubject:c.subject,expectedTranscriptId:c.transcriptId,route:p.route,persona:p.persona,serverBefore:sb,serverAfter:sa},
      b_skeleton_visible:p.skeleton,
      c_full_overlay_current_step:{pre:p.overlay,postCurrentIndex:obs.post?.overlay?.currentIndex??null},
      d_shell:p.shell}};
  fs.appendFileSync((process.env.QA_OVERLAY_OUT||dir+'/out')+'/overlay-capture-evidence.jsonl',JSON.stringify(record)+'\n');
  console.log(record.ok?`OVERLAY_CAPTURE_OK shot=${shot} step=${p.overlay?.currentLabel} sha256=${png.sha256}`:`OVERLAY_CAPTURE_REFUSED shot=${shot} failures=${failures.join(',')}`);
  return record.ok;
}
(async()=>{
  const [mode,...a]=process.argv.slice(2);
  if(mode==='predicate') process.stdout.write(`(${OBSERVE})(${JSON.stringify(expected(config()))}).failures.length===0`);
  else if(mode==='probe') process.stdout.write(`(${OBSERVE})(${JSON.stringify(expected(config()))})`);
  else if(mode==='server'){const e=await server();console.log(JSON.stringify(e));process.exitCode=e.ok?0:1;}
  else if(mode==='verify') process.exitCode=verify(a[0],a[1])?0:1;
  else throw new Error('overlay-capture: predicate|probe|server|verify');
})().catch(e=>{console.error('OVERLAY_CAPTURE_ERROR '+e.message);process.exitCode=2;});
OVERLAY_CAPTURE
  cat >"$qa/agent-browser.sh" <<'AB_WRAPPER'
#!/usr/bin/env bash
# Same persistent remote daemon and synthetic state for every SSH gesture.
set -euo pipefail
source /root/qa/browser.env
for arg in "$@"; do
  case "$arg" in *--headed*|*HEADED=1*|*HEADED=true*) echo 'Headless only' >&2; exit 2;; esac
done
[ "${1:-}" != chat ] || { echo 'Use app UI; agent-browser AI chat is not permitted' >&2; exit 2; }
# Session log (2026-10-30): one JSONL row per wrapper call (box clock, verb only; never args, URLs, text or tokens).
ab_t0=$(date +%s%3N); AB_EAGAIN=0; ab_kind=none
for _a in "$@"; do case "$_a" in --*) ;; *) [[ "$_a" =~ ^[a-z_][a-z_-]{0,23}$ ]] && { ab_kind=$_a; break; };; esac; done
ab_src=driver; [ -z "${QA_AB_INTERNAL:-}" ] || ab_src=internal; [ -z "${QA_AB_PROBE:-}" ] || ab_src=probe
[ "$ab_src" = probe ] || [ ! -d /root/qa ] || : >"/root/qa/ab-inflight.$$" 2>/dev/null || true
ab_done() {  # never changes the command's exit status
  local rc=$? now; now=$(date +%s%3N); rm -f "/root/qa/ab-inflight.$$" 2>/dev/null
  if [[ "$now" =~ ^[0-9]+$ && "$ab_t0" =~ ^[0-9]+$ ]] && [ -f /root/qa/session-log.jsonl ] &&
     [ "$(stat -c %s /root/qa/session-log.jsonl 2>/dev/null || echo 0)" -lt 4194304 ]; then
    printf '{"t_ms":%s,"ev":"cmd","src":"%s","kind":"%s","start_ms":%s,"dur_ms":%s,"rc":%s,"eagain":%s}\n' \
      "$now" "$ab_src" "$ab_kind" "$ab_t0" "$((now-ab_t0))" "$rc" "$AB_EAGAIN" >>/root/qa/session-log.jsonl 2>/dev/null || true
  fi
  exit "$rc"
}
trap ab_done EXIT
if [ "${1:-}" = mark ]; then
  [[ "${2:-}" =~ ^[A-Za-z0-9._-]+$ && "${3:-}" =~ ^[a-z0-9-]+$ ]] || { echo 'mark AC_ID slug' >&2; exit 2; }
  printf '%s\t%s\t%s\n' "$(date +%s%3N)" "$2" "$3" >>/root/qa/ac-marks.tsv; echo "AC_MARK $2 $3"; exit 0
fi
# Relative open (2026-11-03, PR #12537): `open /dashboard` became https://dashboard/, which the dead-proxy host
# allowlist reports as net::ERR_PROXY_CONNECTION_FAILED. App paths resolve against QA_URL.
if [ "${1:-}" = open ] && [[ "${2:-}" = /* ]]; then set -- open "$QA_URL$2" "${@:3}"; fi
if [ "${QA_SIGNED_OUT_AUTH:-0}" = 1 ]; then
  for arg in "$@"; do
    case "$arg" in --state*|--profile*|--session*|--headers*|--proxy*|--config*|--extension*|--cdp*|--auto-connect*) echo 'Signed-out browser refuses identity/transport overrides' >&2; exit 2;; esac
  done
  case "${1:-} ${2:-}" in 'auth '*|'state '*|'cookies set'|'set credentials'|'set headers'|'storage local'|'storage session') echo 'Signed-out browser refuses credential/storage commands; use read-only eval for inspection' >&2; exit 2;; esac
  if [ "${1:-}" = open ]; then
    case "${2:-}" in "$QA_URL/sign-in"|"$QA_URL/sign-up"|"$QA_URL/community") ;; *) echo 'Signed-out capture only supports exact auth/community routes' >&2; exit 2;; esac
  fi
fi
ab_raw() { env -i HOME=/root PATH="$PATH" AGENT_BROWSER_DEFAULT_TIMEOUT=180000 agent-browser --session "$AGENT_BROWSER_SESSION" --config /root/qa/agent-browser.json "$@"; }
# Daemon EAGAIN recovery (2026-10-30, PR #11876 retry1: "Could not configure browser: Failed to read: Resource temporarily
# unavailable (os error 11) (after 5 retries - daemon may be busy or unresponsive)" left the box idle until the 600s window).
# ONE daemon restart per run (re-opens the capture path with the synthetic state). A second EAGAIN, a failed restart, or an
# EAGAIN after the run already finished writes the typed failure AGENT_BROWSER_DAEMON_UNRESPONSIVE to /root/qa/browser-finish
# (the runtime then exits 69 at once). A driver command is never silently retried on the fresh page: it exits 75 so the
# driver re-runs the step knowingly; runtime-internal setup commands (QA_AB_INTERNAL=1) are retried once.
ab_eagain() { [ "$1" != 0 ] && grep -qE 'os error (11|35)\)|Resource temporarily unavailable|daemon may be busy or unresponsive' "$2"; }
ab_note() { echo "$1" >&2; echo "$1" >>/root/qa/out/browser.log 2>/dev/null || true; }
ab_unresponsive() {
  touch /root/qa/ab-daemon-unresponsive
  [ -e /root/qa/browser-finish ] || printf 'AGENT_BROWSER_DAEMON_UNRESPONSIVE\n' >/root/qa/browser-finish
  ab_note "AGENT_BROWSER_DAEMON_UNRESPONSIVE reason=$1 epoch=$(date +%s) (typed failure; run ends now, not a pass)"
  exit 69
}
ab_kill_tree() { local c; for c in $(pgrep -P "$1" 2>/dev/null); do ab_kill_tree "$c"; done; kill -KILL "$1" 2>/dev/null || true; }
ab_restart_daemon() {
  local d=/root/.agent-browser pid
  timeout 20 env -i HOME=/root PATH="$PATH" agent-browser --session "$AGENT_BROWSER_SESSION" close >/dev/null 2>&1 || true
  pid=$(cat "$d/$AGENT_BROWSER_SESSION.pid" 2>/dev/null || true)
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then ab_kill_tree "$pid"
  else pkill -KILL -f 'agent-browser-linux' 2>/dev/null || true; fi   # isolated box: this run owns the only daemon
  rm -f "$d/$AGENT_BROWSER_SESSION.pid" "$d/$AGENT_BROWSER_SESSION.sock"
  sleep 1
  local url="$QA_URL$QA_CAPTURE_PATH"
  if [ "${QA_SIGNED_OUT_AUTH:-0}" = 1 ]; then ab_raw open "$url" >/dev/null 2>&1 || return 1
  else [ -s /root/qa/auth-state.json ] && ab_raw --state /root/qa/auth-state.json open "$url" >/dev/null 2>&1 || return 1; fi
  ab_raw set viewport 1440 900 >/dev/null 2>&1
}
ab() {
  local o e rc=0
  o=$(mktemp /root/qa/ab-out-XXXXXX); e=$(mktemp /root/qa/ab-err-XXXXXX)
  ab_raw "$@" >"$o" 2>"$e" || rc=$?
  if ab_eagain "$rc" "$e"; then
    AB_EAGAIN=1
    cat "$e" >&2; rm -f "$o" "$e"
    [ ! -e /root/qa/browser-finish ] || ab_unresponsive after-finish
    [ ! -e /root/qa/ab-daemon-restarted ] || ab_unresponsive second-eagain
    touch /root/qa/ab-daemon-restarted
    ab_note "AGENT_BROWSER_DAEMON_RESTART epoch=$(date +%s) cmd=${1:-} (once per run; page reset to $QA_CAPTURE_PATH; any active recording is cut here)"
    ab_restart_daemon || ab_unresponsive restart-failed
    if [ -z "${QA_AB_INTERNAL:-}" ]; then
      ab_note 'AGENT_BROWSER_DAEMON_RESTARTED page reset to the capture path; prior page state lost. Re-run this step (exit 75).'
      exit 75
    fi
    o=$(mktemp /root/qa/ab-out-XXXXXX); e=$(mktemp /root/qa/ab-err-XXXXXX); rc=0
    ab_raw "$@" >"$o" 2>"$e" || rc=$?
    if ab_eagain "$rc" "$e"; then cat "$e" >&2; rm -f "$o" "$e"; ab_unresponsive retry-eagain; fi
  fi
  cat "$o"; cat "$e" >&2; rm -f "$o" "$e"
  return "$rc"
}
# Watchdog verbs (runtime health loop only). __probe: cheap read-only liveness check, never restarts on its own.
# __watchdog-restart: the SAME one-restart budget as the EAGAIN path, taken as soon as the loop sees a wedge.
if [ "${1:-}" = __probe ]; then
  timeout 20 env -i HOME=/root PATH="$PATH" AGENT_BROWSER_DEFAULT_TIMEOUT=15000 agent-browser --session "$AGENT_BROWSER_SESSION" --config /root/qa/agent-browser.json get url >/dev/null 2>&1
  exit $?
fi
if [ "${1:-}" = __watchdog-restart ]; then
  [ ! -e /root/qa/browser-finish ] || exit 0
  [ ! -e /root/qa/ab-daemon-restarted ] || ab_unresponsive watchdog-second-wedge
  touch /root/qa/ab-daemon-restarted
  ab_note "AGENT_BROWSER_DAEMON_RESTART epoch=$(date +%s) cmd=watchdog (daemon wedged while idle; once per run; page reset to $QA_CAPTURE_PATH)"
  ab_restart_daemon || ab_unresponsive watchdog-restart-failed
  touch /root/qa/ab-watchdog-restarted
  exit 0
fi
# A watchdog restart reset the page while the driver was idle: tell the driver once, exactly like the EAGAIN path.
if [ "$ab_src" = driver ] && [ "${1:-}" != mark ] && [ -e /root/qa/ab-watchdog-restarted ]; then
  rm -f /root/qa/ab-watchdog-restarted
  ab_note 'AGENT_BROWSER_DAEMON_RESTARTED (watchdog) page reset to the capture path; prior page state lost. Re-run this step (exit 75).'
  exit 75
fi
# Deferred ProofShot start (2026-10-30): the FIRST driver command (not runtime-internal, not `mark`) asks the runtime to
# start recording and waits for it, so the video begins at the first real QA action, not during setup or driver latency.
if [ -z "${QA_AB_INTERNAL:-}" ] && [ "${1:-}" != mark ] && [ -f /root/qa/record.pending ]; then
  touch /root/qa/record.request
  for _ in $(seq 420); do [ -f /root/qa/record.pending ] || break; sleep 1; done
  [ ! -f /root/qa/record.pending ] || echo 'QA_RECORDING_WARN deferred start did not finish in 420s; continuing' >&2
fi
# The ONLY alternative to the editor guard below: `screenshot --capture-kind editor-processing-overlay /root/qa/out/NAME.png`.
# Explicit per screenshot, never inferred; asserts route/persona/owner, visible skeleton, full 3-step overlay + current step, shell.
case " $* " in *' --capture-kind'*)
  [ "${1:-}" = screenshot ] && [ "${2:-}" = --capture-kind ] && [ $# -eq 4 ] || { echo 'usage: screenshot --capture-kind editor-processing-overlay /root/qa/out/NAME.png' >&2; exit 2; }
  [ "$3" = editor-processing-overlay ] || { echo "Unknown capture kind: $3" >&2; exit 2; }
  [ "${QA_SIGNED_OUT_AUTH:-0}" = 0 ] || { echo 'Loading-view capture requires the authenticated synthetic persona' >&2; exit 2; }
  [ -s /root/qa/overlay-capture.json ] || { echo 'Loading-view capture requires --seed-project with the admin persona (no seeded owned transcript)' >&2; exit 2; }
  shot="$4"
  [[ "$shot" =~ ^/root/qa/out/[A-Za-z0-9._-]+\.png$ ]] && [ ! -e "$shot" ] || { echo 'Loading-view capture needs a new /root/qa/out/NAME.png' >&2; exit 2; }
  oc() { node /root/qa/overlay-capture.cjs "$@"; }
  ev=$(mktemp -d /root/qa/overlay-ev-XXXXXX)
  oc server >"$ev/server-before.json" || true
  ab set viewport 1440 900 >/dev/null
  waited=0; ab wait --fn "$(oc predicate)" >/dev/null && waited=1
  ab --json eval "$(oc probe)" >"$ev/pre.json" || true
  if [ "$waited" = 1 ]; then
    ab screenshot "$shot"
    ab --json eval "$(oc probe)" >"$ev/post.json" || true
    oc server >"$ev/server-after.json" || true
  fi
  if ! oc verify "$shot" "$ev"; then
    [ ! -e "$shot" ] || mv "$shot" "$shot.refused"
    exit 3
  fi
  printf '%s\t%s\t%s\n' "$(date +%s%3N)" "$(basename "$shot" .png)" shot >>/root/qa/ac-marks.tsv
  exit 0;;
esac
if [ "${1:-}" = screenshot ]; then
  if [ -e /root/qa/recording.active ]; then
    for arg in "$@"; do case "$arg" in --full|-f) echo 'screenshot --full stops the active ProofShot recording (CDP screencast); app pages scroll inside a 1440x900 shell, so take a plain screenshot' >&2; exit 2;; esac; done
  fi
  # A requested editor must never pass this gate after redirecting to /admin.
  if [[ "$QA_CAPTURE_PATH" = /dashboard/editor/* ]]; then
    ab wait --fn "location.origin === '$QA_URL' && location.pathname === '$QA_CAPTURE_PATH'" >/dev/null
  fi
  ab wait --fn '(() => {
    if (!/^\/dashboard\/editor\/[^/]+/.test(location.pathname)) return true;
    const visible=e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
    return ![...document.querySelectorAll("#editor-skeleton-layout, [data-panel-group-id=editor-skeleton-layout], .editor-scroll-container + .absolute.inset-0:has(.skeleton-shimmer)")].some(visible) && [...document.querySelectorAll("[data-editor-ready=true]:not([inert]) .ProseMirror")].some(e=>visible(e)&&!e.closest("[inert]")&&e.textContent.trim().length>0);
  })()' >/dev/null
  if [ "${QA_SIGNED_OUT_AUTH:-0}" = 1 ]; then
    ab wait --fn "location.origin === '$QA_URL' && ['/sign-in','/sign-up','/community'].includes(location.pathname)" >/dev/null
    ab wait --fn '(() => {
      if (location.pathname === "/community") {
        const heading=[...document.querySelectorAll("h1")].some(e=>e.checkVisibility() && e.textContent.trim()==="Off The Record Lounge");
        return heading && ![...Object.keys(localStorage),...Object.keys(sessionStorage)].some(k=>/synth|clerk|token|session/i.test(k)) && !document.cookie;
      }
      if (!/^\/sign-(in|up)$/.test(location.pathname)) return false;
      const visible=e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
      const email=[...document.querySelectorAll("input[type=email]")].some(visible);
      const primary=[...document.querySelectorAll("button")].some(e=>visible(e)&&/^(sign in|create account)$/i.test(e.textContent.trim()));
      return email && primary && !document.body.innerText.includes("Authentication Unavailable") && ![...Object.keys(localStorage),...Object.keys(sessionStorage)].some(k=>/synth|clerk|token|session/i.test(k)) && !document.cookie;
    })()' >/dev/null
    # Keep operator-selected narrow viewports; never silently reset to desktop.
  else
    ab set viewport 1440 900 >/dev/null
  fi
  # Every screenshot is also an AC checkpoint marker; the posted PNG is extracted from the video at this instant.
  ab "$@"
  shot_label=''
  for arg in "$@"; do case "$arg" in *.png) shot_label=$(basename "$arg" .png); shot_label=$(printf '%s' "$shot_label" | tr -c 'A-Za-z0-9._-' '-');; esac; done
  [ -z "$shot_label" ] || printf '%s\t%s\t%s\n' "$(date +%s%3N)" "$shot_label" shot >>/root/qa/ac-marks.tsv
  exit 0
fi
# Cold-route warm (2026-11-03, PR #12608): a first-hit Next dev compile of /dashboard/editor took 61s, past agent-browser's
# 30s CDP Page.navigate cap; the timed-out page kept loading and wedged the daemon (EAGAIN -> exit 69). Compile the
# route over HTTP first (same synthetic cookies; signed-out stays cookieless), then navigate. Never changes rc.
if [ "${1:-}" = open ] && [[ "${2:-}" = "$QA_URL" || "${2:-}" = "$QA_URL"/* ]]; then
  ab_warm_cfg=(); [ "${QA_SIGNED_OUT_AUTH:-0}" = 1 ] || [ ! -s /root/qa/curl.conf ] || ab_warm_cfg=(--config /root/qa/curl.conf)
  curl --noproxy '*' -s -o /dev/null --max-time 170 "${ab_warm_cfg[@]}" "$2" >/dev/null 2>&1 || true
fi
ab "$@"
AB_WRAPPER
  chmod 700 "$qa/agent-browser.sh"
  # Runtime-internal wrapper calls (setup, recording, cleanup) may be retried once after a daemon restart; driver calls
  # arrive over SSH without this and are never silently retried. Per-run EAGAIN/record markers start clean.
  export QA_AB_INTERNAL=1
  rm -f "$qa/ab-daemon-restarted" "$qa/ab-daemon-unresponsive" "$qa/record.pending" "$qa/record.request"
  ab() { bash "$qa/agent-browser.sh" "$@"; }
  # The native domain policy rejects hostless about:blank navigation. Start at
  # the authenticated target, then resize the already-open browser (skill contract).
  printf 'AGENT_BROWSER_NAVIGATE_STARTED epoch=%s target=%s\n' "$(date +%s)" "$QA_URL$QA_CAPTURE_PATH" >"$out/browser.log"
  if [ "$signed_out_auth" = 1 ]; then
    # Unique ephemeral daemon; no persistent profile, auth vault or state loading.
    ab open "$QA_URL$QA_CAPTURE_PATH" >>"$out/browser.log" 2>&1
    ab set viewport 1440 900 >>"$out/browser.log" 2>&1
    ab screenshot "$out/capture.png" >>"$out/browser.log" 2>&1
    ab snapshot >"$out/snapshot.txt"
    ab --json eval '({url:location.href,width:innerWidth,height:innerHeight})' >"$out/browser-observed.json"
    ab --json cookies | node -e 'let s="";process.stdin.on("data",d=>s+=d);process.stdin.on("end",()=>{const j=JSON.parse(s);if(!j.success || !Array.isArray(j.data.cookies) || j.data.cookies.length)throw Error("Signed-out browser must have zero cookies");console.log("{\"cookies\":[]}");})' >"$out/browser-auth.json"
    qa_egress_assert
    node <<'SIGNED_OUT_READY'
const fs=require('node:fs'), out=process.env.QA_CAPTURE_OUT;
const observed=JSON.parse(fs.readFileSync(out+'/browser-observed.json','utf8'));
const actual=observed.data?.result;
if(!observed.success || actual?.url!==process.env.QA_URL+process.env.QA_CAPTURE_PATH || actual.width!==1440 || actual.height!==900) throw Error('Signed-out route or actual viewport mismatch');
const png=fs.readFileSync(out+'/capture.png');
if(png.readUInt32BE(16)!==1440 || png.readUInt32BE(20)!==900) throw Error('Wrong native PNG dimensions');
const result={head:process.env.QA_HEAD,mode:'signed-out-auth',url:process.env.QA_URL+process.env.QA_CAPTURE_PATH,viewport:{width:1440,height:900},storageState:'none',syntheticLogin:false,processing:false};
for(const path of [out+'/browser-result.json','/root/qa/browser-ready.json']) fs.writeFileSync(path,JSON.stringify(result,null,2));
SIGNED_OUT_READY
  else
  ab --state "$AUTH_STATE" open "$QA_URL$QA_CAPTURE_PATH" >>"$out/browser.log" 2>&1
  ab set viewport 1440 900 >>"$out/browser.log" 2>&1
  # Verify state without ever logging cookie/token values.
  ab --json cookies | node -e 'let s="";process.stdin.on("data",d=>s+=d);process.stdin.on("end",()=>{const j=JSON.parse(s);if(!j.success)throw new Error("Cookie inspection failed");const cookies=(j.data.cookies||[]).map(({name,domain,httpOnly})=>({name,domain,httpOnly}));if(!["synth_session","synth_identity"].every(n=>cookies.some(c=>c.name===n)))throw new Error("Synthetic cookies missing");console.log(JSON.stringify({cookies},null,2));})' >"$out/browser-auth.json"
  ab wait main >>"$out/browser.log" 2>&1
  if [ "$QA_CAPTURE_PATH" = /admin ]; then ab wait --text 'Admin Overview' >>"$out/browser.log" 2>&1; fi
  if [ "$persona" = unadmitted ] && [ "$QA_CAPTURE_PATH" = / ]; then
    ab wait --fn 'location.pathname === "/" && [...document.querySelectorAll("h1,h2")].some(e=>/^Dashboard (unavailable|admission required)$/.test(e.textContent))' >>"$out/browser.log" 2>&1
  fi
  ab wait --fn "location.origin === '$QA_URL' && !location.pathname.startsWith('/sign-in') && !location.pathname.startsWith('/sign-up')" >>"$out/browser.log" 2>&1
  ab screenshot "$out/capture.png" >>"$out/browser.log" 2>&1
  ab snapshot >"$out/snapshot.txt"
  ab get url >"$out/browser-url.txt"
  ab --json eval '(() => ({url:location.href,viewport:{width:innerWidth,height:innerHeight},syntheticTokenPresent:!!localStorage.getItem("synth_convex_token"),editorCharacters:document.querySelector("[data-editor-ready=true]:not([inert]) .ProseMirror")?.textContent.trim().length||0}))()' >"$out/browser-observed.json"
  node <<'AB_READY'
const fs=require('node:fs'),out=process.env.QA_CAPTURE_OUT;
const url=fs.readFileSync(out+'/browser-url.txt','utf8').trim();
if(new URL(url).origin!==process.env.QA_URL || /^\/sign-(in|up)(\/|$)/.test(new URL(url).pathname)) throw new Error('Browser left authenticated app');
if(process.env.QA_CAPTURE_PATH.startsWith('/dashboard/editor/') && new URL(url).pathname!==new URL(process.env.QA_CAPTURE_PATH,process.env.QA_URL).pathname) throw new Error('Browser did not remain on the requested cloned editor');
const observed=JSON.parse(fs.readFileSync(out+'/browser-observed.json','utf8'));
if(!observed.success) throw new Error('Browser observation failed');
const actual=observed.data.result;
const png=fs.readFileSync(out+'/capture.png');
const dimensions={width:png.readUInt32BE(16),height:png.readUInt32BE(20)};
if(actual.url!==url || actual.viewport.width!==1440 || actual.viewport.height!==900 || dimensions.width!==1440 || dimensions.height!==900 || !actual.syntheticTokenPresent) throw new Error('Browser state or real viewport did not persist');
if(process.env.QA_CAPTURE_PATH.startsWith('/dashboard/editor/') && !actual.editorCharacters) throw new Error('Editor has no transcript text');
const result={head:process.env.QA_HEAD,browser:'agent-browser',version:'0.26.0',headless:true,url,viewport:actual.viewport,pngDimensions:dimensions,editorCharacters:actual.editorCharacters,storageState:'canonical synthetic JWT loaded once',captureMode:process.env.QA_CAPTURE_MODE,persona:process.env.QA_CAPTURE_PERSONA};
fs.writeFileSync(out+'/browser-result.json',JSON.stringify(result,null,2));
fs.writeFileSync('/root/qa/browser-ready.json',JSON.stringify(result,null,2));
AB_READY
  fi
  qa_sl browser_ready
  qa_health_loop </dev/null >/dev/null 2>&1 & health_pid=$!
  # Signed-out runs record too (2026-11-03, PR #12591): ProofShot uses the same agent-browser config (no-Clerk
  # observer proxy), so the egress assertion still covers the recorded tab.
  if [ "$record" = 1 ]; then
    if [ "$interactive" = 1 ]; then
      # Deferred (2026-10-30): setup (AUTHENTICATED_HTTP=200 + AGENT_BROWSER_READY) stays out of the video. The wrapper's
      # first driver command raises record.request; the loop below starts ProofShot and clears record.pending.
      touch "$qa/record.pending"
      echo "QA_RECORDING_DEFERRED until first driver command (epoch=$(date +%s))" | tee -a "$out/recording.log"
    else
      qa_record_start >>"$out/recording.log" 2>&1 || echo 'QA_RECORDING_WARN start failed; screenshots unaffected' | tee -a "$out/recording.log"
    fi
  fi
  echo 'AGENT_BROWSER_READY commands: bash /root/qa/agent-browser.sh snapshot -i | click @eN | fill @eN TEXT | screenshot /root/qa/out/NAME.png'
  if [ "$interactive" = 1 ]; then
    echo 'AGENT_BROWSER_INTERACTIVE finish: printf "0\n" > /root/qa/browser-finish (nonzero on QA failure)'
    # Bounded by the existing controller timeout; EXIT still owns all cleanup.
    ready_at=$SECONDS
    while [ ! -f "$qa/browser-finish" ]; do
      if [ -f "$qa/record.request" ] && [ -f "$qa/record.pending" ]; then
        echo "QA_RECORDING_START_REQUESTED epoch=$(date +%s) (first driver command)" >>"$out/recording.log"
        qa_record_start >>"$out/recording.log" 2>&1 || echo 'QA_RECORDING_WARN start failed; screenshots unaffected' | tee -a "$out/recording.log"
        rm -f "$qa/record.pending" "$qa/record.request"
      fi
      # Fail-fast: ready but no browser command issued within 10 minutes (2026-10-22, #10775/#10835).
      if [ ! -f "$qa/browser-cmd-issued" ] && [ $((SECONDS-ready_at)) -ge 600 ]; then
        echo 'ERROR: AGENT_BROWSER_READY but no browser command issued within 600s; driver must run `remote-browser-box.sh browser BOX -- ...` (readiness file is on the box, not the Mac). Aborting.' | tee -a "$out/browser.log" >&2
        exit 3
      fi
      sleep 1
    done
    rm -f "$qa/record.pending" "$qa/record.request"
    finish_rc=$(cat "$qa/browser-finish")
    qa_sl finish result="$finish_rc"
    if [ "$finish_rc" = AGENT_BROWSER_DAEMON_UNRESPONSIVE ]; then
      echo 'ERROR: AGENT_BROWSER_DAEMON_UNRESPONSIVE: agent-browser daemon EAGAIN persisted after one restart; ending run now (QA FAILED, not a pass).' | tee -a "$out/browser.log" >&2
      exit 69
    fi
    [[ "$finish_rc" =~ ^[0-9]+$ && "$finish_rc" -le 255 ]] || exit 2
    exit "$finish_rc"
  fi
else
  echo 'DEPRECATED_BROWSER_LANE=legacy-playwright (.cjs callbacks; prefer agent-browser)' | tee -a "$out/browser.log"
env -u CONVEX_ADMIN_KEY -u CONVEX_DEPLOY_KEY -u MODAL_TOKEN_ID -u MODAL_TOKEN_SECRET \
  -u MODAL_API_KEY -u PIPELINE_AUTH_TOKEN -u R2_ACCESS_KEY_ID -u R2_SECRET_ACCESS_KEY \
  node <<'BROWSER'
const fs = require('node:fs');
const {chromium}=require('playwright');
(async()=>{
 const out=process.env.QA_CAPTURE_OUT;
 const log=fs.createWriteStream(`${out}/browser.log`);
 const browser=await chromium.launch({headless:true});
 let context;
 try {
  context=await browser.newContext({storageState:process.env.AUTH_STATE,viewport:{width:1440,height:900}});
  // Exact-head captures never hot-reload code. A cold route build can otherwise
  // reload the previous page during goto, returning null despite an HTTP 200.
  await context.routeWebSocket(url=>url.host===new URL(process.env.QA_URL).host && url.pathname==='/_next/webpack-hmr',socket=>{
   log.write('DEV_HMR_ISOLATED (immutable exact-head capture)\n');
   socket.onMessage(()=>{}); // No connectToServer: only this dev socket is inert.
  });
  // ProseMirror mounts under the pagination shimmer before the coordinator reveals it.
  // Guard Page.screenshot itself so --script callbacks cannot capture that hidden state.
  const loadingSelector='#editor-skeleton-layout, [data-panel-group-id="editor-skeleton-layout"], .editor-scroll-container + .absolute.inset-0:has(.skeleton-shimmer)';
  const editorSelector='[data-editor-ready="true"]:not([inert]) .ProseMirror';
  const editorIsLoaded=({loadingSelector,editorSelector})=>{
   const visible=el=>el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) && el.getBoundingClientRect().width>0 && el.getBoundingClientRect().height>0;
   return ![...document.querySelectorAll(loadingSelector)].some(visible) &&
    [...document.querySelectorAll(editorSelector)].some(el=>visible(el) && !el.closest('[inert]') && el.textContent.trim().length>0);
  };
  // Quiescence replaces Page networkidle: a loaded editor keeps long-lived media/stream requests open
  // (audio, EventSource, WebSocket) that the Playwright lifecycle event never treats as idle. Every other
  // request type (document, fetch/XHR, script, style, image, font) must be absent for 500ms.
  const inflight=new Map();
  context.on('request',r=>inflight.set(r,Date.now()));
  context.on('requestfinished',r=>inflight.delete(r));
  context.on('requestfailed',r=>inflight.delete(r));
  const longLived=new Set(['media','eventsource','websocket']);
  // Dedicated-worker script loads (spellcheck/regex workers) never emit requestfinished in Playwright and stay
  // "pending" forever; a request still unresolved after 10s is a stuck/long-lived stream, not an in-progress load.
  // Such requests are recorded (staleIgnored) rather than silently dropped.
  const STALE_MS=10000;
  const blocking=()=>[...inflight.entries()].filter(([r,t])=>!longLived.has(r.resourceType()) && Date.now()-t<STALE_MS).map(([r])=>r);
  const staleIgnored=()=>[...inflight.entries()].filter(([r,t])=>!longLived.has(r.resourceType()) && Date.now()-t>=STALE_MS).map(([r])=>{const u=new URL(r.url());return `${r.resourceType()} ${u.pathname}`;});
  const describePending=()=>blocking().slice(0,10).map(r=>{const u=new URL(r.url());return `${r.resourceType()} ${u.host}${u.pathname}`;});
  async function waitForQuiet(timeoutMs) {
    const end=Date.now()+timeoutMs; let quietSince=0;
    while(Date.now()<end) {
      if(blocking().length===0) { quietSince||=Date.now(); if(Date.now()-quietSince>=500) return; }
      else quietSince=0;
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    throw new Error('Pending non-media requests: '+describePending().join('; '));
  }
  async function waitForLoadedEditor(page,screenshotPath) {
   const route=new URL(page.url());
   if(!/^\/dashboard\/editor\/[^/]+/.test(route.pathname)) return;
   if(route.origin!==process.env.QA_URL) throw new Error('Editor screenshot left the authenticated app');
   const started=Date.now(),deadline=started+90000;
   let waitingFor='loading skeleton/overlay hidden or detached AND revealed ProseMirror visible with text';
   const remaining=()=>{
    const ms=deadline-Date.now();
    if(ms<=0) throw new Error('Loaded-editor readiness deadline exceeded');
    return ms;
   };
   try {
    await page.waitForFunction(editorIsLoaded,{loadingSelector,editorSelector},{timeout:remaining()});
    waitingFor='quiescence (500ms without active non-media network requests)';
    await waitForQuiet(remaining());
    waitingFor='loaded editor still revealed with text and no skeleton after network idle';
    await page.waitForFunction(editorIsLoaded,{loadingSelector,editorSelector},{timeout:remaining()});
    if(page.url()!==route.href) throw new Error('Editor navigated during screenshot readiness');
    fs.appendFileSync(`${out}/editor-readiness.jsonl`,JSON.stringify({head:process.env.QA_HEAD,path:route.pathname,screenshot:screenshotPath,ready:true,networkQuiescent:true,ignoredLongLived:[...longLived],staleIgnored:staleIgnored().slice(0,10),loadingSelector,editorSelector,waitedMs:Date.now()-started})+'\n');
   } catch(error) {
    fs.appendFileSync(`${out}/editor-readiness.jsonl`,JSON.stringify({head:process.env.QA_HEAD,path:route.pathname,screenshot:screenshotPath,ready:false,waitingFor,pending:describePending(),loadingSelector,editorSelector,waitedMs:Date.now()-started})+'\n');
    throw new Error(`Editor screenshot refused after ${Date.now()-started}ms (90000ms limit): waiting for ${waitingFor}; loading=${loadingSelector}; editor=${editorSelector}`,{cause:error});
   }
  }
  context.on('page',p=>{
   const screenshot=p.screenshot.bind(p);
   p.screenshot=async options=>{
    await waitForLoadedEditor(p,options?.path);
    return screenshot(options);
   };
   p.on('console',m=>log.write(`console.${m.type()}: ${m.text()}\n`));
   p.on('pageerror',e=>log.write(`pageerror: ${e.message}\n`));
   p.on('requestfailed',r=>log.write(`requestfailed: ${r.url().split('?')[0]} ${r.failure()?.errorText}\n`));
  });
  // Use only the SDK-resolved ownership receipt, never arbitrary endpoint env values.
  const ownedModalHosts=new Set();
  if(process.env.QA_CAPTURE_MODE==='modal') {
   const endpoints=JSON.parse(fs.readFileSync('/root/qa/endpoints.json','utf8'));
   const identity=process.env.QA_MODAL_BOX_ID;
   if(!/^[a-f0-9]{12}$/.test(identity||'')) throw new Error('Missing Modal box identity');
   for(const [key,value] of Object.entries(endpoints)) {
    const url=new URL(value);
    if(!/^MODAL_[A-Z_]+_(ENDPOINT_)?URL$/.test(key) || url.protocol!=='https:' ||
       url.username || url.password || url.port || url.search || url.hash ||
       !url.hostname.endsWith('.modal.run') || !url.hostname.includes('-qa-'+identity+'-')) {
     throw new Error('Invalid box-owned Modal endpoint receipt');
    }
    ownedModalHosts.add(url.hostname);
   }
   if(!ownedModalHosts.size) throw new Error('Empty box-owned Modal endpoint receipt');
  }
  let clerkRequests=0,modalRequests=0;
  await context.route('**/*',async route=>{
   const host=new URL(route.request().url()).hostname;
   if (host.endsWith('.modal.run')) {
    if(process.env.QA_CAPTURE_MODE!=='modal' || !ownedModalHosts.has(host)) {
     modalRequests++; log.write(`BLOCKED_MODAL ${host}\n`); await route.abort(); return;
    }
   }
   if (/(^|\.)(clerk\.com|clerk\.accounts\.dev|clerk\.heydonna\.(law|chat))$/.test(host)) {
    clerkRequests++; log.write(`BLOCKED_CLERK ${host}\n`); await route.abort();
   } else await route.continue();
  });
  const page=await context.newPage();
  const requestedUrl=process.env.QA_URL+process.env.QA_CAPTURE_PATH;
  const res=await page.goto(requestedUrl,{waitUntil:'domcontentloaded',timeout:180000});
  if(res?.status()!==200) throw new Error(`Browser HTTP ${res?.status()}`);
  if(new URL(page.url()).origin!==process.env.QA_URL || /^\/sign-(in|up)(\/|$)/.test(new URL(page.url()).pathname)) throw new Error('Capture left the authenticated app');
  if(process.env.QA_CAPTURE_PATH==='/admin') await page.getByRole('heading',{name:'Admin Overview',exact:true}).waitFor({timeout:90000});
  else await page.locator('main').first().waitFor({timeout:90000});
  if(process.env.QA_CAPTURE_PERSONA==='unadmitted' && process.env.QA_CAPTURE_PATH==='/') {
   if(page.url()!==requestedUrl) throw new Error('Unadmitted root persona unexpectedly redirected');
   await page.getByRole('heading',{name:/^Dashboard (unavailable|admission required)$/}).waitFor({timeout:90000});
  }
  if(fs.existsSync('/root/qa/browser.cjs')) {
   const steps=require('/root/qa/browser.cjs');
   if(typeof steps!=='function') throw new Error('--script must export async ({page,context,out}) => {}');
   // Wait on the application's real readiness link, then use a document
   // navigation rather than the SPA transition aborted in the historical run.
   const openReadyTranscript=async()=>{
    const project=new URL(page.url());
    if(project.origin!==process.env.QA_URL || !/^\/dashboard\/projects\/[^/]+$/.test(project.pathname) || project.pathname.endsWith('/new')) {
     throw new Error('Open editor requires the created project detail page');
    }
    await page.getByText('Ready',{exact:true}).first().waitFor({timeout:300000});
    const link=page.locator('a[data-testid="edit-transcript-button"][href^="/dashboard/editor/"]').first();
    await link.waitFor({state:'visible',timeout:300000});
    const target=new URL(await link.getAttribute('href'),process.env.QA_URL);
    if(target.origin!==process.env.QA_URL || !/^\/dashboard\/editor\/[^/]+$/.test(target.pathname)) {
     throw new Error('Invalid ready transcript destination');
    }
    // HMR is already isolated for this exact-head context above.
    const response=await page.goto(target.href,{waitUntil:'domcontentloaded',timeout:180000});
    if(response?.status()!==200 || page.url()!==target.href) throw new Error('Editor document navigation failed');
    await waitForLoadedEditor(page,'openReadyTranscript');
    fs.appendFileSync(`${out}/editor-navigation.jsonl`,JSON.stringify({head:process.env.QA_HEAD,projectPath:project.pathname,editorPath:target.pathname,method:'ready-link-direct-navigation',mounted:true})+'\n');
    return target.href;
   };
   await steps({page,context,out,openReadyTranscript});
  }
  await page.screenshot({path:`${out}/capture.png`,fullPage:false});
  if(clerkRequests) throw new Error(`Clerk requests blocked: ${clerkRequests}`);
  if(modalRequests) throw new Error(`Capture attempted unowned Modal requests: ${modalRequests}; only the disposable QA endpoints are permitted`);
  fs.writeFileSync(`${out}/browser-result.json`,JSON.stringify({requestedUrl,url:page.url(),redirected:page.url()!==requestedUrl,status:res.status(),viewport:page.viewportSize(),captureMode:process.env.QA_CAPTURE_MODE,persona:process.env.QA_CAPTURE_PERSONA,readinessPath:process.env.QA_READINESS_PATH,livenessPath:process.env.QA_LIVENESS_PATH,authProofPath:process.env.QA_AUTH_PROOF_PATH,clerkRequests,modalRequests,head:process.env.QA_HEAD},null,2));
 } finally { if(context) await context.close(); await browser.close(); log.end(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
BROWSER
fi
}
main "$@"; exit
