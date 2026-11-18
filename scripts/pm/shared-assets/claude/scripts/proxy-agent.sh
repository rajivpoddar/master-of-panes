#!/usr/bin/env bash
# EDITING RULE: never edit this file in place while launchers run (bash reads scripts by byte offset, so a resumed launcher runs garbage, e.g. "notification: command not found" on 2026-11-14); write a temp file and mv it over (new inode).
# proxy-agent.sh — model-selectable PM agent runner. Runs a PM agent definition out of process through CLIProxyAPI (127.0.0.1:8317),
# while the PM session stays on direct Claude. Default model: auto (S129 quota selector; luna is the GPT fallback). Legacy name luna-agent.sh is a symlink.
# Rajiv DM D0AMF0XE6TS thread 1794087326.732679: "keep pm on direct claude" ... "go".
# Rajiv DM ts 1794102299.043149: "set the correct context window sizes in the wrapper ... make the model configureable".
#
# Usage: proxy-agent.sh <agent-type> <prompt-file> [--cwd DIR] [--model M] [--rung N] [--out FILE] [--max-turns N]
#   <agent-type>  basename of an existing .md in --cwd/.claude/agents, ~/.claude/agents,
#                 or the main heydonna-app/.claude/agents, in that lookup order.
#                 SDK built-ins such as general-purpose or claude work only if a matching
#                 definition file exists; list those directories for currently valid types.
#   <prompt-file> complete one-shot brief (native named-peer steering remains available)
#   --model M     auto: *-reviewer REVIEW (fable -> astra); hd-planner / hd-investigator PLAN ladder; hd-builder BUILD1-3; hd-proof-qa PROOF1-2; other roles MID
#                 aliases: luna/haiku LIGHT (hd-builder BUILD1), terra/sonnet MID (hd-builder BUILD2, hd-proof-qa PROOF1), opus TOP (hd-builder BUILD3, hd-proof-qa PROOF2); sol = exact gpt-6.2-sol (S149, no tier picker); fable, astra exact
#                 Escalate a stalled run with --rung N or --model; raw model ids pass through unchanged.
#                 or any raw CLIProxyAPI model id (unknown ids get a 200k window + stderr warning)
#   --rung N      hd-builder 1-3, hd-proof-qa 1-2 (auto only; Rajiv D0AMF0XE6TS thread 1794624936.567599, ts 1794639918.387399)
#   pm-hotfix / pm-proof-qa are compat types: resolved to hd-planner|hd-builder (MODE: PLAN first line -> planner) / hd-proof-qa before selection. hd-investigator = stage 0, diagnose-only, PLAN tier, never auto-escalated.
# Launch from PM with Bash run_in_background:true; the final report lands in --out
# (default: $PROXY_AGENT_OUT_DIR or /tmp/proxy-agent/<type>-<sanitized-name>-<pid>-<UTC>.md).
# Prints PROXY_AGENT_DONE rc=<n> out=<path>. Same-second launches do not share that path.
set -euo pipefail

usage() { sed -n '2,21p' "$0" >&2; exit 2; }
if [[ "${1:-}" == "--help" ]]; then sed -n '2,21p' "$0"; exit 0; fi
[[ $# -ge 2 ]] || usage
RESUME_NAME=""
if [[ "$1" == "--resume" ]]; then
  RESUME_NAME="$2"; TYPE="resume"; PROMPT_FILE=""; shift 2
else
  TYPE="$1"; PROMPT_FILE="$2"; shift 2
fi
MODEL_EXPLICIT=0
CWD_EXPLICIT=0
CWD="${PROXY_AGENT_CHECKOUT:-/Users/rajiv/Downloads/projects/heydonna-app-proxy}"
MODEL="auto"  # Ordered tier selection below.
RUNG=""       # hd-builder 1-3 / hd-proof-qa 1-2 escalation rung; empty = rung 1.
OUT=""
MAX_TURNS="200"
# --data-scope / --qa-mode were the 9ea1b9b free-model scope declarations. They no longer grant anything (see "Free-model guard" below):
# still parsed so a stale caller does not fail its launch, but ignored with a notice.
FREE_SCOPE_FLAG_IGNORED=0
PM_ROLE="${PROXY_AGENT_PM_ROLE:-0}"
# One explicit allowlist; dev types retain their existing routing.
PM_ROLE_TYPES="growth pm-ops-tooling pm-triage-agent customer-artifact-investigator cleanup-pr-runner pm-admission-reviewer donna-influencer explore-issue-agent"
# PM-only skills live in heydonna-app/.claude/skills (Rajiv 2026-11-14, DM D0AMF0XE6TS thread 1794624936.567599: "move all pm skills to
# heydonna-app/.claude/skills so that the dev agents don't get it"). These PM-owned types run from that checkout via the pm-skills plugin.
PM_ROLE_TYPES="$PM_ROLE_TYPES pm-backlog-promoter pm-arch-reviewer e2e-failure-investigator ci-status-investigator mop-main-repair autoscaler-main-repair explee-outreach superproofer main-red-skip issue-contract-auditor lifecycle-email-agent pm-lock-release"
PM_TERRA_DEFAULT_TYPES="pm-admission-reviewer customer-artifact-investigator ci-repair-agent e2e-failure-investigator cleanup-pr-runner mop-main-repair pm-ops-tooling pm-triage-agent explore-issue-agent remote-qa-runner remote-test-runner"
case " $PM_ROLE_TYPES " in
  *" $TYPE "*) PM_ROLE=1;;
esac

# Only ordinary unstaged modifications in the dedicated setup clone are restored.
# Keep this callable independently for offline verification (no model/tmux/relay).
proxy_agent_restore_setup_dir() {
  [[ "$1" == "/Users/rajiv/Downloads/projects/heydonna-app-proxy" ]] || return 0
  python3 -I - "$1" "$HOME/.heydonna/proxy-setup-dirt" <<'PY'
import datetime, os, pathlib, subprocess, sys
cwd, archive = sys.argv[1:]
def git(*args):
    return subprocess.check_output(['git', '-C', cwd, '--literal-pathspecs', *args])
entries = git('status', '--porcelain=v1', '-z', '--untracked-files=no').split(b'\0')
entries = [entry for entry in entries if entry]
if not entries or any(entry[:3] != b' M ' for entry in entries):
    sys.exit(0)  # The existing tracked_changes guard handles every other state.
paths = [os.fsdecode(entry[3:]) for entry in entries]
# Submodule dirt cannot be preserved by a superproject patch.
if any((pathlib.Path(cwd) / path).is_dir() for path in paths):
    sys.exit(0)
patch = git('diff', '--binary', '--no-ext-diff', '--no-textconv', '--', *paths)
if not patch:
    sys.exit(0)
archive = pathlib.Path(archive)
archive.mkdir(parents=True, exist_ok=True, mode=0o700)
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
path = archive / (stamp + '.patch')
with path.open('xb') as stream:
    os.chmod(path, 0o600)
    stream.write(patch)
    stream.flush()
    os.fsync(stream.fileno())
subprocess.run(['git', '-C', cwd, '--literal-pathspecs', 'checkout', '--', *paths], check=True)
print('PROXY_SETUP_DIR_RESTORED patch=' + str(path), file=sys.stderr)
PY
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cwd) CWD="$2"; CWD_EXPLICIT=1; shift 2;;
    --model) MODEL="$2"; MODEL_EXPLICIT=1; shift 2;;
    --rung) RUNG="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --max-turns) MAX_TURNS="$2"; shift 2;;
    --data-scope|--qa-mode) FREE_SCOPE_FLAG_IGNORED=1; shift 2;;
    --pm-role) PM_ROLE=1; shift;;
    *) usage;;
  esac
done
if [[ "$FREE_SCOPE_FLAG_IGNORED" == 1 ]]; then
  printf 'proxy-agent: --data-scope/--qa-mode ignored: hy4-flash-free and muse-spark-1.3-contributor are unconditionally ineligible (no declaration grants them)\n' >&2
fi
case "$RUNG" in ''|1|2|3) ;; *) printf 'PROXY_AGENT_FAILED reason=invalid_rung value=%s (use 1, 2 or 3)\n' "$RUNG" >&2; exit 2;; esac

STATE_HELPER="$HOME/.claude/scripts/proxy-run-state.py"
unset PROXY_RESUME_ORIGINAL
if [[ -n "$RESUME_NAME" ]]; then
  [[ "$MODEL_EXPLICIT" == 1 && "$MODEL" != auto && "$CWD_EXPLICIT" == 0 ]] || { printf 'PROXY_AGENT_FAILED reason=resume_requires_explicit_model_and_original_cwd\n' >&2; exit 2; }
  [[ "${PROXY_AGENT_DRY_RUN:-}" != 1 ]] || { printf 'PROXY_AGENT_FAILED reason=resume_dry_run_unavailable\n' >&2; exit 2; }
fi
if [[ "$MODEL" == ernie || "$MODEL" == ernie-flash ]]; then
  if [[ "${PROXY_AGENT_DRY_RUN:-}" != 1 ]]; then python3 -I "$STATE_HELPER" disable-ernie || true; fi
  printf 'PROXY_AGENT_FAILED reason=ernie_image_input_unsupported\n' >&2
  exit 2
fi
if [[ "${PROXY_AGENT_DRY_RUN:-}" != 1 ]]; then
  export PROXY_AGENT_RUN_NAME="${RESUME_NAME:-${PROXY_AGENT_NAME:-proxy-$TYPE-$(date -u +%Y%m%dT%H%M%SZ)-$$}}"
  export PROXY_AGENT_NAME="$PROXY_AGENT_RUN_NAME"
  export PROXY_AGENT_RUN_TOKEN="$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
  python3 -I "$STATE_HELPER" acquire --name "$PROXY_AGENT_RUN_NAME" --token "$PROXY_AGENT_RUN_TOKEN" || exit 2
  RUN_LOCK_PARENT=1
  trap 'pa_exit_rc=$?; if [[ "${RUN_LOCK_PARENT:-0}" == 1 ]]; then python3 -I "$STATE_HELPER" abandon --name "$PROXY_AGENT_RUN_NAME" --token "$PROXY_AGENT_RUN_TOKEN" || true; fi; exit "$pa_exit_rc"' EXIT
  if [[ -n "$RESUME_NAME" ]]; then
    RESUME_ASSIGNMENTS="$(python3 -I "$STATE_HELPER" resume --name "$PROXY_AGENT_RUN_NAME" --token "$PROXY_AGENT_RUN_TOKEN")" || exit 2
    eval "$RESUME_ASSIGNMENTS"
    export PROXY_RESUME_ORIGINAL
    case " $PM_ROLE_TYPES " in *" $TYPE "*) PM_ROLE=1;; esac
  fi
fi
# The generic Claude agent is PM-role only for the explicit heartbeat brief.
ROLE_BRIEF="${PROXY_RESUME_ORIGINAL:-$PROMPT_FILE}"
if [[ "$TYPE" == "claude" && -r "$ROLE_BRIEF" ]]; then
  IFS= read -r FIRST_BRIEF_LINE < "$ROLE_BRIEF" || true
  [[ "$FIRST_BRIEF_LINE" =~ ^MODE:[[:space:]]heartbeat($|[[:space:]]) ]] && PM_ROLE=1
fi

# S129 selector (Rajiv D0AMF0XE6TS thread 1794144223.162789, ts 1794145277.032389),
# extended 2026-11-08 (same thread, ts 1794162044.123259): Antigravity Claude before grok and GPT.
# Runs before resolve_model and before the ANTHROPIC_DEFAULT_*_MODEL exports.
# The quota probe runs only when --model is omitted or `auto`. An explicit alias
# skips that probe, but an active exhaustion record (until > now) still remaps
# that model off the main id and off every subagent mapping.
# Probe failures are "unusable", never a crash. Tokens are never printed.
HAIKU_MODEL="claude-haiku-5-5"  # Antigravity/Gemini removed (Rajiv D0AMF0XE6TS thread 1794659684.547559 ts 1794660714.234059: "remove hydrogen/carbon. we can’t use antigravity. only grok, claude, codex")
SONNET_MODEL="grok-4.7"
OPUS_MODEL="grok-4.7"
SELECT_REASON="explicit"

proxy_agent_state_dir() {
  printf '%s\n' "${PROXY_AGENT_STATE_DIR:-$HOME/.cache/proxy-agent}"
}

proxy_agent_fmt_rf() {
  case "$1" in
    none|fail|absent) printf '%s\n' "$1";;
    *)
      case "$1" in
        ''|*[!0-9.]*) printf 'fail\n';;
        *) printf '%.2f\n' "$1";;
      esac
      ;;
  esac
}

proxy_agent_gemini_usable() {
  case "$1" in
    none) return 0;;
    fail|absent) return 1;;
    *)
      case "$1" in
        ''|*[!0-9.]*) return 1;;
      esac
      python3 -I -c 'import sys; sys.exit(0 if float(sys.argv[1]) >= 0.10 else 1)' "$1"
      ;;
  esac
}

# Antigravity Claude "none" means quotaInfo was missing: UNKNOWN, not available.
# A numeric fraction uses the same 10% floor as Gemini. Exhaustion is a stored until-time.
proxy_agent_claude_usable() {
  case "$1" in
    none|fail|absent|"") return 1;;
    *)
      case "$1" in
        ''|*[!0-9.]*) return 1;;
      esac
      python3 -I -c 'import sys; sys.exit(0 if float(sys.argv[1]) >= 0.10 else 1)' "$1"
      ;;
  esac
}

proxy_agent_rr_bump() {
  local dir="$1" i n
  mkdir -p "$dir" 2>/dev/null || { printf '1\n'; return 0; }
  i=1
  while [[ "$i" -le 10 ]]; do
    if mkdir "$dir/rr.lock" 2>/dev/null; then
      n="$(cat "$dir/rr" 2>/dev/null || printf '0\n')"
      case "$n" in
        ''|*[!0-9]*) n=0;;
      esac
      n=$((n + 1))
      printf '%s\n' "$n" > "$dir/rr" 2>/dev/null || true
      rmdir "$dir/rr.lock" 2>/dev/null || true
      printf '%s\n' "$n"
      return 0
    fi
    sleep 0.05
    i=$((i + 1))
  done
  printf '1\n'
}

proxy_agent_read_cache() {
  python3 -I - "$1/quota.json" <<'PY'
import json, sys, time
path = sys.argv[1]
try:
    data = json.load(open(path))
    now = int(time.time())
    age = now - int(data["cached_at"])
    rf = str(data["gemini_rf"])
    gemini_pro = str(data["gemini_pro_rf"])
    grok = str(data["grok"])
    opus = str(data["ag_opus_rf"])
    sonnet = str(data["ag_sonnet_rf"])
    grok_pct = str(data.get("grok_used_pct", "none"))
    grok_resets = str(data.get("grok_resets", ""))
except Exception:
    print("miss")
    raise SystemExit(0)
if age < 0 or age >= 300:
    print("miss")
    raise SystemExit(0)

def fraction_ok(token, allow_none=True):
    if token in ("fail", "absent"):
        return True
    if token == "none":
        return allow_none
    return token.replace(".", "", 1).isdigit() and token.count(".") <= 1

# Claude and Gemini-Pro "none" are unknown, not a cached answer. Re-probe instead of serving them.
if (
    not fraction_ok(rf)
    or not fraction_ok(gemini_pro, False)
    or not fraction_ok(opus, False)
    or not fraction_ok(sonnet, False)
):
    print("miss")
    raise SystemExit(0)
if grok != "fail" and not (len(grok) == 3 and grok.isdigit()):
    print("miss")
    raise SystemExit(0)
print(rf)
print(grok)
print(opus)
print(sonnet)
print(grok_pct)
print(grok_resets)
print(gemini_pro)
PY
}

proxy_agent_write_cache() {
  PROXY_AGENT_CACHE_RF="$2" PROXY_AGENT_CACHE_GROK="$3" \
  PROXY_AGENT_CACHE_OPUS="$4" PROXY_AGENT_CACHE_SONNET="$5" \
  PROXY_AGENT_CACHE_GROK_PCT="${6:-none}" PROXY_AGENT_CACHE_GROK_RESETS="${7:-}" \
  PROXY_AGENT_CACHE_OPUS_UNTIL="${8:-0}" PROXY_AGENT_CACHE_SONNET_UNTIL="${9:-0}" \
  PROXY_AGENT_CACHE_GEMINI_PRO="${10:-fail}" PROXY_AGENT_CACHE_GEMINI_PRO_UNTIL="${11:-0}" \
  PROXY_AGENT_CACHE_GEMINI_FLASH_UNTIL="${12:-0}" PROXY_AGENT_CACHE_GROK_UNTIL="${13:-0}" \
  python3 -I - "$1/quota.json" <<'PY'
import json, os, sys, tempfile, time
path = sys.argv[1]
now = int(time.time())

def as_int(raw):
    try:
        return max(0, int(str(raw).strip() or "0"))
    except (TypeError, ValueError):
        return 0

try:
    existing = json.load(open(path))
    if not isinstance(existing, dict):
        existing = {}
except Exception:
    existing = {}

eu = existing.get("exhausted_until")
if not isinstance(eu, dict):
    eu = {}
else:
    eu = {str(k): as_int(v) for k, v in eu.items() if as_int(v) > 0}

def merged_until(env_key, slot_key, model_id):
    val = max(
        as_int(os.environ.get(env_key, "0")),
        as_int(existing.get(slot_key, 0)),
        as_int(existing.get(model_id + "_exhausted_until", 0)),
        as_int(eu.get(model_id, 0)),
    )
    if val > 0:
        eu[model_id] = val
    return val

opus_until = merged_until("PROXY_AGENT_CACHE_OPUS_UNTIL", "ag_opus_exhausted_until", "claude-opus-5-5-high")
sonnet_until = merged_until("PROXY_AGENT_CACHE_SONNET_UNTIL", "ag_sonnet_exhausted_until", "claude-sonnet-5-5-high")
gemini_pro_until = merged_until("PROXY_AGENT_CACHE_GEMINI_PRO_UNTIL", "gemini_pro_exhausted_until", "gemini-4-argon-high")
gemini_flash_until = max(
    merged_until("PROXY_AGENT_CACHE_GEMINI_FLASH_UNTIL", "gemini_flash_exhausted_until", "gemini-3.8-flash-high"),
    as_int(existing.get("gemini_exhausted_until", 0)),
)
if gemini_flash_until > 0:
    eu["gemini-3.8-flash-high"] = gemini_flash_until
grok_until = merged_until("PROXY_AGENT_CACHE_GROK_UNTIL", "grok_exhausted_until", "grok-4.7")

rf = os.environ.get("PROXY_AGENT_CACHE_RF", "fail")
gemini_pro = os.environ.get("PROXY_AGENT_CACHE_GEMINI_PRO", "fail")
grok = os.environ.get("PROXY_AGENT_CACHE_GROK", "fail")
opus = os.environ.get("PROXY_AGENT_CACHE_OPUS", "fail")
sonnet = os.environ.get("PROXY_AGENT_CACHE_SONNET", "fail")

if opus_until > now:
    opus = "0"
if sonnet_until > now:
    sonnet = "0"
if gemini_pro_until > now:
    gemini_pro = "0"
if gemini_flash_until > now:
    rf = "0"
if grok_until > now:
    grok = "429"

payload = dict(existing)
payload.update({
    "cached_at": now,
    "gemini_pro_rf": gemini_pro,
    "gemini_rf": rf,
    "grok": grok,
    "ag_opus_rf": opus,
    "ag_sonnet_rf": sonnet,
    "grok_used_pct": os.environ.get("PROXY_AGENT_CACHE_GROK_PCT", "none"),
    "grok_resets": os.environ.get("PROXY_AGENT_CACHE_GROK_RESETS", ""),
    "ag_opus_exhausted_until": opus_until,
    "ag_sonnet_exhausted_until": sonnet_until,
    "gemini_pro_exhausted_until": gemini_pro_until,
    "gemini_flash_exhausted_until": gemini_flash_until,
    "gemini_exhausted_until": gemini_flash_until,
    "grok_exhausted_until": grok_until,
    "exhausted_until": eu,
})
folder = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=folder, prefix=".quota-")
try:
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except Exception:
    try:
        os.unlink(tmp)
    except Exception:
        pass
PY
}

proxy_agent_probe_gemini() {
  python3 -I - <<'PY'
import glob, json, os, urllib.error, urllib.request

TARGET = "gemini-3.8-flash-high"
PREFIX = "gemini-3.8-flash"
PRO_TARGET = "gemini-4-argon-high"
PRO_PREFIX = "gemini-4-argon"
CLAUDE_TARGETS = ("claude-opus-5-5-high", "claude-sonnet-5-5-high")

def finish(token):
    # Early failure: Gemini Flash, both Claude fractions, and Gemini Pro are unknown.
    print(token)
    print("fail")
    print("fail")
    print("fail")
    raise SystemExit(0)

pattern = os.environ.get(
    "PROXY_AGENT_ANTIGRAVITY_GLOB",
    os.path.expanduser("~/.cli-proxy-api/antigravity-*.json"),
)
chosen = None
pro_hinted = False
for path in sorted(glob.glob(pattern)):
    try:
        data = json.load(open(path))
    except Exception:
        continue
    if not isinstance(data, dict) or data.get("disabled") is True:
        continue
    hints = data.get("antigravity_capability_hints") or {}
    if not isinstance(hints, dict):
        hints = {}
    endpoint = hints.get("endpoint")
    token = data.get("access_token")
    project = hints.get("project_id") or data.get("project_id")
    if isinstance(endpoint, str) and endpoint and isinstance(token, str) and token and isinstance(project, str) and project:
        chosen = (endpoint, token, project)
        model_ids = hints.get("model_ids")
        if isinstance(model_ids, list):
            for item in model_ids:
                if isinstance(item, str) and PRO_PREFIX in item:
                    pro_hinted = True
                    break
        break
if chosen is None:
    finish("fail")
endpoint, token, project = chosen
url = endpoint.rstrip("/") + "/v1internal:fetchAvailableModels"
body = json.dumps({"project": project}).encode()
req = urllib.request.Request(url, data=body, method="POST")
req.add_header("Authorization", "Bearer " + token)
req.add_header("Content-Type", "application/json")
req.add_header("User-Agent", "antigravity")
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        status = getattr(resp, "status", 200)
except Exception:
    finish("fail")
if status != 200:
    finish("fail")
try:
    payload = json.loads(raw.decode("utf-8"))
except Exception:
    finish("fail")

found = {}
pro_found = {}
claude_found = {}

def remember(name, frac):
    if not isinstance(name, str):
        return
    model_id = name.strip().split("/")[-1]
    if PREFIX not in model_id:
        return
    if model_id not in found or (found[model_id] is None and frac is not None):
        found[model_id] = frac

def remember_pro(name, frac):
    if not isinstance(name, str):
        return
    model_id = name.strip().split("/")[-1]
    if PRO_PREFIX not in model_id:
        return
    if model_id not in pro_found or (pro_found[model_id] is None and frac is not None):
        pro_found[model_id] = frac

def remember_claude(name, frac):
    if not isinstance(name, str):
        return
    model_id = name.strip().split("/")[-1]
    if model_id not in CLAUDE_TARGETS:
        return
    if model_id not in claude_found or (claude_found[model_id] is None and frac is not None):
        claude_found[model_id] = frac

def note(name, frac):
    remember(name, frac)
    remember_pro(name, frac)
    remember_claude(name, frac)

def fraction_of(obj):
    if not isinstance(obj, dict):
        return None
    quota = obj.get("quotaInfo")
    if not isinstance(quota, dict):
        quota = obj.get("quota_info")
    if not isinstance(quota, dict):
        return None
    raw_fraction = quota.get("remainingFraction", quota.get("remaining_fraction"))
    if raw_fraction is None:
        if "resetTime" in quota or "reset_time" in quota:
            return 0.0
        return None
    try:
        return float(raw_fraction)
    except (TypeError, ValueError):
        return None

models = payload.get("models") if isinstance(payload, dict) else None
if isinstance(models, dict):
    for key, value in models.items():
        note(key, fraction_of(value) if isinstance(value, dict) else None)
        if isinstance(value, dict):
            for field in ("name", "id", "model", "modelId"):
                if isinstance(value.get(field), str):
                    note(value[field], fraction_of(value))
                    break
elif isinstance(models, list):
    for value in models:
        if not isinstance(value, dict):
            continue
        for field in ("name", "id", "model", "modelId"):
            if isinstance(value.get(field), str):
                note(value[field], fraction_of(value))
                break

def claude_token(model_id):
    if model_id not in claude_found:
        return "absent"
    frac = claude_found[model_id]
    if frac is None:
        return "none"
    return "%.6f" % frac

def pick_family_token(table, target, hinted=False):
    if not table:
        return "none" if hinted else "absent"
    reporting = [(name, frac) for name, frac in table.items() if frac is not None]
    if not reporting:
        return "none"
    def rank(item):
        name = item[0]
        if name == target:
            return (0, 0, 0, name)
        common = 0
        for left, right in zip(name, target):
            if left != right:
                break
            common += 1
        return (1, -common, abs(len(name) - len(target)), name)
    _name, frac = sorted(reporting, key=rank)[0]
    return "%.6f" % frac

gemini_tok = pick_family_token(found, TARGET, False)
gemini_pro_tok = pick_family_token(pro_found, PRO_TARGET, pro_hinted)
print(gemini_tok)
print(claude_token("claude-opus-5-5-high"))
print(claude_token("claude-sonnet-5-5-high"))
print(gemini_pro_tok)
PY
}

proxy_agent_probe_grok() {
  python3 -I - <<'PY'
import json, os, time, urllib.error, urllib.request

def finish(token):
    print(token)
    raise SystemExit(0)

state_dir = os.environ.get("PROXY_AGENT_STATE_DIR") or os.path.expanduser("~/.cache/proxy-agent")
try:
    qdata = json.load(open(os.path.join(state_dir, "quota.json")))
    now = int(time.time())
    eu = qdata.get("exhausted_until") if isinstance(qdata, dict) else {}
    if not isinstance(eu, dict):
        eu = {}
    grok_until = max(
        int(qdata.get("grok_exhausted_until") or 0),
        int(qdata.get("grok-4.7_exhausted_until") or 0),
        int(eu.get("grok-4.7") or 0),
        int(eu.get("grok") or 0),
    )
    if grok_until > now:
        finish("429")
except Exception:
    pass

path = os.environ.get(
    "PROXY_AGENT_CLIENT_ENV",
    os.path.expanduser("~/.config/cliproxyapi/client.env"),
)
key = os.environ.get("CLIPROXY_API_KEY", "")
base = os.environ.get("PROXY_AGENT_GROK_BASE") or os.environ.get("CLIPROXY_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "http://127.0.0.1:8317"
try:
    for line in open(path):
        item = line.strip()
        if item.startswith("export "):
            item = item[len("export "):].strip()
        if item.startswith("CLIPROXY_API_KEY="):
            key = item.split("=", 1)[1].strip().strip('"').strip("'")
        elif item.startswith("CLIPROXY_BASE_URL="):
            base = item.split("=", 1)[1].strip().strip('"').strip("'")
except Exception:
    if not key:
        finish("fail")
if os.environ.get("PROXY_AGENT_GROK_BASE"):
    base = os.environ["PROXY_AGENT_GROK_BASE"]
if not key or not base:
    finish("fail")
timeout = int(os.environ.get("PROXY_AGENT_GROK_TIMEOUT", "30"))
url = base.rstrip("/") + "/v1/messages"
payload = json.dumps({
    "model": "grok-4.7",
    "max_tokens": 1,
    "stream": False,
    "messages": [{"role": "user", "content": "."}],
}).encode()
req = urllib.request.Request(url, data=payload, method="POST")
req.add_header("content-type", "application/json")
req.add_header("authorization", "Bearer " + key)
req.add_header("x-api-key", key)
req.add_header("anthropic-version", "2023-06-01")
raw = b""
status = None
try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(4096)
        status = getattr(resp, "status", 200)
except urllib.error.HTTPError as exc:
    try:
        raw = exc.read(4096)
    except Exception:
        raw = b""
    status = exc.code
except Exception:
    finish("fail")
text = raw.decode("utf-8", "replace").lower()
quota_error = ("quota" in text or "credit" in text or "billing" in text or "insufficient" in text or "rate" in text or "exceeded" in text)
if status == 200 and "error" in text and quota_error:
    finish("429")
if status == 200 and "error" in text:
    finish("500")
if status == 200:
    finish("200")
if isinstance(status, int) and status > 0:
    finish("%03d" % status)
finish("fail")
PY
}

# 1-token /v1/messages probe for an Antigravity model id. Same cliproxy base as the
# Grok probe. Prints "<status>\n<reset-seconds>". 429 (or a 200 body that says the
# quota was reached) is exhausted for the reset window parsed from the body, else 1h.
# Never treats a 1-token probe 200 as available while recorded exhausted_until > now.
# The body itself is never printed.
proxy_agent_probe_claude_messages() {
  python3 -I - <<'PY'
import json, os, re, time, urllib.error, urllib.request

def finish(status, reset):
    print(status)
    print(int(reset))
    raise SystemExit(0)

def reset_seconds(text):
    match = re.search(r"quotaresettimestamp\s*[:=]\s*['\"]?(\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}z?)", text, re.I)
    if match:
        try:
            from datetime import datetime, timezone
            stamp = match.group(1).replace("z", "+00:00")
            return max(1, int(datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()) - int(time.time()))
        except Exception:
            pass
    patterns = (
        (r"resets in\s+(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", lambda m: int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))),
        (r"resets in\s+(\d+)\s*h\s*(\d+)\s*m", lambda m: int(m.group(1)) * 3600 + int(m.group(2)) * 60),
        (r"resets in\s+(\d+)\s*h", lambda m: int(m.group(1)) * 3600),
        (r"resets in\s+(\d+)\s*m\s*(\d+)\s*s", lambda m: int(m.group(1)) * 60 + int(m.group(2))),
        (r"resets in\s+(\d+)\s*m", lambda m: int(m.group(1)) * 60),
        (r"resets in\s+(\d+)\s*s", lambda m: int(m.group(1))),
    )
    for pattern, calc in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            seconds = calc(match)
            return seconds if seconds > 0 else 3600
    return 3600

model = os.environ.get("PROXY_AGENT_CLAUDE_PROBE_MODEL", "")
if model not in ("claude-opus-5-5-high", "claude-sonnet-5-5-high", "gemini-4-argon-high", "gemini-3.8-flash-high", "gemini-4-hydrogen-xhigh", "gemini-4-carbon-high", "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "grok-4.7", "gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6.2-sol", "gpt-6-astra", "claude-sonnet-4-6", "claude-opus-4-6-thinking", "claude-fable-5-2", "hy4-flash-free", "deepseek-v4.1-flash", "glm-5.3", "muse-spark-1.3-contributor"):
    finish("fail", 0)

state_dir = os.environ.get("PROXY_AGENT_STATE_DIR") or os.path.expanduser("~/.cache/proxy-agent")
try:
    qdata = json.load(open(os.path.join(state_dir, "quota.json")))
    now = int(time.time())
    eu = qdata.get("exhausted_until") if isinstance(qdata, dict) else {}
    if not isinstance(eu, dict):
        eu = {}
    slot_keys = {
        "claude-opus-5-5-high": ("ag_opus_exhausted_until", "claude-opus-5-5-high_exhausted_until"),
        "claude-sonnet-5-5-high": ("ag_sonnet_exhausted_until", "claude-sonnet-5-5-high_exhausted_until"),
        "gemini-4-argon-high": ("gemini_pro_exhausted_until", "gemini-4-argon-high_exhausted_until"),
        "gemini-3.8-flash-high": ("gemini_flash_exhausted_until", "gemini_exhausted_until", "gemini-3.8-flash-high_exhausted_until"),
        "gemini-4-hydrogen-xhigh": ("hydrogen_exhausted_until", "gemini_hydrogen_exhausted_until", "gemini-4-hydrogen-xhigh_exhausted_until"),
    }.get(model, ())
    until = int(eu.get(model) or 0)
    for k in slot_keys:
        v = int(qdata.get(k) or 0)
        if v > until:
            until = v
    if until > now:
        finish("429", until - now)
except Exception:
    pass

path = os.environ.get(
    "PROXY_AGENT_CLIENT_ENV",
    os.path.expanduser("~/.config/cliproxyapi/client.env"),
)
key = os.environ.get("CLIPROXY_API_KEY", "")
base = os.environ.get("PROXY_AGENT_GROK_BASE") or os.environ.get("CLIPROXY_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "http://127.0.0.1:8317"
try:
    for line in open(path):
        item = line.strip()
        if item.startswith("export "):
            item = item[len("export "):].strip()
        if item.startswith("CLIPROXY_API_KEY="):
            key = item.split("=", 1)[1].strip().strip('"').strip("'")
        elif item.startswith("CLIPROXY_BASE_URL=") and not os.environ.get("PROXY_AGENT_GROK_BASE"):
            base = item.split("=", 1)[1].strip().strip('"').strip("'")
except Exception:
    if not key:
        finish("fail", 0)
if os.environ.get("PROXY_AGENT_GROK_BASE"):
    base = os.environ["PROXY_AGENT_GROK_BASE"]
if not key or not base:
    finish("fail", 0)
timeout = int(os.environ.get("PROXY_AGENT_GROK_TIMEOUT", "30"))
url = base.rstrip("/") + "/v1/messages"
payload = json.dumps({
    "model": model,
    "max_tokens": 1,
    "stream": False,
    "messages": [{"role": "user", "content": "."}],
}).encode()
req = urllib.request.Request(url, data=payload, method="POST")
req.add_header("content-type", "application/json")
req.add_header("authorization", "Bearer " + key)
req.add_header("x-api-key", key)
req.add_header("anthropic-version", "2023-06-01")
raw = b""
status = None
try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(4096)
        status = getattr(resp, "status", 200)
except urllib.error.HTTPError as exc:
    try:
        raw = exc.read(4096)
    except Exception:
        raw = b""
    status = exc.code
except Exception:
    finish("fail", 0)
text = raw.decode("utf-8", "replace")
low = text.lower()
quota_hit = ("quota reached" in low) or ("individual quota" in low) or (status == 429 and "quota" in low)
if status in (402, 429) or (status == 200 and "error" in low and quota_hit):
    finish(str(status) if status in (402, 429) else "429", reset_seconds(low))
if status == 200 and "error" in low:
    finish("fail", 0)
if status == 200:
    finish("200", 0)
finish("fail", 0)
PY
}

# Prints the epoch until which a model/slot stays exhausted, or 0. Ignores the 5-min cache TTL.
proxy_agent_read_until() {
  python3 -I - "$1/quota.json" "$2" "${3:-}" <<'PY'
import json, sys
path, key, model_id = sys.argv[1], sys.argv[2], sys.argv[3]
value = 0
try:
    data = json.load(open(path))
    candidates = [data.get(key)]
    eu = data.get("exhausted_until") if isinstance(data, dict) else {}
    if not isinstance(eu, dict):
        eu = {}
    if model_id:
        candidates.append(data.get(model_id + "_exhausted_until"))
        candidates.append(eu.get(model_id))
        alias_map = {
            "claude-opus-5-5-high": ("ag_opus_exhausted_until", "ag-opus_exhausted_until"),
            "claude-sonnet-5-5-high": ("ag_sonnet_exhausted_until", "ag-sonnet_exhausted_until"),
            "gemini-4-argon-high": ("gemini_pro_exhausted_until", "gemini-pro_exhausted_until"),
            "gemini-3.8-flash-high": ("gemini_flash_exhausted_until", "gemini_exhausted_until", "gemini-flash_exhausted_until"),
            "grok-4.7": ("grok_exhausted_until",),
            "gpt-6-luna": ("luna_exhausted_until",),
        }
        for k in alias_map.get(model_id, ()):
            candidates.append(data.get(k))
            candidates.append(eu.get(k.replace("_exhausted_until", "")))
    for c in candidates:
        try:
            v = int(c or 0)
            if v > value:
                value = v
        except (TypeError, ValueError):
            pass
except Exception:
    value = 0
if value < 0:
    value = 0
print(value)
PY
}

# $1 token, $2 model id, $3 saved until-epoch. Prints "<token>\n<until>".
# A live until wins over a fraction or a 1-token probe 200. "none" is probed; 200 becomes 1.0; 429 becomes 0 until reset.
proxy_agent_clarify_claude() {
  local token="$1" model="$2" until_now="$3" now probe status reset
  now="$(date +%s 2>/dev/null || printf '0')"
  case "$now" in ''|*[!0-9]*) now=0;; esac
  case "$until_now" in ''|*[!0-9]*) until_now=0;; esac
  if [[ "$until_now" -gt "$now" ]]; then
    printf '0\n%s\n' "$until_now"
    return 0
  fi
  if [[ "$token" != "none" ]]; then
    printf '%s\n0\n' "$token"
    return 0
  fi
  probe="$(PROXY_AGENT_CLAUDE_PROBE_MODEL="$model" proxy_agent_probe_claude_messages 2>/dev/null || printf 'fail\n0\n')"
  status="${probe%%$'\n'*}"
  reset="${probe#*$'\n'}"
  reset="${reset%%$'\n'*}"
  case "$reset" in ''|*[!0-9]*) reset=3600;; esac
  case "$status" in
    200) printf '1.000000\n0\n';;
    429)
      [[ "$reset" -gt 0 ]] || reset=3600
      printf '0\n%s\n' "$((now + reset))"
      ;;
    *) printf 'fail\n0\n';;
  esac
}

# Force a fraction (or status) to exhausted while its exhaustion window is still open. Cache hits and 1-token probe 200s included.
proxy_agent_mask_exhausted() {
  local until now mask_val="${5:-0}"
  until="$(proxy_agent_read_until "$1" "$2" "${4:-}" 2>/dev/null || printf '0\n')"
  until="${until%%$'\n'*}"
  now="$(date +%s 2>/dev/null || printf '0')"
  case "$until" in ''|*[!0-9]*) until=0;; esac
  case "$now" in ''|*[!0-9]*) now=0;; esac
  if [[ "$until" -gt "$now" ]]; then
    printf '%s\n' "$mask_val"
  else
    printf '%s\n' "$3"
  fi
}

# Main model exited with a 429 quota error in the run output (.md) or log (.log).
# Record that model's exhaustion window for its exact model id (and slot alias) in quota.json
# so subsequent auto launches skip it, and print one clear PROXY_AGENT_MODEL_EXHAUSTED line.
proxy_agent_note_log_exhaustion() {
  local model="$1" logfile="${2:-}" outfile="${3:-}" model_id alias state
  [[ -n "$logfile" || -n "$outfile" ]] || return 0
  read -r model_id _ _ < <(resolve_model "$model")
  alias="$(proxy_agent_alias_for_id "$model_id")"
  [[ -n "$alias" ]] || alias="$model"
  state="$(proxy_agent_state_dir)"
  mkdir -p "$state" 2>/dev/null || return 0
  chmod 700 "$state" 2>/dev/null || true
  PROXY_AGENT_EXHAUST_MODEL_ID="$model_id" PROXY_AGENT_EXHAUST_ALIAS="$alias" \
    python3 -I - "$state/quota.json" "$logfile" "$outfile" <<'PY'
import json, os, re, sys, tempfile, time

def parse_reset(text):
    stamp = re.search(r"quotaresettimestamp\s*[:=]\s*['\"]?(\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}z?)", text, re.I)
    if stamp:
        try:
            from datetime import datetime, timezone
            raw = stamp.group(1).replace("z", "+00:00").replace("Z", "+00:00")
            return ("quotaResetTimeStamp", max(1, int(datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).timestamp()) - int(time.time())))
        except Exception:
            pass
    patterns = (
        (r"resets in\s+(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", lambda m: (f"{int(m.group(1))}h{int(m.group(2))}m{int(m.group(3))}s", int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)))),
        (r"resets in\s+(\d+)\s*h\s*(\d+)\s*m", lambda m: (f"{int(m.group(1))}h{int(m.group(2))}m", int(m.group(1)) * 3600 + int(m.group(2)) * 60)),
        (r"resets in\s+(\d+)\s*h", lambda m: (f"{int(m.group(1))}h", int(m.group(1)) * 3600)),
        (r"resets in\s+(\d+)\s*m\s*(\d+)\s*s", lambda m: (f"{int(m.group(1))}m{int(m.group(2))}s", int(m.group(1)) * 60 + int(m.group(2)))),
        (r"resets in\s+(\d+)\s*m", lambda m: (f"{int(m.group(1))}m", int(m.group(1)) * 60)),
        (r"resets in\s+(\d+)\s*s", lambda m: (f"{int(m.group(1))}s", int(m.group(1)))),
    )
    for pattern, calc in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            label, seconds = calc(match)
            if seconds > 0:
                return label, seconds
            return "1h", 3600
    return "1h", 3600

path, logfile, outfile = sys.argv[1], sys.argv[2], sys.argv[3]
chunks = []
for candidate in (outfile, logfile):
    if not candidate or not os.path.isfile(candidate):
        continue
    try:
        lines = [line for line in open(candidate, errors="replace").read().splitlines() if line.strip()]
        if lines:
            chunks.append("\n".join(lines[-30:]).lower())
    except Exception:
        continue
if not chunks:
    raise SystemExit(0)

matched_text = None
for chunk in chunks:
    has_429 = "429" in chunk or "402" in chunk
    has_quota = (
        "quota reached" in chunk
        or "individual quota" in chunk
        or "quota exceeded" in chunk
        or "resource_exhausted" in chunk
    )
    if has_429:
        matched_text = chunk
        break
if matched_text is None:
    raise SystemExit(0)

resets_in, seconds = parse_reset(matched_text)
model_id = os.environ.get("PROXY_AGENT_EXHAUST_MODEL_ID", "").strip()
alias = os.environ.get("PROXY_AGENT_EXHAUST_ALIAS", "").strip()
if not model_id:
    raise SystemExit(0)

now = int(time.time())
until = now + seconds
try:
    data = json.load(open(path))
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
if "cached_at" in data:
    data["cached_at"] = now
else:
    data["cached_at"] = 0
data.setdefault("gemini_pro_rf", "fail")
data.setdefault("gemini_rf", "fail")
data.setdefault("grok", "fail")
data.setdefault("ag_opus_rf", "fail")
data.setdefault("ag_sonnet_rf", "fail")

eu = data.get("exhausted_until")
if not isinstance(eu, dict):
    eu = {}
eu[model_id] = until
data["exhausted_until"] = eu
data[model_id + "_exhausted_until"] = until

if model_id == "claude-opus-5-5-high" or alias == "ag-opus":
    data["ag_opus_rf"] = "0"
    data["ag_opus_exhausted_until"] = until
elif model_id == "claude-sonnet-5-5-high" or alias == "ag-sonnet":
    data["ag_sonnet_rf"] = "0"
    data["ag_sonnet_exhausted_until"] = until
elif model_id == "gemini-4-argon-high" or alias == "gemini-pro":
    data["gemini_pro_rf"] = "0"
    data["gemini_pro_exhausted_until"] = until
elif model_id == "gemini-3.8-flash-high" or alias == "gemini-flash":
    data["gemini_rf"] = "0"
    data["gemini_flash_exhausted_until"] = until
    data["gemini_exhausted_until"] = until
elif model_id == "grok-4.7" or alias == "grok":
    data["grok"] = "429"
    data["grok_exhausted_until"] = until
elif alias:
    data[alias + "_exhausted_until"] = until

folder = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=folder, prefix=".quota-")
try:
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except Exception:
    try:
        os.unlink(tmp)
    except Exception:
        pass
    raise SystemExit(0)
sys.stderr.write(
    "PROXY_AGENT_MODEL_EXHAUSTED model=%s resets_in=%s seconds=%d until=%d\n"
    % (model_id, resets_in, seconds, until)
)
PY
}

proxy_agent_norm_rf() {
  case "$1" in
    none|fail|absent) printf '%s\n' "$1";;
    ''|*[!0-9.]*) printf 'fail\n';;
    *) printf '%s\n' "$1";;
  esac
}

proxy_agent_load_probes() {
  local state="$1" cached gemini gemini_pro grok opus sonnet block rest grok_pct grok_resets grok_usage_json
  local opus_until sonnet_until gemini_pro_until gemini_flash_until grok_until clarified now
  if [[ -n "${PROXY_AGENT_STUB_GEMINI_PRO:-}" || -n "${PROXY_AGENT_STUB_GEMINI:-}" || -n "${PROXY_AGENT_STUB_GROK:-}" || -n "${PROXY_AGENT_STUB_AG_OPUS:-}" || -n "${PROXY_AGENT_STUB_AG_SONNET:-}" || -n "${PROXY_AGENT_STUB_GROK_USED_PCT:-}" ]]; then
    gemini="$(proxy_agent_norm_rf "${PROXY_AGENT_STUB_GEMINI:-fail}")"
    gemini_pro="$(proxy_agent_norm_rf "${PROXY_AGENT_STUB_GEMINI_PRO:-fail}")"
    if [[ -n "${PROXY_AGENT_STUB_GROK:-}" ]]; then
      grok="$PROXY_AGENT_STUB_GROK"
    else
      grok="$(proxy_agent_probe_grok 2>/dev/null || printf 'fail\n')"
    fi
    opus="$(proxy_agent_norm_rf "${PROXY_AGENT_STUB_AG_OPUS:-fail}")"
    sonnet="$(proxy_agent_norm_rf "${PROXY_AGENT_STUB_AG_SONNET:-fail}")"
    grok_pct="${PROXY_AGENT_STUB_GROK_USED_PCT:-none}"
    grok_resets="${PROXY_AGENT_STUB_GROK_RESETS:-}"
    case "$grok" in
      fail|[0-9][0-9][0-9]) ;;
      *) grok=fail;;
    esac
    printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n' "$gemini" "$grok" "$opus" "$sonnet" "$grok_pct" "$grok_resets" "$gemini_pro"
    return 0
  fi
  cached="$(proxy_agent_read_cache "$state" 2>/dev/null || printf 'miss\n')"
  if [[ "$cached" != "miss" && "$cached" == *$'\n'* ]]; then
    printf '%s\n' "$cached"
    return 0
  fi
  # One live probe for a burst; everyone else waits for quota.json (5 min TTL).
  local lock="$state/probe.lock" held=0 mtime i
  if [[ -d "$lock" ]]; then
    now="$(date +%s 2>/dev/null || printf '0\n')"
    mtime="$(stat -f %m "$lock" 2>/dev/null || printf '0\n')"
    case "$mtime" in
      ''|*[!0-9]*) mtime=0;;
    esac
    if [[ $((now - mtime)) -gt 45 ]]; then
      rmdir "$lock" 2>/dev/null || true
    fi
  fi
  if mkdir "$lock" 2>/dev/null; then
    held=1
  else
    i=0
    while [[ "$i" -lt 60 ]]; do
      cached="$(proxy_agent_read_cache "$state" 2>/dev/null || printf 'miss\n')"
      if [[ "$cached" != "miss" && "$cached" == *$'\n'* ]]; then
        printf '%s\n' "$cached"
        return 0
      fi
      sleep 0.5
      i=$((i + 1))
    done
  fi
  if [[ "$held" -eq 1 ]]; then
    cached="$(proxy_agent_read_cache "$state" 2>/dev/null || printf 'miss\n')"
    if [[ "$cached" != "miss" && "$cached" == *$'\n'* ]]; then
      rmdir "$lock" 2>/dev/null || true
      printf '%s\n' "$cached"
      return 0
    fi
  fi
  block="$(proxy_agent_probe_gemini 2>/dev/null || printf 'fail\nfail\nfail\nfail\n')"
  grok="$(proxy_agent_probe_grok 2>/dev/null || printf 'fail\n')"
  gemini="${block%%$'\n'*}"
  rest="${block#*$'\n'}"
  opus="${rest%%$'\n'*}"
  rest="${rest#*$'\n'}"
  sonnet="${rest%%$'\n'*}"
  gemini_pro="${rest#*$'\n'}"
  gemini_pro="${gemini_pro%%$'\n'*}"
  gemini="$(proxy_agent_norm_rf "$gemini")"
  opus="$(proxy_agent_norm_rf "$opus")"
  sonnet="$(proxy_agent_norm_rf "$sonnet")"
  gemini_pro="$(proxy_agent_norm_rf "$gemini_pro")"
  grok="${grok%%$'\n'*}"
  case "$grok" in
    fail|[0-9][0-9][0-9]) ;;
    *) grok=fail;;
  esac
  opus_until="$(proxy_agent_read_until "$state" ag_opus_exhausted_until claude-opus-5-5-high 2>/dev/null || printf '0\n')"
  sonnet_until="$(proxy_agent_read_until "$state" ag_sonnet_exhausted_until claude-sonnet-5-5-high 2>/dev/null || printf '0\n')"
  gemini_pro_until="$(proxy_agent_read_until "$state" gemini_pro_exhausted_until gemini-4-argon-high 2>/dev/null || printf '0\n')"
  gemini_flash_until="$(proxy_agent_read_until "$state" gemini_flash_exhausted_until gemini-3.8-flash-high 2>/dev/null || printf '0\n')"
  grok_until="$(proxy_agent_read_until "$state" grok_exhausted_until grok-4.7 2>/dev/null || printf '0\n')"
  opus_until="${opus_until%%$'\n'*}"
  sonnet_until="${sonnet_until%%$'\n'*}"
  gemini_pro_until="${gemini_pro_until%%$'\n'*}"
  gemini_flash_until="${gemini_flash_until%%$'\n'*}"
  grok_until="${grok_until%%$'\n'*}"
  clarified="$(proxy_agent_clarify_claude "$opus" claude-opus-5-5-high "$opus_until")"
  opus="${clarified%%$'\n'*}"
  opus_until="${clarified#*$'\n'}"
  opus_until="${opus_until%%$'\n'*}"
  clarified="$(proxy_agent_clarify_claude "$sonnet" claude-sonnet-5-5-high "$sonnet_until")"
  sonnet="${clarified%%$'\n'*}"
  sonnet_until="${clarified#*$'\n'}"
  sonnet_until="${sonnet_until%%$'\n'*}"
  clarified="$(proxy_agent_clarify_claude "$gemini_pro" gemini-4-argon-high "$gemini_pro_until")"
  gemini_pro="${clarified%%$'\n'*}"
  gemini_pro_until="${clarified#*$'\n'}"
  gemini_pro_until="${gemini_pro_until%%$'\n'*}"
  now="$(date +%s 2>/dev/null || printf '0')"
  case "$now" in ''|*[!0-9]*) now=0;; esac
  case "$gemini_flash_until" in ''|*[!0-9]*) gemini_flash_until=0;; esac
  case "$grok_until" in ''|*[!0-9]*) grok_until=0;; esac
  if [[ "$gemini_flash_until" -gt "$now" ]]; then
    gemini="0"
  fi
  if [[ "$grok_until" -gt "$now" ]]; then
    grok="429"
  fi
  grok_pct="none"
  grok_resets=""
  if [[ -x "$HOME/.claude/scripts/grok-weekly-usage.sh" ]]; then
    grok_usage_json="$("$HOME/.claude/scripts/grok-weekly-usage.sh" 2>/dev/null || printf '{"error":"exec_failed"}\n')"
    eval "$(python3 -I - "$grok_usage_json" <<'PY'
import json, sys
raw = sys.argv[1]
pct = "none"
resets = ""
try:
    data = json.loads(raw)
    if "used_pct" in data and isinstance(data["used_pct"], (int, float)):
        pct = str(int(data["used_pct"]))
    if "resets" in data and isinstance(data["resets"], str):
        resets = data["resets"].replace('"', '').replace("'", "")
except Exception:
    pct = "fail"
print(f'grok_pct="{pct}"')
print(f'grok_resets="{resets}"')
PY
)"
  fi
  proxy_agent_write_cache "$state" "$gemini" "$grok" "$opus" "$sonnet" "$grok_pct" "$grok_resets" "$opus_until" "$sonnet_until" "$gemini_pro" "$gemini_pro_until" "$gemini_flash_until" "$grok_until" 2>/dev/null || true
  if [[ "$held" -eq 1 ]]; then
    rmdir "$lock" 2>/dev/null || true
  fi
  printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n' "$gemini" "$grok" "$opus" "$sonnet" "$grok_pct" "$grok_resets" "$gemini_pro"
}

proxy_agent_canonical_id() {
  # No Codex for proxies (Rajiv C0ALZJHGE49 ts 1794834402.077499): while ~/.claude/state/proxy-no-codex exists, gpt-* ids remap to Claude equivalents.
  if [[ -e "$HOME/.claude/state/proxy-no-codex" ]]; then
    case "$1" in
      luna|gpt-6-luna) printf 'claude-haiku-5-5\n'; return;;
      terra|gpt-6.1-terra) printf 'claude-sonnet-5-5\n'; return;;
      sol|gpt-6.1-sol|gpt-6.2-sol) printf 'claude-opus-5-5\n'; return;;
      astra|gpt-6-astra) printf 'claude-fable-5-2\n'; return;;
    esac
  fi
  case "$1" in
    luna|gpt-6-luna) printf 'gpt-6-luna\n';;
    terra|gpt-6.1-terra) printf 'gpt-6.1-terra\n';;
    sol|gpt-6.2-sol) printf 'gpt-6.2-sol\n';;  # sol alias -> 6.2 (Rajiv ts 1794940770.892719)
    gpt-6.1-sol) printf 'gpt-6.1-sol\n';;
    astra|gpt-6-astra) printf 'gpt-6-astra\n';;
    grok|grok-4.7) printf 'grok-4.7\n';;
    # Antigravity (Gemini + ag-* Claude) is banned: only grok, claude, codex (Rajiv ts 1794660714.234059). Old aliases remap to Claude equivalents.
    gemini-pro|gemini-4-argon-high|carbon|gemini-4-carbon-high|ag-opus|claude-opus-5-5-high) printf 'claude-opus-5-5\n';;
    gemini-flash|gemini-3.8-flash-high) printf 'claude-haiku-5-5\n';;
    hydrogen|gemini-4-hydrogen-xhigh|ag-sonnet|claude-sonnet-5-5-high) printf 'claude-sonnet-5-5\n';;
    *) printf '%s\n' "$1";;
  esac
}

# Epoch until which $1 (canonical model id) stays exhausted, or 0. Read-only.
proxy_agent_exhausted_until() {
  local id="$1" slot until
  case "$id" in
    gemini-3.8-flash-high) slot=gemini_flash_exhausted_until;;
    grok-4.7) slot=grok_exhausted_until;;
    gemini-4-argon-high) slot=gemini_pro_exhausted_until;;
    gemini-4-hydrogen-xhigh) slot=hydrogen_exhausted_until;;
    claude-opus-5-5-high) slot=ag_opus_exhausted_until;;
    claude-sonnet-5-5-high) slot=ag_sonnet_exhausted_until;;
    gpt-6-luna) slot=luna_exhausted_until;;
    gpt-6.1-terra) slot=terra_exhausted_until;;
    gpt-6.1-sol) slot=sol_exhausted_until;;
    gpt-6-astra) slot=astra_exhausted_until;;
    *) slot="${id}_exhausted_until";;
  esac
  until="$(proxy_agent_read_until "$(proxy_agent_state_dir)" "$slot" "$id" 2>/dev/null || printf '0\n')"
  until="${until%%$'\n'*}"
  case "$until" in
    ''|*[!0-9]*) until=0;;
  esac
  printf '%s\n' "$until"
}

# Print the id/alias to use for $1 (main|haiku|sonnet|opus) given the chosen id in $2.
# An open exhaustion window is never selected. Next choice is grok-4.7, then the
# role's existing GPT fallback (luna for main/haiku, terra for sonnet/opus).
# Logs one PROXY_AGENT_MODEL_REMAP line when the choice changes. Does not write quota.json.
proxy_agent_avoid_exhausted() {
  local role="$1" chosen="$2" id until now alt alt_until fb state
  state="$(proxy_agent_state_dir)"
  if [[ ! -f "$state/quota.json" ]]; then
    printf '%s\n' "$chosen"
    return 0
  fi
  id="$(proxy_agent_canonical_id "$chosen")"
  until="$(proxy_agent_exhausted_until "$id")"
  now="$(date +%s 2>/dev/null || printf '0')"
  case "$now" in ''|*[!0-9]*) now=0;; esac
  if [[ "$until" -le "$now" ]]; then
    printf '%s\n' "$chosen"
    return 0
  fi
  alt="grok-4.7"
  if [[ "$id" != "$alt" ]]; then
    alt_until="$(proxy_agent_exhausted_until "$alt")"
    if [[ "$alt_until" -le "$now" ]]; then
      printf 'PROXY_AGENT_MODEL_REMAP role=%s from=%s to=%s until=%s\n' "$role" "$id" "$alt" "$until" >&2
      if [[ "$role" == "main" ]]; then
        printf 'grok\n'
      else
        printf '%s\n' "$alt"
      fi
      return 0
    fi
  fi
  case "$role" in
    haiku|main) fb="gpt-6-luna";;
    *) fb="gpt-6.1-terra";;
  esac
  printf 'PROXY_AGENT_MODEL_REMAP role=%s from=%s to=%s until=%s\n' "$role" "$id" "$fb" "$until" >&2
  if [[ "$role" == "main" ]]; then
    case "$fb" in
      gpt-6-luna) printf 'luna\n';;
      gpt-6.1-terra) printf 'terra\n';;
      *) printf '%s\n' "$fb";;
    esac
  else
    printf '%s\n' "$fb"
  fi
}

proxy_agent_apply_exhaustion_remaps() {
  local remapped
  remapped="$(proxy_agent_avoid_exhausted main "$MODEL")"
  MODEL="$remapped"
  remapped="$(proxy_agent_avoid_exhausted haiku "$HAIKU_MODEL")"
  HAIKU_MODEL="$remapped"
  remapped="$(proxy_agent_avoid_exhausted sonnet "$SONNET_MODEL")"
  SONNET_MODEL="$remapped"
  remapped="$(proxy_agent_avoid_exhausted opus "$OPUS_MODEL")"
  OPUS_MODEL="$remapped"
}

proxy_agent_hydrogen_usable() {
  [[ "${PROXY_AGENT_NO_HYDROGEN:-0}" != 1 ]] || return 1
  local until now probe status reset
  until="$(proxy_agent_exhausted_until gemini-4-hydrogen-xhigh)"
  now="$(date +%s 2>/dev/null || printf '0')"
  case "$now" in ''|*[!0-9]*) now=0;; esac
  [[ "$until" -le "$now" ]] || return 1
  if [[ -n "${PROXY_AGENT_STUB_HYDROGEN:-}" ]]; then
    [[ "$PROXY_AGENT_STUB_HYDROGEN" == 200 || "$PROXY_AGENT_STUB_HYDROGEN" == available ]]
    return
  fi
  mkdir -p "$(proxy_agent_state_dir)" 2>/dev/null || return 1
  probe="$(PROXY_AGENT_CLAUDE_PROBE_MODEL=gemini-4-hydrogen-xhigh proxy_agent_probe_claude_messages 2>/dev/null || printf 'fail\n0\n')"
  status="${probe%%$'\n'*}"
  reset="${probe#*$'\n'}"
  if [[ "$status" == 429 ]]; then
    python3 -I - "$(proxy_agent_state_dir)/quota.json" "$reset" <<'PY'
import json, os, sys, tempfile, time
path, raw = sys.argv[1:]
try: seconds = max(1, int(raw))
except Exception: seconds = 3600
until = int(time.time()) + seconds
try: data = json.load(open(path))
except Exception: data = {}
if not isinstance(data, dict): data = {}
eu = data.get("exhausted_until") if isinstance(data.get("exhausted_until"), dict) else {}
eu["gemini-4-hydrogen-xhigh"] = until
data.update({"cached_at": 0, "hydrogen_exhausted_until": until,
              "gemini-4-hydrogen-xhigh_exhausted_until": until, "exhausted_until": eu})
folder = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=folder, prefix=".quota-")
with os.fdopen(fd, "w") as stream: json.dump(data, stream); stream.write("\\n")
os.chmod(tmp, 0o600); os.replace(tmp, path)
PY
  fi
  [[ "$status" == 200 ]]
}

# Subscription-first (Rajiv D0AMF0XE6TS thread 1794573312.657559, ts 1794578432.554649): "we have to use antrigravity
# and grok when it's available. i guess grok4.7 is at sol/opus level, gemini hydrogen is sonnet and carbon opus. we
# should try these first and then fallback to the normal ladder." Carbon has no CLIProxyAPI id yet (400 unknown provider).
proxy_agent_sub_usable() {
  local id="$1" until now probe status reset
  [[ "${PROXY_AGENT_NO_SUBSCRIPTION:-0}" != 1 ]] || return 1
  until="$(proxy_agent_exhausted_until "$id")"
  now="$(date +%s 2>/dev/null || printf '0')"
  case "$until" in ''|*[!0-9]*) until=0;; esac
  [[ "$until" -le "$now" ]] || return 1
  mkdir -p "$(proxy_agent_state_dir)" 2>/dev/null || return 1
  probe="$(PROXY_AGENT_CLAUDE_PROBE_MODEL="$id" proxy_agent_probe_claude_messages 2>/dev/null || printf 'fail\n0\n')"
  status="${probe%%$'\n'*}"
  reset="${probe#*$'\n'}"
  if [[ "$status" == 429 || "$status" == 402 ]]; then
    python3 -I - "$(proxy_agent_state_dir)/quota.json" "$reset" "$id" <<'PY' || true
import json, os, sys, tempfile, time
path, raw, mid = sys.argv[1:]
try: seconds = max(1, int(raw))
except Exception: seconds = 3600
until = int(time.time()) + seconds
try: data = json.load(open(path))
except Exception: data = {}
if not isinstance(data, dict): data = {}
eu = data.get("exhausted_until") if isinstance(data.get("exhausted_until"), dict) else {}
eu[mid] = max(until, int(eu.get(mid, 0) or 0))
data.update({"cached_at": 0, mid + "_exhausted_until": eu[mid], "exhausted_until": eu})
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".quota-")
with os.fdopen(fd, "w") as stream: json.dump(data, stream); stream.write("\n")
os.chmod(tmp, 0o600); os.replace(tmp, path)
PY
  fi
  case "$status" in 200) return 0;; 402|429) return 1;; *) return 2;; esac
}

# Daily dial (morning brief, proxy-burn-dial.py --apply) writes the caps here; explicit env still wins.
_PROXY_TOTAL_CAP_ENV="${PROXY_AGENT_TOTAL_CAP:-}"
if [[ -r "$HOME/.claude/state/proxy-caps.env" ]]; then
  while IFS== read -r _k _v; do
    case "$_k" in
      PROXY_AGENT_TOTAL_CAP|PROXY_AGENT_CLAUDE_CAP|PM_SUBAGENT_CAP|CODEX_PROXY_CAP|CLAUDE_PROXY_CAP|GROK_PROXY_CAP|OPENCODE_PROXY_CAP) [[ -z "${!_k:-}" && "$_v" =~ ^[0-9]+$ ]] && printf -v "$_k" %s "$_v";;
      PROXY_BURN_GATE) [[ -z "${!_k:-}" && ( "$_v" == enforce || "$_v" == shadow ) ]] && printf -v "$_k" %s "$_v";;
    esac
  done < "$HOME/.claude/state/proxy-caps.env"
fi
PROXY_BURN_GATE="${PROXY_BURN_GATE:-enforce}"
if [[ "$PROXY_BURN_GATE" != enforce && "$PROXY_BURN_GATE" != shadow ]]; then
  printf 'proxy-burn: invalid mode=%s; using enforce\n' "$PROXY_BURN_GATE" >&2
  PROXY_BURN_GATE=enforce
fi
# Concurrent-Claude cap (Rajiv D0AMF0XE6TS thread 1794624936.567599, ts 1794632685.538419: "yes, cap concurrent claude proxies").
# Counts live proxy claude processes whose --model is a claude-* id; at/over the cap, claude-* tiers are skipped (reason claude_cap).
PROXY_AGENT_CLAUDE_CAP="${PROXY_AGENT_CLAUDE_CAP:-5}"  # lowered 8→5 at 05:48Z; 5→3 at 09:46Z; raised 3→5 at 12:51Z (Rajiv D0AMF0XE6TS thread 1794659684.547559 ts 1794660569.044089: "raise it"; 4 Claude team accounts)
# Only long-running roles participate in either cap (Rajiv ts 1794737134.234539).
proxy_agent_is_capped() {
  case "$1" in hd-planner|hd-builder|hd-proof-qa|pm-hotfix|pm-proof-qa|hd-investigator|superproofer) return 0;; *) return 1;; esac
}
proxy_agent_live_count() {
  python3 -I "$HOME/.claude/scripts/proxy-cap-count.py" "$1" || printf '999\n'
}
# OpenCode Go pool: hy4-flash-free, deepseek-v4.1-flash, glm-5.3 and muse-spark-1.3-contributor share ONE cap (OPENCODE_PROXY_CAP, default 2).
# Membership is the router-equivalent identity (CTO REVISE of ec3729ed, C0ALZJHGE49 thread 1795010687.065819, ts 1795010788.320629): a decorated
# `deepseek-v4.1-flash(high)` is the same pool id as the canonical name. proxy_agent_router_identity is defined just below; bash resolves it at call time.
proxy_agent_is_opencode_id() {
  local id
  id="$(proxy_agent_router_identity "$1")"
  case "$id" in hy4-flash-free|deepseek-v4.1-flash|glm-5.3|muse-spark-1.3-contributor) return 0;; *) return 1;; esac
}
# Muse Spark Contributor trains on prompts: last-resort emergency entry (Rajiv D0AMF0XE6TS thread 1794926930.078169, ts 1794929496.349809).
proxy_agent_is_emergency_muse() {
  [[ "$(proxy_agent_router_identity "$1")" == muse-spark-1.3-contributor ]]
}
# Free-model guard (Rajiv DM D0AMF0XE6TS ts 1794984605.382609, "yes", to: block free/trial OpenCode models (hy4-flash-free,
# muse-spark-1.3-contributor) for agents that read customer content or write prod). Trigger 2026-11-18: customer-data and prod-write
# launches landed on unvetted free models. Only these two ids; deepseek-v4.1-flash and glm-5.3 are not part of that decision.
# ROUTER-EQUIVALENT IDENTITY (CTO REVISE of 3003f414, C0ALZJHGE49 thread 1794985651.254889, ts 1794989163.540309): the literal-id checks above were
# bypassable by a decorated name. CLIProxyAPI 8.0.50 strips a supported reasoning suffix `model(value)` and resolves the provider from the BASE
# model, so `hy4-flash-free(high)` passed a literal compare yet routed to the restricted upstream; resolve_model passes any unrecognized string
# through unchanged, so the launch carried it verbatim. The deny therefore runs on the identity the router derives: lowercase, cut at the first
# `(` (router thinking suffix) or `[` (Claude Code context suffix such as [1m]), trim whitespace. Deliberately a SUPERSET of the router's own
# parse (cuts at the FIRST bracket, case-folds, trims unicode space): this is a deny-side check, so over-matching only refuses more, and no
# permitted id contains `(` or `[`, so a permitted model's identity is its own name and its original argument still flows to --model unchanged.
# Plain ids (the common path, every tier-list entry) skip the interpreter.
proxy_agent_router_identity() {
  case "$1" in
    ''|*[!a-z0-9._-]*) ;;
    *) printf '%s' "$1"; return 0;;
  esac
  python3 -I -c 'import re, sys; print(re.split(r"[(\[]", sys.argv[1].lower(), 1)[0].strip(), end="")' "$1" 2>/dev/null && return 0
  # No interpreter: ASCII-only sed fallback (same cut), never an empty pass-through of the raw string.
  printf '%s' "$1" | tr '\n\r\t' '   ' | tr '[:upper:]' '[:lower:]' | sed -E 's/[([].*$//; s/^[[:space:]]+//; s/[[:space:]]+$//'
}
# The two restricted upstreams and the router aliases that resolve to them (cliproxyapi.conf openai-compatibility `opencode-go`: alias
# `hy4-flash` -> hy4-flash-free, alias `opencode-go-muse-spark-1.3-contributor` -> muse-spark-1.3-contributor). An alias is the same restricted
# model under another name, so it is the same unconditional denial. Single source: the guard, the tier pick and the generated continuation
# all read this list.
PROXY_AGENT_FREE_TRIAL_IDS="hy4-flash-free hy4-flash muse-spark-1.3-contributor opencode-go-muse-spark-1.3-contributor"
proxy_agent_is_free_trial_id() {
  local _id
  for _id in $PROXY_AGENT_FREE_TRIAL_IDS; do [[ "$1" == "$_id" ]] && return 0; done
  return 1
}
# UNCONDITIONAL (CTO REVISE of 970d140 / 9ea1b9b, C0ALZJHGE49 thread 1794985651.254889, ts 1794987952.900739: grant eligibility only from an
# existing restricted execution profile whose authority covers the actual inputs, tools and deployment through resume and continuation;
# "if that authority cannot be established, leave the two IDs ineligible"). No such profile exists: a caller-declared --data-scope, a brief
# or role check, a QA replay mode and a recorded verdict are all assertions that do not bind what the launched tools can read or write.
# So hy4-flash-free and muse-spark-1.3-contributor are never eligible for ANY launch, resume or continuation: there is no allow path, no
# declaration, no sidecar and no env that lifts it. Callers use a vetted model. Only the two ids are covered; deepseek-v4.1-flash, glm-5.3,
# the caps, the Muse emergency gate and every paid route are untouched. Refused in three places: the tier pick (falls to the next tier
# model, before the emergency gate and before `last` is set), the resolved primary (raw --model, and --resume which requires an explicit
# model), and the auto-continuation block of the generated run.sh.
proxy_agent_free_guard_refuses() {
  proxy_agent_is_free_trial_id "$(proxy_agent_router_identity "$1")"
}
# Unresolved automatic selection (CTO REVISE of c901e33 / f1a7f8a, C0ALZJHGE49 thread 1794985651.254889, ts 1794991405.296039). Only the LITERAL
# `auto` is a tier request in proxy_agent_select; a decorated `auto(high)` / `AUTO` / `auto[1m]` falls through to RAW and reaches the router
# verbatim, and CLIProxyAPI 8.0.50 derives the base identity `auto` before resolving it and may pick ANY registered model (including the two
# restricted ones), so the free-model deny above, which sees only the identity `auto`, never fires. A resolved primary is a concrete model:
# literal `auto` was replaced by the tier pick before the primary guard, so an `auto` identity here (fresh launch, --resume --model, or a
# generated continuation) is always an unvetted router choice and is refused.
proxy_agent_is_unresolved_auto() {
  [[ "$(proxy_agent_router_identity "$1")" == auto ]]
}
# Ordered tiers; escalation after a stall is LIGHT -> MID -> TOP. This is the single source of the tier lists: the picker and the
# emergency eligibility check (proxy_agent_emergency_open) both read it. Prints the model ids, no-Codex flag applied.
proxy_agent_tier_models() {
  local models _m _kept=""
  case "$1" in
    # OpenCode Go pool (Rajiv D0AMF0XE6TS thread 1794926930.078169, ts 1794930258.014469): hy4-flash-free joins LIGHT, deepseek-v4.1-flash joins MID.
    # Every tier ends with the emergency-only Muse Spark entry (see proxy_agent_is_emergency_muse). Grok stays in its existing slot.
    LIGHT) models="claude-haiku-5-5 hy4-flash-free grok-4.7 gpt-6-luna muse-spark-1.3-contributor";;
    # glm-5.3 registered, explicit --model only until one test build passes, Rajiv condition ts 1794942421.644269 (PM steer 2026-11-18: not in any automatic tier list).
    MID) models="claude-sonnet-5-5 deepseek-v4.1-flash grok-4.7 gpt-6.1-terra muse-spark-1.3-contributor";;
    TOP) models="claude-opus-5-5 grok-4.7 gpt-6.1-terra muse-spark-1.3-contributor";;  # sol dropped (Rajiv C0ALZJHGE49 ts 1794720589.429929: "cut down sol usage on proxies")
    # gpt-6.2-sol replaces gpt-6.1-sol (the "sol" alias); gpt-6.1-terra stays in the tiers (Rajiv D0AMF0XE6TS thread 1794939318.063889, ts 1794941131.743129: "6.1 terra should stay. 6.2 sol should replace 6.1 sol").
    # Per-role ladders (Rajiv D0AMF0XE6TS thread 1794624936.567599: ts 1794639918.387399 ladder, ts 1794640102.756179 hydrogen in the mid rungs).
    # First id not in 429/402 cooldown wins within a rung; claude-* ids still skip at the cap (claude_cap), then the next id in the SAME rung is tried.
    PLAN) models="grok-4.7 claude-opus-5-5 gpt-6.1-terra muse-spark-1.3-contributor";;
    # Grok before Codex in every rung: Codex must last to Thu (Rajiv ts 1794660714.234059 "only grok, claude, codex").
    # Dev ladder (Rajiv C0ALZJHGE49 thread 1794665799.784789, ts 1794665970.826379): haiku/luna → sonnet/terra → opus/sol. No Grok on dev rungs.
    BUILD1) models="claude-haiku-5-5 deepseek-v4.1-flash gpt-6-luna muse-spark-1.3-contributor";;  # DeepSeek after Claude (Rajiv C0ALZJHGE49 ts 1794998807.846819 "ok for 2")
    BUILD2) models="claude-sonnet-5-5 gpt-6.1-terra muse-spark-1.3-contributor";;
    # S149 (Rajiv ts 1794941131.743129 "6.1 terra should stay. 6.2 sol should replace 6.1 sol"): gpt-6.2-sol follows terra on the top conveyor rungs only; never MID/TOP/PLAN (ts 1794720589.429929).
    BUILD3) models="claude-opus-5-5 gpt-6.1-terra gpt-6.2-sol muse-spark-1.3-contributor";;
    PROOF1) models="claude-sonnet-5-5 deepseek-v4.1-flash gpt-6.1-terra muse-spark-1.3-contributor";;  # DeepSeek after Claude (Rajiv ts 1794998807.846819 "ok for 2")
    PROOF2) models="claude-opus-5-5 gpt-6.1-terra gpt-6.2-sol muse-spark-1.3-contributor";;
    # 12b reviews: carbon -> fable -> astra (Rajiv D0AMF0XE6TS thread 1794624936.567599, ts 1794640502.657479 "the 12b reviewer ladder can be carbon/fable/astra").
    # Supersedes the earlier fable-first list (thread 1794573312.657559, ts 1794623641.572859). Reviews never skip claude-* at the cap (see below).
    REVIEW) models="claude-fable-5-2 gpt-6-astra muse-spark-1.3-contributor";;
    *) return 1;;
  esac
  # No Codex for proxies (Rajiv ts 1794834402.077499): drop gpt-* from every rung while the flag file exists.
  if [[ -e "$HOME/.claude/state/proxy-no-codex" ]]; then
    for _m in $models; do [[ "$_m" == gpt-* ]] || _kept="${_kept:+$_kept }$_m"; done
    models="$_kept"
  fi
  printf '%s\n' "$models"
}
# Emergency Muse gate (CTO REVISE 2026-11-17, C0ALZJHGE49 thread 1794931098.013929, ts 1794932145.795929): Muse is emergency-only
# across EVERY route, not merely last within one rung.
# 0 when route $1 is KNOWN unusable right now: refused by the burn gate (capped roles, enforce mode), at its own pool cap, or in a
# 402/429 cooldown (recorded, or a fresh probe answer). 1 when it is usable OR unknown: a probe failure or the operator's
# no-subscription switch never proves exhaustion.
proxy_agent_route_exhausted() {
  local id="$1" rc
  [[ "${PROXY_AGENT_NO_SUBSCRIPTION:-0}" != 1 ]] || return 1
  if proxy_agent_burn_refused "$id"; then return 0; fi
  if proxy_agent_pool_blocked "$id"; then return 0; fi
  if proxy_agent_sub_usable "$id"; then return 1; else rc=$?; fi
  [[ "$rc" == 1 ]]
}
# 0 when Muse may be used: EVERY eligible non-emergency route in EVERY tier list (Claude, Grok, Codex and the other OpenCode ids,
# not only the rung being resolved) is known exhausted. The first route that is usable or unknown closes the gate and is named in
# PA_EMERGENCY_BLOCKER. The verdict is computed once per launch and shared by the tier pick and the final-primary guard.
PA_EMERGENCY_VERDICT=""
PA_EMERGENCY_BLOCKER=""
proxy_agent_emergency_open() {
  local tier id seen=" "
  if [[ -n "${PA_EMERGENCY_VERDICT:-}" ]]; then
    [[ "$PA_EMERGENCY_VERDICT" == open ]] && return 0 || return 1
  fi
  for tier in LIGHT MID TOP PLAN BUILD1 BUILD2 BUILD3 PROOF1 PROOF2 REVIEW; do
    for id in $(proxy_agent_tier_models "$tier"); do
      proxy_agent_is_emergency_muse "$id" && continue
      case "$seen" in *" $id "*) continue;; esac
      seen="$seen$id "
      if ! proxy_agent_route_exhausted "$id"; then
        PA_EMERGENCY_VERDICT=closed
        PA_EMERGENCY_BLOCKER="$id"
        return 1
      fi
    done
  done
  PA_EMERGENCY_VERDICT=open
  return 0
}
# Picks the first usable model of a tier into PICK_MODEL. Second argument "nested" is used for the Haiku/Sonnet/Opus alias picks:
# those aliases are exported to the child, and an OpenCode id there would run uncounted by OPENCODE_PROXY_CAP (the live count sees
# only the child's primary --model), so nested picks stay on the existing approved Claude/Grok/Codex routes and never use the pool.
proxy_agent_pick_tier() {
  local tier="$1" nested="${2:-}" id skipped="" last="" models rc claude_live emergency_ok=1 gate _m _kept=""
  claude_live=0
  if proxy_agent_is_capped "$TYPE"; then claude_live="$(proxy_agent_live_count claude)"; fi
  models="$(proxy_agent_tier_models "$tier")" || return 1
  if [[ "$nested" == nested ]]; then
    for _m in $models; do proxy_agent_is_opencode_id "$_m" || _kept="${_kept:+$_kept }$_m"; done
    models="$_kept"
  fi
  for id in $models; do
    # Free-model guard (unconditional): before the emergency gate and before `last` is set, so a refused id can never become the fallback pick.
    if proxy_agent_free_guard_refuses "$id"; then
      skipped="${skipped:+$skipped,}$id:free_model_guard"
      printf 'proxy-agent: REFUSED free-model guard model=%s type=%s (free/trial ids are unconditionally ineligible; trying the next tier model)\n' "$id" "${TYPE:-}" >&2
      continue
    fi
    if proxy_agent_is_emergency_muse "$id"; then
      # Emergency only. Rung-local: an earlier probe failure in this rung is unknown, not exhausted. Global: every eligible
      # non-emergency route in every tier list must also be capped, burn-refused or cooled (proxy_agent_emergency_open).
      # The fallback_last pick below never lands on Muse.
      gate=""
      if [[ "$emergency_ok" != 1 ]]; then gate=rung_probe_failed
      elif ! proxy_agent_emergency_open; then gate="${PA_EMERGENCY_BLOCKER:-unknown}"; fi
      if [[ -n "$gate" ]]; then
        skipped="${skipped:+$skipped,}$id:emergency_gate"
        printf 'proxy-agent: emergency_gate skip model=%s blocker=%s\n' "$id" "$gate" >&2
        continue
      fi
    else
      last="$id"
    fi
    if proxy_agent_is_capped "$TYPE" && [[ "$id" == claude-* && "${claude_live:-0}" -ge "$PROXY_AGENT_CLAUDE_CAP" ]]; then
      printf 'proxy-agent: telemetry claude_cap (%s live >= %s)\n' "$claude_live" "$PROXY_AGENT_CLAUDE_CAP" >&2
    fi
    # Burn decision runs BEFORE the usability probe: a refused pool never sends a provider request.
    if proxy_agent_burn_refused "$id"; then
      skipped="${skipped:+$skipped,}$id:burn_refused"
      continue
    fi
    # Per-pool concurrency. A Claude deny skips only Claude ids; Grok and Codex keep their own caps.
    # Exempt roles (and a P0 override) are telemetry-only: never skipped here. pm-triage-agent is not exempt.
    if proxy_agent_pool_blocked "$id"; then
      skipped="${skipped:+$skipped,}$id:pool_cap"
      printf 'proxy-agent: pool_cap skip model=%s\n' "$id" >&2
      continue
    fi
    if proxy_agent_sub_usable "$id"; then
      PICK_MODEL="$id"
      PICK_REASON="${skipped:+cooled:$skipped}"
      PICK_REASON="${PICK_REASON:-first_usable}"
      return 0
    else
      rc=$?
      if [[ "$rc" != 1 ]]; then
        skipped="${skipped:+$skipped,}$id:probe_failed"
        emergency_ok=0
      else
        skipped="${skipped:+$skipped,}$id"
      fi
    fi
  done
  if proxy_agent_burn_enforced; then
    # Fail closed for capped roles: no unchecked fallback is ever exported.
    PICK_MODEL=""
    printf 'proxy-agent: REFUSED no usable tier model (%s)\n' "$skipped" >&2
    return 75
  fi
  PICK_MODEL="$last"; PICK_REASON="fallback_last"
}

# Ordinary burn gate for capped roles in enforce mode (proxy-burn-shadow.py; overrides and exemptions live there).
# Applied to EVERY pick (primary and nested Haiku/Sonnet/Opus aliases) and always before a usability probe.
proxy_agent_burn_enforced() {
  [[ "$PROXY_BURN_GATE" == enforce ]] && proxy_agent_is_capped "$TYPE"
}
# Returns 0 when the burn gate refuses the model id, 1 when it allows it or does not apply.
proxy_agent_burn_refused() {
  proxy_agent_burn_enforced || return 1
  # Grok has no %/h budget. GROK_PROXY_CAP and a 402 cooldown decide; a weekly "unknown" must not queue it.
  case "$1" in
    grok-4.7|grok) return 1;;
  esac
  # OpenCode Go has no %/h budget meter: OPENCODE_PROXY_CAP and a 402/429 cooldown decide.
  if proxy_agent_is_opencode_id "$1"; then return 1; fi
  python3 -I "$(dirname "$0")/proxy-burn-shadow.py" "$1" "$TYPE" "${PROXY_AGENT_CAP_OVERRIDE:-}" >&2 && return 1
  return 0
}

# Caps do not apply to these launches (Rajiv C0ALZJHGE49 thread 1794919743.223239, ts 1794920944.182879,
# triage removed at ts 1794921123.696379). They still take the first non-cooled model.
proxy_agent_cap_exempt() {
  # A P0 override exempts even a role that is otherwise capped (triage included).
  if [[ "${PROXY_AGENT_CAP_OVERRIDE:-}" =~ ^P0:[0-9]+$ ]]; then
    return 0
  fi
  case "$1" in
    ci-repair-agent|e2e-failure-investigator|main-red-skip|pm-admission-reviewer|cleanup-pr-runner|pm-ops-tooling|mop-main-repair|autoscaler-main-repair|growth)
      return 0;;
  esac
  if [[ -r "${ROLE_BRIEF:-}" ]]; then
    local first=""
    IFS= read -r first < "$ROLE_BRIEF" || true
    if [[ "$1" == claude && "$first" =~ ^MODE:[[:space:]]*(heartbeat|digest)($|[[:space:]]) ]]; then
      return 0
    fi
    if [[ "$first" =~ ^[[:space:]]*(PRIORITY|priority)[[:space:]]*:[[:space:]]*P0($|[[:space:]]) ]]; then
      return 0
    fi
  fi
  return 1
}

proxy_agent_pool_live() {
  if [[ -n "${PROXY_AGENT_STUB_POOL_LIVE:-}" ]]; then
    printf '%s\n' "$PROXY_AGENT_STUB_POOL_LIVE"
    return 0
  fi
  python3 -I - "$1" <<'PY'
import re, subprocess, sys
pool = sys.argv[1]
# Same shape as proxy-cap-count.py: one count per real `claude -p` process, not the wrapper line that embeds --model.
pat = re.compile(r'(?:^|\s)\S*/claude\s+-p\s+--name\s+\S+\s+--model\s+(\S+)')
try:
    out = subprocess.check_output(["/bin/ps", "-axo", "command="], text=True, timeout=5)
except Exception:
    raise SystemExit(1)
n = 0
for line in out.splitlines():
    match = pat.search(line)
    if not match:
        continue
    raw = match.group(1)
    # Same cut as proxy_agent_router_identity: lowercase, drop the first '(' or '[' suffix, trim.
    # A decorated OpenCode primary must count toward OPENCODE_PROXY_CAP (CTO REVISE ts 1795010788.320629).
    model = re.split(r"[(\[]", raw.lower(), 1)[0].strip()
    kind = "claude" if model.startswith("claude-") else "codex" if model.startswith("gpt-") else "grok" if model.startswith("grok") else "opencode" if model in ("hy4-flash-free", "deepseek-v4.1-flash", "glm-5.3", "muse-spark-1.3-contributor") else ""
    if kind == pool:
        n += 1
print(n)
PY
}

# 0 when this model id is at or over its own pool cap. Exempt launches and an unreadable count are not blocked
# for Claude, Grok and Codex. The OpenCode Go pool is the exception: it has no %/h burn meter, so OPENCODE_PROXY_CAP
# applies to every new launch, including cap-exempt roles and a P0 override. Otherwise a cooled Claude tier falls
# through to unlimited deepseek-v4.1-flash (2026-11-18: cap 2, 9 live primaries, all ci-repair-agent or growth,
# logs "reason=cooled:claude-sonnet-5-5" with no pool_cap skip). The process match already counts those primaries;
# the early exemption return never consulted it. Existing processes are not touched.
# The id is the router-equivalent identity, so `deepseek-v4.1-flash(high)` is the same admission as the canonical id
# (CTO REVISE ts 1795010788.320629). An unreadable OpenCode count is not a free pass: the id is skipped and tier
# selection tries the next route. Claude, Grok and Codex stay fail-open on an unreadable count, and their role exemption stays.
proxy_agent_pool_blocked() {
  local id pool cap live
  id="$(proxy_agent_router_identity "$1")"
  case "$id" in
    claude-*) pool=claude; cap="${CLAUDE_PROXY_CAP:-${PROXY_AGENT_CLAUDE_CAP:-5}}";;
    gpt-*) pool=codex; cap="${CODEX_PROXY_CAP:-8}";;
    grok-4.7|grok) pool=grok; cap="${GROK_PROXY_CAP:-8}";;
    hy4-flash-free|deepseek-v4.1-flash|glm-5.3|muse-spark-1.3-contributor) pool=opencode; cap="${OPENCODE_PROXY_CAP:-2}";;
    *) return 1;;
  esac
  if [[ "$pool" != opencode ]] && proxy_agent_cap_exempt "$TYPE"; then return 1; fi
  case "$cap" in ''|*[!0-9]*) return 1;; esac
  # OpenCode cap 0 means the pool is off: blocked without counting, so an unreadable process list cannot let a launch through.
  if [[ "$pool" == opencode && "$cap" =~ ^0+$ ]]; then return 0; fi
  if ! live="$(proxy_agent_pool_live "$pool")"; then
    printf 'proxy-agent: pool_cap UNAVAILABLE pool=%s\n' "$pool" >&2
    if [[ "$pool" == opencode ]]; then return 0; fi
    return 1
  fi
  [[ "${live:-0}" -ge "$cap" ]]
}
# Final-primary OpenCode guard (CTO REVISE 2026-11-17, C0ALZJHGE49 thread 1794931098.013929, ts 1794932145.795929). Runs in
# proxy_agent_select on the RESOLVED primary, RAW included: a raw `--model hy4-flash-free` skips proxy_agent_pick_tier and the burn
# check treats OpenCode ids as unmetered, so without this an explicit pool id launched past OPENCODE_PROXY_CAP (even 0), and an
# explicit Muse skipped the emergency gate. Same pool_blocked decision as the picker: Claude/Grok/Codex exemptions
# still apply, the OpenCode pool cap does not (see proxy_agent_pool_blocked). An exemption is never emergency
# permission for Muse. Returns 75 on refusal, 0 otherwise.
proxy_agent_primary_pool_guard() {
  local _guard_id _guard_window _guard_max
  read -r _guard_id _guard_window _guard_max < <(resolve_model "$MODEL")
  # Free-model guard FIRST, on the router-equivalent identity of the resolved primary (the exact string that becomes --model): a decorated
  # `hy4-flash-free(high)` / `muse-spark-1.3-contributor[1m]` is not an exact OpenCode id, so the early return below used to let it through.
  # A raw `--model <restricted>` (or its alias) and a --resume of one have no tier to fall back within.
  if proxy_agent_free_guard_refuses "$_guard_id"; then
    printf 'proxy-agent: REFUSED free-model guard primary %s identity=%s type=%s (free/trial ids are unconditionally ineligible; name a vetted model)\n' "$_guard_id" "$(proxy_agent_router_identity "$_guard_id")" "${TYPE:-}" >&2
    return 75
  fi
  if proxy_agent_is_unresolved_auto "$_guard_id"; then
    printf 'proxy-agent: REFUSED unresolved automatic selection primary %s identity=auto type=%s (a decorated auto lets the router pick any model; use literal auto for the tier path or name a model)\n' "$_guard_id" "${TYPE:-}" >&2
    return 75
  fi
  case "$_guard_id" in
    gpt-*)
      if [[ -e "$HOME/.claude/state/proxy-no-codex" ]]; then
        printf 'proxy-agent: REFUSED primary %s (proxy-no-codex)\n' "$_guard_id" >&2
        return 75
      fi
      if proxy_agent_pool_blocked "$_guard_id"; then
        printf 'proxy-agent: REFUSED primary %s (codex pool_cap)\n' "$_guard_id" >&2
        return 75
      fi
      return 0;;
  esac
  proxy_agent_is_opencode_id "$_guard_id" || return 0
  if proxy_agent_pool_blocked "$_guard_id"; then
    printf 'proxy-agent: REFUSED primary %s (opencode pool_cap)\n' "$_guard_id" >&2
    return 75
  fi
  if proxy_agent_is_emergency_muse "$_guard_id" && ! proxy_agent_emergency_open; then
    printf 'proxy-agent: REFUSED primary %s (emergency_gate blocker=%s)\n' "$_guard_id" "${PA_EMERGENCY_BLOCKER:-unknown}" >&2
    return 75
  fi
  return 0
}
# Primary burn check (CTO REVISE 2026-11-15, C0ALZJHGE49 ts 1794754734.596349). Runs in proxy_agent_select right after the
# PRIMARY pick and BEFORE the nested alias picks, which make usability POSTs. A refused primary (e.g. a raw
# claude-opus-5-5, which skips pick_tier) therefore spends no alias probes. Same check, same decisions; only its position moved.
# Returns 75 when enforce mode refuses a capped role's primary; 0 otherwise (shadow mode never refuses).
proxy_agent_primary_burn_check() {
  local _burn_model _burn_window _burn_max _burn_rc=0
  read -r _burn_model _burn_window _burn_max < <(resolve_model "$MODEL")
  # Grok is balance-metered. The %/h shadow decision is "unknown" and must not refuse the launch.
  case "$_burn_model" in
    grok-4.7|grok) return 0;;
  esac
  # OpenCode Go has no %/h budget meter (cap + cooldown only), same as Grok.
  if proxy_agent_is_opencode_id "$_burn_model"; then return 0; fi
  python3 -I "$(dirname "$0")/proxy-burn-shadow.py" "$_burn_model" "$TYPE" "${PROXY_AGENT_CAP_OVERRIDE:-}" >&2 || _burn_rc=$?
  if [[ "$_burn_rc" -ne 0 ]]; then
    printf 'proxy-burn: mode=%s decision=would_refuse rc=%s\n' "$PROXY_BURN_GATE" "$_burn_rc" >&2
    if [[ "$PROXY_BURN_GATE" == enforce ]] && proxy_agent_is_capped "$TYPE"; then return 75; fi
  elif [[ "$PROXY_BURN_GATE" == enforce ]] && proxy_agent_is_capped "$TYPE" && [[ "${PROXY_AGENT_CAP_OVERRIDE:-}" =~ ^(P0:[0-9]+|rajiv:[0-9]+\.[0-9]+)$ ]]; then
    printf '%s BURN_OVERRIDE name=%s type=%s model=%s reason=%s\n' "$(date -u +%FT%TZ)" "${PROXY_AGENT_NAME:-}" "$TYPE" "$_burn_model" "$PROXY_AGENT_CAP_OVERRIDE" >> "$HOME/.claude/state/proxy-cap-overrides.log"
  fi
  return 0
}

# hd-* split compat (Rajiv D0AMF0XE6TS thread 1794624936.567599, ts 1794639434.146359 split, ts 1794639511.486209 hd prefix):
# pm-hotfix and pm-proof-qa stay launchable so queued/in-flight briefs and resumes keep working.
# pm-hotfix -> hd-planner when the brief's FIRST line is `MODE: PLAN`, otherwise hd-builder (MODE: IMPLEMENT/REWORK/none). pm-proof-qa -> hd-proof-qa.
proxy_agent_resolve_type() {
  local first_line=""
  REQUESTED_TYPE="$TYPE"
  case "$TYPE" in
    pm-proof-qa) TYPE=hd-proof-qa;;
    pm-hotfix)
      [[ -r "$ROLE_BRIEF" ]] && { IFS= read -r first_line < "$ROLE_BRIEF" || true; }
      if [[ "$first_line" =~ ^MODE:[[:space:]]*PLAN($|[[:space:]]) ]]; then TYPE=hd-planner; else TYPE=hd-builder; fi;;
    *) return 0;;
  esac
  printf 'proxy-agent: compat type %s -> %s\n' "$REQUESTED_TYPE" "$TYPE" >&2
}

proxy_agent_select() {
  local requested="$MODEL" first_line="" tier="RAW" rung="${RUNG:-1}" max_rung=0
  case "$TYPE" in hd-builder) max_rung=3;; hd-proof-qa) max_rung=2;; esac
  # An out-of-range rung climbs to the top rung of the role (proof-QA has two).
  if [[ "$max_rung" -gt 0 && "$rung" -gt "$max_rung" ]]; then rung="$max_rung"; fi
  case "$requested" in
    auto)
      [[ -r "$ROLE_BRIEF" ]] && { IFS= read -r first_line < "$ROLE_BRIEF" || true; }
      if [[ "$TYPE" == *-reviewer ]]; then tier=REVIEW  # all reviewer agents: Rajiv ts 1794623867.575529 "use fable for reviews"
      # Conveyor ladders (Rajiv ts 1794639918.387399): planner and investigator (stage 0, diagnose-only) PLAN, builder rung 1-3, proof-QA rung 1-2.
      elif [[ "$TYPE" == hd-planner || "$TYPE" == hd-investigator ]]; then tier=PLAN
      elif [[ "$TYPE" == hd-builder ]]; then tier="BUILD$rung"
      elif [[ "$TYPE" == hd-proof-qa ]]; then tier="PROOF$rung"
      elif [[ "$first_line" =~ ^MODE:[[:space:]]*PLAN($|[[:space:]]) ]]; then tier=TOP; else tier=MID; fi;;
    # Explicit aliases are the S131 escalation levers: on the conveyor roles they land on that role's rung.
    luna|haiku) case "$TYPE" in hd-builder) tier=BUILD1;; *) tier=LIGHT;; esac;;
    terra|sonnet) case "$TYPE" in hd-builder) tier=BUILD2;; hd-proof-qa) tier=PROOF1;; *) tier=MID;; esac;;
    opus) case "$TYPE" in hd-builder) tier=BUILD3;; hd-proof-qa) tier=PROOF2;; *) tier=TOP;; esac;;
    # S149: --model sol is the exact gpt-6.2-sol id (tier stays RAW, like astra). A tier here would let the picker return claude-opus-5-5 and override the alias.
    sol) ;;
  esac
  if [[ -n "${RUNG:-}" && ( "$requested" != auto || "$max_rung" -eq 0 ) ]]; then
    printf 'proxy-agent: --rung %s ignored (%s)\n' "$RUNG" "$([[ "$requested" != auto ]] && echo "explicit --model $requested" || echo "type $TYPE has no rungs")" >&2
  fi
  SELECT_TIER="$tier"; SELECT_REASON=first_usable
  # Effective rung for the run state (auto-escalation reads it back): BUILD1-3 / PROOF1-2 only; empty for raw models and other roles.
  case "$tier" in BUILD[123]|PROOF[12]) SELECT_RUNG="${tier: -1}";; *) SELECT_RUNG="";; esac
  # Exact sol on a conveyor role is that role's top rung (BUILD3 / PROOF2), so auto-escalation still reports top_rung instead of rung_unknown.
  if [[ "$requested" == sol && "$max_rung" -gt 0 ]]; then SELECT_RUNG="$max_rung"; fi
  if [[ "$tier" != RAW ]]; then
    proxy_agent_pick_tier "$tier" || return 75
    MODEL="$PICK_MODEL"; SELECT_REASON="$PICK_REASON"
  fi
  # OpenCode pool cap and Muse emergency gate on the resolved primary (covers RAW primaries too), before any alias usability POST.
  proxy_agent_primary_pool_guard || return 75
  # Primary burn decision first (covers RAW primaries too): a refused primary returns before any alias usability POST.
  proxy_agent_primary_burn_check || return 75
  # Nested aliases get the same burn gate. A refused alias leaves PICK_MODEL empty; the export block below fails it closed.
  # They never use the OpenCode pool (uncounted by its cap): "nested" keeps them on the existing Claude/Grok/Codex routes.
  proxy_agent_pick_tier LIGHT nested || true; HAIKU_MODEL="$PICK_MODEL"
  proxy_agent_pick_tier MID nested || true; SONNET_MODEL="$PICK_MODEL"
  proxy_agent_pick_tier TOP nested || true; OPUS_MODEL="$PICK_MODEL"
}

# Model table: alias | CLIProxyAPI model id | context window | max output tokens.
# Sources (checked 2026-11-08):
#   gpt-6-luna       OpenAI docs developers.openai.com/api/docs/models/gpt-6-luna: 1,050,000 ctx / 128,000 max output
#   gpt-6.1-terra    OpenAI docs developers.openai.com/api/docs/models/gpt-6.1-terra: 1,050,000 / 128,000
#   gpt-6.1-sol      OpenAI docs developers.openai.com/api/docs/models/gpt-6.1-sol: 1,050,000 / 128,000 (922k max input)
#   gpt-6-astra      served by CLIProxyAPI (/v1/models, owned_by=openai; 1-token probe HTTP 200 on 2026-11-14). No window/output doc found,
#                    so the conservative 200,000 / 32,000 default applies until a source is added (12b review model, ts 1794640502.657479)
#   grok-4.7         docs.x.ai/docs/models/grok-4.7: 500,000 ctx; output NOT stated -> conservative 32,000 (aggregators say 128k-500k)
#   gemini-4-argon-high  Google docs (docs.cloud.google.com .../get-started-with-gemini-4): gemini-4-argon has a unified
#                    1,048,576 input+output limit; default output 65,536. "-high" suffix is the variant the proxy exposes.
#   gemini-3.8-flash-high  aggregators only (haimaker/guptadeepak): 1,048,576 ctx / 65,536 out; no Google doc found.
#   ag-opus / ag-sonnet  Antigravity effort variants of Claude Opus 5.5 / Sonnet 5.5
#                    (cliproxy ids claude-opus-5-5-high, claude-sonnet-5-5-high, owned_by=antigravity).
#                    Claude API models table: 1,000,000 context / 128,000 max output. 1-token probe HTTP 200 on 2026-11-08.
# Claude Code limits: CLAUDE_CODE_AUTO_COMPACT_WINDOW accepts 100000..1000000 (clamped below), and
# CLAUDE_CODE_MAX_CONTEXT_TOKENS sets the assumed window for ids that do not start with "claude-".
# Defined before dry-run so a dry launch can print the resolved id without starting a session.
# A case pattern cannot sit inside $(...): its ")" closes the substitution.
proxy_agent_alias_for_id() {
  case "$1" in
    gpt-6-luna) echo luna;;
    gpt-6.1-terra) echo terra;;
    gpt-6.1-sol) echo sol;;
    gpt-6-astra) echo astra;;
    grok-4.7) echo grok;;
    gemini-4-argon-high) echo gemini-pro;;
    gemini-4-hydrogen-xhigh) echo hydrogen;;
    gemini-4-carbon-high) echo carbon;;
    claude-opus-5-5-high) echo ag-opus;;
    claude-sonnet-5-5-high) echo ag-sonnet;;
    gemini-3.8-flash-high) echo gemini-flash;;
    claude-haiku-5-5) echo haiku;;
    claude-sonnet-5-5) echo sonnet;;
    claude-opus-5-5) echo opus;;
    claude-fable-5-2) echo fable;;
    *) echo "";;
  esac
}

resolve_model() {
  # echoes: <model_id> <window> <max_output>
  case "$1" in
    luna)         echo "gpt-6-luna 1050000 128000";;
    terra)        echo "gpt-6.1-terra 1050000 128000";;
    sol)          echo "gpt-6.2-sol 1050000 128000";;
    astra)        echo "gpt-6-astra 200000 32000";;
    grok)         echo "grok-4.7 500000 32000";;
    ernie)        echo "ernie-flash 1024000 921600";;
    hydrogen)     echo "gemini-4-hydrogen-xhigh 1048576 65536";;
    gemini-4-hydrogen-xhigh) echo "gemini-4-hydrogen-xhigh 1048576 65536";;
    gemini-pro)   echo "gemini-4-argon-high 1048576 65536";;
    gemini-flash) echo "gemini-3.8-flash-high 1048576 65536";;
    ag-opus)      echo "claude-opus-5-5-high 1000000 128000";;
    ag-sonnet)    echo "claude-sonnet-5-5-high 1000000 128000";;
    haiku)        echo "claude-haiku-5-5 200000 64000";;
    sonnet)       echo "claude-sonnet-5-5 200000 64000";;
    opus)         echo "claude-opus-5-5 200000 64000";;
    fable)        echo "claude-fable-5-2 200000 64000";;
    # DeepSeek direct (balance-metered, emergencies only; Rajiv D0AMF0XE6TS ts 1794806595.680589). Never in a tier list.
    # (The OpenCode Go entry deepseek-v4.1-flash is a different id and a MID tier entry: see proxy_agent_pick_tier.)
    deepseek|deepseek-flash-4.1) echo "deepseek-flash-4.1 128000 8192";;
    gpt-6.2-sol)  echo "gpt-6.2-sol 1050000 128000";;  # Rajiv D0AMF0XE6TS ts 1794940288.370549; exact id only, not in any tier ladder
    gpt-6-luna|gpt-6.1-terra|gpt-6.1-sol|gpt-6-astra|grok-4.7|gemini-4-argon-high|gemini-3.8-flash-high|gemini-4-hydrogen-xhigh|claude-opus-5-5-high|claude-sonnet-5-5-high|claude-haiku-5-5|claude-sonnet-5-5|claude-opus-5-5|claude-fable-5-2)
                  resolve_model "$(proxy_agent_alias_for_id "$1")";;
    *)            echo "$1 200000 32000";;
  esac
}

proxy_agent_print_selection() {
  local id window max_out
  read -r id window max_out < <(resolve_model "$MODEL")
  printf 'proxy-agent: tier=%s model=%s reason=%s\n' "$SELECT_TIER" "$id" "$SELECT_REASON" >&2
}

proxy_agent_resolve_type
proxy_agent_select || exit 75  # includes the primary burn check (proxy_agent_primary_burn_check), before alias probes
# A UTC-second stamp alone is not unique: several same-type proxies started in
# one second overwrite one markdown file, and delivery/jsonl follow that name.
# PROXY_AGENT_NAME (sanitized) plus this process id keeps concurrent launches apart.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${PROXY_AGENT_OUT_DIR:-${LUNA_OUT_DIR:-/tmp/proxy-agent}}"
mkdir -p "$OUT_DIR"
SAFE_NAME="$(printf '%s' "${PROXY_AGENT_NAME:-$TYPE}" | tr -c 'A-Za-z0-9._-' '-')"
while [[ "$SAFE_NAME" == -* ]]; do SAFE_NAME="${SAFE_NAME#-}"; done
while [[ "$SAFE_NAME" == *- ]]; do SAFE_NAME="${SAFE_NAME%-}"; done
if [[ ${#SAFE_NAME} -gt 80 ]]; then SAFE_NAME="${SAFE_NAME:0:80}"; fi
while [[ "$SAFE_NAME" == *- ]]; do SAFE_NAME="${SAFE_NAME%-}"; done
[[ -n "$SAFE_NAME" ]] || SAFE_NAME="agent"
if [[ -z "$OUT" ]]; then
  OUT="$OUT_DIR/$TYPE-$SAFE_NAME-$$-$STAMP.md"
fi
[[ "$OUT" = /* ]] || OUT="$PWD/$OUT"
LOG="${OUT%.md}.log"
DELIVERY="${OUT}.delivery"
JSONL="${OUT%.md}.jsonl"
# Dry-run stops before checkout, git, credential export, tmux, or launch.
# Stdout is the path line only; model selection stays on stderr.
if [[ "${PROXY_AGENT_DRY_RUN:-}" == "1" ]]; then
  proxy_agent_print_selection
  if [[ "$PM_ROLE" == "1" ]]; then
    printf 'PROXY_AGENT_ROUTING role=pm cwd=/Users/rajiv/Downloads/projects/heydonna-app\n' >&2
  else
    printf 'PROXY_AGENT_ROUTING role=dev cwd=%s\n' "$CWD" >&2
  fi
  if [[ -n "${PROXY_AGENT_EXHAUSTION_LOG:-}" || -n "${PROXY_AGENT_EXHAUSTION_OUT:-}" ]]; then
    proxy_agent_note_log_exhaustion "$MODEL" "${PROXY_AGENT_EXHAUSTION_LOG:-}" "${PROXY_AGENT_EXHAUSTION_OUT:-}" || true
  fi
  printf 'PROXY_AGENT_PATHS out=%s delivery=%s jsonl=%s\n' "$OUT" "$DELIVERY" "$JSONL"
  exit 0
fi

# PM-role proxies (triage, hourly, morning brief, digest, cleanup) need PM-only skills/files:
# run them from the PM checkout with user-level settings only (no PM project hooks). (PM 2026-11-08)
if [[ "$PM_ROLE" == "1" && -z "$RESUME_NAME" ]]; then CWD="/Users/rajiv/Downloads/projects/heydonna-app"; fi
# Resolve symlinks before rejecting live slots; never use the PM checkout (except --pm-role).
[[ -d "$CWD" ]] || { echo "PROXY_AGENT_FAILED reason=checkout_missing" >&2; exit 2; }
CWD="$(cd "$CWD" && pwd -P)"
case "$CWD" in
  */heydonna-app-300[1-4]|*/heydonna-app-300[1-4]/*)
    echo "PROXY_AGENT_FAILED reason=live_or_pm_checkout" >&2; exit 2;;
  /Users/rajiv/Downloads/projects/heydonna-app)
    [[ "$PM_ROLE" == "1" ]] || { echo "PROXY_AGENT_FAILED reason=live_or_pm_checkout" >&2; exit 2; };;
esac
[[ "$(git -C "$CWD" rev-parse --show-toplevel)" == "$CWD" ]] || { echo "PROXY_AGENT_FAILED reason=not_clone_root" >&2; exit 2; }
if [[ "$PM_ROLE" != "1" && -z "$RESUME_NAME" ]]; then
  proxy_agent_restore_setup_dir "$CWD" || { echo "PROXY_AGENT_FAILED reason=setup_restore_failed" >&2; exit 2; }
fi
[[ "$PM_ROLE" == "1" || -n "$RESUME_NAME" || -z "$(git -C "$CWD" status --porcelain --untracked-files=no)" ]] || { echo "PROXY_AGENT_FAILED reason=tracked_changes" >&2; exit 2; }
# Keep the dev launch checkout on fresh main (fast-forward only; never reset). Rajiv 2026-11-08 ts 1794116888.584269.
if [[ "$PM_ROLE" != "1" && -z "$RESUME_NAME" ]]; then
  if [[ "$(git -C "$CWD" branch --show-current)" == "main" ]]; then
    git -C "$CWD" fetch -q origin main && git -C "$CWD" merge -q --ff-only origin/main >/dev/null 2>&1 \
      || echo "PROXY_AGENT_WARN reason=checkout_not_fast_forwarded cwd=$CWD" >&2
  else
    echo "PROXY_AGENT_WARN reason=checkout_not_on_main cwd=$CWD branch=$(git -C "$CWD" branch --show-current)" >&2
  fi
fi
PROMPT_FILE="$(cd "$(dirname "$PROMPT_FILE")" && pwd)/$(basename "$PROMPT_FILE")"
export PROXY_AGENT_RUN=1
if [[ "$PM_ROLE" == "1" ]]; then SETTING_SOURCES="user"; else SETTING_SOURCES="user,project"; fi

# Launch resolution. Same table as the dry-run line; defined above so dry-run can print it.
read -r MODEL_ID WINDOW MAX_OUT < <(resolve_model "$MODEL")
# Emergency Muse Spark launches are logged (primary model and any nested alias that resolved to it). Best effort, never blocks a launch.
proxy_agent_log_emergency_muse() {
  local role="$1" id="$2" reason
  proxy_agent_is_emergency_muse "$id" || return 0
  reason="${SELECT_REASON:-none}"
  { mkdir -p "$HOME/.claude/state" \
      && printf '%s EMERGENCY_MUSE role=%s name=%s type=%s tier=%s model=%s reason=%s\n' \
        "$(date -u +%FT%TZ)" "$role" "${PROXY_AGENT_NAME:-$TYPE}" "$TYPE" "${SELECT_TIER:-}" "$id" "$reason" \
        >> "$HOME/.claude/state/proxy-emergency-muse.log"; } 2>/dev/null || true
}
proxy_agent_log_emergency_muse primary "$MODEL_ID"
proxy_agent_log_emergency_muse haiku "${HAIKU_MODEL:-}"
proxy_agent_log_emergency_muse sonnet "${SONNET_MODEL:-}"
proxy_agent_log_emergency_muse opus "${OPUS_MODEL:-}"
case "$MODEL" in
  ag-opus|ag-sonnet|luna|terra|sol|astra|grok|hydrogen|ernie|gemini-pro|gemini-flash|gpt-6-luna|gpt-6.1-terra|gpt-6.1-sol|gpt-6.2-sol|gpt-6-astra|grok-4.7|gemini-4-argon-high|gemini-3.8-flash-high|gemini-4-hydrogen-xhigh|claude-opus-5-5-high|claude-sonnet-5-5-high|haiku|sonnet|opus|fable|claude-haiku-5-5|claude-sonnet-5-5|claude-opus-5-5|claude-fable-5-2) ;;
  hy4-flash-free|deepseek-v4.1-flash|glm-5.3|muse-spark-1.3-contributor) ;;  # OpenCode Go via CLIProxyAPI; conservative 200000/32000 window until a source is added
  deepseek|deepseek-flash-4.1)
    # Emergencies only: explicit --model plus a P0 cap override (Rajiv D0AMF0XE6TS ts 1794806595.680589).
    case "${PROXY_AGENT_CAP_OVERRIDE:-}" in
      P0:*) echo "proxy-agent: DeepSeek emergency route for ${PROXY_AGENT_CAP_OVERRIDE}" >&2;;
      *) echo "PROXY_AGENT_FAILED reason=deepseek_requires_p0_override (set PROXY_AGENT_CAP_OVERRIDE=P0:<issue>)" >&2; exit 2;;
    esac;;
  *) echo "proxy-agent: model '$MODEL' not in alias table; using 200000-token window (warning)" >&2;;
esac
# Auto-compact window is capped by Claude Code at 1,000,000 tokens.
AUTO_COMPACT=$(( WINDOW > 1000000 ? 1000000 : WINDOW ))

[[ -s "$PROMPT_FILE" ]] || { echo "PROXY_AGENT_FAILED reason=prompt_file_missing_or_empty path=$PROMPT_FILE" >&2; exit 2; }

AGENT_FILE=""
for cand in "$CWD/.claude/agents/$TYPE.md" "$HOME/.claude/agents/$TYPE.md" "/Users/rajiv/Downloads/projects/heydonna-app/.claude/agents/$TYPE.md"; do
  [[ -f "$cand" ]] && { AGENT_FILE="$cand"; break; }
done
[[ -n "$AGENT_FILE" ]] || { echo "PROXY_AGENT_FAILED reason=agent_definition_not_found type=$TYPE" >&2; exit 2; }

# Split frontmatter: body -> system prompt; `tools:` -> --allowedTools (when present and not '*').
BODY="$(awk 'BEGIN{fm=0} NR==1&&/^---$/{fm=1;next} fm==1&&/^---$/{fm=2;next} fm!=1{print}' "$AGENT_FILE")"
TOOLS="$(awk 'NR==1&&/^---$/{fm=1;next} fm==1&&/^---$/{exit} fm==1&&/^tools:/{sub(/^tools:[ ]*/,"");print}' "$AGENT_FILE" | tr -d '[]"' )"

# CLIProxyAPI env, scoped to this child process only. Never echo the key.
# shellcheck disable=SC1091
[[ -r "$HOME/.config/cliproxyapi/client.env" ]] || { printf 'PROXY_AGENT_FAILED reason=client_env_unavailable\n' >&2; exit 2; }
source "$HOME/.config/cliproxyapi/client.env" || { printf 'PROXY_AGENT_FAILED reason=client_env_invalid\n' >&2; exit 2; }
export ANTHROPIC_BASE_URL="${CLIPROXY_BASE_URL:-http://127.0.0.1:8317}"
export ANTHROPIC_AUTH_TOKEN="$CLIPROXY_API_KEY"
unset ANTHROPIC_API_KEY
# S129: nested models come from the pre-launch selector (or the static fallback when --model is explicit).
# An empty alias (burn-refused for a capped role) exports a sentinel that is not a model id, so a nested call fails at the
# proxy instead of unsetting the alias (which would let Claude Code fall back to its built-in id, unchecked).
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${HAIKU_MODEL:-burn-refused-haiku}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${SONNET_MODEL:-burn-refused-sonnet}"
export ANTHROPIC_DEFAULT_OPUS_MODEL="${OPUS_MODEL:-burn-refused-opus}"
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
export PROXY_AGENT=1
# Context sizing for the selected model (Claude Code docs: env-vars, model-config).
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="$WINDOW"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="$AUTO_COMPACT"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="$MAX_OUT"
# -p mode kills background subagents 10m after the last turn; proxy runs own their own bg work (13056 bisect lost 11-08).
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:-0}"

# Load user + clone project settings, excluding local MoP relays.
# PROXY_AGENT_RUN suppresses reporting side effects, never safety guards.
# Retain the PM project's safety subset as extra settings even when the clone's
# settings contain no such guards. The deny rules below remain unchanged.
SAFETY_SETTINGS="$(mktemp "${TMPDIR:-/tmp}/proxy-agent-safety.XXXXXX")"
# Report-only helpers (heartbeat/hourly) may not act on CI/PR lanes. On 11-16 02:00 IST a heartbeat
# helper ran an unauthorized "gh run rerun" for #13844 (Rajiv D0AMF0XE6TS 1794787507.634289; PM trace 12:10 IST).
_HELPER_READONLY=0
if [[ "${PROXY_AGENT_NAME:-}" == *heartbeat* || "${PROXY_AGENT_NAME:-}" == *hourly* ]] || head -1 "$PROMPT_FILE" 2>/dev/null | grep -qiE '^MODE:[[:space:]]*(heartbeat|hourly)'; then _HELPER_READONLY=1; fi
HELPER_READONLY="$_HELPER_READONLY" python3 - "/Users/rajiv/Downloads/projects/heydonna-app/.claude/settings.json" "$SAFETY_SETTINGS" <<'PY'
import json, sys
src, out = sys.argv[1], sys.argv[2]
want = ("block-cto-mention-without-ladder", "block-invalid-backlog-issue-create",
        "block-status-todo-without-ready-pool", "block-missing-directive-contract-issue-create",
        "block-modal-secret-create", "block-raw-tmux-slot")
keep = []
try:
    for g in json.load(open(src)).get("hooks", {}).get("PreToolUse", []):
        hs = [h for h in g.get("hooks", []) if any(w in h.get("command", "") for w in want)]
        if hs:
            keep.append({"matcher": g.get("matcher", ""), "hooks": hs})
except Exception:
    pass
keep.append({"matcher": "Bash", "hooks": [{"type": "command",
             "command": "bash /Users/rajiv/.claude/hooks/block-force-push-rebase.sh"}]})
import os
deny = ["Bash(git reset --hard*)", "Bash(git restore *)",
        "Bash(gh pr merge *)", "Bash(git push --force*)",
        "Bash(git push -f*)"]
if os.environ.get("HELPER_READONLY") == "1":
    deny += ["Bash(gh run rerun*)", "Bash(gh run cancel*)", "Bash(gh workflow run*)",
             "Bash(gh pr ready*)", "Bash(gh pr close*)", "Bash(gh pr edit*)",
             "Bash(*pm-admit-ci.sh*)", "Bash(git push*)"]
# Bash sandbox for proxy agents only (this --settings file). Not the PM pane.
# Rajiv DM D0AMF0XE6TS thread 1795004502.133389, ts 1795006580.326659.
# bypassPermissions (--dangerously-skip-permissions) leaves the sandbox off unless
# allowUnsandboxedCommands is false. Network stays unrestricted (allowedDomains omitted).
# Do not allowWrite $HOME or ~/.heydonna itself: that would permit deleting siblings.
allow_write = [
    "/tmp",
    "/private/tmp",
    "/var/folders",
    "/private/var/folders",
    "/Users/rajiv/Downloads/projects/heydonna-app-proxy",
    "/Users/rajiv/Downloads/projects/heydonna-app",
    "/Users/rajiv/.heydonna/qa",
    "/Users/rajiv/.heydonna/qa-*",
    "/Users/rajiv/.heydonna/qa-*/**",
    "/Users/rajiv/.heydonna/qa-modal-leases",
    "/Users/rajiv/.heydonna/qa-convex-leases",
    "/Users/rajiv/.heydonna/qa-deployment-leases",
    "/Users/rajiv/.heydonna/qa-config-snapshots",
    "/Users/rajiv/.heydonna/qa-evidence",
    "/Users/rajiv/.heydonna/qa-inputs",
    "/Users/rajiv/.heydonna/qa-logs",
    "/Users/rajiv/.heydonna/qa-private",
    "/Users/rajiv/.heydonna/private-qa",
    "/Users/rajiv/.heydonna/evidence",
    "/Users/rajiv/.heydonna/rtb-custody",
    "/Users/rajiv/.heydonna/proxy-setup-dirt",
    "/Users/rajiv/.heydonna/remote-*",
    "/Users/rajiv/.heydonna/remote-*/**",
    "/Users/rajiv/.claude/state",
    "/Users/rajiv/.claude/projects",
    "/Users/rajiv/.claude/logs",
    "/Users/rajiv/.cache",
    "/Users/rajiv/.npm",
    "/Users/rajiv/.bun",
    "/Users/rajiv/Library/Caches",
    "/Users/rajiv/.config/hcloud",
]
json.dump({"hooks": {"PreToolUse": keep},
           "permissions": {"deny": deny},
           "sandbox": {
               "enabled": False,  # PM emergency off 2026-11-18 19:00 IST: live sandbox broke gh TLS (x509 OSStatus -26276) and git worktree in /tmp
               "failIfUnavailable": True,
               "autoAllowBashIfSandboxed": True,
               "allowUnsandboxedCommands": False,
               "filesystem": {
                   "allowWrite": allow_write,
                   "denyWrite": [],
                   "denyRead": [],
               },
           }}, open(out, "w"))
PY
# Hard total cap on live long-running proxies (S143; Rajiv D0AMF0XE6TS thread 1794659684.547559, ts 1794661097.599409: "dial things down and keep all accounts within limits"). Reviewers are exempt so 12b is never blocked; override with PROXY_AGENT_TOTAL_CAP.
PROXY_AGENT_TOTAL_CAP="${PROXY_AGENT_TOTAL_CAP:-8}"
# Override gate (Rajiv D0AMF0XE6TS thread 1794659684.547559; side-agent review 19:14 IST: three overrides in one evening meant the cap didn't hold).
# An env PROXY_AGENT_TOTAL_CAP above the dialed cap is honoured ONLY with PROXY_AGENT_CAP_OVERRIDE="P0:<issue>" or "rajiv:<slack ts>"; every use is logged.
_dialed_total="$(sed -n 's/^PROXY_AGENT_TOTAL_CAP=\([0-9][0-9]*\)$/\1/p' "$HOME/.claude/state/proxy-caps.env" 2>/dev/null | head -1)"; _dialed_total="${_dialed_total:-8}"
if [[ -n "$_PROXY_TOTAL_CAP_ENV" && "$_PROXY_TOTAL_CAP_ENV" -gt "$_dialed_total" ]]; then
  if [[ "${PROXY_AGENT_CAP_OVERRIDE:-}" =~ ^(P0:[0-9]+|rajiv:[0-9]+\.[0-9]+)$ ]]; then
    printf '%s %s name=%s type=%s cap=%s->%s reason=%s\n' "$(date -u +%FT%TZ)" OVERRIDE "${PROXY_AGENT_NAME:-}" "$TYPE" "$_dialed_total" "$_PROXY_TOTAL_CAP_ENV" "$PROXY_AGENT_CAP_OVERRIDE" >> "$HOME/.claude/state/proxy-cap-overrides.log"
  else
    printf 'proxy-agent: cap override ignored (needs PROXY_AGENT_CAP_OVERRIDE=P0:<issue> or rajiv:<ts>); using dialed cap %s\n' "$_dialed_total" >&2
    PROXY_AGENT_TOTAL_CAP="$_dialed_total"
  fi
fi
if proxy_agent_is_capped "$TYPE"; then
  _live_total="$(proxy_agent_live_count total)"
  if [[ "${_live_total:-999}" -ge "$PROXY_AGENT_TOTAL_CAP" ]]; then
    printf 'proxy-agent: telemetry total_cap (%s live >= %s).\n' "$_live_total" "$PROXY_AGENT_TOTAL_CAP" >&2
  fi
  if [[ "$MODEL_ID" == claude-* ]]; then
    _live_claude="$(proxy_agent_live_count claude)"
    if [[ "${_live_claude:-999}" -ge "$PROXY_AGENT_CLAUDE_CAP" ]]; then
      printf 'proxy-agent: telemetry claude_cap (%s live >= %s).\n' "$_live_claude" "$PROXY_AGENT_CLAUDE_CAP" >&2
    fi
  fi
fi
ARGS=(-p --name "${PROXY_AGENT_NAME:-proxy-$TYPE-$STAMP}" --model "$MODEL_ID" --max-turns "$MAX_TURNS" --output-format stream-json --verbose
      --dangerously-skip-permissions --setting-sources "$SETTING_SOURCES" --settings "$SAFETY_SETTINGS"
      --append-system-prompt "$BODY")
if [[ -n "$TOOLS" && "$TOOLS" != "*" && "$TOOLS" != "All tools" ]]; then
  ARGS+=(--allowedTools "$TOOLS")
fi
# PM-only skills: PM-type proxies load them through a plugin (skills dir symlinked to heydonna-app/.claude/skills),
# NOT through the project setting source, which would also load the PM project hooks (rule 50). Dev types get no plugin.
PM_SKILLS_PLUGIN_DIR="/Users/rajiv/.claude/pm-skills-plugin"
if [[ "$PM_ROLE" == "1" ]]; then
  if [[ -d "$PM_SKILLS_PLUGIN_DIR/skills" ]]; then
    ARGS+=(--plugin-dir "$PM_SKILLS_PLUGIN_DIR")
  else
    echo "PROXY_AGENT_WARN reason=pm_skills_plugin_missing dir=$PM_SKILLS_PLUGIN_DIR" >&2
  fi
  SKILL_NOTE_DIRS="/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills (PM-only skills) or ~/.claude/skills (shared skills)"
else
  SKILL_NOTE_DIRS="the main checkout .claude/skills or ~/.claude/skills"
fi

# Persist a private runner, not credentials in tmux's command line/global env.
JOB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/proxy-agent-job.XXXXXX")"
CHANNEL="proxy-agent-$$-$STAMP"
NAME="${PROXY_AGENT_NAME:-proxy-$TYPE-$STAMP}"
CLAUDE_BIN="$(command -v claude)"
[[ "$OUT" = /* ]] || OUT="$PWD/$OUT"
[[ "$LOG" = /* ]] || LOG="$PWD/$LOG"
{
  printf '#!/bin/bash\nset +e\n'
  # tmux server may predate the caller: explicitly transfer child-only env.
  while IFS= read -r key; do
    case "$key" in
      ANTHROPIC_*|CLAUDE_*|PROXY_*|PATH|HOME) printf 'export %s=%q\n' "$key" "${!key}";;
    esac
  done < <(compgen -e)
  printf 'unset ANTHROPIC_API_KEY CLAUDECODE\n'
  # Own window id, captured inside this pane. Never kill by px-/proxy- name:
  # a later launch can reuse the name. remain-on-exit off so a non-zero exit
  # cannot leave a dead pane if kill-window itself fails.
  printf 'OWN_WINDOW_ID="$(tmux display-message -p -t "$TMUX_PANE" "#{window_id}" 2>/dev/null || true)"\n'
  printf 'tmux set-option -w -t "$TMUX_PANE" remain-on-exit off || true\n'
  printf 'complete() { rc=$?; trap - EXIT;\n'
  # PROXY_STOP (stop script) must return before kill-window. Out/log/jsonl are
  # already on disk; this only closes the tmux window.
  printf 'PROXY_AGENT_STOP_MODEL=%q PROXY_AGENT_STOP_LOG=%q PROXY_AGENT_STOP_JSONL=%q bash %q %q "$rc" %q %q\n' "$MODEL_ID" "$LOG" "$JSONL" "$HOME/.claude/scripts/proxy-agent-stop.sh" "$NAME" "$OUT" "$DELIVERY"
  # Issue-thread result line (Rajiv DM D0AMF0XE6TS ts 1794720957.200479). Runs synchronously but is
  # budget-bounded and fail-open (always exits 0); it must finish before kill-window, like the stop relay.
  case "$TYPE" in
    hd-planner|hd-builder|hd-proof-qa|hd-investigator)
      printf 'PROXY_AGENT_NAME=%q python3 -I %q stop --type %q --brief %q --name %q --model %q --out %q --jsonl %q --rc "$rc" </dev/null >/dev/null 2>>%q || true\n' \
        "$NAME" "$HOME/.claude/scripts/proxy-agent-notify.py" "$TYPE" "$PROMPT_FILE" "$NAME" "$MODEL_ID" "$OUT" "$JSONL" "$LOG";;
  esac
  printf 'printf "%%s\\n" "$rc" > %q\n' "$JOB_DIR/rc"
  printf 'printf "PROXY_AGENT_DONE rc=%%s\\n" "$rc"\n'
  printf 'tmux wait-for -S %q\n' "$CHANNEL"
  printf 'if [[ -n "${OWN_WINDOW_ID:-}" ]]; then tmux kill-window -t "$OWN_WINDOW_ID"; fi\n'
  printf 'exit "$rc"; }\n'
  printf "trap complete EXIT\ntrap 'exit 130' INT\ntrap 'exit 143' TERM\ntrap 'exit 129' HUP\n"
  printf 'cd %q || exit 2\n' "$CWD"
  # Bounded auto-continue (PM 2026-11-12 01:25 IST; side-agent flag + PM diagnosis: proxies
  # ended runs with stop_reason=end_turn/subtype=success and an "Incomplete:" report). After a
  # successful run whose final result lacks a DONE:/BLOCKED:/HANDOFF: marker, resume the SAME
  # session with a fixed continue prompt, up to PROXY_AGENT_MAX_CONTINUE (6) times and within
  # PROXY_AGENT_MAX_WALL_S (10800s). Each continuation streams to numbered files that are then
  # appended to the main jsonl and copied over the main out file, so the stream renderer's
  # truncation never loses earlier events. PROXY_AGENT_NO_CONTINUE=1 disables it.
  printf 'PA_START=$(date +%%s); export PA_START PROXY_AGENT_NOTICE_MODEL=%q\n' "$MODEL_ID"
  printf 'python3 -I - %q %q %q %q %q > %q <<'"'"'PY'"'"'\n' "$PROMPT_FILE" "$NAME" "$MODEL" "$MODEL_ID" "$PM_ROLE" "$LOG"
  printf '%s\n' 'import datetime, pathlib, re, sys' 'brief, name, alias, model, pm = sys.argv[1:]' 'text = pathlib.Path(brief).read_text(errors="replace")' 'refs = []' 'for key in ("ISSUE", "PR"):' '    m = re.search(r"^\s*" + key + r"\s*:\s*#?(\d{3,6})\b", text, re.I | re.M)' '    refs.append(key + ":" + (m.group(1) if m else "UNKNOWN"))' 'print("[{}] LAUNCH name={} {} model_alias={} model_id={} MODE:{}".format(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), name, " ".join(refs), alias, model, "pm" if pm == "1" else "dev"))' 'PY'
  printf '{ cat %q; printf "\\n\\n---\\nNOTE: Project skills may not be discoverable through Skill in proxy sessions. If a skill is unavailable, read its SKILL.md directly from %s and follow its instructions; do not repeatedly self-call an unknown skill.\\nPROXY RUN CONTRACT: end your turn ONLY when the task is finished or truly blocked, with a final line starting DONE:, BLOCKED: (exact external blocker) or HANDOFF:. Ending without a marker auto-resumes this session.\\nDo not send interim progress peer messages to PM; send ONE message only for a blocker that needs a PM decision, plus the final Stop.\\n"; } > %q\n' "$PROMPT_FILE" "$SKILL_NOTE_DIRS" "$JOB_DIR/prompt0.md"
  printf '%q ' "$CLAUDE_BIN" "${ARGS[@]}"
  printf '< %q 2>> %q | python3 -I -u %q %q %q\n' "$JOB_DIR/prompt0.md" "$LOG" "$HOME/.claude/scripts/proxy-agent-stream.py" "$OUT" "$JSONL"
  printf 'statuses=("${PIPESTATUS[@]}"); rc=${statuses[0]}; if [[ "$rc" == 0 ]]; then rc=${statuses[1]}; fi\n'
  printf 'pa_n=0; pa_max=${PROXY_AGENT_MAX_CONTINUE:-6}; pa_wall=${PROXY_AGENT_MAX_WALL_S:-10800}\n'
  # Production-touching / safety-stop agent types NEVER auto-continue (side-agent flag
  # 2026-11-12): a deliberate abort must not be retried. Single-shot by type.
  case "$TYPE" in
    pm-lock-release|prod-*|manual-lease-release|customer-artifact-investigator|remote-qa-runner|pm-admission-reviewer|codex-*|main-red-skip)
      printf 'PROXY_AGENT_NO_CONTINUE=1\n';;
  esac
  # Continuations reuse this launch's resolved model and settings (no re-selection). A free/trial-id run NEVER continues: the launch guard
  # already refuses the restricted ids, and this is the same unconditional denial at the continuation boundary (CTO REVISE 1794987952.900739).
  # The case subject is the router-equivalent identity of the launched model (CTO REVISE ts 1794989163.540309), not the literal argument, so a
  # decorated name cannot slip past here either; the original MODEL_ID itself still flows to --model unchanged.
  printf 'case %q in %s) PROXY_AGENT_NO_CONTINUE=1;; esac\n' "$(proxy_agent_router_identity "$MODEL_ID")" "${PROXY_AGENT_FREE_TRIAL_IDS// /|}|auto"
  # Auto-continuation starts a fresh claude process after the previous one exits, so it is a new admission.
  # Re-check OPENCODE_PROXY_CAP at that spawn with the same identity, counter and pool_blocked decision (CTO REVISE ts 1795010788.320629).
  # Role exemption is unchanged: this check runs only for an OpenCode identity, and pool_blocked still skips the exemption for that pool only.
  printf 'MODEL_ID=%q\n' "$MODEL_ID"
  declare -f proxy_agent_router_identity
  declare -f proxy_agent_is_opencode_id
  declare -f proxy_agent_pool_live
  declare -f proxy_agent_pool_blocked
  printf 'printf "Continue the task in your brief from exactly where you stopped. Do not end your turn until it is DONE, or BLOCKED with the exact external blocker (or HANDOFF). Do not send interim progress peer messages to PM; send ONE message only for a blocker that needs a PM decision, plus the final Stop.\\n" > %q\n' "$JOB_DIR/continue.md"
  # Also continue after an error_max_turns stop (rc!=0): 4 sol runs died that way on 2026-11-13 with work mid-flight (PM).
  printf 'pa_maxturns() { tail -c 6000 %q 2>/dev/null | grep -q "\\"subtype\\":\\"error_max_turns\\""; }\n' "$JSONL"
  printf 'while [[ ( "$rc" == 0 || $(pa_maxturns && echo 1 || echo 0) == 1 ) && "${PROXY_AGENT_NO_CONTINUE:-0}" != 1 && "$pa_n" -lt "$pa_max" && $(( $(date +%%s) - PA_START )) -lt "$pa_wall" ]]; do\n'
  printf '  if python3 -I %q quota --out %q --log %q --jsonl %q; then break; fi\n' "$STATE_HELPER" "$OUT" "$LOG" "$JSONL"
  printf '  IFS=$'"'"'\\t'"'"' read -r pa_sid pa_term < <(python3 -I %q %q)\n' "$HOME/.claude/scripts/proxy-agent-result.py" "$JSONL"
  printf '  [[ "$pa_term" == 1 || -z "$pa_sid" ]] && break\n'
  printf '  pa_reason=other; if pa_maxturns; then pa_reason=max_turns; elif [[ "$rc" != 0 ]]; then pa_reason=stream_error; fi\n'
  printf '  pa_n=$((pa_n+1)); printf "[%%s] PROXY_AGENT_CONTINUE n=%%s reason=%%s sid=%%s\\n" "$(date -u +%%FT%%TZ)" "$pa_n" "$pa_reason" "$pa_sid" >> %q\n' "$LOG"
  printf '  if proxy_agent_is_opencode_id "$MODEL_ID"; then\n'
  printf '    if [[ -r "$HOME/.claude/state/proxy-caps.env" ]]; then while IFS== read -r _k _v; do [[ "$_k" == OPENCODE_PROXY_CAP && "$_v" =~ ^[0-9]+$ ]] && OPENCODE_PROXY_CAP="$_v"; done < "$HOME/.claude/state/proxy-caps.env"; fi\n'
  printf '    if proxy_agent_pool_blocked "$MODEL_ID"; then printf "[%%s] PROXY_AGENT_CONTINUE_REFUSED model=%%s reason=opencode_pool_cap\\n" "$(date -u +%%FT%%TZ)" "$MODEL_ID" >> %q; break; fi\n' "$LOG"
  printf '  fi\n'
  printf '  %q ' "$CLAUDE_BIN" "${ARGS[@]}"
  printf ' --resume "$pa_sid" < %q 2>> %q | python3 -I -u %q %q %q\n' "$JOB_DIR/continue.md" "$LOG" "$HOME/.claude/scripts/proxy-agent-stream.py" "$OUT.c" "$JSONL.c"
  printf '  statuses=("${PIPESTATUS[@]}"); rc=${statuses[0]}; if [[ "$rc" == 0 ]]; then rc=${statuses[1]}; fi\n'
  printf '  cat %q >> %q 2>/dev/null; cp %q %q 2>/dev/null; rm -f %q %q\n' "$JSONL.c" "$JSONL" "$OUT.c" "$OUT" "$JSONL.c" "$OUT.c"
  printf 'done\n'
  printf 'printf "PROXY_AGENT_CONTINUATIONS n=%%s\\n" "$pa_n" >> %q\n' "$LOG"
  printf 'exit "$rc"\n'
} > "$JOB_DIR/run.sh"
chmod 700 "$JOB_DIR/run.sh"
VISIBLE_SESSION="${PROXY_AGENT_TMUX_SESSION:-0}"
WINDOW_NAME="px-${NAME#proxy-}"
proxy_agent_print_selection
python3 -I "$STATE_HELPER" start --name "$PROXY_AGENT_RUN_NAME" --token "$PROXY_AGENT_RUN_TOKEN" \
  --type "$TYPE" --brief "$PROMPT_FILE" --cwd "$CWD" --out "$OUT" --model "$MODEL_ID" --pm-role "$PM_ROLE" \
  --role "$TYPE" --rung "${SELECT_RUNG:-}" --lane-started-at "${PROXY_LANE_STARTED_AT:-}" \
  --predecessor "${PROXY_PREDECESSOR:-}" --predecessor-out "${PROXY_PREDECESSOR_OUT:-}" \
  --inherited-head "${PROXY_INHERITED_HEAD:-}" --escalations "${PROXY_ESCALATIONS:-}" || exit 2
# Assignment notices are reserved before their single Slack attempt; any notification
# failure is best-effort and must never prevent the proxy session from starting.
PROXY_AGENT_NAME="$NAME" PROXY_AGENT_NOTICE_MODEL="$MODEL_ID" \
  python3 -I "$HOME/.claude/scripts/proxy-assignment-notice.py" "$TYPE" "$PROMPT_FILE" \
  || echo "PROXY_AGENT_WARN reason=assignment_notice_failed" >&2
# Issue-thread status line (Rajiv DM D0AMF0XE6TS thread 1794720837.453299, ts 1794720957.200479:
# "go. track the issue thread in pm ops db to save lookups."). One line as HeyDonna Alerts into the
# issue's stored assignment thread for hd-* roles. Detached with a bounded budget and fail-open:
# a lookup/post failure only logs and can never delay or block the launch.
case "$TYPE" in
  hd-planner|hd-builder|hd-proof-qa|hd-investigator)
    ( PROXY_AGENT_NAME="$NAME" python3 -I "$HOME/.claude/scripts/proxy-agent-notify.py" launch \
        --type "$TYPE" --brief "$PROMPT_FILE" --name "$NAME" --model "$MODEL_ID" \
        </dev/null >/dev/null 2>>"$LOG" & ) 2>/dev/null || true;;
esac
if tmux has-session -t "=$VISIBLE_SESSION" 2>/dev/null; then
  tmux new-window -d -t "$VISIBLE_SESSION:" -n "$WINDOW_NAME" -c "$CWD" "bash $(printf '%q' "$JOB_DIR/run.sh")"
else
  tmux new-session -d -s "$VISIBLE_SESSION" -n "$WINDOW_NAME" -c "$CWD" "bash $(printf '%q' "$JOB_DIR/run.sh")"
fi
RUN_LOCK_PARENT=0
tmux wait-for "$CHANNEL"
[[ -s "$JOB_DIR/rc" ]] || { echo "PROXY_AGENT_FAILED reason=missing_completion_rc" >&2; exit 125; }
read -r RC < "$JOB_DIR/rc"
[[ "$RC" =~ ^[0-9]+$ ]] || exit 125
# Clear secret-bearing runner after completion; retain result/rc for inspection.
: > "$JOB_DIR/run.sh"
# A 429 quota rejection in the output (.md) or log (.log) sticks in the quota cache for the reset window.
if [[ "$RC" -ne 0 ]]; then
  proxy_agent_note_log_exhaustion "$MODEL_ID" "$LOG" "$OUT" || true
fi
echo "PROXY_AGENT_DONE rc=$RC type=$TYPE model=$MODEL_ID window=$WINDOW out=$OUT log=$LOG"
exit "$RC"
