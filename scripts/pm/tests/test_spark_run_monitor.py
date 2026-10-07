"""Offline tests for spark-run-monitor.sh (CTO REVISE 2026-10-07, thread 1791336431.353319).

Fake ssh (remote shell incl. docker) and fake Slack poster on PATH; no host, Slack or Docker.
"""
import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/spark-run-monitor.sh"
A_ID = "a" * 64
B_ID = "b" * 64
S1 = "2026-10-07T00:00:00.000000001Z"
S2 = "2026-10-07T05:00:00.000000002Z"

FAKE_SSH = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    cmd = sys.argv[-1]
    cfg = json.load(open(os.environ["FAKE_CFG"]))
    with open(os.environ["FAKE_CALLS"], "a") as fh: fh.write(cmd + "\n")
    if "##TAIL" in cmd:
        print("##TAIL"); print(cfg["tail"])
        print("##MTIME"); print("100"); print("200")
        print("##SMI"); print(cfg.get("smi", "[N/A], [N/A], 87 %"))
        print("##MEM"); print("MemAvailable: %d kB" % cfg["mem_kb"])
        print("##HB"); print(cfg.get("hb", ""))
        print("##START"); print(cfg.get("start", "")); print(cfg.get("now", ""))
        print("##PROC"); print(cfg.get("proc", "1"))
    elif "docker stop" in cmd:
        if cfg.get("stop_ack_lost"): sys.exit(255)
    elif "{{.Id}} {{.Name}}" in cmd:
        ident = cfg.get("identities", [])
        n = int(open(os.environ["FAKE_CFG"] + ".n").read()) if os.path.exists(os.environ["FAKE_CFG"] + ".n") else 0
        open(os.environ["FAKE_CFG"] + ".n", "w").write(str(n + 1))
        v = ident[min(n, len(ident) - 1)] if ident else None
        if v is None: sys.exit(1)
        print(v + (" " + cfg.get("running_after_stop", "true") if "{{.State.Running}}" in cmd else ""))
    elif "{{.State.Running}}" in cmd:
        print(cfg.get("running_after_stop", "true"))
    elif "df -Pk" in cmd:
        print("/dev/x 1 1 %d 1%% /home/user" % cfg.get("disk_free_kb", 10**9))
    elif "docker ps" in cmd:
        pass
''').lstrip()

FAKE_SEND = textwrap.dedent('''
    #!/bin/bash
    cat >> "$FAKE_POSTS"; printf '\\n=====\\n' >> "$FAKE_POSTS"; echo "OK ts=1791000000.000100"
''').lstrip()


def run(tmp_path, cfg, *args):
    bindir = tmp_path / "bin"; bindir.mkdir(exist_ok=True)
    (bindir / "ssh").write_text(FAKE_SSH); (bindir / "ssh").chmod(0o755)
    send = tmp_path / "send.sh"; send.write_text(FAKE_SEND); send.chmod(0o755)
    cfgp = tmp_path / "cfg.json"; cfgp.write_text(json.dumps(cfg))
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]), FAKE_CFG=str(cfgp),
               FAKE_CALLS=str(tmp_path / "calls"), FAKE_POSTS=str(tmp_path / "posts"), SPARK_MONITOR_SEND=str(send))
    env.pop("MONITOR_DRY_RUN", None); env.pop("MONITOR_TEST", None)
    r = subprocess.run(["bash", str(SCRIPT), "--run-name", "cl-test", "--log", "/r/log", "--thread-ts", "1.2",
                        "--interval", "0.01", "--max-hours", "0.0000001", *args],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "calls").read_text() if (tmp_path / "calls").exists() else ""
    posts = (tmp_path / "posts").read_text() if (tmp_path / "posts").exists() else ""
    return calls, posts


DECODE_TAIL = "decoding window 40/100"
LOW_MEM_KB = 1024 * 1024  # 1 GiB < 16 GiB floor


def test_a_default_is_report_only_never_stops_unrelated_trainer(tmp_path):
    cfg = {"tail": DECODE_TAIL + '\n{"loss": nan, "grad_norm": inf}', "mem_kb": LOW_MEM_KB,
           "hb": '{"global_step": 40}', "start": "2026-10-07T00:00:00Z", "now": "7200",
           "disk_free_kb": 1, "identities": [B_ID + " /trainer-b " + S1]}
    calls, posts = run(tmp_path, cfg, "--heartbeat", "/r/hb", "--total", "100", "--proc-pattern", "decode-a")
    assert "docker stop" not in calls and "docker ps" not in calls
    assert "WARN: LOW_MEM (report-only)" in posts
    assert "stopped" not in posts


def test_b_enforce_missing_target_no_stop(tmp_path):
    cfg = {"tail": "step 5/10", "mem_kb": LOW_MEM_KB, "identities": [None]}
    calls, posts = run(tmp_path, cfg, "--enforce-container", "train-a")
    assert "docker stop" not in calls
    assert "stopped" not in posts and "ENFORCE skipped" in posts


def test_b_enforce_changed_identity_no_stop(tmp_path):
    cfg = {"tail": "step 5/10", "mem_kb": LOW_MEM_KB,
           "identities": [A_ID + " /train-a " + S1, B_ID + " /train-a " + S1]}
    calls, posts = run(tmp_path, cfg, "--enforce-container", "train-a")
    assert "docker stop" not in calls
    assert "identity missing or changed" in posts and "stopped target" not in posts


def test_b_enforce_stop_not_confirmed_no_false_claim(tmp_path):
    cfg = {"tail": "step 5/10", "mem_kb": LOW_MEM_KB, "identities": [A_ID + " /train-a " + S1],
           "running_after_stop": "true"}
    calls, posts = run(tmp_path, cfg, "--enforce-container", "train-a")
    assert "docker stop %s" % A_ID in calls
    assert "NOT confirmed" in posts and "stopped target container (confirmed)" not in posts


def test_b_enforce_confirmed_stop_targets_exact_id_only(tmp_path):
    cfg = {"tail": "step 5/10", "mem_kb": LOW_MEM_KB, "identities": [A_ID + " /train-a " + S1],
           "running_after_stop": "false"}
    calls, posts = run(tmp_path, cfg, "--enforce-container", "train-a")
    stops = [l for l in calls.splitlines() if "docker stop" in l]
    assert len(stops) == 1 and stops[0].endswith("docker stop %s" % A_ID)
    assert B_ID not in stops[0]
    assert "stopped target container (confirmed)" in posts


def test_c_credentials_and_private_tail_never_posted(tmp_path):
    secret = '{"api_key": "SUPERSECRET123", "loss": "x"}\nprivate deposition text Traceback Error here'
    cfg = {"tail": secret + "\nCUDA error: boom PRIVATEWORD\nREFUSED PRIVATE2", "mem_kb": 64 * 1024 * 1024}
    calls, posts = run(tmp_path, cfg)
    for bad in ("SUPERSECRET", "api_key", "private", "PRIVATE", "deposition", "boom", "log tail", "verdict line"):
        assert bad not in posts, bad
    assert "CUDA_ERROR x1" in posts and "TRACEBACK x1" in posts and "ended: REFUSED" in posts


def test_d_decode_format_unchanged(tmp_path):
    import datetime
    cfg = {"tail": "x", "mem_kb": 64 * 1024 * 1024, "hb": '{"global_step": 40}',
           "start": "2026-10-07T00:00:00", "proc": "1"}
    t0 = datetime.datetime(2026, 10, 7, tzinfo=datetime.timezone.utc).timestamp()
    cfg["now"] = str(int(t0) + 7200)
    calls, posts = run(tmp_path, cfg, "--heartbeat", "/r/hb", "--total", "100", "--proc-pattern", "decode-a")
    first = posts.split("=====")[0].strip().splitlines()
    assert first[0] == "*[cl-test] decode*"
    assert first[1] == "40/100 windows (40%) | elapsed 2h00m | 0.33 windows/min (avg since start) | ETA 2h59m"
    assert first[2] == "GPU util 87% | free RAM (unified) 64 GiB, min 64 GiB, floor 16 GiB"
    assert len(first) == 3


def test_e_lost_ack_then_continuing_ticks_at_most_one_stop(tmp_path):
    cfg = {"tail": "step 5/10", "mem_kb": LOW_MEM_KB, "identities": [A_ID + " /train-a " + S1],
           "running_after_stop": "true", "stop_ack_lost": True}
    calls, posts = run(tmp_path, cfg, "--enforce-container", "train-a", "--max-hours", "0.0003")
    assert calls.count("##TAIL") >= 2, "need a continuing tick"
    assert len([l for l in calls.splitlines() if "docker stop" in l]) == 1
    assert "NOT confirmed" in posts and "already consumed" in posts


def test_f_same_id_name_new_startedat_refused(tmp_path):
    cfg = {"tail": "step 5/10", "mem_kb": LOW_MEM_KB,
           "identities": [A_ID + " /train-a " + S1, A_ID + " /train-a " + S2], "running_after_stop": "false"}
    calls, posts = run(tmp_path, cfg, "--enforce-container", "train-a")
    assert "docker stop" not in calls
    assert "identity missing or changed" in posts and "stopped target" not in posts


def boundary_drift(tmp_path):
    """Run the actual remote shell against fake Docker, restarting after the local check."""
    import contextlib
    import io
    import sys
    import time
    from unittest.mock import patch

    state_file = tmp_path / "docker.json"
    state_file.write_text(json.dumps({"start": S1, "stops": 0, "running": True}))
    docker = tmp_path / "docker"
    docker.write_text('''#!/usr/bin/env python3
import os,sys,json
p=os.environ["DOCKER_STATE"];s=json.load(open(p))
if sys.argv[1]=="inspect":
 f=sys.argv[3]
 if f=="{{.State.Running}}": print(str(s["running"]).lower())
 else: print("a"*64+" /train-a "+s["start"]+(" "+str(s["running"]).lower() if "Running" in f else ""))
elif sys.argv[1]=="stop":
 s["stops"]+=1;s["running"]=False;json.dump(s,open(p,"w"))
''')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + ":" + os.environ["PATH"], DOCKER_STATE=str(state_file))
    real_run = subprocess.run
    posts = []
    clock = [1000.0]

    def fake_run(args, **kwargs):
        if args[0] == "bash":
            posts.append(kwargs.get("input", ""))
            return subprocess.CompletedProcess(args, 0, "OK ts=1.2\n", "")
        assert args[0] == "ssh"
        cmd = args[-1]
        if "##TAIL" in cmd:
            return subprocess.CompletedProcess(args, 0, "##TAIL\nstep 5/10\n##MEM\nMemAvailable: 1024 kB\n", "")
        if "docker stop" in cmd:
            state = json.loads(state_file.read_text()); state["start"] = S2
            state_file.write_text(json.dumps(state))
        return real_run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=10)

    source = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    with patch.object(sys, "argv", ["monitor", "--run-name", "train-a", "--log", "/fixture/log", "--thread-ts", "1.2",
                                   "--enforce-container", "train-a", "--interval", "1", "--max-hours", "0.0006"]), \
         patch.object(subprocess, "run", fake_run), patch.object(time, "time", lambda: clock[0]), \
         patch.object(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
         patch.dict(os.environ, {"MONITOR_DRY_RUN": "", "MONITOR_TEST": ""}), \
         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        exec(compile(source, str(SCRIPT), "exec"), {"__name__": "__main__"})
    return json.loads(state_file.read_text()), posts


def test_g_generation_drift_after_local_check_refused_before_stop(tmp_path):
    state, posts = boundary_drift(tmp_path)
    assert state["stops"] == 0
    assert state["running"] is True
    assert not any("stopped target container (confirmed)" in post for post in posts)
