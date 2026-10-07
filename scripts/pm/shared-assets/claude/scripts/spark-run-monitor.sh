#!/bin/bash
# Spark run monitor (Rajiv 2026-10-04 16:49 IST): posts compact stats to a Slack thread every --interval s.
# Usage: spark-run-monitor.sh --run-name N --log /remote/log [--thread-ts TS] [--interval 900] [--max-hours H]
#        [--proc-pattern PGREP_PATTERN] [--floor-mib 16384] [--channel C0ALZJHGE49] [--host dgx-spark]
#        [--enforce-container EXACT_DOCKER_NAME]
# No --thread-ts: posts a new top-level parent, prints its ts. Never mentions CTO. Never prints secrets.
# Disclosure (CTO review 2026-10-07): posts ONLY allowlisted numeric fields and CLASSIFIED error codes,
# never raw log text. Enforcement is report-only by default; only --enforce-container stops a container,
# and only after revalidating its name + docker id + State.StartedAt (generation) captured at start, at most
# ONCE per monitor (the attempt is consumed before the stop is issued), confirming the stop via inspect.
exec python3 - "$@" <<'PY'
import argparse, re, subprocess, sys, time, os, shlex, math
ap = argparse.ArgumentParser()
ap.add_argument("--run-name", required=True); ap.add_argument("--log", required=True)
ap.add_argument("--thread-ts"); ap.add_argument("--interval", type=float, default=900)
ap.add_argument("--max-hours", type=float, default=48); ap.add_argument("--proc-pattern")
ap.add_argument("--floor-mib", type=float, default=16384); ap.add_argument("--channel", default="C0ALZJHGE49")
ap.add_argument("--host", default="dgx-spark"); ap.add_argument("--heartbeat"); ap.add_argument("--total", type=int)
ap.add_argument("--enforce-container")
a = ap.parse_args()
SEND = os.environ.get("SPARK_MONITOR_SEND") or os.path.expanduser("~/.claude/skills/slack-message/scripts/slack-send.sh")
RUN = re.sub(r"[^\w.:-]", "_", a.run_name)[:80]

def post(text, thread=None):
    cmd = ["bash", SEND, "-c", a.channel] + (["-t", thread] if thread else []) + ["-f"]
    r = subprocess.run(cmd, input=text, capture_output=True, text=True)
    m = re.search(r"ts=(\d+\.\d+)", r.stdout or "")
    if r.returncode != 0 or not m: print("post failed: rc=%d" % r.returncode, file=sys.stderr)
    return m.group(1) if m else None

def ssh(cmd, timeout=60):
    return subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", a.host, cmd],
                          capture_output=True, text=True, timeout=timeout)

REMOTE = ("L=%s; P=%s; H=%s; echo '##TAIL'; tail -c 30000 \"$L\" 2>&1 | tr '\\r' '\\n' | tail -n 400; grep -o \"{.loss.: [^}]*}\" \"$L\" 2>/dev/null | tail -1;"
          " echo '##MTIME'; stat -c %%Y \"$L\" 2>&1; date +%%s;"
          " echo '##SMI'; nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader 2>&1;"
          " echo '##MEM'; grep MemAvailable /proc/meminfo;"
          " echo '##HB'; [ -n \"$H\" ] && cat \"$H\" 2>/dev/null; echo;"
          " echo '##START'; [ -n \"$P\" ] && docker inspect -f '{{.State.StartedAt}}' \"$P\" 2>/dev/null; date -u +%%s;"
          " echo '##PROC'; if [ -n \"$P\" ]; then pgrep -fc -- \"$P\" || true; else echo NA; fi")

def gather():
    cmd = REMOTE % (shlex.quote(a.log), shlex.quote(a.proc_pattern or ""), shlex.quote(a.heartbeat or ""))
    r = ssh(cmd)
    if r.returncode != 0 and "##TAIL" not in r.stdout: raise RuntimeError("ssh rc=%d" % r.returncode)
    sec = {}; cur = None
    for line in r.stdout.splitlines():
        if line.startswith("##"): cur = line[2:]; sec[cur] = []
        elif cur: sec[cur].append(line)
    return sec

# Classified error codes: the ONLY error information ever posted (no raw lines).
ERR_CODES = [("OOM", r"\bOOM\b|out of memory|Killed"), ("CUDA_ERROR", r"CUDA error"), ("TRACEBACK", r"Traceback"),
             ("NONFINITE", r"non-finite|['\"](?:loss|grad_norm)['\"]\s*:\s*['\"]?-?(?:nan|inf)\b"), ("REFUSED", r"\bREFUSED\b"),
             ("OTHER", r"Error")]
END_CODES = [("REFUSED", r"\bREFUSED\b"), ("SELECT_ONLY", r"\bSELECT_ONLY\b"), ("VERDICT", r"\"verdict\"|\bVERDICT\b"),
             ("TRAIN_COMPLETE", r"train_runtime|Training complete"), ("EVAL", r"^EVAL "), ("DONE", r"\bDONE\b")]
def classify(ln, table):
    for code, pat in table:
        if re.search(pat, ln, re.I if code in ("OOM", "NONFINITE") else 0): return code
    return None

num = r"(-?\d+(?:\.\d+)?(?:e-?\d+)?)"
def parse(tail):
    d = {"step": None, "total": None, "loss": None, "gn": None, "lr": None, "spi": None, "errs": {}, "terminal": None, "mins": []}
    for ln in tail:
        m = re.search(r"\b(\d+)/(\d+)\s*\[[^\]]*?(?:([\d.]+)s/it|([\d.]+)it/s)", ln)
        if m:
            d["step"], d["total"] = int(m[1]), int(m[2])
            d["spi"] = float(m[3]) if m[3] else (1 / float(m[4]) if m[4] and float(m[4]) else None)
        else:
            m = re.search(r"(?:\bstep\b[\s=:]*|\bmicro[_-]?step\b[\s=:]*)(\d+)\s*(?:/|of)\s*(\d+)", ln, re.I)
            if m: d["step"], d["total"] = int(m[1]), int(m[2])
        m = re.search(r"['\"]?(?:train_)?loss['\"]?\s*[:=]\s*['\"]?" + num, ln, re.I)
        if m: d["loss"] = float(m[1])
        m = re.search(r"['\"]?grad_norm['\"]?\s*[:=]\s*['\"]?" + num, ln, re.I)
        if m: d["gn"] = float(m[1])
        m = re.search(r"['\"]?(?:learning_rate|\blr)['\"]?\s*[:=]\s*['\"]?" + num, ln, re.I)
        if m: d["lr"] = float(m[1])
        for k in ("cuda_free_mib", "os_memavailable_mib"):
            for mm in re.finditer(k + r"['\"]?\s*[:=]\s*(\d+)", ln): d["mins"].append(float(mm[1]))
        c = classify(ln, ERR_CODES)
        if c: d["errs"][c] = d["errs"].get(c, 0) + 1
        t = classify(ln.strip(), END_CODES)
        if t: d["terminal"] = t
    return d

def errs_line(errs):
    return "errors: " + ", ".join("%s x%d" % (k, errs[k]) for k in sorted(errs)) if errs else ""
def first(sec, k, i=0, default=""):
    v = sec.get(k, []); return v[i] if len(v) > i else default
def fmt_eta(s):
    if s is None or s < 0: return "n/a"
    h, m = int(s // 3600), int(s % 3600 // 60); return "%dh%02dm" % (h, m) if h else "%dm" % m
def f(v, spec="%s"):
    return "n/a" if v is None or (isinstance(v, float) and not math.isfinite(v)) else spec % v

# Enforcement target identity, captured once at start (exact name + full docker id + State.StartedAt generation).
# Never rebound: a restarted/new container generation is a different identity and is refused.
def inspect_identity(ref):
    try:
        r = ssh("docker inspect -f '{{.Id}} {{.Name}} {{.State.StartedAt}}' %s" % shlex.quote(ref), timeout=30)
    except Exception:
        return None
    parts = (r.stdout or "").strip().split()
    if r.returncode != 0 or len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]): return None
    return parts[0], parts[1].lstrip("/"), parts[2]
target = None
if a.enforce_container:
    target = inspect_identity(a.enforce_container)
    if not target or target[1] != a.enforce_container:
        print("enforce target not found at start; enforcement disabled (report-only)", file=sys.stderr); target = None

stop_attempt_consumed = False
def enforce():
    """Stop ONLY the captured target, at most once. Returns a status line; 'stopped' only on inspect-confirmed state."""
    global stop_attempt_consumed
    if not target: return "ENFORCE skipped: no validated target (report-only)", False
    if stop_attempt_consumed: return "ENFORCE skipped: single stop attempt already consumed", False
    now = inspect_identity(target[0])
    if now != target: return "ENFORCE skipped: target identity missing or changed (id/name/StartedAt)", False
    stop_attempt_consumed = True  # consumed BEFORE issuing: success, failure or lost ack all count
    try:
        ssh("docker stop %s" % target[0], timeout=120)
        st = ssh("docker inspect -f '{{.State.Running}}' %s" % target[0], timeout=30)
        if st.returncode == 0 and st.stdout.strip() == "false": return "ENFORCE: stopped target container (confirmed)", True
    except Exception:
        pass
    return "ENFORCE: stop NOT confirmed for target container", False

t0 = time.time(); deadline = t0 + a.max_hours * 3600
thread = a.thread_ts
if not thread:
    thread = post("*Spark run started: %s*\nmonitor every %ds (stats replies below)" % (RUN, a.interval))
    if not thread: sys.exit("could not create parent thread")
print("thread_ts=" + thread, flush=True)
min_avail = None; unreachable_posted = False; obs = []; last_mtime = None; stale = 0
while True:
    try:
        sec = gather(); unreachable_posted = False
        d = parse([l for l in sec.get("TAIL", []) if "Loading weights" not in l and "Loading checkpoint" not in l])
        hb = re.search(r'"global_step"\s*:\s*(\d+)', "\n".join(sec.get("HB", [])))
        if hb: d["step"] = int(hb[1])
        if a.total: d["total"] = a.total
        mem = re.search(r"(\d+)", first(sec, "MEM")); avail = int(mem[1]) / 1024 if mem else None
        smi = first(sec, "SMI"); gpu = "n/a (unified memory)"
        sm = re.match(r"\s*(\d+) MiB,\s*(\d+) MiB,\s*(\d+) %", smi)
        if sm: gpu = "%s/%s MiB, util %s%%" % sm.groups()
        else:
            m2 = re.search(r",\s*(\d+)\s*%\s*$", smi)
            if m2: gpu = "mem n/a (unified), util %s%%" % m2[1]
        cands = [x for x in [avail] + d["mins"] if x is not None]
        if cands: min_avail = min(cands + ([min_avail] if min_avail is not None else []))
        mt = first(sec, "MTIME", 0)
        mtime = int(mt) if mt.isdigit() else None
        stale = stale + 1 if (mtime is not None and mtime == last_mtime) else 0; last_mtime = mtime
        eta = None
        if d["step"] is not None and d["total"]:
            rem = d["total"] - d["step"]
            obs.append((time.time(), d["step"])); obs[:] = [o for o in obs if o[0] > time.time() - 3600]
            if obs[0][1] < d["step"]: eta = rem * (time.time() - obs[0][0]) / (d["step"] - obs[0][1])
            elif d["spi"]: eta = rem * d["spi"]
        proc = first(sec, "PROC"); proc_gone = proc.strip() == "0"
        ended = bool(d["terminal"]) or proc_gone
        floor = a.floor_mib
        fl = "n/a" if min_avail is None else "%d MiB (%s floor %d)" % (min_avail, "BELOW" if min_avail < floor else "above", floor)
        end_line = ("ended: %s" % d["terminal"]) if d["terminal"] else ("process no longer running" if proc_gone else "")
        if a.heartbeat:
            import datetime as _dt
            st = first(sec, "START", 0); nowr = first(sec, "START", 1)
            elapsed = None
            try:
                ts0 = _dt.datetime.fromisoformat(st.strip()[:26].rstrip("Z")).replace(tzinfo=_dt.timezone.utc).timestamp()
                elapsed = int(nowr) - ts0
            except Exception:
                pass
            stp = d["step"]; tot = d["total"]
            rate = (stp / elapsed * 60) if (stp and elapsed and elapsed > 60) else None
            eta_s = ((tot - stp) / rate * 60) if (rate and tot and stp is not None) else None
            def hm(x): return "%dh%02dm" % (x // 3600, (x % 3600) // 60) if x is not None else "?"
            prog = "%s/%s windows (%d%%)" % (stp, tot, 100 * stp // tot) if (stp is not None and tot) else "progress: %s windows" % stp
            lines = [("*[%s] decode ended*" if ended else "*[%s] decode*") % RUN,
                     "%s | elapsed %s | %.2f windows/min (avg since start) | ETA %s" % (prog, hm(elapsed), rate or 0.0, hm(eta_s)),
                     "GPU util %s | free RAM (unified) %s GiB, min %s GiB, floor %d GiB" % ((gpu.split("util ")[-1] if "util" in gpu else "?"), "?" if avail is None else "%.0f" % (avail/1024), "?" if min_avail is None else "%.0f" % (min_avail/1024), floor/1024)]
        else:
            lines = [("*[%s] run ended*" if ended else "*[%s]*") % RUN if not os.environ.get("MONITOR_TEST") else "*[TEST - monitor self-test, ignore] %s*" % RUN,
                     "step %s/%s | loss %s | grad_norm %s | lr %s | ETA %s" % (f(d["step"]), d["total"] or "n/a",
                        f(d["loss"]), f(d["gn"]), f(d["lr"], "%.2e"), fmt_eta(eta)),
                     "GPU %s | MemAvailable %s MiB | min-so-far %s" % (gpu, "n/a" if avail is None else "%d" % avail, fl)]
            if stale >= 3 and not ended: lines.append("warn: log not updated for %d checks" % stale)
        lines.append(errs_line(d["errs"])); lines.append(end_line)
        # Resource/health warning. Report-only unless --enforce-container names a validated target.
        harm = None
        if avail is not None and avail < floor: harm = "LOW_MEM"
        elif d["errs"].get("NONFINITE"): harm = "NONFINITE"
        else:
            try:
                dfo = ssh("df -Pk /home/user | tail -1", timeout=30).stdout.split()
                if len(dfo) >= 4 and int(dfo[3]) < 50 * 1024 * 1024: harm = "LOW_DISK"
            except Exception: pass
        stopped = False
        if harm and not ended:
            lines.append("WARN: %s (report-only)" % harm if not a.enforce_container else "WARN: %s" % harm)
            if a.enforce_container:
                if os.environ.get("MONITOR_DRY_RUN"): lines.append("ENFORCE (dry-run): would stop target container")
                else:
                    msg, stopped = enforce(); lines.append(msg)
        post("\n".join(l for l in lines if l), thread)
        if ended or stopped: break
    except Exception as e:
        if not unreachable_posted:
            post("monitor: Spark unreachable (%s); retrying every %ds" % (type(e).__name__, a.interval), thread); unreachable_posted = True
    if time.time() + a.interval > deadline:
        post("monitor: max-hours reached, stopping (run %s may still be going)" % RUN, thread); break
    time.sleep(a.interval)
PY
