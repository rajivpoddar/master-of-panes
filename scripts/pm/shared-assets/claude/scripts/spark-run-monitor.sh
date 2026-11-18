#!/bin/bash
# Spark run monitor (Rajiv 2026-10-04 16:49 IST): posts compact stats to a Slack thread every --interval s.
# Usage: spark-run-monitor.sh --run-name N --log /remote/log --memory-config LAUNCHER_EXPORT.json
#        [--thread-ts TS] [--interval 900] [--max-hours H] [--container OWNED_CONTAINER]
#        [--proc-pattern PGREP_PATTERN] [--floor-mib ASSERT_FLOOR] [--channel C0ALZJHGE49] [--host dgx-spark]
# [--reminder-key KEY] keeps the run's existing bounded Alerts reminder alive by EXTENDING its cutoff only (never replaced).
# [--down-obligation-id N] binds an already-open training_run_down row to this monitor (else exactly one open row for the run is adopted).
# --validate-config-only binds the installed CPU policy without Slack, memory clearance or enforcement.
# No --thread-ts: posts a new top-level parent, prints its ts. Never mentions CTO. Never prints secrets.
# Step stats post as before. A DOWN run (container/process gone, no training-complete marker) or an unreachable Spark
# alerts as the HeyDonna Alerts bot every 30 min via the thread given with --thread-ts. The monitor binds ONE pm-ops
# training_run_down obligation id per outage, never re-opens a closed row, and resolves that exact id only after fresh,
# identity-bound trainer progress (or a verified terminal state) AND announces only after the ledger confirms.
# Posts are DELIVERED, REFUSED (confirmed zero-effect Slack refusal) or UNCERTAIN (never retried, never re-sent as another identity).
exec python3 - "$@" <<'PY'
import argparse, re, subprocess, sys, time, os, tempfile, shlex, json, math, datetime

# This exact CPU-only program is regression-tested. It imports the hash-pinned
# installed policy, not torch; all GPU readings belong to the existing supervisor.
MEMORY_PROGRAM = r'''
import hashlib, json, os, sys, time
cfg = json.loads(sys.argv[1])
out = {"floor_mib": cfg["floor_mib"], "cuda_free_mib": None, "os_avail_mib": None,
       "verdict": {"state": "UNKNOWN", "reason": "guard telemetry unavailable"}}
try:
    lib = cfg["policy_lib_dir"]
    with open(os.path.join(lib, "watchdogs.py"), "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() != cfg["policy_sha256"]:
            raise ValueError("policy hash mismatch")
    sys.path.insert(0, lib)
    import watchdogs as W
    floor = W.resolve_memory_floor(cfg["run_name"], cfg.get("floor_authority"))
    if floor != cfg["floor_mib"]:
        raise ValueError("monitor differs from resolved floor")
    # Split ruling (Rajiv ts 1793212861.439229): ACTIVE CUDA floor resolved by the same hash-pinned policy; a one-floor policy has no resolver.
    cuda_floor = W.resolve_memory_floors(cfg["run_name"], cfg.get("floor_authority"), "ACTIVE")[1] if hasattr(W, "resolve_memory_floors") else floor
    if cfg.get("cuda_floor_mib", floor) != cuda_floor:
        raise ValueError("monitor differs from resolved CUDA floor")
    if sys.argv[2] == "config":
        out["verdict"] = {"state": "CONFIG_ONLY", "reason": "policy bound; not memory clearance"}
    else:
        with open(cfg["guard_telemetry"], "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            row = json.loads(f.read().splitlines()[-1])
        out["verdict"] = W.check_guard_memory(row, floor, cfg["telemetry_max_age_s"], time.time(),
                                              **({"cuda_floor_mib": cuda_floor} if cuda_floor != floor else {}))
        for k in ("cuda_free_mib", "os_avail_mib"):
            out[k] = row.get(k) if W._valid_memory(row.get(k)) else None
except Exception as exc:
    out["verdict"] = {"state": "UNKNOWN", "reason": "policy/telemetry unavailable: " + type(exc).__name__}
print(json.dumps(out, allow_nan=False))
'''

def load_memory_config(path, run_name, floor_mib):
    with open(path) as f:
        cfg = json.load(f)
    floor = cfg.get("floor_mib")
    age = cfg.get("telemetry_max_age_s")
    if (cfg.get("schema") != "spark_memory_monitor_v1" or cfg.get("run_name") != run_name
            or isinstance(floor, bool) or not isinstance(floor, (int, float)) or not math.isfinite(floor)
            or (floor_mib is not None and floor_mib != floor)
            or not isinstance(age, (int, float)) or isinstance(age, bool) or not math.isfinite(age) or age <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", cfg.get("policy_sha256") or "")
            or any(not isinstance(cfg.get(k), str) or not os.path.isabs(cfg[k])
                   for k in ("policy_lib_dir", "guard_telemetry"))):
        raise ValueError("REFUSE[monitor]: configuration or CLI differs from resolved guard policy")
    return cfg

HOST_UNREACHABLE = "memory telemetry SSH failed"

def memory_status(cfg, host, mode="sample"):
    cmd = shlex.join(["python3", "-I", "-c", MEMORY_PROGRAM, json.dumps(cfg), mode])
    result = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, cmd],
                            capture_output=True, text=True, errors="replace", timeout=60)
    if result.returncode != 0:
        return {"verdict": {"state": "UNKNOWN", "reason": HOST_UNREACHABLE},
                "floor_mib": cfg["floor_mib"], "cuda_free_mib": None, "os_avail_mib": None}
    return json.loads(result.stdout)

def bind_status(cfg, host):
    """Policy binding check. An unreachable host (ssh failure or timeout) is reported as HOST_UNREACHABLE so the caller can
    keep the coverage gap visible and retry; a mismatch or malformed answer still raises/refuses and is never bypassed."""
    try:
        return memory_status(cfg, host, "config")
    except (subprocess.TimeoutExpired, OSError):
        return {"verdict": {"state": "UNKNOWN", "reason": HOST_UNREACHABLE},
                "floor_mib": cfg["floor_mib"], "cuda_free_mib": None, "os_avail_mib": None}

class SSHFailure(Exception):
    def __init__(self, rc):
        self.rc = rc
        super().__init__("ssh rc=%d" % rc)

def monitor_error_line(exc, interval):
    if isinstance(exc, SSHFailure):
        return "monitor: Spark unreachable (ssh rc=%d); retrying every %ds" % (exc.rc, interval)
    if isinstance(exc, subprocess.TimeoutExpired):
        return "monitor: Spark unreachable (ssh rc=124); retrying every %ds" % interval
    msg = str(exc).replace("\n", " ").replace("\r", " ").strip()[:120] or "no details"
    return "monitor error (%s: %s); Spark reachability unaffected" % (type(exc).__name__, msg)

def progress_from_history(history, now, total_steps):
    # history contains (gather_epoch_seconds, optimizer_step); no rate is shown until two observations exist.
    recent = [(ts, step) for ts, step in history if now - ts <= 3600 and now >= ts]
    steps_hour = None
    if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
        elapsed = recent[-1][0] - recent[0][0]
        steps_hour = max(0, round((recent[-1][1] - recent[0][1]) * 3600 / elapsed))
    rate = None
    for (t0, s0), (t1, s1) in zip(reversed(history[:-1]), reversed(history[1:])):
        if t1 > t0 and s1 > s0:
            rate = (s1 - s0) / (t1 - t0)
            break
    sec_step = 1 / rate if rate else None
    eta_seconds = (max(0, total_steps - history[-1][1]) / rate
                   if rate and total_steps is not None else None)
    eta = "n/a"
    if eta_seconds is not None:
        eta_epoch = now + eta_seconds
        eta = "%s UTC / %s IST" % (
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(eta_epoch)),
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(eta_epoch + 19800)))
    return "ETA %s | sec/step %s | steps last hour %s" % (
        eta, ("%.1f" % sec_step) if sec_step is not None else "n/a",
        str(steps_hour) if steps_hour is not None else "n/a")
ap = argparse.ArgumentParser()
ap.add_argument("--run-name", required=True); ap.add_argument("--log", required=True)
ap.add_argument("--thread-ts"); ap.add_argument("--interval", type=float, default=900)
ap.add_argument("--max-hours", type=float, default=48); ap.add_argument("--proc-pattern")
ap.add_argument("--floor-mib", type=float, help="optional assertion, never an independent override")
ap.add_argument("--memory-config", required=True, help="launcher-generated, hash-bound floor and telemetry policy")
ap.add_argument("--validate-config-only", action="store_true", help="CPU-only binding check; no Slack or enforcement")
ap.add_argument("--channel", default="C0ALZJHGE49")
ap.add_argument("--host", default="dgx-spark")
ap.add_argument("--container", help="exact owned training container name/id; enforcement stops ONLY this one (never by pattern)")
ap.add_argument("--total-steps", type=int, help="authoritative optimizer-step total for this run; required for persisted progress")
ap.add_argument("--reminder-key", help="MoP alert-reminder key whose cutoff this monitor EXTENDS (7-day bound, schedule/counters/stop policy untouched; a stopped, expired or cancelled reminder is never revived)")
ap.add_argument("--down-obligation-id", type=int, help="open pm-ops training_run_down obligation id to bind (else exactly one open row for this run is adopted at start)")
a = ap.parse_args()
memory_config = load_memory_config(a.memory_config, a.run_name, a.floor_mib)
binding = bind_status(memory_config, a.host)
if a.validate_config_only:
    print(json.dumps(binding, sort_keys=True))
    sys.exit(0 if binding["verdict"]["state"] == "CONFIG_ONLY" else 1)
if binding["verdict"]["state"] != "CONFIG_ONLY" and binding["verdict"].get("reason") != HOST_UNREACHABLE:
    sys.exit("REFUSE[monitor]: installed policy binding unavailable or mismatched")
# An unreachable host at startup is handled below, once post() exists: it stays a visible coverage gap and is retried;
# nothing is sampled, enforced or reported as covered until the hash-pinned policy binding is CONFIG_ONLY.
SEND = os.path.expanduser("~/.claude/skills/slack-message/scripts/slack-send.sh")
SECRET = re.compile(r"(xox[a-z]-[\w-]+|sk-[\w-]{8,}|hf_\w{8,}|ghp_\w{8,}|Bearer\s+\S+|(?i:(?:token|secret|password|api[_-]?key)\s*[=:]\s*\S+))")
def redact(s): return SECRET.sub("[redacted]", s)

ALERT_DIR = os.environ.get("SPARK_MONITOR_ALERT_DIR") or os.path.expanduser("~/.claude/state/spark-monitor/alerts")
POST_RETRY_DELAY_S = 10

# One attempt. DELIVERED = slack-send.sh rc 0 AND its "OK ts=" line. A confirmed auth or membership
# refusal (zero-effect) is REFUSED. A timeout, lost response or anything else is UNCERTAIN and is consumed:
# never retried, never re-sent as the default identity (CTO REVISE 2026-11-18). Alerts posts use only the
# Alerts bot in the channel the caller named (C0ALZJHGE49 / the run thread). C09TYQC1DEF is not a route.
AUTH_REFUSAL_ERRORS = {
    "not_in_channel", "channel_not_found", "invalid_auth", "account_inactive", "token_revoked",
    "not_authed", "missing_scope", "is_archived", "thread_parent_not_in_channel", "ekm_access_denied",
    "org_login_required", "no_permission", "restricted_action",
}

def classify_post(rc, stdout, stderr, timed_out):
    if timed_out or rc is None:
        return "UNCERTAIN", None
    blob = (stdout or "") + "\n" + (stderr or "")
    match = re.search(r"^OK ts=(\d+\.\d+)\b", stdout or "", re.M)
    if rc == 0 and match:
        return "DELIVERED", match.group(1)
    if "SLACK_SEND_REFUSED" in blob:
        return "REFUSED", None
    err = re.search(r"\berror=([A-Za-z0-9_]+)", blob)
    if err and err.group(1) in AUTH_REFUSAL_ERRORS:
        return "REFUSED", None
    return "UNCERTAIN", None

def post(text, thread=None, alert=False):
    text = redact(text)
    cmd = ["bash", SEND, "-c", a.channel] + (["-t", thread] if thread else []) + ["-f"]
    env = dict(os.environ, SLACK_SEND_AS="alerts") if alert else None
    why, outcome, ts = "", "UNCERTAIN", None
    try:
        r = subprocess.run(cmd, input=text, capture_output=True, text=True, errors="replace", timeout=120, env=env)
        outcome, ts = classify_post(r.returncode, r.stdout, r.stderr, False)
        why = "rc=%d %s" % (r.returncode, redact(((r.stdout or "") + (r.stderr or "")).strip())[:200])
    except subprocess.TimeoutExpired:
        outcome, why = "UNCERTAIN", "TimeoutExpired"
    except OSError as e:
        outcome, why = "UNCERTAIN", type(e).__name__
    if outcome != "DELIVERED":
        print("post %s: %s" % (outcome, why), file=sys.stderr, flush=True)
        try:
            os.makedirs(ALERT_DIR, exist_ok=True)
            path = os.path.join(ALERT_DIR, re.sub(r"[^\w.-]", "_", a.run_name) + ".alerts.jsonl")
            with open(path, "a") as f:
                f.write(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "run": a.run_name,
                                    "channel": a.channel, "thread": thread, "outcome": outcome, "reason": why, "text": text}) + "\n")
            print("%s: Slack post consumed; alert appended to %s" % (outcome, path), file=sys.stderr, flush=True)
        except OSError as e:
            print("%s: Slack post consumed and the alert file failed: %s" % (outcome, type(e).__name__), file=sys.stderr, flush=True)
    return ts if outcome == "DELIVERED" else None

# Down handling and reminder renewal (Rajiv 2026-11-18, D0AMF0XE6TS thread 1794988313.192109). Incident: the sindhu-r7
# hourly reminder reached its mandatory 7-day until_at at 2026-11-17 16:05Z and completed (expired/schedule_complete);
# the run then stopped 17:05Z in a save_protection HOLD and nothing alerted: the monitor posted once as the PM identity
# (which PM does not process) and exited. The reminder stays bounded; this watcher renews it while the run is unfinished.
DOWN_REALERT_S = 1800
DOWN_KIND = "training_run_down"
REMINDER_RENEW_BELOW_S = 3 * 86400
REMINDER_HORIZON_S = 7 * 86400 - 3600  # inside MoP's 7-day cap
PM_OPS = os.path.expanduser("~/.claude/scripts/pm-ops.py")
MOP_CLI = os.path.expanduser("~/.claude/scripts/mop.py")
LIVE = not os.environ.get("MONITOR_TEST") and not os.environ.get("MONITOR_DRY_RUN")

def iso_epoch(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def reminder_extend_argv(row, now):
    """argv tail for `mop.py reminder extend`, or None.
    Extends ONLY an active row whose cutoff is still in the future and inside 3 days. A stopped, expired,
    cancelled or lapsed row is never revived, and the schedule (interval, next_at, text, stop policy) is not rewritten."""
    if not row or row.get("status") != "active": return None
    remaining = iso_epoch(row["until_at"]) - now
    if remaining <= 0 or remaining >= REMINDER_RENEW_BELOW_S: return None
    until = datetime.datetime.fromtimestamp(now + REMINDER_HORIZON_S, datetime.timezone.utc).isoformat()
    return ["--key", row["key"], "--until", until]

def renew_reminder(key):
    if not key: return
    try:
        r = subprocess.run(["python3", MOP_CLI, "reminder", "list", "--status", "all"], capture_output=True, text=True, errors="replace", timeout=30)
        row = next((x for x in json.loads(r.stdout).get("reminders", []) if x.get("key") == key), None)
        argv = reminder_extend_argv(row, time.time())
        if not argv: return
        # extend is a conditional UPDATE of until_at. A cancel that lands first makes it refuse; it cannot undo one.
        r = subprocess.run(["python3", MOP_CLI, "reminder", "extend", *argv], capture_output=True, text=True, errors="replace", timeout=30)
        try: outcome = json.loads(r.stdout).get("outcome")
        except ValueError: outcome = None
        print("reminder %s extend rc=%d outcome=%s" % (key, r.returncode, outcome), file=sys.stderr, flush=True)
    except Exception as e:
        print("reminder %s extend failed: %s" % (key, type(e).__name__), file=sys.stderr, flush=True)

def pm_ops(*args):
    return subprocess.run(["python3", PM_OPS, *args], capture_output=True, text=True, errors="replace", timeout=60)

def open_down_rows(run):
    """Open training_run_down rows for this run, or None when the ledger cannot be read (never 'closed')."""
    try:
        r = pm_ops("obligation-list", "--kind", DOWN_KIND, "--format", "json")
        if r.returncode != 0: return None
        return [x for x in json.loads(r.stdout).get("rows", []) if x.get("target_id") == run]
    except Exception:
        return None

def fresh_trainer_progress(previous, step, identity):
    """True only when the optimizer step increased on the SAME log inode and container identity.
    Docker 'running', an unbound container ('NA') and a stale step are not progress."""
    if previous is None or step is None or not identity: return False
    pstep, plog, pcont = previous
    if identity.get("log_id") != plog or identity.get("container") != pcont: return False
    return step > pstep

def may_upsert_down(state, is_open):
    """Do not open a new row over a closed one, and do not guess among several. An unreadable ledger blocks writes."""
    if state.get("named_closed") or state.get("ambiguous") or state.get("hold"): return False
    if state.get("id") is None: return True
    return is_open is True

def may_resolve_down(state, is_open, terminal, fresh, stopped_this_iteration):
    if stopped_this_iteration or state.get("named_closed") or state.get("ambiguous") or state.get("hold"): return False
    if not state.get("id") or is_open is not True: return False
    return bool(terminal or fresh)

def may_announce_resolution(ledger_rc, still_open):
    """Announce only after the exact-id resolve succeeded AND the ledger no longer lists that id."""
    return ledger_rc == 0 and still_open is False

def down_obligation_upsert(run, reason, since):
    try:
        r = pm_ops("obligation-upsert", "--kind", DOWN_KIND, "--target-type", "run", "--target-id", run,
                   "--severity", "high", "--owner", "pm", "--horizon", "hourly", "--title", "Training run down: " + run,
                   "--action", "Inspect the run's HOLD/FAILED state and resume or release it per training-run-processing; resolves when the monitor sees fresh identity-bound progress or a training-complete marker",
                   "--evidence-json", json.dumps({"run": run, "reason": reason[:200], "obligation_id": bound.get("id"),
                                                  "down_since_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))}),
                   "--print-id")
        if r.returncode != 0: return None
        tail = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        return int(tail[-1]) if tail and tail[-1].isdigit() else None
    except Exception:
        return None

def obligation_is_open(oid, run):
    rows = open_down_rows(run)
    if rows is None: return None
    return any(int(x.get("id")) == int(oid) for x in rows)

def adopt_down_obligation():
    rows = open_down_rows(a.run_name)
    if rows is None:
        bound["hold"] = True
        if a.down_obligation_id: bound["id"] = a.down_obligation_id
        print("obligation list unreadable; no training_run_down write until it can be read", file=sys.stderr, flush=True)
        return
    bound["hold"] = False
    if a.down_obligation_id:
        bound["id"] = a.down_obligation_id
        bound["named_closed"] = not any(int(x.get("id")) == a.down_obligation_id for x in rows)
        if bound["named_closed"]:
            print("bound obligation %s is not open; will not open a replacement" % a.down_obligation_id, file=sys.stderr, flush=True)
        return
    if len(rows) == 1:
        bound["id"] = int(rows[0]["id"])
    elif len(rows) > 1:
        bound["ambiguous"] = True
        print("REFUSE[monitor]: %d open %s rows for %s; not guessing an id" % (len(rows), DOWN_KIND, a.run_name), file=sys.stderr, flush=True)

def resolve_bound(reason):
    """Resolve the bound id only. Returns (rc, still_open). Never key-resolves, so a sibling row cannot be swept."""
    oid = bound.get("id")
    if not oid: return 1, None
    try:
        r = pm_ops("obligation-resolve", "--kind", DOWN_KIND, "--id", str(int(oid)), "--reason", reason)
    except Exception as e:
        print("obligation resolve failed: %s" % type(e).__name__, file=sys.stderr, flush=True)
        return 1, None
    still = obligation_is_open(oid, a.run_name)
    if r.returncode != 0:
        print("obligation resolve rc=%d id=%s" % (r.returncode, oid), file=sys.stderr, flush=True)
    return r.returncode, still

def successor_argv():
    flags, i, incoming = [], 0, [x for x in sys.argv[1:] if x != "-"]
    while i < len(incoming):
        tok = incoming[i]
        if tok == "--down-obligation-id":
            i += 2
            continue
        if tok.startswith("--down-obligation-id="):
            i += 1
            continue
        flags.append(tok)
        i += 1
    if bound.get("id"): flags += ["--down-obligation-id", str(int(bound["id"]))]
    return ["bash", os.path.expanduser("~/.claude/scripts/spark-run-monitor.sh")] + flags

def handoff_to_owner():
    """Before the default 48h exit, hand the same watch to its existing owner, including while the run is down."""
    if os.environ.get("MONITOR_TEST") or os.environ.get("MONITOR_DRY_RUN"):
        print("HANDOFF skipped", file=sys.stderr, flush=True)
        return False
    cmd = successor_argv()
    owner = None
    try:
        found = subprocess.run(["tmux", "display-message", "-p", "#{session_name}"], capture_output=True, text=True, timeout=5)
        if found.returncode == 0 and found.stdout.strip(): owner = found.stdout.strip()
    except Exception:
        owner = None
    try:
        if owner:
            subprocess.Popen(["tmux", "new-window", "-d", "-t", owner, "-n", ("mon-" + a.run_name)[:32], "--", *cmd], start_new_session=True)
        else:
            subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("handoff failed: %s" % type(e).__name__, file=sys.stderr, flush=True)
        post("monitor could not hand off before the %gh exit; %s obligation stays open" % (a.max_hours, DOWN_KIND), thread, alert=True)
        return False
    state = "DOWN" if down_since is not None else "active"
    post("monitor handed off to its existing owner before the %gh exit; successor keeps the watch (run %s)" % (a.max_hours, state), thread, alert=down_since is not None)
    return True

def down_banner(run, since, now, recorded):
    mins = int(max(0, now - since) // 60)
    return (":rotating_light: *TRAINING RUN DOWN: %s* since %s UTC (%dh%02dm). Re-alerting every %d min until recovery. pm-ops %s: %s."
            % (run, time.strftime("%Y-%m-%d %H:%M", time.gmtime(since)), mins // 60, mins % 60, DOWN_REALERT_S // 60, DOWN_KIND,
               "open" if recorded else "NOT recorded (pm-ops write failed)"))

REMOTE = ("L=%s; P=%s; C=%s; echo '##IDENTITY_BEFORE'; stat -c '%%d:%%i:%%s' \"$L\" 2>/dev/null; if [ -n \"$C\" ]; then docker inspect -f '{{.Id}}:{{.State.StartedAt}}' \"$C\" 2>/dev/null; fi;"
          " echo '##TAIL'; tail -c 30000 \"$L\" 2>&1 | tr '\\r' '\\n' | tail -n 400; grep -o \"{.loss.: [^}]*}\" \"$L\" 2>/dev/null | tail -1;"
          " echo '##IDENTITY_AFTER'; stat -c '%%d:%%i:%%s' \"$L\" 2>/dev/null; if [ -n \"$C\" ]; then docker inspect -f '{{.Id}}:{{.State.StartedAt}}' \"$C\" 2>/dev/null; fi;"
          " echo '##MTIME'; stat -c %%Y \"$L\" 2>&1; date +%%s;"
          " echo '##SMI'; nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader 2>&1;"
          " echo '##MEM'; grep MemAvailable /proc/meminfo;"
          " echo '##PROC'; if [ -n \"$P\" ]; then pgrep -fc -- \"$P\" || true; else echo NA; fi;"
          " echo '##CONT'; if [ -n \"$C\" ]; then docker inspect -f '{{.State.Status}}' \"$C\" 2>&1 | tr -s '\\n' ' ' | head -c 300; echo; else echo NA; fi;"
          " echo '##THERM'; tail -n 400 /var/log/spark-thermal.log 2>&1; echo '##TALERT'; tail -n 3 /var/log/spark-thermal-alert 2>/dev/null")

# Read-only thermal summary (2026-11-02, three hard resets at 97-98 C): last logger sample's hottest zone + GPU temp,
# window max, and watchdog TRIP/WARN counts in the tail. Never acts on it; the host watchdog owns SIGSTOP.
def thermal(lines):
    d = {"zone_max": None, "gpu_c": None, "win_zone_max": None, "trips": 0, "warns": 0, "ts": None}
    for ln in lines:
        if " WATCHDOG " in ln:
            d["trips"] += " TRIP " in ln; d["warns"] += " WARN " in ln; continue
        z = re.search(r"zones=(.*)$", ln)
        if not z: continue
        vals = [int(v) for v in re.findall(r"\d+=(\d+)", z[1])]
        if not vals: continue
        g = re.search(r"gpu=(\d+),", ln)
        d["zone_max"], d["gpu_c"], d["ts"] = max(vals), (int(g[1]) if g else None), ln.split(" ", 1)[0]
        d["win_zone_max"] = max(vals) if d["win_zone_max"] is None else max(d["win_zone_max"], max(vals))
    return d

def thermal_line(d, alerts):
    if d["zone_max"] is None: return "thermal: n/a (no /var/log/spark-thermal.log sample)"
    s = "thermal: hottest zone %d C (GPU %s C) at %s | tail max %d C | watchdog trips %d" % (
        d["zone_max"], d["gpu_c"] if d["gpu_c"] is not None else "n/a", d["ts"], d["win_zone_max"], d["trips"])
    if d["warns"] or [x for x in alerts if x.strip()]:
        s += " | :warning: watchdog WARN %d; last alert `%s`" % (d["warns"], (([x for x in alerts if x.strip()] or [""])[-1])[:120])
    return s

def gather():
    cmd = REMOTE % (shlex.quote(a.log), shlex.quote(a.proc_pattern or ""), shlex.quote(a.container or ""))
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", a.host, cmd],
                           capture_output=True, text=True, errors="replace", timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise SSHFailure(124) from exc
    if r.returncode != 0: raise SSHFailure(r.returncode)
    sec = {}; cur = None
    for line in r.stdout.splitlines():
        if line.startswith("##"): cur = line[2:]; sec[cur] = []
        elif cur: sec[cur].append(line)
    return sec

num = r"(-?\d+(?:\.\d+)?(?:e-?\d+)?)"
def parse(tail, expected_total=None):
    d = {"step": None, "total": None, "loss": None, "gn": None, "lr": None, "tps": None, "spi": None, "errs": [], "terminal": None}
    for ln in tail:
        # A bare tqdm bar can be dataset loading or a probe, even with a matching total.
        # Accept trainer bars only when accompanied by the optimizer's loss and grad norm.
        m = re.search(r"\b(\d+)/(\d+)\s*\[[^\]]*?(?:([\d.]+)s/it|([\d.]+)it/s)", ln)
        optimizer_bar = m and re.search(r"['\"](?:train_)?loss['\"]\s*:", ln) and re.search(r"['\"]grad_norm['\"]\s*:", ln)
        if optimizer_bar and expected_total and int(m[2]) == expected_total and int(m[1]) <= expected_total:
            d["step"], d["total"] = int(m[1]), int(m[2])
            d["spi"] = float(m[3]) if m[3] else (1 / float(m[4]) if m[4] and float(m[4]) else None)
        m = re.search(r"['\"]?(?:train_)?loss['\"]?\s*[:=]\s*['\"]?" + num, ln, re.I)
        if m: d["loss"] = float(m[1])
        m = re.search(r"['\"]?grad_norm['\"]?\s*[:=]\s*['\"]?" + num, ln, re.I)
        if m: d["gn"] = float(m[1])
        m = re.search(r"['\"]?(?:learning_rate|\blr)['\"]?\s*[:=]\s*['\"]?" + num, ln, re.I)
        if m: d["lr"] = float(m[1])
        m = re.search(r"(?:tokens?[_ ]per[_ ]sec(?:ond)?|tok/s|\btps)['\"]?\s*[:=]?\s*" + num, ln, re.I)
        if m: d["tps"] = float(m[1])
        m = re.search(r"tokens['\"]?\s*[:=]\s*(\d+).*?['\"]sec['\"]\s*:\s*" + num, ln)
        if m and float(m[2]) > 0: d["tps"] = int(m[1]) / float(m[2])
        if re.search(r"Traceback|Error|OOM|out of memory|CUDA error|Killed|non-finite|REFUSED", ln): d["errs"].append(ln.strip()[:160])
        # Terminal marker = explicit training-complete evidence ONLY. Never a "verdict"/DONE/REFUSED substring
        # (memory-guard ticks emit "verdict"; false "run ended" 2026-10-23 16:18 IST).
        if re.search(r"['\"]train_runtime['\"]|Training complete", ln):
            d["terminal"] = ln.strip()[:160]
    return d

# Explicit docker evidence only, never ssh errors. The whole answer is read: Docker 29 prints an empty stdout
# line before "error: no such object" on stderr, so a first-line read missed a removed --rm container (2026-11-07).
def docker_gone(cstate):
    s = cstate.strip().lower()
    return s in ("exited", "dead") or "no such" in s

def first(sec, k, i=0, default=""):
    v = sec.get(k, []); return v[i] if len(v) > i else default
def fmt_eta(s):
    if s is None or s < 0: return "n/a"
    h, m = int(s // 3600), int(s % 3600 // 60); return "%dh%02dm" % (h, m) if h else "%dm" % m

t0 = time.time(); deadline = t0 + a.max_hours * 3600
thread = a.thread_ts
if not thread:
    thread = post("*Spark run started: %s*\nlog: `%s`\nmonitor every %ds (stats replies below)" % (a.run_name, a.log, a.interval))
    if not thread: sys.exit("could not create parent thread")
print("thread_ts=" + thread, flush=True)
min_avail = min_cuda = None; unreachable_posted = False; obs = []; step_history = []; last_mtime = None; stale = 0; last = None
down_since = None; down_recorded = False; last_down_alert = 0.0; last_unreach_alert = 0.0
policy_bound = binding["verdict"]["state"] == "CONFIG_ONLY"
bound = {"id": None, "named_closed": False, "ambiguous": False, "hold": False}
progress_baseline = None
if LIVE: adopt_down_obligation()
while True:
    if LIVE: renew_reminder(a.reminder_key)
    if not policy_bound:
        retry = bind_status(memory_config, a.host)
        if retry["verdict"].get("reason") == HOST_UNREACHABLE:
            now_gap = time.time()
            if now_gap - last_unreach_alert >= DOWN_REALERT_S:
                post("monitor: Spark host unreachable; coverage gap remains visible; installed policy binding was not bypassed", thread, alert=True)
                last_unreach_alert = now_gap
            if time.time() + a.interval > deadline:
                handoff_to_owner()
                break
            time.sleep(min(a.interval, DOWN_REALERT_S))
            continue
        if retry["verdict"]["state"] != "CONFIG_ONLY":
            sys.exit("REFUSE[monitor]: installed policy binding unavailable or mismatched")
        policy_bound = True
    try:
        sec = gather(); unreachable_posted = False
        d = parse(sec.get("TAIL", []), a.total_steps)
        mem = re.search(r"(\d+)", first(sec, "MEM")); avail = int(mem[1]) / 1024 if mem else None
        smi = first(sec, "SMI"); gpu = "n/a (unified memory)"
        sm = re.match(r"\s*(\d+) MiB,\s*(\d+) MiB,\s*(\d+) %", smi)
        if sm: gpu = "%s/%s MiB, util %s%%" % sm.groups()
        elif "utilization" not in smi and smi.strip(): 
            m2 = re.search(r"(\d+)\s*%\s*$", smi); gpu = "mem n/a (unified), util %s%%" % m2[1] if m2 else gpu
        memory = memory_status(memory_config, a.host)
        cuda, guard_os = memory["cuda_free_mib"], memory["os_avail_mib"]
        if cuda is not None: min_cuda = cuda if min_cuda is None else min(min_cuda, cuda)
        if guard_os is not None: min_avail = guard_os if min_avail is None else min(min_avail, guard_os)
        mt = first(sec, "MTIME", 0); now = first(sec, "MTIME", 1)
        mtime = int(mt) if mt.isdigit() else None
        stale = stale + 1 if (mtime is not None and mtime == last_mtime) else 0; last_mtime = mtime
        observed_at = time.time()
        if d["step"] is not None:
            if not step_history or (observed_at > step_history[-1][0] and d["step"] >= step_history[-1][1]):
                step_history.append((observed_at, d["step"]))
            step_history = [(ts, step) for ts, step in step_history if observed_at - ts <= 3600]
        progress = progress_from_history(step_history, observed_at, a.total_steps or d["total"])
        before, after = sec.get("IDENTITY_BEFORE", []), sec.get("IDENTITY_AFTER", [])
        identity = None
        if (first(sec, 'CONT').strip() == 'running' and len(before) == len(after) == 2
                and before[1] == after[1] and re.fullmatch(r'[0-9a-f]{64}:.+', after[1])):
            b = re.fullmatch(r'(\d+):(\d+):(\d+)', before[0])
            e = re.fullmatch(r'(\d+):(\d+):(\d+)', after[0])
            if b and e and b.group(1, 2) == e.group(1, 2) and int(e[3]) >= int(b[3]):
                identity = {'log_id': ':'.join(e.group(1, 2)), 'size': int(e[3]), 'container': after[1]}
        if d["step"] is not None and d["total"] and identity:
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location("spark_heartbeat", os.path.expanduser("~/.claude/scripts/pm/heartbeat/heartbeat-compose.py"))
                composer = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(composer)
                composer.spark_progress_update(a.run_name, a.host, a.log, d["step"], d["total"], identity=identity)
            except Exception:
                progress += " (progress state unavailable)"
        proc = first(sec, "PROC"); proc_gone = proc.strip() == "0"
        cstate = first(sec, "CONT").strip()
        container_gone = docker_gone(cstate)
        ended = bool(d["terminal"]) or proc_gone or container_gone
        last = d
        floor = memory_config["floor_mib"]
        fl = ("%s; floor %d MiB; minima CUDA=%s OS=%s" % (memory["verdict"]["state"], floor, min_cuda, min_avail)
              if memory_config.get("cuda_floor_mib", floor) == floor else
              "%s; floor OS %d / CUDA %d MiB (ACTIVE); minima CUDA=%s OS=%s" % (memory["verdict"]["state"], floor, memory_config["cuda_floor_mib"], min_cuda, min_avail))
        lines = [("*[%s] run ended*" if ended else "*[%s]*") % a.run_name if not os.environ.get("MONITOR_TEST") else "*[TEST - monitor self-test, ignore] %s*" % a.run_name,
                 "step %s/%s (%s) | loss %s | grad_norm %s | lr %s | tok/s %s | %s" % (d["step"] if d["step"] is not None else "n/a", d["total"] or "n/a", ("%.1f%%" % (100.0 * d["step"] / d["total"])) if (d["step"] is not None and d["total"]) else "n/a",
                    d["loss"] if d["loss"] is not None else "n/a", d["gn"] if d["gn"] is not None else "n/a", ("%.2e" % d["lr"]) if d["lr"] is not None else "n/a", "%.1f" % d["tps"] if d["tps"] else "n/a (not logged)", progress),
                 "GPU %s | CUDA free %s MiB | MemAvailable %s MiB | %s" % (gpu, cuda, guard_os, fl),
                 thermal_line(thermal(sec.get("THERM", [])), sec.get("TALERT", []))]
        if not d["step"] and not d["loss"]:
            tl = [l for l in sec.get("TAIL", []) if l.strip()]
            if tl: lines.append("log tail: `%s`" % redact(tl[-1])[:140])
        if stale >= 3 and not ended: lines.append("warn: log not updated for %d checks" % stale)
        if d["errs"]: lines.append("errors: " + " || ".join(sorted(set(d["errs"]))[-3:]))
        if d["terminal"]: lines.append("training-complete marker: `%s`" % d["terminal"])
        elif container_gone: lines.append("container exited (%s)" % cstate[:60])
        elif proc_gone: lines.append("process no longer running")
        # Enforcement (CTO review 2026-10-04): stop ONLY the training container on concrete harm.
        harm = None
        stopped_this_iteration = False
        tail_txt = " ".join(sec.get("TAIL", []))
        if memory["verdict"]["state"] != "OK" and memory["verdict"].get("reason") != HOST_UNREACHABLE: harm = "memory guard: " + memory["verdict"]["reason"]
        elif re.search(r"['\"](?:loss|grad_norm)['\"]\s*:\s*(?:nan|inf|-inf)", tail_txt, re.I): harm = "non-finite loss/grad_norm in log"
        else:
            try:
                dfo = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", a.host, "df -Pk /home/user | tail -1"], capture_output=True, text=True, errors="replace", timeout=30).stdout.split()
                if len(dfo) >= 4 and int(dfo[3]) < 50 * 1024 * 1024: harm = "free disk %d GiB < 50 GiB" % (int(dfo[3]) // (1024 * 1024))
            except Exception: pass
        if harm and not ended:
            if os.environ.get("MONITOR_DRY_RUN"):
                lines.append("ENFORCE (dry-run): would stop training container: " + harm)
            else:
                if not a.container:
                    lines.append(":warning: ENFORCE skipped (no --container given; never stop by pattern): " + harm)
                else:
                    cmd = "docker stop " + shlex.quote(a.container)
                    rr = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", a.host, cmd], capture_output=True, text=True, errors="replace", timeout=120)
                    stopped_this_iteration = True
                    lines.append(":octagonal_sign: ENFORCE: stopped owned container %s (%s); rc=%d" % (a.container, harm, rr.returncode))
        handed_off = False
        # Copy-timeout only (Rajiv ts 1794287346.192189). Memory and OOM still fall through to the ended post.
        if ended and not d["terminal"] and a.container and not os.environ.get("MONITOR_TEST") and down_since is None:
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "copy_timeout_resume",
                    os.environ.get("SPARK_COPY_TIMEOUT_RESUME") or os.path.expanduser("~/.claude/scripts/copy_timeout_resume.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                fetched = mod.fetch_guard(a.host, a.log, subprocess.run)
                ledger_path = os.path.join(os.path.expanduser("~/.claude/state/spark-monitor/copy-timeout-resume"),
                                           re.sub(r"[^\w.-]", "_", a.run_name) + ".json")
                ledger = mod.load_ledger(ledger_path)

                def copy_once(timeout_s, _f=fetched):
                    argv = mod.ssh_copy_argv(a.host, _f.get("leg") or "", _f.get("checkpoint_step"), a.container)
                    r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=2100)
                    if r.returncode != 0 or not (r.stdout or "").strip():
                        return {"ok": False}
                    return json.loads(r.stdout.strip().splitlines()[-1])

                def launch_once(plan, _f=fetched):
                    argv = mod.ssh_launch_argv(a.host, _f.get("leg") or "", _f.get("checkpoint_step"), a.container)
                    r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=180)
                    if r.returncode != 0 or not (r.stdout or "").strip():
                        return {"ok": False}
                    data = json.loads(r.stdout.strip().splitlines()[-1])
                    if not data.get("ok"):
                        return {"ok": False}
                    local_cfg = os.path.join(os.path.dirname(ledger_path), data["container"] + ".monitor_config.json")
                    os.makedirs(os.path.dirname(local_cfg), exist_ok=True)
                    scp = subprocess.run(["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                                          a.host + ":" + data["memory_config_remote"], local_cfg],
                                         capture_output=True, text=True, errors="replace", timeout=60)
                    if scp.returncode != 0:
                        return {"ok": False}
                    flags = [x for x in sys.argv[1:] if x != "-"]
                    rearm = ["bash", os.path.expanduser("~/.claude/scripts/spark-run-monitor.sh")] + mod.rearm_monitor_argv(
                        flags, data["container"], data["log"], local_cfg)
                    return {"ok": True, "rearm": rearm, "container": data["container"], "log": data["log"]}

                result = mod.handle(fetched.get("receipt"), fetched.get("checkpoint_step"), ledger, copy_once, launch_once,
                                    lambda text, channel, th: post(text, th))
                mod.save_ledger(ledger_path, ledger)
                launch = result.get("launch") or {}
                if result.get("launched") and launch.get("rearm"):
                    subprocess.Popen(["tmux", "new-window", "-d", "-t", "main", "-n",
                                      "r7-mon-%s" % fetched.get("checkpoint_step"), "--", *launch["rearm"]])
                    post("auto-resume handed off to container `%s`" % launch["container"], thread)
                    handed_off = True
            except Exception as exc:
                lines.append("auto-resume did not start: %s" % type(exc).__name__)
        if handed_off:
            break
        if d["terminal"]:
            # Verified terminal state only. Resolve the bound id, and announce only after the ledger confirms.
            post("\n".join(lines), thread)
            if LIVE and may_resolve_down(bound, obligation_is_open(bound.get("id"), a.run_name) if bound.get("id") else False, True, False, stopped_this_iteration):
                rc, still = resolve_bound("training complete")
                if may_announce_resolution(rc, still):
                    post(":white_check_mark: *[%s]* training complete; %s %s resolved" % (a.run_name, DOWN_KIND, bound.get("id")), thread)
                    bound["id"] = None
            break
        if not ended:
            post("\n".join(lines), thread)  # step stats: unchanged
            fresh = fresh_trainer_progress(progress_baseline, d.get("step"), identity)
            is_open = obligation_is_open(bound.get("id"), a.run_name) if (LIVE and bound.get("id")) else False
            if LIVE and may_resolve_down(bound, is_open, False, fresh, stopped_this_iteration):
                rc, still = resolve_bound("fresh identity-bound trainer progress")
                if may_announce_resolution(rc, still):
                    post(":white_check_mark: *[%s]* recovered; %s %s resolved" % (a.run_name, DOWN_KIND, bound.get("id")), thread)
                    bound["id"] = None
                    down_since = None; down_recorded = False; last_down_alert = 0.0
            if identity and d.get("step") is not None and not stopped_this_iteration:
                progress_baseline = (d["step"], identity["log_id"], identity["container"])
            # No container to stop: the harm cannot be acted on, so stop monitoring as before. With a container the
            # stop was issued above and the next sample sees it gone, which is the down path below.
            if harm and not os.environ.get("MONITOR_DRY_RUN") and not a.container: break
        else:
            # Container/process gone with no training-complete marker: DOWN. Check the bound obligation's closure
            # BEFORE any alert or upsert. A closed row is not re-opened. Alert as Alerts every 30 min.
            now_t = time.time()
            if down_since is None: down_since = now_t
            due = now_t - last_down_alert >= DOWN_REALERT_S
            reason = "no training-complete marker; " + ("container exited (%s)" % cstate[:60] if container_gone else "process no longer running")
            is_open = obligation_is_open(bound.get("id"), a.run_name) if bound.get("id") else None
            if LIVE and bound.get("id") and is_open is False:
                bound["named_closed"] = True
                post("%s %s for %s was closed; monitor stopping without opening another (run still down)" % (DOWN_KIND, bound["id"], a.run_name), thread)
                break
            if LIVE and (due or not down_recorded) and may_upsert_down(bound, True if bound.get("id") is None else is_open):
                new_id = down_obligation_upsert(a.run_name, reason, down_since)
                if bound.get("id") is None and new_id:
                    bound["id"] = new_id
                    down_recorded = True
                elif new_id and new_id == bound.get("id"):
                    down_recorded = True
                elif new_id and new_id != bound.get("id"):
                    print("REFUSE[monitor]: upsert returned %s, bound id is %s; not adopting a new row" % (new_id, bound.get("id")), file=sys.stderr, flush=True)
            if due:
                recorded = bool(bound.get("id")) and not bound.get("named_closed") and down_recorded
                post("\n".join(lines + [down_banner(a.run_name, down_since, now_t, recorded)]), thread, alert=True)
                last_down_alert = now_t
    except Exception as e:
        reach = isinstance(e, (SSHFailure, subprocess.TimeoutExpired))
        if not unreachable_posted or (reach and time.time() - last_unreach_alert >= DOWN_REALERT_S):
            post(monitor_error_line(e, a.interval), thread, alert=reach)
            unreachable_posted = reach
            if reach: last_unreach_alert = time.time()
    if time.time() + a.interval > deadline:
        handoff_to_owner()
        break
    time.sleep(min(a.interval, max(30, last_down_alert + DOWN_REALERT_S - time.time())) if down_since is not None else a.interval)
PY
