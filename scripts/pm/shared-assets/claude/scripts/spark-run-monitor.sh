#!/bin/bash
# Spark run monitor (Rajiv 2026-10-04 16:49 IST): posts compact stats to a Slack thread every --interval s.
# Usage: spark-run-monitor.sh --run-name N --log /remote/log --memory-config LAUNCHER_EXPORT.json
#        [--thread-ts TS] [--interval 900] [--max-hours H] [--container OWNED_CONTAINER]
#        [--proc-pattern PGREP_PATTERN] [--floor-mib ASSERT_FLOOR] [--channel C0ALZJHGE49] [--host dgx-spark]
# --validate-config-only binds the installed CPU policy without Slack, memory clearance or enforcement.
# No --thread-ts: posts a new top-level parent, prints its ts. Never mentions CTO. Never prints secrets.
exec python3 - "$@" <<'PY'
import argparse, re, subprocess, sys, time, os, tempfile, shlex, json, math

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

def memory_status(cfg, host, mode="sample"):
    cmd = shlex.join(["python3", "-I", "-c", MEMORY_PROGRAM, json.dumps(cfg), mode])
    result = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, cmd],
                            capture_output=True, text=True, errors="replace", timeout=60)
    if result.returncode != 0:
        return {"verdict": {"state": "UNKNOWN", "reason": "memory telemetry SSH failed"},
                "floor_mib": cfg["floor_mib"], "cuda_free_mib": None, "os_avail_mib": None}
    return json.loads(result.stdout)

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
a = ap.parse_args()
memory_config = load_memory_config(a.memory_config, a.run_name, a.floor_mib)
binding = memory_status(memory_config, a.host, "config")
if a.validate_config_only:
    print(json.dumps(binding, sort_keys=True))
    sys.exit(0 if binding["verdict"]["state"] == "CONFIG_ONLY" else 1)
if binding["verdict"]["state"] != "CONFIG_ONLY":
    sys.exit("REFUSE[monitor]: installed policy binding unavailable or mismatched")
SEND = os.path.expanduser("~/.claude/skills/slack-message/scripts/slack-send.sh")
SECRET = re.compile(r"(xox[a-z]-[\w-]+|sk-[\w-]{8,}|hf_\w{8,}|ghp_\w{8,}|Bearer\s+\S+|(?i:(?:token|secret|password|api[_-]?key)\s*[=:]\s*\S+))")
def redact(s): return SECRET.sub("[redacted]", s)

ALERT_DIR = os.environ.get("SPARK_MONITOR_ALERT_DIR") or os.path.expanduser("~/.claude/state/spark-monitor/alerts")
POST_RETRY_DELAY_S = 10

# Delivered = slack-send.sh rc 0 AND its "OK ts=" line (printed only after Slack returned ok:true with a ts).
# One retry; a second failure appends a local alert row so a terminal/dead alert is never silent (2026-11-07).
def post(text, thread=None):
    text = redact(text)
    cmd = ["bash", SEND, "-c", a.channel] + (["-t", thread] if thread else []) + ["-f"]
    why = ""
    for attempt in (1, 2):
        try:
            r = subprocess.run(cmd, input=text, capture_output=True, text=True, errors="replace", timeout=120)
            m = re.search(r"^OK ts=(\d+\.\d+)\b", r.stdout or "", re.M)
            if r.returncode == 0 and m: return m.group(1)
            why = "rc=%d %s" % (r.returncode, redact(((r.stdout or "") + (r.stderr or "")).strip())[:200])
        except (subprocess.TimeoutExpired, OSError) as e:
            why = type(e).__name__
        print("post attempt %d failed: %s" % (attempt, why), file=sys.stderr, flush=True)
        if attempt == 1: time.sleep(POST_RETRY_DELAY_S)
    try:
        os.makedirs(ALERT_DIR, exist_ok=True)
        path = os.path.join(ALERT_DIR, re.sub(r"[^\w.-]", "_", a.run_name) + ".alerts.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "run": a.run_name,
                                "channel": a.channel, "thread": thread, "reason": why, "text": text}) + "\n")
        print("UNDELIVERED: Slack post failed twice; alert appended to " + path, file=sys.stderr, flush=True)
    except OSError as e:
        print("UNDELIVERED: Slack post failed twice and the alert file failed: " + type(e).__name__, file=sys.stderr, flush=True)
    return None

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
while True:
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
        tail_txt = " ".join(sec.get("TAIL", []))
        if memory["verdict"]["state"] != "OK": harm = "memory guard: " + memory["verdict"]["reason"]
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
                    lines.append(":octagonal_sign: ENFORCE: stopped owned container %s (%s); rc=%d" % (a.container, harm, rr.returncode))
        handed_off = False
        # Copy-timeout only (Rajiv ts 1794287346.192189). Memory and OOM still fall through to the ended post.
        if ended and not d["terminal"] and a.container and not os.environ.get("MONITOR_TEST"):
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
        post("\n".join(lines), thread)
        if ended or (harm and not os.environ.get("MONITOR_DRY_RUN")): break
    except Exception as e:
        if not unreachable_posted:
            post(monitor_error_line(e, a.interval), thread)
            unreachable_posted = isinstance(e, (SSHFailure, subprocess.TimeoutExpired))
    if time.time() + a.interval > deadline:
        post("monitor stopping at max-hours; run still active (step %s/%s)" % ((last or {}).get("step", "n/a"), (last or {}).get("total", "n/a")), thread); break
    time.sleep(a.interval)
PY
