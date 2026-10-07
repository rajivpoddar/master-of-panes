#!/bin/bash
# Spark run monitor (Rajiv 2026-10-04 16:49 IST): posts compact stats to a Slack thread every --interval s.
# Usage: spark-run-monitor.sh --run-name N --log /remote/log [--thread-ts TS] [--interval 900] [--max-hours H]
#        [--proc-pattern PGREP_PATTERN] [--floor-mib 16384] [--channel C0ALZJHGE49] [--host dgx-spark]
# No --thread-ts: posts a new top-level parent, prints its ts. Never mentions CTO. Never prints secrets.
exec python3 - "$@" <<'PY'
import argparse, re, subprocess, sys, time, os, tempfile, shlex
ap = argparse.ArgumentParser()
ap.add_argument("--run-name", required=True); ap.add_argument("--log", required=True)
ap.add_argument("--thread-ts"); ap.add_argument("--interval", type=float, default=900)
ap.add_argument("--max-hours", type=float, default=48); ap.add_argument("--proc-pattern")
ap.add_argument("--floor-mib", type=float, default=16384); ap.add_argument("--channel", default="C0ALZJHGE49")
ap.add_argument("--host", default="dgx-spark"); ap.add_argument("--heartbeat"); ap.add_argument("--total", type=int)
a = ap.parse_args()
SEND = os.path.expanduser("~/.claude/skills/slack-message/scripts/slack-send.sh")
SECRET = re.compile(r"(xox[a-z]-[\w-]+|sk-[\w-]{8,}|hf_\w{8,}|ghp_\w{8,}|Bearer\s+\S+|(?i:(?:token|secret|password|api[_-]?key)\s*[=:]\s*\S+))")
def redact(s): return SECRET.sub("[redacted]", s)

def post(text, thread=None):
    text = redact(text)
    cmd = ["bash", SEND, "-c", a.channel] + (["-t", thread] if thread else []) + ["-f"]
    r = subprocess.run(cmd, input=text, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"ts=(\d+\.\d+)", out)
    if r.returncode != 0 or not m: print("post failed:", redact(out.strip())[:200], file=sys.stderr)
    return m.group(1) if m else None

REMOTE = ("L=%s; P=%s; H=%s; echo '##TAIL'; tail -c 30000 \"$L\" 2>&1 | tr '\\r' '\\n' | tail -n 400; grep -o \"{.loss.: [^}]*}\" \"$L\" 2>/dev/null | tail -1;"
          " echo '##MTIME'; stat -c %%Y \"$L\" 2>&1; date +%%s;"
          " echo '##SMI'; nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader 2>&1;"
          " echo '##MEM'; grep MemAvailable /proc/meminfo;"
          " echo '##HB'; [ -n \"$H\" ] && cat \"$H\" 2>/dev/null; echo;"
          " echo '##START'; [ -n \"$P\" ] && docker inspect -f '{{.State.StartedAt}}' \"$P\" 2>/dev/null; date -u +%%s;"
          " echo '##PROC'; if [ -n \"$P\" ]; then pgrep -fc -- \"$P\" || true; else echo NA; fi")

def gather():
    cmd = REMOTE % (shlex.quote(a.log), shlex.quote(a.proc_pattern or ""), shlex.quote(a.heartbeat or ""))
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", a.host, cmd],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 and "##TAIL" not in r.stdout: raise RuntimeError("ssh rc=%d" % r.returncode)
    sec = {}; cur = None
    for line in r.stdout.splitlines():
        if line.startswith("##"): cur = line[2:]; sec[cur] = []
        elif cur: sec[cur].append(line)
    return sec

num = r"(-?\d+(?:\.\d+)?(?:e-?\d+)?)"
def parse(tail):
    d = {"step": None, "total": None, "loss": None, "gn": None, "lr": None, "tps": None, "spi": None, "errs": [], "terminal": None, "mins": []}
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
        m = re.search(r"(?:tokens?[_ ]per[_ ]sec(?:ond)?|tok/s|\btps)['\"]?\s*[:=]?\s*" + num, ln, re.I)
        if m: d["tps"] = float(m[1])
        m = re.search(r"tokens['\"]?\s*[:=]\s*(\d+).*?['\"]sec['\"]\s*:\s*" + num, ln)
        if m and float(m[2]) > 0: d["tps"] = int(m[1]) / float(m[2])
        for k in ("cuda_free_mib", "os_memavailable_mib"):
            for mm in re.finditer(k + r"['\"]?\s*[:=]\s*(\d+)", ln): d["mins"].append(float(mm[1]))
        if re.search(r"Traceback|Error|OOM|out of memory|CUDA error|Killed|non-finite|REFUSED", ln): d["errs"].append(ln.strip()[:160])
        if re.search(r"train_runtime|^EVAL |\bREFUSED\b|\bSELECT_ONLY\b|\"verdict\"|\bVERDICT\b|\bDONE\b|Training complete", ln):
            d["terminal"] = ln.strip()[:160]
    return d

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
min_avail = min_cuda = None; unreachable_posted = False; obs = []; last_mtime = None; stale = 0; last = None
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
        elif "utilization" not in smi and smi.strip(): 
            m2 = re.search(r"(\d+)\s*%\s*$", smi); gpu = "mem n/a (unified), util %s%%" % m2[1] if m2 else gpu
        for v in d["mins"]:
            min_cuda = v if min_cuda is None else min(min_cuda, v) if "cuda" else v
        cands = [x for x in [avail] + d["mins"] if x is not None]
        if cands: min_avail = min(cands + ([min_avail] if min_avail is not None else []))
        mt = first(sec, "MTIME", 0); now = first(sec, "MTIME", 1)
        mtime = int(mt) if mt.isdigit() else None
        stale = stale + 1 if (mtime is not None and mtime == last_mtime) else 0; last_mtime = mtime
        eta = None
        if d["step"] is not None and d["total"]:
            rem = d["total"] - d["step"]
            obs.append((time.time(), d["step"])); obs[:] = [o for o in obs if o[0] > time.time() - 3600]
            # Recent-rate ETA: oldest observation in the last hour; fall back to tqdm s/it.
            if obs[0][1] < d["step"]: eta = rem * (time.time() - obs[0][0]) / (d["step"] - obs[0][1])
            elif d["spi"]: eta = rem * d["spi"]
        proc = first(sec, "PROC"); proc_gone = proc.strip() == "0"
        ended = bool(d["terminal"]) or proc_gone
        floor = a.floor_mib
        fl = "n/a" if min_avail is None else "%d MiB (%s floor %d)" % (min_avail, "BELOW" if min_avail < floor else "above", floor)
        if a.heartbeat:
            import datetime as _dt
            st = first(sec, "START", 0); nowr = first(sec, "START", 1)
            elapsed = None
            try:
                t0 = _dt.datetime.fromisoformat(st.strip()[:26].rstrip("Z")).replace(tzinfo=_dt.timezone.utc).timestamp()
                elapsed = int(nowr) - t0
            except Exception:
                pass
            stp = d["step"]; tot = d["total"]
            rate = (stp / elapsed * 60) if (stp and elapsed and elapsed > 60) else None
            eta_s = ((tot - stp) / rate * 60) if (rate and tot and stp is not None) else None
            def hm(x): return "%dh%02dm" % (x // 3600, (x % 3600) // 60) if x is not None else "?"
            prog = "%s/%s windows (%d%%)" % (stp, tot, 100 * stp // tot) if (stp is not None and tot) else "progress: %s windows" % stp
            lines = [("*[%s] decode ended*" if ended else "*[%s] decode*") % a.run_name,
                     "%s | elapsed %s | %.2f windows/min (avg since start) | ETA %s" % (prog, hm(elapsed), rate or 0.0, hm(eta_s)),
                     "GPU util %s | free RAM (unified) %s GiB, min %s GiB, floor %d GiB" % ((gpu.split("util ")[-1] if "util" in gpu else "?"), "?" if avail is None else "%.0f" % (avail/1024), "?" if min_avail is None else "%.0f" % (min_avail/1024), floor/1024)]
            if ended: lines.append("process no longer running" if proc_gone else "")
            if d["errs"]: lines.append("errors: " + " || ".join(sorted(set(d["errs"]))[-3:]))
            text = "\n".join(l for l in lines if l)
            thread = thread  # keep
            _decode_text = text
        else:
            _decode_text = None
        lines = [("*[%s] run ended*" if ended else "*[%s]*") % a.run_name if not os.environ.get("MONITOR_TEST") else "*[TEST - monitor self-test, ignore] %s*" % a.run_name,
                 "step %s/%s | loss %s | grad_norm %s | lr %s | ETA %s" % (d["step"] if d["step"] is not None else "n/a", d["total"] or "n/a",
                    d["loss"] if d["loss"] is not None else "n/a", d["gn"] if d["gn"] is not None else "n/a", ("%.2e" % d["lr"]) if d["lr"] is not None else "n/a", fmt_eta(eta)),
                 "GPU %s | MemAvailable %s MiB | min-so-far %s" % (gpu, "n/a" if avail is None else "%d" % avail, fl)]
        if not d["step"] and not d["loss"]:
            tl = [l for l in sec.get("TAIL", []) if l.strip()]
            if tl: lines.append("log tail: `%s`" % redact(tl[-1])[:140])
        if stale >= 3 and not ended: lines.append("warn: log not updated for %d checks" % stale)
        if d["errs"]: lines.append("errors: " + " || ".join(sorted(set(d["errs"]))[-3:]))
        if d["terminal"]: lines.append("verdict line: `%s`" % d["terminal"])
        elif proc_gone: lines.append("process no longer running")
        # Enforcement (CTO review 2026-10-04): stop ONLY the training container on concrete harm.
        harm = None
        tail_txt = " ".join(sec.get("TAIL", []))
        if avail is not None and avail < floor: harm = "MemAvailable %d MiB < floor %d" % (avail, floor)
        elif re.search(r"['\"](?:loss|grad_norm)['\"]\s*:\s*(?:nan|inf|-inf)", tail_txt, re.I): harm = "non-finite loss/grad_norm in log"
        else:
            try:
                dfo = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", a.host, "df -Pk /home/user | tail -1"], capture_output=True, text=True, timeout=30).stdout.split()
                if len(dfo) >= 4 and int(dfo[3]) < 50 * 1024 * 1024: harm = "free disk %d GiB < 50 GiB" % (int(dfo[3]) // (1024 * 1024))
            except Exception: pass
        if harm and not ended:
            if os.environ.get("MONITOR_DRY_RUN"):
                lines.append("ENFORCE (dry-run): would stop training container: " + harm)
            else:
                cmd = "for c in $(docker ps -q); do docker inspect --format '{{.Id}} {{.Config.Cmd}}' $c | grep -q train_sft && docker stop $c; done"
                rr = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", a.host, cmd], capture_output=True, text=True, timeout=120)
                lines.append(":octagonal_sign: ENFORCE: stopped training container (%s); rc=%d" % (harm, rr.returncode))
        post(_decode_text or "\n".join(lines), thread)
        if ended or (harm and not os.environ.get("MONITOR_DRY_RUN")): break
    except Exception as e:
        if not unreachable_posted:
            post("monitor: Spark unreachable (%s); retrying every %ds" % (type(e).__name__, a.interval), thread); unreachable_posted = True
    if time.time() + a.interval > deadline:
        post("monitor: max-hours reached, stopping (run %s may still be going)" % a.run_name, thread); break
    time.sleep(a.interval)
PY
