"""MoP bulk-read guard, bulk-read, code-write and their wiring (added to the repo; MoP changes need no local proof).

Rajiv D0AMF0XE6TS thread 1795287257.526309, ts 1795287629.063499; PM amendments of 22 Nov. CTO BLOCK on 38bd1682 (ts 1795291197.575539, add-on
ts 1795291271.444299) and REVISE on fb6de68d6a35 (ts 1795292716.429489). The deployed entrypoints take nothing from the environment that chooses a
source root, an origin, a model alias, the Claude binary or config, HOME or the state dir. These tests replace the managed constants of
bulk_read_lib through a TEST-ONLY harness (monkeypatch in process; scripts/pm/tests/bulk_read_harness.py for subprocess runs). Every transport
is a local stub or a fake claude binary; nothing here submits to a live model.
"""
import builtins
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[2]
sys.path.insert(0, str(SCRIPTS))
import bulk_read_guard as guard  # noqa: E402
import bulk_read_lib as lib  # noqa: E402

DEFAULT_HEALTH_URL = lib.INKLING_HEALTH_URL  # the managed value, captured before any test patches it
DEFAULT_MANAGED_PYTHON = lib.MANAGED_PYTHON
HARNESS = Path(__file__).parent / "bulk_read_harness.py"
PROXY_AGENT = (Path(__file__).parents[1] / "shared-assets/claude/scripts/proxy-agent.sh").read_text()
LANE = {"PROXY_AGENT_RUN": "1", "PROXY_AGENT_TYPE": "hd-builder"}
SCRUBBED = ("PROXY_AGENT", "BULK_READ_EXEMPT", "MOP_BULK_READ", "BULK_READ_CLAUDE", "BULK_READ_MODEL", "BULK_READ_FALLBACK", "CLIPROXY", "INKLING",
            "CLAUDE_", "ANTHROPIC_")
DEAD = "http://127.0.0.1:9"
FENCE = chr(96) * 3
KEY = "managed-router-key-7"


import bulk_read_cli as bulk_read  # noqa: E402  (logic; the launchers bind the route and call main())
import code_write_cli as code_write  # noqa: E402

STUB = {}  # the approved transport stub (the managed router origin in these tests)
OVERRIDE = []  # every request that reached the server a CALLER tried to point the helpers at


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        STUB.setdefault("gets", []).append((self.path, dict(self.headers)))
        self.send_response(STUB.get("health", 200) if self.path.startswith("/health") else STUB.get("router", 200))
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        body = json.loads(raw)
        STUB.setdefault("requests", []).append(body)
        STUB.setdefault("raw", []).append((raw, dict(self.headers)))
        cfg = STUB.get("models", {}).get(body["model"], {})
        if cfg.get("delay"):
            threading.Event().wait(cfg["delay"])
        if cfg.get("redirect"):
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.end_headers()
            return
        self.send_response(cfg.get("status", 200))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        content = cfg.get("content", STUB.get("content", "- ok (a:1)"))
        self.wfile.write(json.dumps({"choices": [{"message": {"content": content}}],
                                     "usage": {"prompt_tokens": 11, "completion_tokens": 7}}).encode())


class OverrideHandler(BaseHTTPRequestHandler):
    """Where a caller-set CLIPROXY_BASE_URL or INKLING_HEALTH_URL would send things. It answers everything, so any byte that reaches it shows up."""

    def log_message(self, *args):
        pass

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        OVERRIDE.append((self.command, self.path, dict(self.headers), self.rfile.read(length) if length else b""))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": "- hijacked (x:1)"}}]}).encode())

    do_GET = do_POST = _record


FAKE_CLAUDE = "#!" + sys.executable + r'''
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
name = os.path.basename(sys.argv[0])
mode = json.load(open(os.path.join(here, name + ".mode")))
open(os.path.join(here, name + ".call.json"), "w").write(json.dumps(
    {"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "stdin": sys.stdin.read()}))
if mode.get("exit"):
    sys.exit(mode["exit"])
print(json.dumps({"result": mode.get("result", "- ok (a:1)"), "is_error": mode.get("is_error", False),
                  "usage": {"input_tokens": 5, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 0, "output_tokens": 7},
                  "total_cost_usd": 0.0002}))
'''


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_port


def make_fake_claude(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_CLAUDE)
    path.chmod(0o755)
    (path.parent / (path.name + ".mode")).write_text("{}")


def git(cwd, *args):
    """Real git for the fixtures: a repo is what makes a file SOURCE."""
    environment = {"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=environment).stdout


REPO = {"root": None}


def track(*paths):
    """Commit these files into the fixture checkout (they are then tracked source at HEAD)."""
    git(REPO["root"], "add", "-f", "--", *[str(p) for p in paths])
    git(REPO["root"], "commit", "-q", "--allow-empty", "-m", "t")


def put(path, text):
    """Write a file and, when it sits in the fixture checkout, track it. Files elsewhere stay plain."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if REPO["root"] and str(path).startswith(str(REPO["root"]) + os.sep):
        track(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    server, base = serve(Handler)
    other, other_base = serve(OverrideHandler)
    STUB.clear()
    OVERRIDE.clear()
    home = tmp_path / "home"
    projects = home / "Downloads" / "projects"
    src = projects / "heydonna-app"  # an operator-approved checkout of the (patched) managed home, a real git repo
    src.mkdir(parents=True)
    git(src, "init", "-q")
    (src / "app.ts").write_text("export const x = 1;\n")
    git(src, "add", "app.ts")
    git(src, "commit", "-q", "-m", "init")
    REPO["root"] = os.path.realpath(src)
    key_file = tmp_path / "client.env"
    key_file.write_text("export CLIPROXY_API_KEY=%s\n" % KEY)
    claude = tmp_path / "bin" / "claude"
    make_fake_claude(claude)
    patches = {"HOME_DIR": str(home), "PROJECTS_DIR": str(projects), "STATE_DIR": str(tmp_path / "state"),
               "RELEASES_ROOT": str(home / ".local/share/master-of-panes/releases"), "ROUTER_ORIGIN": base,
               "ROUTER_KEY_FILE": str(key_file), "INKLING_HEALTH_URL": base + "/health", "CLAUDE_CANDIDATES": [str(claude)],
               "GIT_CANDIDATES": [shutil.which("git")], "MANAGED_PYTHON": sys.executable}
    for key, value in patches.items():
        monkeypatch.setattr(lib, key, tuple(value) if key in ("CLAUDE_CANDIDATES", "GIT_CANDIDATES") else value)
    monkeypatch.setattr(lib, "BOUND_ROUTE", "claude")  # the PM launcher's route; lane() rebinds it for the lane launcher
    for key in list(os.environ):
        if key.startswith(SCRUBBED):
            monkeypatch.delenv(key)
    monkeypatch.setenv("BULK_READ_TIMEOUT_S", "4")  # an env value can only lower a managed timeout
    monkeypatch.setenv("BULK_READ_INKLING_TIMEOUT_S", "1")
    yield {"state": tmp_path / "state", "tmp": tmp_path, "dir": src, "home": home, "claude": claude, "base": base, "other": other_base,
           "patches": patches}
    REPO["root"] = None
    server.shutdown()
    other.shutdown()


def claude_call(env, name="claude"):
    return json.loads((env["claude"].parent / (name + ".call.json")).read_text())


def set_claude_mode(env, mode, name="claude"):
    (env["claude"].parent / (name + ".mode")).write_text(json.dumps(mode))


def make(path, lines, terminate=True):
    put(path, "".join("line %d\n" % n for n in range(1, lines + 1)) if terminate
        else "\n".join("line %d" % n for n in range(1, lines + 1)))
    return str(path)


def run_entry(env, entry, args, stdin="", cfg=None, python=None, **session):
    """Run a REAL deployed entrypoint in a subprocess through the test-only harness. python: the interpreter command (default this one, -I)."""
    config = env["tmp"] / "harness.json"
    config.write_text(json.dumps({**env["patches"], **(cfg or {})}))
    environment = {k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED)}
    environment.update({"BULK_READ_HARNESS_CONFIG": str(config), "BULK_READ_TIMEOUT_S": "4", "BULK_READ_INKLING_TIMEOUT_S": "1"})
    environment.update(session)
    return subprocess.run([*(python or [sys.executable, "-I"]), str(HARNESS), entry, *args], input=stdin, capture_output=True, text=True,
                          env=environment, timeout=60)


def run_guard(env, payload, cfg=None, **session):
    done = run_entry(env, "bulk_read_guard.py", [], json.dumps(payload), cfg, **session)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if done.stdout.strip() else None


def read(env, path, **tool_input):
    return {"tool_name": "Read", "tool_input": {"file_path": path, **tool_input}, "cwd": str(env["dir"]), "session_id": "s1"}


def bash(env, command):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(env["dir"]), "session_id": "s1"}


def events(env):
    path = env["state"] / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def lane(monkeypatch):
    """Bind the lane route the way bulk-read-lane does (the environment plays no part)."""
    monkeypatch.setattr(lib, "BOUND_ROUTE", "lane")


class Spy:
    """Records every path opened through os.open or builtins.open, and the bytes read through os.read."""

    def __init__(self, monkeypatch):
        self.opened, self.read_bytes, self.fds = [], 0, set()
        real_os_open, real_open, real_read = os.open, builtins.open, os.read

        def os_open(path, *args, **kwargs):
            self.opened.append(os.fspath(path))
            fd = real_os_open(path, *args, **kwargs)
            if os.fspath(path) != os.devnull:
                self.fds.add(fd)
            return fd

        def b_open(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)):
                self.opened.append(os.fspath(file))
            return real_open(file, *args, **kwargs)

        def os_read(fd, n):
            data = real_read(fd, n)
            if fd in self.fds:  # bytes read from files the helpers opened, not from subprocess pipes
                self.read_bytes += len(data)
            return data

        monkeypatch.setattr(os, "open", os_open)
        monkeypatch.setattr(builtins, "open", b_open)
        monkeypatch.setattr(os, "read", os_read)

    def touched(self, path):
        real = os.path.realpath(path)
        return any(os.path.realpath(p) == real for p in self.opened)


# ---- threshold and the redirect message -------------------------------------------------------------------------

def test_routine_lane_big_read_is_blocked_with_the_exact_redirect(env):
    big = make(env["dir"] / "big.txt", 450)
    reason = run_guard(env, read(env, big), **LANE)
    assert "has 450 lines (limit 400)" in reason
    assert lib.LANE_LAUNCHER + ' --paths <files> --question "<q>"' in reason  # a lane is told the absolute lane launcher, the only exempt command
    assert "Read with offset/limit (200 lines or fewer)" in reason
    assert "BULK_READ_ALLOW" not in reason  # the evidence hatch is spelled out for the PM session only
    logged = events(env)[-1]
    assert logged["event"] == "block" and logged["lines"] == 450 and logged["tool"] == "Read" and logged["path"].endswith("big.txt")
    assert isinstance(logged["latency_ms"], int) and logged["backend"] == "inkling"
    assert "line 1" not in (env["state"] / "events.jsonl").read_text()  # paths and counts only, never content


@pytest.mark.parametrize("lines,terminate,blocked", [(400, True, False), (401, True, True), (400, False, False), (401, False, True)])
def test_threshold_is_more_than_400_lines(env, lines, terminate, blocked):
    path = make(env["dir"] / "t.txt", lines, terminate)
    assert (run_guard(env, read(env, path), **LANE) is not None) is blocked


@pytest.mark.parametrize("name,payload_extra", [("offset", {"offset": 10}), ("limit", {"limit": 50}), ("both", {"offset": 1, "limit": 200})])
def test_any_offset_or_limit_is_allowed(env, name, payload_extra):
    big = make(env["dir"] / "big.txt", 900)
    assert run_guard(env, read(env, big, **payload_extra), **LANE) is None
    assert run_guard(env, read(env, big, **payload_extra)) is None  # and in the PM session


@pytest.mark.parametrize("name", ["shot.png", "doc.pdf", "nb.ipynb", "photo.JPG"])
def test_images_pdf_and_notebooks_are_carved_out(env, name):
    path = env["dir"] / name
    path.write_text("x\n" * 900)
    assert run_guard(env, read(env, str(path)), **LANE) is None


def test_binary_files_and_missing_files_and_own_helpers_are_carved_out(env):
    binary = env["dir"] / "blob.dat"
    binary.write_bytes(b"\0abc\n" * 900)
    assert run_guard(env, read(env, str(binary)), **LANE) is None
    assert run_guard(env, read(env, str(env["dir"] / "absent.txt")), **LANE) is None
    for helper in ("bulk_read_guard.py", "bulk_read_lib.py", "bulk_read_cli.py", "code_write_cli.py", "bulk-read", "bulk-read-lane", "code-write",
                   "code-write-lane"):
        assert run_guard(env, read(env, str(SCRIPTS / helper)), **LANE) is None
    state_file = env["state"] / "events.jsonl"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("x\n" * 900)
    assert run_guard(env, read(env, str(state_file)), **LANE) is None


# ---- the PM session: blocked, with an escape hatch -----------------------------------------------------------------

def test_pm_session_is_blocked_and_told_how_to_do_an_evidence_read(env):
    """No PROXY_AGENT_RUN: the PM pane (and its in-process subagents). Rajiv ts 1795289208.297109: the PM is not exempt."""
    big = make(env["dir"] / "big.txt", 900)
    reason = run_guard(env, read(env, big))
    assert "has 900 lines (limit 400)" in reason and 'bulk-read --paths <files> --question "<q>"' in reason
    assert "offset=1 and limit=900" in reason and "BULK_READ_ALLOW=1" in reason
    assert events(env)[-1]["event"] == "block" and events(env)[-1]["backend"] == "claude"
    assert run_guard(env, bash(env, "cat " + big)) is not None


def test_pm_escape_hatches_allow_the_original_read(env):
    big = make(env["dir"] / "big.txt", 900)
    assert run_guard(env, read(env, big, offset=1, limit=900)) is None
    assert run_guard(env, bash(env, "BULK_READ_ALLOW=1 cat " + big)) is None
    assert run_guard(env, bash(env, "BULK_READ_ALLOW=1 head -n 900 " + big)) is None
    assert run_guard(env, bash(env, "cat " + big)) is not None  # the marker covers that one call only


def test_pm_session_never_waits_on_the_network_and_fails_open_without_claude(env):
    big = make(env["dir"] / "big.txt", 900)
    dead = {"INKLING_HEALTH_URL": DEAD + "/health", "ROUTER_ORIGIN": DEAD}
    assert run_guard(env, read(env, big), cfg=dead) is not None  # the PM route has no network gate: a dead Spark and router change nothing
    assert not list((env["state"]).glob("health-*.json"))
    assert run_guard(env, read(env, big), cfg={"CLAUDE_CANDIDATES": ["/nonexistent/claude"]}) is None
    assert events(env)[-1]["event"] == "fail_open" and events(env)[-1]["reason"] == "claude_missing"


def test_pm_breaker_is_separate_from_the_lane_breaker(env):
    big = make(env["dir"] / "big.txt", 900)
    for _ in range(3):
        lib.breaker_record(False, name="claude")
    assert run_guard(env, read(env, big)) is None and events(env)[-1]["reason"] == "breaker_open"
    assert run_guard(env, read(env, big), **LANE) is not None  # lanes are unaffected


def test_inkling_health_default_matches_the_proxy_launcher_gate():
    assert DEFAULT_HEALTH_URL == "http://192.168.68.113:18120/v1/models"
    assert "INKLING_HEALTH_URL:-http://192.168.68.113:18120/v1/models" in PROXY_AGENT  # one URL, two gates


# ---- who is exempt in a proxy lane (PM amendment 22 Nov 00:42 IST) -------------------------------------------------

def test_judgment_lane_types_match_proxy_agent_and_are_exempt(env):
    big = make(env["dir"] / "big.txt", 900)
    match = re.search(r'proxy_agent_is_judgment_lane\(\) \{.*?" ((?:[a-z0-9-]+ )+)" ==', PROXY_AGENT, re.S)
    assert match, "judgment helper missing from proxy-agent.sh"
    assert tuple(match.group(1).split()) == guard.JUDGMENT_TYPES  # one list, two places
    for kind in guard.JUDGMENT_TYPES:
        assert run_guard(env, read(env, big), PROXY_AGENT_RUN="1", PROXY_AGENT_TYPE=kind) is None, kind


@pytest.mark.parametrize("kind", ["hd-builder", "hd-proof-qa", "claude", "pm-triage-agent", "mop-main-repair"])
def test_routine_lane_types_are_blocked(env, kind):
    big = make(env["dir"] / "big.txt", 900)
    assert run_guard(env, read(env, big), PROXY_AGENT_RUN="1", PROXY_AGENT_TYPE=kind) is not None


def test_p0_override_and_explicit_exemption_and_opt_outs(env):
    big = make(env["dir"] / "big.txt", 900)
    assert run_guard(env, read(env, big), PROXY_AGENT_CAP_OVERRIDE="P0:14221", **LANE) is None
    assert run_guard(env, read(env, big), PROXY_AGENT_CAP_OVERRIDE="rajiv:1795.1", **LANE) is not None  # only P0 counts
    assert run_guard(env, read(env, big), PROXY_AGENT_CAP_OVERRIDE="P0:14221") is not None  # the P0 exemption is for lanes, not the PM
    assert run_guard(env, read(env, big), BULK_READ_EXEMPT="1", **LANE) is None
    for value in ("0", "off", "false", "disabled"):
        assert run_guard(env, read(env, big), MOP_BULK_READ_GUARD=value, **LANE) is None
        assert run_guard(env, read(env, big), MOP_BULK_READ_GUARD=value) is None
    env["state"].mkdir(parents=True, exist_ok=True)
    (env["state"] / "DISABLED").write_text("")
    assert run_guard(env, read(env, big), **LANE) is None and run_guard(env, read(env, big)) is None


def test_lane_type_falls_back_to_the_lane_name_and_unclassified_lanes_are_exempt(env):
    big = make(env["dir"] / "big.txt", 900)
    stamp = "-20261121T185233Z-15690"
    named = {"PROXY_AGENT_RUN": "1"}
    assert run_guard(env, read(env, big), PROXY_AGENT_NAME="proxy-hd-investigator" + stamp, **named) is None
    assert run_guard(env, read(env, big), PROXY_AGENT_NAME="proxy-hd-builder" + stamp, **named) is not None
    assert run_guard(env, read(env, big), PROXY_AGENT_NAME="custom-name", **named) is None
    assert run_guard(env, read(env, big), **named) is None


# ---- Bash cat/head/tail/less ------------------------------------------------------------------------------------

@pytest.mark.parametrize("command,blocked", [
    ("cat {f}", True), ("less {f}", True), ("more {f}", True), ("cat -n {f}", True), ("cd {d} && cat big.txt", True),
    ("head -n 500 {f}", True), ("head -500 {f}", True), ("tail -n +1 {f}", True), ("tail -n +5 {f}", True),
    ("head {f}", False), ("head -n 50 {f}", False), ("tail -20 {f}", False), ("tail -n 400 {f}", False),
    ("cat {f} | grep x", False), ("cat {f} > /dev/null", False), ("grep x {f}", False), ("cat {f} other.txt", False),
    ("head -c 100 {f}", False), ("tail -f {f}", False), ("echo $(cat {f})", False),
])
def test_bash_single_path_reads(env, command, blocked):
    big = make(env["dir"] / "big.txt", 450)
    text = command.format(f=big, d=env["dir"])
    assert (run_guard(env, bash(env, text), **LANE) is not None) is blocked


def test_bash_parser_and_line_math():
    assert guard.parse_bash("cat a.txt") == {"tool": "Bash:cat", "path": "a.txt", "cd": None, "count": None}
    assert guard.parse_bash("tail -n +10 a.txt")["count"] == ("from", 10)
    assert guard.parse_bash("head -n -5 a.txt")["count"] == ("all_but", 5)
    assert guard.parse_bash("head -n x a.txt") is None
    assert guard.printed_lines(("from", 10), 450) == 441 and guard.printed_lines(("all_but", 5), 450) == 445
    assert guard.printed_lines(None, 450) == 450 and guard.printed_lines(("first", 10), 450) == 10


# ---- fail open --------------------------------------------------------------------------------------------------------

def test_lane_with_inkling_down_still_blocks_because_luna_answers(env):
    big = make(env["dir"] / "big.txt", 450)
    assert run_guard(env, read(env, big), cfg={"INKLING_HEALTH_URL": DEAD + "/health"}, **LANE) is not None
    assert events(env)[-1]["event"] == "block" and events(env)[-1]["backend"] == "luna"


def test_lane_fails_open_only_when_inkling_and_the_router_are_both_down(env):
    big = make(env["dir"] / "big.txt", 450)
    dead = {"INKLING_HEALTH_URL": DEAD + "/health", "ROUTER_ORIGIN": DEAD}
    assert run_guard(env, read(env, big), cfg=dead, **LANE) is None
    assert events(env)[-1]["event"] == "fail_open" and events(env)[-1]["reason"].startswith("no_backend")


def test_unhealthy_statuses_fail_open_for_a_lane(env):
    STUB["health"], STUB["router"] = 503, 503
    big = make(env["dir"] / "big.txt", 450)
    assert run_guard(env, read(env, big), **LANE) is None
    assert events(env)[-1]["reason"].startswith("no_backend")


def test_hook_error_and_garbage_input_allow_the_call(env):
    for stdin in ("not json at all", "[]", '{"tool_name": "Read", "tool_input": "oops"}', ""):
        done = run_entry(env, "bulk_read_guard.py", [], stdin, **LANE)
        assert done.returncode == 0 and done.stdout.strip() == ""


def test_allowed_reads_never_touch_the_network_or_the_log(env):
    small = make(env["dir"] / "small.txt", 10)
    assert run_guard(env, read(env, small), cfg={"INKLING_HEALTH_URL": DEAD + "/health"}, **LANE) is None
    assert not list((env["state"]).glob("health-*.json")) and events(env) == []


def test_circuit_breaker_opens_after_three_failures_and_closes_on_success(env):
    big = make(env["dir"] / "big.txt", 450)
    for _ in range(3):
        lib.breaker_record(False)
    assert lib.breaker_open()
    assert run_guard(env, read(env, big), **LANE) is None and events(env)[-1]["reason"] == "breaker_open"
    lib.breaker_record(True)
    assert not lib.breaker_open() and run_guard(env, read(env, big), **LANE) is not None


# ---- bulk-read: backend order and fail open --------------------------------------------------------------------------

def test_lane_asks_inkling_first_prints_only_bullets_and_sends_numbered_lines(env, monkeypatch, capsys):
    lane(monkeypatch)
    path = make(env["dir"] / "a.txt", 5)
    STUB["content"] = "<think>hmm</think>Here is the answer:\n- first fact (a.txt:1)\n* second fact (a.txt:2-3)\n3. third (a.txt:5)\nThat is all."
    assert bulk_read.main(["--paths", path, "--question", "what?"]) == 0
    assert capsys.readouterr().out.splitlines() == ["- first fact (a.txt:1)", "- second fact (a.txt:2-3)", "- third (a.txt:5)"]
    assert [r["model"] for r in STUB["requests"]] == ["inkling-2-mini"]
    sent = STUB["requests"][0]
    assert "    1| line 1" in sent["messages"][1]["content"] and "Question: what?" in sent["messages"][1]["content"]
    logged = events(env)[-1]
    assert logged["event"] == "delegate" and logged["backend"] == "inkling-2-mini" and logged["lines"] == 5
    assert logged["latency_ms"] >= 0 and "line 1" not in json.dumps(logged)


def test_lane_with_inkling_down_uses_luna(env, monkeypatch, capsys):
    """Rajiv ts 1795289295.887739: 'if inkling is down, proxies should use luna'."""
    lane(monkeypatch)
    monkeypatch.setattr(lib, "INKLING_HEALTH_URL", DEAD + "/health")
    path = make(env["dir"] / "a.txt", 5)
    STUB["content"] = "- luna fact (a.txt:4)"
    assert bulk_read.main(["--paths", path, "--question", "q"]) == 0
    assert capsys.readouterr().out.strip() == "- luna fact (a.txt:4)"
    assert [r["model"] for r in STUB["requests"]] == ["gpt-6-luna"]
    assert events(env)[-1]["backend"] == "gpt-6-luna"


@pytest.mark.parametrize("inkling", [{"status": 500}, {"delay": 3}, {"content": "<think>only thoughts</think>"}])
def test_lane_falls_back_to_luna_when_inkling_errors_times_out_or_is_empty(env, monkeypatch, capsys, inkling):
    lane(monkeypatch)
    path = make(env["dir"] / "a.txt", 5)
    STUB["models"] = {"inkling-2-mini": inkling, "gpt-6-luna": {"content": "- luna fact (a.txt:1)"}}
    assert bulk_read.main(["--paths", path, "--question", "q"]) == 0
    assert capsys.readouterr().out.strip() == "- luna fact (a.txt:1)"
    assert [r["model"] for r in STUB["requests"]] == ["inkling-2-mini", "gpt-6-luna"]


def test_lane_fails_open_to_the_original_read_only_when_both_fail(env, monkeypatch, capsys):
    lane(monkeypatch)
    monkeypatch.setenv("BULK_READ_TIMEOUT_S", "2")
    big = make(env["dir"] / "big.txt", 450)
    assert run_guard(env, read(env, big), **LANE) is not None
    STUB["models"] = {"inkling-2-mini": {"status": 500}, "gpt-6-luna": {"delay": 4}}  # luna times out
    assert bulk_read.main(["--paths", big, "--question", "q"]) == 3
    assert "no backend answered (inkling_http_500,luna_timeout)" in capsys.readouterr().err
    assert events(env)[-1]["event"] == "delegate_fail_open"
    assert run_guard(env, read(env, big), **LANE) is None  # the original read is now allowed
    assert events(env)[-1]["reason"] == "bypass_token"


def test_lane_with_no_backend_exits_3_without_calling_a_model(env, monkeypatch):
    lane(monkeypatch)
    monkeypatch.setattr(lib, "INKLING_HEALTH_URL", DEAD + "/health")
    monkeypatch.setattr(lib, "ROUTER_ORIGIN", DEAD)
    big = make(env["dir"] / "big.txt", 450)
    assert bulk_read.main(["--paths", big, "--question", "q"]) == 3
    assert "requests" not in STUB and lib.bypass_active(big)


def test_three_failed_runs_open_the_breaker_so_the_guard_stops_blocking(env, monkeypatch):
    lane(monkeypatch)
    big = make(env["dir"] / "big.txt", 450)
    STUB["models"] = {"inkling-2-mini": {"status": 500}, "gpt-6-luna": {"status": 500}}
    for _ in range(3):
        assert bulk_read.main(["--paths", big, "--question", "q"]) == 3
    assert lib.breaker_open(name="lane")
    (env["state"] / "bypass").rename(env["state"] / "bypass-gone")
    assert run_guard(env, read(env, big), **LANE) is None and events(env)[-1]["reason"] == "breaker_open"


def test_bulk_read_usage_errors_and_size_cap(env, monkeypatch):
    lane(monkeypatch)
    big = make(env["dir"] / "big.txt", 450)
    assert bulk_read.main(["--paths", str(env["dir"] / "nope"), "--question", "q"]) == 2
    monkeypatch.setenv("BULK_READ_MAX_CHARS", "100")
    assert bulk_read.main(["--paths", big, "--question", "q"]) == 4
    assert not lib.bypass_active(big) and "requests" not in STUB  # too large is not an outage


def test_default_size_cap_is_smaller_in_a_lane_than_for_the_pm(env, monkeypatch, capsys):
    path = env["dir"] / "mid.txt"
    put(path, "x" * 60 + "\n" * 1 + "".join("line %d with some padding text\n" % n for n in range(1500)))  # about 50k chars
    assert bulk_read.main(["--paths", str(path), "--question", "q"]) == 0  # the PM route takes it (cap 80000)
    lane(monkeypatch)
    assert bulk_read.main(["--paths", str(path), "--question", "q"]) == 4  # a lane refuses without waiting 30 s (cap 36000)
    assert "over the 36000 cap" in capsys.readouterr().err and "requests" not in STUB and not lib.bypass_active(str(path))


@pytest.mark.parametrize("name", ["BULK_READ_MAX_CHARS", "BULK_READ_TIMEOUT_S", "BULK_READ_INKLING_TIMEOUT_S", "CODE_WRITE_MAX_CHARS", "CODE_WRITE_TIMEOUT_S"])
def test_env_can_only_lower_a_cap_or_a_timeout(monkeypatch, name):
    defaults = {"BULK_READ_MAX_CHARS": 36000, "BULK_READ_TIMEOUT_S": 30, "BULK_READ_INKLING_TIMEOUT_S": 15, "CODE_WRITE_MAX_CHARS": 80000,
                "CODE_WRITE_TIMEOUT_S": 120}
    monkeypatch.setenv(name, "99999999")
    assert lib.cap_env(name, defaults[name]) == defaults[name]  # raising is ignored
    monkeypatch.setenv(name, "5")
    assert lib.cap_env(name, defaults[name]) == 5
    for junk in ("", "abc", "-3", "0", "nan"):
        monkeypatch.setenv(name, junk)
        assert lib.cap_env(name, defaults[name]) == defaults[name]


# ---- PM session backend: direct Claude Haiku or Sonnet -------------------------------------------------------------------

def test_pm_session_answers_with_haiku_on_direct_claude_and_never_touches_the_router(env, monkeypatch, capsys):
    path = make(env["dir"] / "a.txt", 5)
    set_claude_mode(env, {"result": "Intro\n- pm fact (a.txt:2)"})
    assert bulk_read.main(["--paths", path, "--question", "what?"]) == 0
    assert capsys.readouterr().out.strip() == "- pm fact (a.txt:2)"
    assert "requests" not in STUB  # neither Inkling nor luna
    call = claude_call(env)
    args = call["argv"]
    assert args[args.index("--model") + 1] == "haiku" and args[args.index("--setting-sources") + 1] == ""
    assert json.loads(args[args.index("--settings") + 1]) == {"disableAllHooks": True}
    assert args[args.index("--tools") + 1] == "" and "--no-session-persistence" in args and "-p" in args
    assert "    1| line 1" in call["stdin"] and "Question: what?" in call["stdin"]
    assert Path(call["cwd"]).resolve() == Path(tempfile.gettempdir()).resolve()  # no project, no CLAUDE.md
    logged = events(env)[-1]
    assert logged["backend"] == "claude-haiku" and logged["prompt_tokens"] == 15 and logged["cost_usd"] == 0.0002


@pytest.mark.parametrize("setting,expected", [("sonnet", "sonnet"), ("SONNET", "sonnet"), ("haiku", "haiku"), ("opus", "haiku"), ("gpt-6-luna", "haiku")])
def test_pm_model_is_haiku_by_default_and_sonnet_on_request_nothing_else(env, monkeypatch, setting, expected):
    monkeypatch.setenv("BULK_READ_CLAUDE_MODEL", setting)
    path = make(env["dir"] / "a.txt", 5)
    assert bulk_read.main(["--paths", path, "--question", "q"]) == 0
    args = claude_call(env)["argv"]
    assert args[args.index("--model") + 1] == expected


def test_pm_claude_failure_fails_open_for_the_guard(env, capsys):
    big = make(env["dir"] / "big.txt", 450)
    assert run_guard(env, read(env, big)) is not None
    set_claude_mode(env, {"exit": 1})
    assert bulk_read.main(["--paths", big, "--question", "q"]) == 3
    assert "no backend answered" in capsys.readouterr().err
    assert run_guard(env, read(env, big)) is None and events(env)[-1]["reason"] == "bypass_token"


# ---- code-write ------------------------------------------------------------------------------------------------------

def test_code_write_strips_fences_and_prints_path_and_line_count(env, monkeypatch, capsys):
    lane(monkeypatch)
    ref, target = env["dir"] / "ref.py", env["dir"] / "out" / "new.py"
    put(ref, "print(1)\n")
    STUB["content"] = FENCE + "python\nimport os\nprint(os.name)\n" + FENCE
    assert code_write.main(["write a script", str(ref), str(target)]) == 0
    assert target.read_text() == "import os\nprint(os.name)\n"
    assert capsys.readouterr().out.strip() == "%s 2 lines" % target
    assert code_write.main(["again", str(ref), str(target)]) == 2  # refuses an existing target
    assert target.read_text() == "import os\nprint(os.name)\n"  # and leaves it untouched
    assert code_write.main(["--force", "again", str(ref), str(target)]) == 0


def test_code_write_lane_uses_luna_when_inkling_is_down_and_pm_uses_claude(env, monkeypatch):
    ref = env["dir"] / "ref.py"
    put(ref, "x\n")
    set_claude_mode(env, {"result": "print('pm')"})
    assert code_write.main(["spec", str(ref), str(env["dir"] / "pm.py")]) == 0
    assert (env["dir"] / "pm.py").read_text() == "print('pm')\n" and "requests" not in STUB
    lane(monkeypatch)
    monkeypatch.setattr(lib, "INKLING_HEALTH_URL", DEAD + "/health")
    STUB["content"] = "print('luna')"
    assert code_write.main(["spec", str(ref), str(env["dir"] / "lane.py")]) == 0
    assert (env["dir"] / "lane.py").read_text() == "print('luna')\n" and [r["model"] for r in STUB["requests"]] == ["gpt-6-luna"]


def test_code_write_failures_write_nothing(env, monkeypatch):
    lane(monkeypatch)
    ref, target = env["dir"] / "ref.py", env["dir"] / "new.py"
    put(ref, "x\n")
    STUB["content"] = "<think>nothing</think>"
    assert code_write.main(["spec", str(ref), str(target)]) == 3 and not target.exists()  # both backends answered empty
    monkeypatch.setattr(lib, "INKLING_HEALTH_URL", DEAD + "/health")
    monkeypatch.setattr(lib, "ROUTER_ORIGIN", DEAD)
    for cached in env["state"].glob("health-*.json"):
        cached.unlink()
    assert code_write.main(["spec", str(ref), str(target)]) == 3 and not target.exists()


# ---- source boundary --------------------------------------------------------------------------------------------------

RESTRICTED = [".env", ".env.local", "app/.env.production", "config/secrets.yaml", "api_secret.txt", "my-credentials.json", "tls/server.pem",
              "tls/server.key", ".ssh/id_rsa", "id_ed25519", ".codex/auth.json", ".cli-proxy-api/config.yaml", ".claude/state/pm-ops.db",
              "customer-artifacts/a.txt", "evidence/log.txt", "notes-evidence.txt", "slack-images/a.png.txt", ".git/config",
              ".claude/settings.local.json", "data.sqlite"]
ALLOWED = ["src/app.ts", "docs/readme.md", ".claude/skills/x/SKILL.md", "scripts/slack-send.sh", "lib/token-usage.ts", ".github/workflows/ci.yml"]


@pytest.mark.parametrize("relative", RESTRICTED)
def test_restricted_data_classes_are_refused_even_inside_an_approved_root(env, monkeypatch, relative):
    lane(monkeypatch)
    path = env["dir"] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("CLASSIFIED-CANARY\n")
    assert lib.input_refusal(os.path.realpath(path)) == "restricted_data_class"
    assert bulk_read.main(["--paths", str(path), "--question", "q"]) == 6
    assert "requests" not in STUB


@pytest.mark.parametrize("relative", ALLOWED)
def test_ordinary_source_paths_are_approved(env, relative):
    path = env["dir"] / relative
    put(path, "ok\n")
    assert lib.input_refusal(os.path.realpath(path)) is None and lib.membership_refusal(os.path.realpath(path)) is None  # tracked source


@pytest.mark.parametrize("outside", ["/etc/hosts", "~/.ssh/config", "~/.codex/auth.json", "~/.cli-proxy-api/x.yaml", "~/.claude/state/pm-ops.db",
                                     "/tmp/some-evidence-dir/log.txt", "~/.config/cliproxyapi/client.env"])
def test_paths_outside_the_approved_roots_are_refused_and_send_nothing(env, monkeypatch, outside):
    lane(monkeypatch)
    assert bulk_read.main(["--paths", outside, "--question", "q"]) == 6
    assert "requests" not in STUB


def worktree_fixture(env, name, register=True, back_link=True):
    """A worktree outside every approved root (like a /tmp lane clone), registered by the heydonna-app checkout's OWN .git."""
    checkout = env["home"] / "Downloads" / "projects" / "heydonna-app"
    admin = checkout / ".git" / "worktrees" / name
    top = env["tmp"] / "lanes" / name
    top.mkdir(parents=True)
    (top / "app.ts").write_text("export const x = 1;\n")
    (top / ".env").write_text("TOKEN=1\n")
    if register:
        admin.mkdir(parents=True)
        (admin / "gitdir").write_text(str(top / ".git") + "\n")
    if back_link:
        (top / ".git").write_text("gitdir: %s\n" % admin)
    return top


def test_approved_roots_are_the_fixed_checkouts_and_the_worktrees_their_own_git_registers(env):
    projects = env["home"] / "Downloads" / "projects"
    for name in ("heydonna-app-3003", "heydonna-app-proxy", "master-of-panes", "heydonna-app-3002-evidence", "heydonna-app-3004-artifacts",
                 "heydonna-app-3006-raw", "heydonna-app-2", "master-of-panes-capture-adapter", "heydonna-private-9828"):
        (projects / name / ".git").mkdir(parents=True)  # a git checkout
    (projects / "heydonna-app-3005").mkdir()  # a listed name that is not a git checkout is not a root
    roots = {os.path.basename(root.path) for root in lib.source_roots()}
    assert "heydonna-app-3005" not in roots
    assert {"heydonna-app", "heydonna-app-3003", "heydonna-app-proxy", "master-of-panes", "releases"} <= roots
    assert not roots & {"heydonna-app-3002-evidence", "heydonna-app-3004-artifacts", "heydonna-app-3006-raw", "heydonna-app-2",
                        "master-of-panes-capture-adapter", "heydonna-private-9828"}
    good = worktree_fixture(env, "lane-good")
    assert lib.input_refusal(os.path.realpath(good / "app.ts")) is None  # registered in the repo's own .git, back-linked
    assert lib.input_refusal(os.path.realpath(good / ".env")) == "restricted_data_class"
    evidence = worktree_fixture(env, "lane-evidence")
    assert lib.input_refusal(os.path.realpath(evidence / "app.ts")) == "restricted_data_class"  # restricted classes look at the FULL path
    ok = worktree_fixture(env, "lane-ok")
    nested = ok / "evidence"
    nested.mkdir()
    (nested / "log.txt").write_text("x\n")
    assert lib.input_refusal(os.path.realpath(nested / "log.txt")) == "restricted_data_class"  # and an evidence dir inside is denied


def test_a_tmp_clone_or_a_forged_git_pointer_grants_nothing(env):
    """CTO REVISE: writable /tmp git config must not grant approval. Only the repo's own .git/worktrees registration counts."""
    clone = env["tmp"] / "lanes" / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/heydonna-app/heydonna-app.git\n')
    (clone / "app.ts").write_text("x\n")
    assert lib.input_refusal(os.path.realpath(clone / "app.ts")) == "outside_approved_roots"
    registered = worktree_fixture(env, "lane-real")
    forged = env["tmp"] / "lanes" / "forged"  # points at the real registration, but the registration names a different path
    forged.mkdir()
    (forged / "app.ts").write_text("x\n")
    (forged / ".git").write_text((registered / ".git").read_text())
    assert lib.input_refusal(os.path.realpath(forged / "app.ts")) == "outside_approved_roots"
    unregistered = worktree_fixture(env, "lane-unregistered", register=False)  # a .git file alone is nothing
    assert lib.input_refusal(os.path.realpath(unregistered / "app.ts")) == "outside_approved_roots"
    no_back_link = worktree_fixture(env, "lane-nolink", back_link=False)  # registered, but the directory does not point back
    assert lib.input_refusal(os.path.realpath(no_back_link / "app.ts")) == "outside_approved_roots"


def test_target_of_code_write_may_not_sit_in_a_restricted_place(env):
    assert lib.target_refusal(str(env["dir"] / ".ssh" / "authorized_keys")) == "restricted_data_class"
    assert lib.target_refusal(str(env["dir"] / "out" / "new.py")) is None


def test_code_write_refuses_bad_references_and_targets_with_zero_bytes(env, monkeypatch, tmp_path_factory):
    lane(monkeypatch)
    ok = env["dir"] / "ref.py"
    put(ok, "x\n")
    secret_ref = env["dir"] / ".env"
    secret_ref.write_text("SECRET-CANARY\n")
    outside = tmp_path_factory.mktemp("outside") / "ref.py"
    outside.write_text("OUTSIDE-CANARY\n")
    spy = Spy(monkeypatch)
    assert code_write.main(["spec", str(secret_ref), str(env["dir"] / "a.py")]) == 6
    assert code_write.main(["spec", str(outside), str(env["dir"] / "b.py")]) == 6
    assert code_write.main(["spec", str(ok), str(env["dir"] / ".ssh" / "keys")]) == 6
    assert "requests" not in STUB and not spy.touched(secret_ref) and not spy.touched(outside)
    assert not (env["dir"] / "a.py").exists() and not (env["dir"] / "b.py").exists() and not (env["dir"] / ".ssh").exists()


def test_guard_leaves_restricted_and_outside_root_files_to_the_native_read(env, tmp_path_factory):
    approved = make(env["dir"] / "big.ts", 900)
    restricted = make(env["dir"] / ".env.big", 900)
    outside = make(tmp_path_factory.mktemp("outside") / "big.txt", 900)
    assert run_guard(env, read(env, approved), **LANE) is not None  # an approved big file is still redirected
    assert run_guard(env, read(env, restricted), **LANE) is None  # bulk-read would refuse these, so the native Read stands
    assert run_guard(env, read(env, outside), **LANE) is None
    assert run_guard(env, bash(env, "cat " + restricted), **LANE) is None
    assert run_guard(env, read(env, restricted)) is None and run_guard(env, read(env, outside)) is None  # the PM session too


# ---- THE isolated proof (CTO REVISE on fb6de68d6a35, ts 1795292716.429489) ----------------------------------------------

CALLER_OVERRIDES = ("MOP_BULK_READ_TEST_MODE", "MOP_BULK_READ_EXTRA_ROOTS", "MOP_BULK_READ_STATE_DIR", "CLIPROXY_BASE_URL", "CLIPROXY_API_KEY",
                    "INKLING_HEALTH_URL", "BULK_READ_MODEL", "BULK_READ_FALLBACK_MODEL", "BULK_READ_CLAUDE_BIN", "CLAUDE_CONFIG_DIR",
                    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "HTTP_PROXY", "http_proxy", "ALL_PROXY")


def set_caller_overrides(env, monkeypatch, untrusted_root):
    """Everything a caller could set to widen the source or redirect the egress, aimed at the OVERRIDE server."""
    values = {"MOP_BULK_READ_TEST_MODE": "1", "MOP_BULK_READ_EXTRA_ROOTS": str(untrusted_root), "MOP_BULK_READ_STATE_DIR": str(env["tmp"] / "evil-state"),
              "CLIPROXY_BASE_URL": env["other"], "CLIPROXY_API_KEY": "caller-key-1", "INKLING_HEALTH_URL": env["other"] + "/health",
              "BULK_READ_MODEL": "evil-model", "BULK_READ_FALLBACK_MODEL": "evil-fallback", "BULK_READ_CLAUDE_BIN": str(env["tmp"] / "evil-claude"),
              "CLAUDE_CONFIG_DIR": str(env["tmp"] / "evil-config"), "CLAUDE_CODE_OAUTH_TOKEN": "caller-oauth-token",
              "ANTHROPIC_BASE_URL": env["other"], "ANTHROPIC_AUTH_TOKEN": "caller-anthropic-token", "HTTP_PROXY": env["other"],
              "http_proxy": env["other"], "ALL_PROXY": env["other"], "HOME": str(untrusted_root / "fake-home")}
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_isolated_proof_trusted_source_pinned_egress(env, monkeypatch, tmp_path_factory):
    """An approved source reaches the approved stub. With every caller override set and an untrusted source, zero content AND zero credential
    bytes reach any transport, and nothing is even opened. The override server never hears from the helpers in any case."""
    lane(monkeypatch)
    untrusted = tmp_path_factory.mktemp("untrusted")
    set_caller_overrides(env, monkeypatch, untrusted)
    (untrusted / "fake-home" / "Downloads" / "projects" / "heydonna-app").mkdir(parents=True)  # a lookalike checkout under the caller's HOME
    sources = {}
    plain = untrusted / "plain.ts"  # (1) named by the EXTRA_ROOTS override
    plain.write_text("UNTRUSTED-CANARY-1\n")
    sources["plain"] = plain
    lookalike = untrusted / "fake-home" / "Downloads" / "projects" / "heydonna-app" / "app.ts"  # (2) under the caller's $HOME
    lookalike.write_text("UNTRUSTED-CANARY-2\n")
    sources["lookalike-home"] = lookalike
    clone = untrusted / "clone"  # (3) a /tmp-style clone whose git config names the real repo
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/heydonna-app/heydonna-app.git\n')
    (clone / "app.ts").write_text("UNTRUSTED-CANARY-3\n")
    sources["clone"] = clone / "app.ts"
    registered = worktree_fixture(env, "lane-proof")
    forged = untrusted / "forged"  # (4) a forged pointer at a real registration
    forged.mkdir()
    (forged / "app.ts").write_text("UNTRUSTED-CANARY-4\n")
    (forged / ".git").write_text((registered / ".git").read_text())
    sources["forged"] = forged / "app.ts"
    approved = env["dir"] / "src.ts"
    put(approved, "export const answer = 'APPROVED-CANARY';\n" * 3)
    spy = Spy(monkeypatch)
    for label, source in sources.items():
        assert bulk_read.main(["--paths", str(source), "--question", "q"]) == 6, label
        assert code_write.main(["spec", str(source), str(env["dir"] / ("out-%s.py" % label))]) == 6, label
    assert "requests" not in STUB and "gets" not in STUB and OVERRIDE == []  # zero bytes of any kind, at either server
    assert not any(spy.touched(s) for s in sources.values()) and spy.read_bytes == 0  # not even opened
    assert not list(env["dir"].glob("out-*.py"))
    assert bulk_read.main(["--paths", str(approved), "--question", "q"]) == 0  # the approved source goes to the approved stub only
    assert [r["model"] for r in STUB["requests"]] == ["inkling-2-mini"] and "APPROVED-CANARY" in json.dumps(STUB["requests"][0])
    raw, headers = STUB["raw"][0]
    assert headers["Authorization"] == "Bearer " + KEY  # the managed key, to the managed origin
    assert OVERRIDE == []  # and not one request, with or without a credential, reached the caller's server
    blob = raw + b"".join(json.dumps(h).encode() for _, h in STUB["raw"]) + b"".join(json.dumps(h).encode() for _, h in STUB.get("gets", []))
    for secret in (b"caller-key-1", b"caller-oauth-token", b"caller-anthropic-token", b"UNTRUSTED-CANARY"):
        assert secret not in blob


def test_pm_route_with_caller_overrides_uses_the_managed_claude_with_a_fixed_environment(env, monkeypatch, tmp_path_factory):
    untrusted = tmp_path_factory.mktemp("untrusted")
    set_caller_overrides(env, monkeypatch, untrusted)
    evil = untrusted / "evil-claude"
    make_fake_claude(evil)
    monkeypatch.setenv("BULK_READ_CLAUDE_BIN", str(evil))
    path = make(env["dir"] / "a.ts", 5)
    assert bulk_read.main(["--paths", path, "--question", "q"]) == 0
    assert not (evil.parent / "evil-claude.call.json").exists()  # the caller's binary never ran
    call = claude_call(env)  # the managed one did
    assert set(call["env"]) - {"__CF_USER_TEXT_ENCODING"} == {"HOME", "USER", "LOGNAME", "PATH", "LANG", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"}  # macOS adds the CF key
    assert call["env"]["HOME"] == lib.HOME_DIR and call["env"]["PATH"] == lib.CLAUDE_PATH
    blob = json.dumps(call["env"])
    assert "CLAUDE_CONFIG_DIR" not in call["env"] and OVERRIDE == [] and "requests" not in STUB
    assert not any(s in blob for s in ("caller-oauth-token", "caller-anthropic-token", "evil-config", "evil-claude", "caller-key-1"))


def test_managed_claude_binary_must_be_a_safe_fixed_path(env):
    assert lib.claude_bin() == str(env["claude"])
    env["claude"].chmod(0o775)  # group writable
    assert lib.claude_bin() == ""
    env["claude"].chmod(0o755)
    assert lib.claude_bin() == str(env["claude"])
    env["claude"].chmod(0o644)  # not executable
    assert lib.claude_bin() == ""


def test_home_comes_from_the_account_not_the_environment():
    saved = os.environ.get("HOME")
    os.environ["HOME"] = "/tmp/elsewhere"
    try:
        assert lib._expand("~/x") == lib.HOME_DIR + "/x" and "/tmp/elsewhere" not in lib.HOME_DIR
    finally:
        if saved is not None:
            os.environ["HOME"] = saved


def test_egress_is_pinned_to_the_managed_origin_and_the_approved_aliases(env, monkeypatch):
    sent = []

    def spy_open(request, timeout=None):
        sent.append((request.full_url, request.data))
        raise OSError("blocked by test")

    monkeypatch.setattr(lib.OPENER, "open", spy_open)
    for url in ("http://example.com:8317", "https://127.0.0.1:8317", "http://127.0.0.1.evil.example", "http://user:pw@127.0.0.1:8317",
                "http://192.168.68.113:8317", "ftp://127.0.0.1"):
        monkeypatch.setattr(lib, "ROUTER_ORIGIN", url)
        with pytest.raises(lib.InkError) as excinfo:
            lib.chat("inkling-2-mini", "s", "u", 10, 1)
        assert excinfo.value.reason == "egress_denied", url
    monkeypatch.setattr(lib, "ROUTER_ORIGIN", env["base"])
    for alias in ("evil-model", "gpt-6.2-sol", "claude-opus-5-5", ""):
        with pytest.raises(lib.InkError) as excinfo:
            lib.chat(alias, "s", "u", 10, 1)
        assert excinfo.value.reason == "alias_denied", alias
    assert sent == []  # nothing was built or sent
    for url in ("http://example.com/v1/models", "https://192.168.68.113/v1/models"):
        monkeypatch.setattr(lib, "INKLING_HEALTH_URL", url)
        with pytest.raises(lib.InkError):
            lib.health_url()
    assert lib.ROUTER_ORIGIN == env["base"]
    assert DEFAULT_HEALTH_URL.startswith("http://192.168.68.113:18120/")


def test_transport_ignores_env_proxies_and_never_follows_a_redirect(env, monkeypatch, capsys):
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in lib.OPENER.handlers)  # build_opener dropped the default one
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # a dead proxy: only a transport that honors it would fail
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    lane(monkeypatch)
    path = make(env["dir"] / "ok.ts", 5)
    assert bulk_read.main(["--paths", path, "--question", "q"]) == 0 and len(STUB["requests"]) == 1  # reached the stub directly
    STUB.clear()
    path = make(env["dir"] / "a.ts", 5)
    STUB["models"] = {"inkling-2-mini": {"redirect": True}, "gpt-6-luna": {"redirect": True}}
    assert bulk_read.main(["--paths", path, "--question", "q"]) == 3
    assert "inkling_http_307,luna_http_307" in capsys.readouterr().err
    assert [r["model"] for r in STUB["requests"]] == ["inkling-2-mini", "gpt-6-luna"]  # one POST each, no follow-up request


# ---- code-write destination binding ----------------------------------------------------------------------------------

def swapping_complete(monkeypatch, env, out, other):
    """The path is re-pointed AFTER validation and pinning, while the model call is in flight."""
    real_complete = lib.complete

    def swap(*args, **kwargs):
        answer = real_complete(*args, **kwargs)
        os.rename(out, env["dir"] / "out-moved")
        os.symlink(other, out)
        return answer

    monkeypatch.setattr(lib, "complete", swap)


def test_destination_swapped_after_validation_leaves_the_other_file_untouched(env, monkeypatch):
    lane(monkeypatch)
    ref = env["dir"] / "ref.py"
    put(ref, "x\n")
    out, other = env["dir"] / "out", env["dir"] / "other"
    out.mkdir()
    other.mkdir()
    (other / "new.py").write_text("OTHER-CONTENT\n")
    STUB["content"] = "print('new')"
    swapping_complete(monkeypatch, env, out, other)
    assert code_write.main(["spec", str(ref), str(out / "new.py")]) == 0
    assert (other / "new.py").read_text() == "OTHER-CONTENT\n"  # the other file is untouched
    assert (env["dir"] / "out-moved" / "new.py").read_text() == "print('new')\n"  # the write went to the pinned directory
    assert sorted(p.name for p in other.iterdir()) == ["new.py"]  # no stray temp file there either
    assert sorted(p.name for p in (env["dir"] / "out-moved").iterdir()) == ["new.py"]


def test_destination_swap_with_force_replaces_only_the_pinned_directorys_file(env, monkeypatch):
    lane(monkeypatch)
    ref = env["dir"] / "ref.py"
    put(ref, "x\n")
    out, other = env["dir"] / "out", env["dir"] / "other"
    out.mkdir()
    other.mkdir()
    (out / "new.py").write_text("OLD\n")
    (other / "new.py").write_text("OTHER-CONTENT\n")
    STUB["content"] = "print('forced')"
    swapping_complete(monkeypatch, env, out, other)
    assert code_write.main(["--force", "spec", str(ref), str(out / "new.py")]) == 0
    assert (other / "new.py").read_text() == "OTHER-CONTENT\n"
    assert (env["dir"] / "out-moved" / "new.py").read_text() == "print('forced')\n"


def test_create_only_never_overwrites_a_file_that_appears_after_the_check(env, monkeypatch, capsys):
    lane(monkeypatch)
    ref = env["dir"] / "ref.py"
    put(ref, "x\n")
    out = env["dir"] / "out"
    out.mkdir()
    real_complete = lib.complete

    def race(*args, **kwargs):
        answer = real_complete(*args, **kwargs)
        (out / "new.py").write_text("RACE-WINNER\n")  # someone creates the target after the existence check
        return answer

    monkeypatch.setattr(lib, "complete", race)
    STUB["content"] = "print('late')"
    assert code_write.main(["spec", str(ref), str(out / "new.py")]) == 2
    assert (out / "new.py").read_text() == "RACE-WINNER\n"  # create-only: the existing file is never touched
    assert "target_exists" in capsys.readouterr().err
    assert sorted(p.name for p in out.iterdir()) == ["new.py"]  # the exclusive temp file is gone


def test_a_symlink_at_the_target_name_is_replaced_not_followed(env, monkeypatch):
    lane(monkeypatch)
    ref = env["dir"] / "ref.py"
    put(ref, "x\n")
    victim = env["dir"] / "victim.txt"
    victim.write_text("VICTIM\n")
    (env["dir"] / "link.py").symlink_to(victim)
    STUB["content"] = "print('forced')"
    assert code_write.main(["spec", str(ref), str(env["dir"] / "link.py")]) == 2  # create-only: a symlink counts as existing
    assert code_write.main(["--force", "spec", str(ref), str(env["dir"] / "link.py")]) == 0
    assert victim.read_text() == "VICTIM\n" and not (env["dir"] / "link.py").is_symlink()


# ---- resource bound, disabled switch, hard deadline --------------------------------------------------------------------

def test_oversized_input_is_refused_before_any_read(env, monkeypatch):
    lane(monkeypatch)
    huge = env["dir"] / "huge.ts"
    put(huge, "x" * 300000)
    spy = Spy(monkeypatch)
    assert bulk_read.main(["--paths", str(huge), "--question", "q"]) == 4
    assert not spy.touched(huge) and spy.read_bytes == 0 and "requests" not in STUB


def test_oversized_input_stops_reading_at_the_cap_even_if_its_size_changes_after_the_check(env, monkeypatch):
    path = env["dir"] / "grows.ts"
    put(path, "y" * 50000)
    ((real, meta),) = lib.approve_inputs([str(path)], 100000)
    spy = Spy(monkeypatch)
    real_fstat = os.fstat

    class Lie:  # the file is reported small, as if it grew after approval
        def __init__(self, st):
            self.st_mode, self.st_dev, self.st_ino, self.st_nlink, self.st_size = st.st_mode, st.st_dev, st.st_ino, st.st_nlink, 0

    monkeypatch.setattr(os, "fstat", lambda fd: Lie(real_fstat(fd)))
    with pytest.raises(lib.Refusal) as excinfo:
        lib.read_approved(real, meta, 1000)
    assert excinfo.value.reason == "too_large" and spy.read_bytes <= 1001  # streamed under the cap, never the whole file


@pytest.mark.parametrize("how", ["marker", "env_off"])
def test_disabled_refuses_at_entry_with_zero_bytes_and_no_file_opened(env, monkeypatch, capsys, how):
    """CTO add-on ts 1795291271.444299: bulk-read and code-write check the DISABLED marker and MOP_BULK_READ_GUARD=0 themselves."""
    lane(monkeypatch)
    src, ref, target = env["dir"] / "src.ts", env["dir"] / "ref.py", env["dir"] / "out.py"
    put(src, "export const x = 1;\n")
    put(ref, "x\n")
    if how == "marker":
        env["state"].mkdir(parents=True)
        (env["state"] / "DISABLED").write_text("")
        monkeypatch.setenv("MOP_BULK_READ_STATE_DIR", str(env["tmp"] / "elsewhere"))  # a caller cannot hide the marker
    else:
        monkeypatch.setenv("MOP_BULK_READ_GUARD", "0")
    spy = Spy(monkeypatch)
    assert bulk_read.main(["--paths", str(src), "--question", "q"]) == 7
    assert code_write.main(["spec", str(ref), str(target)]) == 7
    err = capsys.readouterr().err
    assert "bulk-read: disabled" in err and "code-write: disabled" in err
    assert "requests" not in STUB and "gets" not in STUB and OVERRIDE == [] and not target.exists()
    assert not any(spy.touched(p) for p in (src, ref, target)) and spy.read_bytes == 0  # no file opened, nothing read
    assert not (env["state"] / "events.jsonl").exists()


def test_guard_honors_the_marker_even_when_a_caller_names_another_state_dir(env):
    big = make(env["dir"] / "big.ts", 900)
    assert run_guard(env, read(env, big), **LANE) is not None
    env["state"].mkdir(parents=True, exist_ok=True)
    (env["state"] / "DISABLED").write_text("")
    assert run_guard(env, read(env, big), MOP_BULK_READ_STATE_DIR=str(env["tmp"] / "elsewhere"), **LANE) is None
    assert run_guard(env, read(env, big), MOP_BULK_READ_STATE_DIR=str(env["tmp"] / "elsewhere")) is None


def test_one_hard_deadline_covers_reading_and_the_model_call(env, monkeypatch, capsys):
    lane(monkeypatch)
    monkeypatch.setenv("BULK_READ_TIMEOUT_S", "0.5")
    path = make(env["dir"] / "a.ts", 5)
    monkeypatch.setattr(lib, "approve_inputs", lambda paths, cap: time.sleep(10))  # a stalled read: slow disk, FIFO, hung mount
    started = time.time()
    assert bulk_read.main(["--paths", path, "--question", "q"]) == 3
    assert time.time() - started < 4 and "hard_deadline" in capsys.readouterr().err
    assert "requests" not in STUB and events(env)[-1]["event"] == "delegate_fail_open"


def test_code_write_hard_deadline_closes_the_pinned_directory(env, monkeypatch, capsys):
    lane(monkeypatch)
    monkeypatch.setenv("CODE_WRITE_TIMEOUT_S", "0.5")
    ref = env["dir"] / "ref.py"
    put(ref, "x\n")
    monkeypatch.setattr(lib, "complete", lambda *a, **k: time.sleep(10))
    closed = []
    real_close = lib.Destination.close
    monkeypatch.setattr(lib.Destination, "close", lambda self: (closed.append(self.fd), real_close(self))[1])
    started = time.time()
    assert code_write.main(["spec", str(ref), str(env["dir"] / "out" / "new.py")]) == 3
    assert time.time() - started < 4 and "hard deadline" in capsys.readouterr().err and len(closed) == 1
    assert not (env["dir"] / "out" / "new.py").exists()


# ---- CTO REVISE on 4184d7c0a643 (ts 1795293836.257189): the real launch, and SOURCE files rather than writable worktrees -----------------

LAUNCHERS = {"bulk-read": "claude", "bulk-read-lane": "lane", "code-write": "claude", "code-write-lane": "lane"}
SHEBANG = "#!" + DEFAULT_MANAGED_PYTHON + " -IS"
needs_managed_python = pytest.mark.skipif(not os.path.exists(DEFAULT_MANAGED_PYTHON), reason="the fixed Apple python3 is not installed")


def real_worktree(env, name):
    """A REAL worktree outside every approved root (like a /tmp lane worktree), registered by git itself in the checkout's own .git."""
    top = env["tmp"] / "lanes" / name
    git(env["dir"], "worktree", "add", "-q", "--detach", str(top), "HEAD")
    return top


def test_launchers_pin_the_interpreter_scrub_the_environment_and_bind_the_route():
    assert DEFAULT_MANAGED_PYTHON == "/Library/Developer/CommandLineTools/usr/bin/python3"
    for name, route in LAUNCHERS.items():
        text = (SCRIPTS / name).read_text()
        assert text.splitlines()[0] == SHEBANG, name  # the real Apple binary with -IS: isolated, no site; not /usr/bin/env, not the xcrun shim
        assert os.access(SCRIPTS / name, os.X_OK), name
        assert 'lib.BOUND_ROUTE = "%s"' % route in text and "lib.scrub_environment()" in text, name
        assert text.index("lib.scrub_environment()") < text.index("lib.BOUND_ROUTE") < text.index("import bulk_read_cli" if "bulk" in name else "import code_write_cli")
    for name in ("bulk_read_cli.py", "code_write_cli.py"):  # the logic modules are not entrypoints
        text = (SCRIPTS / name).read_text()
        assert not text.startswith("#!") and "__main__" not in text and not os.access(SCRIPTS / name, os.X_OK), name


def test_route_is_bound_by_the_launcher_and_no_environment_variable_can_change_it(env, monkeypatch, capsys):
    path = make(env["dir"] / "a.ts", 5)
    lane(monkeypatch)
    for value in (None, "", "0", "1"):  # the lane launcher means the router whatever PROXY_AGENT_RUN says
        monkeypatch.delenv("PROXY_AGENT_RUN", raising=False)
        if value is not None:
            monkeypatch.setenv("PROXY_AGENT_RUN", value)
        STUB.pop("requests", None)
        assert bulk_read.main(["--paths", path, "--question", "q"]) == 0 and lib.route() == "lane"
        assert [r["model"] for r in STUB["requests"]] == ["inkling-2-mini"]
    assert not (env["claude"].parent / "claude.call.json").exists()  # direct Claude was never selected
    monkeypatch.setattr(lib, "BOUND_ROUTE", "claude")  # the PM launcher inside a proxy lane session refuses
    monkeypatch.setenv("PROXY_AGENT_RUN", "1")
    STUB.clear()
    assert bulk_read.main(["--paths", path, "--question", "q"]) == 8 and code_write.main(["spec", path, str(env["dir"] / "o.py")]) == 8
    assert "requests" not in STUB and "bulk-read: a proxy lane uses" in capsys.readouterr().err
    monkeypatch.setattr(lib, "BOUND_ROUTE", None)  # run without a launcher: refuses, whatever the environment says
    assert bulk_read.main(["--paths", path, "--question", "q"]) == 8 and code_write.main(["spec", path, str(env["dir"] / "o.py")]) == 8
    assert "requests" not in STUB and not (env["dir"] / "o.py").exists()


def test_untracked_artifacts_and_staged_only_files_in_a_real_registered_worktree_are_not_source(env, monkeypatch):
    lane(monkeypatch)
    top = real_worktree(env, "lane-wt")
    assert lib.registered_worktrees() and os.path.realpath(top) in [root.path for root in lib.registered_worktrees()]
    tracked = top / "app.ts"
    assert lib.membership_refusal(os.path.realpath(tracked)) is None  # tracked in the repo's index and HEAD
    tracked.write_text("export const edited = true;\n")
    assert lib.membership_refusal(os.path.realpath(tracked)) is None  # a work-in-progress edit of tracked source is still source
    artifact = top / "notes.ts"  # an ordinary name a lane wrote
    artifact.write_text("LOCAL-ARTIFACT-CANARY\n")
    staged = top / "staged.ts"
    staged.write_text("STAGED-CANARY\n")
    git(top, "add", "staged.ts")  # staged by a lane, never committed
    assert lib.membership_refusal(os.path.realpath(artifact)) == "untracked"
    assert lib.membership_refusal(os.path.realpath(staged)) == "not_at_head"
    spy = Spy(monkeypatch)
    for victim in (artifact, staged):
        assert bulk_read.main(["--paths", str(tracked), str(victim), "--question", "q"]) == 6
    assert "requests" not in STUB and not any(spy.touched(p) for p in (artifact, staged, tracked))  # zero bytes, nothing opened
    assert bulk_read.main(["--paths", str(tracked), "--question", "q"]) == 0 and "EDITED-OK" not in json.dumps(STUB["requests"])


def test_hardlink_aliases_are_not_source_even_when_tracked_with_an_ordinary_name(env, monkeypatch, tmp_path_factory):
    lane(monkeypatch)
    restricted = env["dir"] / ".env.local"
    restricted.write_text("RESTRICTED-CANARY\n")
    alias = env["dir"] / "notes.ts"
    os.link(restricted, alias)  # ordinary name, same inode as restricted data
    track(alias)
    outside = tmp_path_factory.mktemp("outside") / "leak.txt"
    outside.write_text("OUTSIDE-CANARY\n")
    alias2 = env["dir"] / "alias2.ts"
    os.link(outside, alias2)
    track(alias2)
    assert os.stat(alias).st_nlink == 2
    for name in (alias, alias2):
        assert lib.membership_refusal(os.path.realpath(name)) == "hardlink_alias"
        assert bulk_read.main(["--paths", str(name), "--question", "q"]) == 6
    assert "requests" not in STUB
    late = make(env["dir"] / "late.ts", 5)  # approved with one link, aliased before the read: the descriptor check refuses it
    ((real, meta),) = lib.approve_inputs([late], 100000)
    os.link(real, env["dir"] / "late-alias.ts")
    with pytest.raises(lib.Refusal) as excinfo:
        lib.read_approved(real, meta, 100000)
    assert excinfo.value.reason == "hardlink_alias"


def test_restricted_classes_apply_to_the_full_resolved_path(env):
    for relative in ("evidence/x.ts", "customer-artifacts/x.ts", "sub/secrets/x.ts", ".ssh/x.ts"):
        path = env["dir"] / relative
        put(path, "x\n")
        assert lib.input_refusal(os.path.realpath(path)) == "restricted_data_class", relative
    top = real_worktree(env, "hd-evidence-run")  # an evidence-named worktree: the name is part of the resolved path
    assert lib.input_refusal(os.path.realpath(top / "app.ts")) == "restricted_data_class"


def test_release_files_are_source_only_when_the_release_manifest_lists_them(env):
    release = env["home"] / ".local/share/master-of-panes/releases/abc123"
    for relative in ("src/a.ts", "src/b.ts", "node_modules/m/index.js", "dist/a.js"):
        (release / relative).parent.mkdir(parents=True, exist_ok=True)
        (release / relative).write_text("x\n")
    (release / "RELEASE_MANIFEST.json").write_text(json.dumps({"files": [{"kind": "file", "path": p} for p in ("src/a.ts", "node_modules/m/index.js", "dist/a.js")]}))
    assert lib.membership_refusal(os.path.realpath(release / "src/a.ts")) is None
    assert lib.membership_refusal(os.path.realpath(release / "src/b.ts")) == "not_source"  # not in the manifest
    assert lib.membership_refusal(os.path.realpath(release / "node_modules/m/index.js")) == "not_source"
    assert lib.membership_refusal(os.path.realpath(release / "dist/a.js")) == "not_source"


def test_guard_leaves_untracked_big_files_and_hardlink_aliases_to_the_native_read(env):
    tracked = make(env["dir"] / "big.ts", 900)
    artifact = env["dir"] / "artifact.txt"  # an ordinary name, not source
    artifact.write_text("".join("line %d\n" % n for n in range(900)))
    alias = env["dir"] / "alias.ts"
    os.link(tracked, alias)
    track(alias)
    assert run_guard(env, read(env, tracked), **LANE) is None  # its inode now has two names, so it is an alias too: native
    solo = make(env["dir"] / "solo.ts", 900)
    assert run_guard(env, read(env, solo), **LANE) is not None  # tracked source: still redirected
    assert run_guard(env, read(env, str(artifact)), **LANE) is None  # untracked: native
    assert run_guard(env, read(env, str(alias)), **LANE) is None  # hardlink alias: native
    assert run_guard(env, read(env, str(artifact))) is None and run_guard(env, bash(env, "cat " + str(artifact))) is None  # the PM session too


def test_guard_fails_open_when_the_launcher_interpreter_is_missing(env):
    big = make(env["dir"] / "big.ts", 900)
    assert run_guard(env, read(env, big), cfg={"MANAGED_PYTHON": "/nonexistent/python3"}, **LANE) is None
    assert events(env)[-1]["event"] == "fail_open" and events(env)[-1]["reason"] == "launcher_missing"


def hostile_startup(root):
    """Everything a caller could plant to change the interpreter or run code before the guard: each one drops a canary when it runs."""
    root.mkdir(parents=True, exist_ok=True)
    canaries = {}
    for name in ("sitecustomize", "usercustomize", "bulk_read_lib", "startup"):
        canaries[name] = root / ("canary-" + name)
        (root / (name + ".py")).write_text("open(%r, 'w').write('ran')\n" % str(canaries[name]))
    for name in ("python3", "python", "git"):
        canaries[name] = root / ("canary-bin-" + name)
        (root / name).write_text("#!/bin/sh\necho ran > %s\n" % canaries[name])
        (root / name).chmod(0o755)
    environment = {"PYTHONPATH": str(root), "PYTHONSTARTUP": str(root / "startup.py"), "PYTHONHOME": "/nonexistent/python-home",
                   "PYTHONUSERBASE": str(root), "PYTHONPYCACHEPREFIX": str(root / "pyc"), "PYTHONSAFEPATH": "", "PYTHONINSPECT": "1",
                   "PATH": str(root) + ":/usr/bin:/bin", "PROXY_AGENT_RUN": "", "BULK_READ_CLAUDE_MODEL": "opus", "CLAUDE_CONFIG_DIR": str(root),
                   "HOME": str(root), "TMPDIR": str(root)}
    return environment, canaries


@needs_managed_python
def test_real_launcher_under_hostile_startup_settings(env, tmp_path_factory):
    """THE launcher proof. The real bulk-read-lane launcher, run by the fixed Apple python3 -IS, with hostile startup settings:
    an approved TRACKED source reaches the managed stub (as the router route, whatever PROXY_AGENT_RUN says); an untracked artifact in a
    real registered worktree and a restricted hardlink alias send zero bytes; none of the planted startup code or binaries ever runs."""
    hostile, canaries = hostile_startup(tmp_path_factory.mktemp("hostile"))
    cfg = {"MANAGED_PYTHON": DEFAULT_MANAGED_PYTHON}
    python = [DEFAULT_MANAGED_PYTHON, "-IS"]
    approved = env["dir"] / "src.ts"
    put(approved, "export const answer = 'APPROVED-CANARY';\n" * 3)
    top = real_worktree(env, "lane-launch")
    artifact = top / "notes.ts"
    artifact.write_text("ARTIFACT-CANARY\n")
    restricted = env["dir"] / ".env.local"
    restricted.write_text("RESTRICTED-CANARY\n")
    alias = env["dir"] / "alias.ts"
    os.link(restricted, alias)
    track(alias)
    ok = run_entry(env, "bulk-read-lane", ["--paths", str(approved), "--question", "q"], cfg=cfg, python=python, **hostile)
    assert ok.returncode == 0, ok.stderr
    assert [r["model"] for r in STUB["requests"]] == ["inkling-2-mini"] and "APPROVED-CANARY" in json.dumps(STUB["requests"][0])
    assert STUB["raw"][0][1]["Authorization"] == "Bearer " + KEY and not (env["claude"].parent / "claude.call.json").exists()
    for victim in (artifact, alias, restricted):
        refused = run_entry(env, "bulk-read-lane", ["--paths", str(victim), "--question", "q"], cfg=cfg, python=python, **hostile)
        assert refused.returncode == 6, (victim, refused.stderr)
    assert len(STUB["requests"]) == 1 and OVERRIDE == []  # zero bytes for the three
    blob = json.dumps(STUB["requests"]) + json.dumps(STUB["raw"][0][1])
    assert "ARTIFACT-CANARY" not in blob and "RESTRICTED-CANARY" not in blob
    assert not [name for name, marker in canaries.items() if marker.exists()]  # no planted code, binary or startup file ran
    pm = run_entry(env, "bulk-read", ["--paths", str(approved), "--question", "q"], cfg=cfg, python=python, **hostile)  # the PM launcher, router never
    assert pm.returncode == 0 and len(STUB["requests"]) == 1 and (env["claude"].parent / "claude.call.json").exists()


@needs_managed_python
@pytest.mark.parametrize("launcher", ["bulk-read-lane", "bulk-read", "code-write-lane", "code-write"])
def test_launcher_file_starts_under_its_own_shebang_with_hostile_startup_settings(tmp_path_factory, launcher):
    """The kernel's exec of the real file, no harness: a non-isolated interpreter would die on the bogus PYTHONHOME. Real constants, but the
    input is outside every approved root (or the real DISABLED marker answers first), so nothing can reach a transport either way."""
    hostile, canaries = hostile_startup(tmp_path_factory.mktemp("hostile"))
    args = ["--paths", "/etc/hosts", "--question", "q"] if launcher.startswith("bulk") else ["spec", "/etc/hosts", "/nonexistent-dir/out.py"]
    done = subprocess.run([str(SCRIPTS / launcher), *args], capture_output=True, text=True, env={**hostile}, timeout=60)
    assert done.returncode in (6, 7, 8), (done.returncode, done.stderr)  # 7 while the DISABLED marker exists, 6 outside the roots, 8 a lane refuses
    assert launcher.split("-")[0] in done.stderr and "Traceback" not in done.stderr and "Fatal Python error" not in done.stderr
    assert not [name for name, marker in canaries.items() if marker.exists()]


# ---- wiring: proxy lanes, the PM checkout settings, links and proxy-agent.sh ----------------------------------------

def lane_settings(tmp_path):
    """Run the real settings builder (the SAFETY_SETTINGS heredoc in proxy-agent.sh) and return the JSON a lane would load."""
    code = re.search(r"<<'PY'\n(import json, sys\nsrc, out = .*?)\nPY\n", PROXY_AGENT, re.S).group(1)
    out = tmp_path / "lane-settings.json"
    done = subprocess.run([sys.executable, "-", "/nonexistent/settings.json", str(out)], input=code, capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "PROXY_AGENT_TYPE_FOR_SANDBOX": "hd-builder"})
    assert done.returncode == 0, done.stderr
    return json.loads(out.read_text())


def test_proxy_lanes_load_the_guard_for_read_and_bash_and_bulk_read_leaves_the_fence(tmp_path):
    settings = lane_settings(tmp_path)
    guards = [g for g in settings["hooks"]["PreToolUse"] if "bulk_read_guard.py" in json.dumps(g)]
    assert sorted(g["matcher"] for g in guards) == ["Bash", "Read"]
    assert all(g["hooks"][0]["timeout"] == 8 for g in guards)
    assert any("block-force-push-rebase" in json.dumps(g) for g in settings["hooks"]["PreToolUse"])  # existing hooks kept
    excluded = settings["sandbox"]["excludedCommands"]
    lane_launcher = "/Users/rajiv/.local/share/master-of-panes/current/scripts/bulk-read-lane *"
    assert lane_launcher in excluded  # the one absolute launcher, never the bare command or a link
    assert not any(e.startswith("bulk-read") or "/.local/bin/" in e for e in excluded) and not any("code-write" in e for e in excluded)


def test_lane_hook_command_exits_zero_when_the_script_is_missing_and_runs_when_present(tmp_path):
    command = lane_settings(tmp_path)["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
    assert "/current/scripts/bulk_read_guard.py" in command
    missing = command.replace("/Users/rajiv/.local/share/master-of-panes/current/scripts", str(tmp_path / "absent"))
    gone = subprocess.run(missing, shell=True, input="{}", capture_output=True, text=True)
    assert gone.returncode == 0 and gone.stdout == ""  # an exit 2 here would block every Read and Bash in the lane
    present = command.replace("/Users/rajiv/.local/share/master-of-panes/current/scripts", str(SCRIPTS))
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/nonexistent"}})
    assert subprocess.run(present, shell=True, input=payload, capture_output=True, text=True).returncode == 0


def relay_install(path, scripts="/x/master-of-panes/current/scripts"):
    return subprocess.run([sys.executable, str(SCRIPTS / "install-relay-hooks.py"), str(path), "--scripts-dir", scripts],
                          check=True, capture_output=True, text=True)


def test_pm_checkout_settings_get_the_guard_beside_the_relay_and_user_prompt_submit_is_untouched(tmp_path):
    pm = tmp_path / "heydonna-app" / ".claude"
    pm.mkdir(parents=True)
    settings = pm / "settings.json"
    original = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "safety.sh"}]}],
                          "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "audit.sh"}]}]}}
    settings.write_text(json.dumps(original))
    relay_install(settings)
    first = json.loads(settings.read_text())
    guards = [g for g in first["hooks"]["PreToolUse"] if "bulk_read_guard.py" in json.dumps(g)]
    assert sorted(g["matcher"] for g in guards) == ["Bash", "Read"] and all(g["hooks"][0]["timeout"] == 8 for g in guards)
    assert first["hooks"]["PreToolUse"][0] == original["hooks"]["PreToolUse"][0]
    relay_install(settings)
    assert json.loads(settings.read_text()) == first  # idempotent: no duplicate guard groups
    assert not any("bulk_read_guard" in json.dumps(g) for g in first["hooks"]["UserPromptSubmit"])  # rule 50: that chain never carries the guard


def test_dev_slot_settings_get_no_guard(tmp_path):
    slot = tmp_path / "heydonna-app-3002" / ".claude"
    slot.mkdir(parents=True)
    settings = slot / "settings.json"
    settings.write_text(json.dumps({"env": {"HEYDONNA_SLOT_STOP_GATED": "1"}}))
    relay_install(settings)
    assert "bulk_read_guard" not in settings.read_text()


def test_link_script_links_both_commands_through_the_given_scripts_dir(tmp_path):
    done = subprocess.run(["bash", str(SCRIPTS / "install-bulk-read-links.sh"), str(SCRIPTS), str(tmp_path / "bin")], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    for name in ("bulk-read", "code-write"):
        assert (tmp_path / "bin" / name).resolve() == (SCRIPTS / name).resolve()


def test_proxy_agent_exports_lane_type_and_exemption_for_judgment_lanes():
    function = re.search(r"proxy_agent_is_judgment_lane\(\) \{.*?\n\}\n", PROXY_AGENT, re.S).group(0)

    def judged(kind, override=""):
        done = subprocess.run(["bash", "-c", function + "\nproxy_agent_is_judgment_lane && echo yes || echo no"], capture_output=True,
                              text=True, env={"PATH": os.environ["PATH"], "TYPE": kind, "PROXY_AGENT_CAP_OVERRIDE": override})
        return done.stdout.strip()

    assert judged("hd-planner") == "yes" and judged("hd-builder", "P0:7") == "yes"
    assert judged("hd-builder") == "no" and judged("hd-builder", "rajiv:1.2") == "no" and judged("") == "no"
    assert 'export PROXY_AGENT_TYPE="$TYPE"' in PROXY_AGENT
    assert "if proxy_agent_is_judgment_lane; then export BULK_READ_EXEMPT=1; fi" in PROXY_AGENT
    assert "BULK_READ_*|MOP_BULK_READ_*" in PROXY_AGENT  # the wrapper's env transfer carries both
    assert "\n  if proxy_agent_is_judgment_lane; then\n" in PROXY_AGENT  # Inkling routing and the guard share the helper
