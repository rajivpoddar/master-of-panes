"""MoP bulk-read guard, bulk-read, code-write and their wiring (added to the repo; MoP changes need no local proof).

Rajiv D0AMF0XE6TS thread 1795287257.526309, ts 1795287629.063499; PM amendments 22 Nov 00:42, 00:54 and 01:00 IST (Rajiv ts 1795289208.297109,
1795289295.887739): proxy lanes load the guard; judgment lanes are exempt; the PM session is blocked too, with an escape hatch and fail-open;
lanes answer with Inkling, then luna; the PM session answers with Haiku or Sonnet on direct Claude. Every test drives the real script or module
against a local stub of the router and a fake claude binary; nothing here calls a live model.
"""
import importlib.machinery
import importlib.util
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

PROXY_AGENT = (Path(__file__).parents[1] / "shared-assets/claude/scripts/proxy-agent.sh").read_text()
LANE = {"PROXY_AGENT_RUN": "1", "PROXY_AGENT_TYPE": "hd-builder"}
SCRUBBED = ("PROXY_AGENT", "BULK_READ_EXEMPT", "MOP_BULK_READ_GUARD", "BULK_READ_CLAUDE_MODEL")
DEAD = "http://127.0.0.1:9"
FENCE = chr(96) * 3


def load_tool(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(SCRIPTS / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bulk_read = load_tool("bulk-read")
code_write = load_tool("code-write")

STUB = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(STUB.get("health", 200) if self.path.startswith("/health") else STUB.get("router", 200))
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        STUB.setdefault("requests", []).append(body)
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


FAKE_CLAUDE = "#!" + sys.executable + r'''
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
mode = json.load(open(os.path.join(here, "fake-claude.mode")))
open(os.path.join(here, "fake-claude.call.json"), "w").write(json.dumps(
    {"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "stdin": sys.stdin.read()}))
if mode.get("exit"):
    sys.exit(mode["exit"])
print(json.dumps({"result": mode.get("result", "- ok (a:1)"), "is_error": mode.get("is_error", False),
                  "usage": {"input_tokens": 5, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 0, "output_tokens": 7},
                  "total_cost_usd": 0.0002}))
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    STUB.clear()
    claude = tmp_path / "fake-claude"
    claude.write_text(FAKE_CLAUDE)
    claude.chmod(0o755)
    (tmp_path / "fake-claude.mode").write_text("{}")
    base = "http://127.0.0.1:%d" % server.server_port
    home = tmp_path / "home"  # a fake HOME: the real DISABLED marker and the real checkouts must not leak into these tests
    home.mkdir()
    # tmp_path is the one approved source root (test mode); everything the tests send must sit under it
    values = {"MOP_BULK_READ_STATE_DIR": str(tmp_path / "state"), "CLIPROXY_BASE_URL": base, "CLIPROXY_API_KEY": "test",
              "INKLING_HEALTH_URL": base + "/health", "BULK_READ_TIMEOUT_S": "4", "BULK_READ_INKLING_TIMEOUT_S": "1",
              "BULK_READ_CLAUDE_BIN": str(claude), "HOME": str(home), "MOP_BULK_READ_TEST_MODE": "1",
              "MOP_BULK_READ_EXTRA_ROOTS": str(tmp_path)}
    for key in list(os.environ):
        if key.startswith(SCRUBBED):
            monkeypatch.delenv(key)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    yield {"state": tmp_path / "state", "dir": tmp_path, "values": values, "claude": claude, "base": base}
    server.shutdown()


def claude_call(env):
    return json.loads((env["dir"] / "fake-claude.call.json").read_text())


def make(path, lines, terminate=True):
    path.write_text("".join("line %d\n" % n for n in range(1, lines + 1)) if terminate
                    else "\n".join("line %d" % n for n in range(1, lines + 1)))
    return str(path)


def run_guard(env, payload, **extra):
    environment = {k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED)}
    environment.update(env["values"])
    environment.update(extra)
    done = subprocess.run([sys.executable, "-I", str(SCRIPTS / "bulk_read_guard.py")], input=json.dumps(payload),
                          capture_output=True, text=True, env=environment, timeout=30)
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
    monkeypatch.setenv("PROXY_AGENT_RUN", "1")


# ---- threshold and the redirect message -------------------------------------------------------------------------

def test_routine_lane_big_read_is_blocked_with_the_exact_redirect(env):
    big = make(env["dir"] / "big.txt", 450)
    reason = run_guard(env, read(env, big), **LANE)
    assert "has 450 lines (limit 400)" in reason
    assert 'bulk-read --paths <files> --question "<q>"' in reason
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
    for helper in ("bulk_read_guard.py", "bulk_read_lib.py", "bulk-read", "code-write"):
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
    dead = {"INKLING_HEALTH_URL": DEAD + "/health", "CLIPROXY_BASE_URL": DEAD}
    assert run_guard(env, read(env, big), **dead) is not None  # the PM route has no network gate: a dead Spark and router change nothing
    assert not list((env["state"]).glob("health-*.json"))
    assert run_guard(env, read(env, big), BULK_READ_CLAUDE_BIN="/nonexistent/claude", PATH="/nonexistent") is None
    assert events(env)[-1]["event"] == "fail_open" and events(env)[-1]["reason"] == "claude_missing"


def test_inkling_health_default_matches_the_proxy_launcher_gate(monkeypatch):
    monkeypatch.delenv("INKLING_HEALTH_URL", raising=False)
    assert lib.health_url() == "http://192.168.68.113:18120/v1/models"
    assert "INKLING_HEALTH_URL:-http://192.168.68.113:18120/v1/models" in PROXY_AGENT  # one URL, two gates


def test_pm_breaker_is_separate_from_the_lane_breaker(env):
    big = make(env["dir"] / "big.txt", 900)
    for _ in range(3):
        lib.breaker_record(False, name="claude")
    assert run_guard(env, read(env, big)) is None and events(env)[-1]["reason"] == "breaker_open"
    assert run_guard(env, read(env, big), **LANE) is not None  # lanes are unaffected


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
    (env["state"]).mkdir(parents=True, exist_ok=True)
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
    assert run_guard(env, read(env, big), INKLING_HEALTH_URL=DEAD + "/health", **LANE) is not None
    assert events(env)[-1]["event"] == "block" and events(env)[-1]["backend"] == "luna"


def test_lane_fails_open_only_when_inkling_and_the_router_are_both_down(env):
    big = make(env["dir"] / "big.txt", 450)
    assert run_guard(env, read(env, big), INKLING_HEALTH_URL=DEAD + "/health", CLIPROXY_BASE_URL=DEAD, **LANE) is None
    assert events(env)[-1]["event"] == "fail_open" and events(env)[-1]["reason"].startswith("no_backend")


def test_unhealthy_statuses_fail_open_for_a_lane(env):
    STUB["health"], STUB["router"] = 503, 503
    big = make(env["dir"] / "big.txt", 450)
    assert run_guard(env, read(env, big), **LANE) is None
    assert events(env)[-1]["reason"].startswith("no_backend")


def test_hook_error_and_garbage_input_allow_the_call(env):
    environment = {**os.environ, **env["values"], **LANE}
    for stdin in ("not json at all", "[]", '{"tool_name": "Read", "tool_input": "oops"}', ""):
        done = subprocess.run([sys.executable, "-I", str(SCRIPTS / "bulk_read_guard.py")], input=stdin, capture_output=True,
                              text=True, env=environment, timeout=30)
        assert done.returncode == 0 and done.stdout.strip() == ""


def test_allowed_reads_never_touch_the_network_or_the_log(env):
    small = make(env["dir"] / "small.txt", 10)
    assert run_guard(env, read(env, small), INKLING_HEALTH_URL=DEAD + "/health", **LANE) is None
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
    monkeypatch.setenv("INKLING_HEALTH_URL", DEAD + "/health")
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
    monkeypatch.setenv("INKLING_HEALTH_URL", DEAD + "/health")
    monkeypatch.setenv("CLIPROXY_BASE_URL", DEAD)
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
    path.write_text("x" * 60 + "\n" * 1 + "".join("line %d with some padding text\n" % n for n in range(1500)))  # about 50k chars
    assert bulk_read.main(["--paths", str(path), "--question", "q"]) == 0  # the PM route takes it (cap 80000)
    lane(monkeypatch)
    assert bulk_read.main(["--paths", str(path), "--question", "q"]) == 4  # a lane refuses without waiting 30 s (cap 36000)
    assert "over the 36000 cap" in capsys.readouterr().err and "requests" not in STUB and not lib.bypass_active(str(path))


# ---- PM session backend: direct Claude Haiku or Sonnet -------------------------------------------------------------------

def test_pm_session_answers_with_haiku_on_direct_claude_and_never_touches_the_router(env, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "router-secret")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    path = make(env["dir"] / "a.txt", 5)
    (env["dir"] / "fake-claude.mode").write_text(json.dumps({"result": "Intro\n- pm fact (a.txt:2)"}))
    assert bulk_read.main(["--paths", path, "--question", "what?"]) == 0
    assert capsys.readouterr().out.strip() == "- pm fact (a.txt:2)"
    assert "requests" not in STUB  # neither Inkling nor luna
    call = claude_call(env)
    args = call["argv"]
    assert args[args.index("--model") + 1] == "haiku" and args[args.index("--setting-sources") + 1] == ""
    assert json.loads(args[args.index("--settings") + 1]) == {"disableAllHooks": True}
    assert args[args.index("--tools") + 1] == "" and "--no-session-persistence" in args and "-p" in args
    assert "    1| line 1" in call["stdin"] and "Question: what?" in call["stdin"]
    assert "ANTHROPIC_AUTH_TOKEN" not in call["env"] and "CLAUDE_CODE_SESSION_ID" not in call["env"] and "CLIPROXY_API_KEY" not in call["env"]
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
    (env["dir"] / "fake-claude.mode").write_text(json.dumps({"exit": 1}))
    assert bulk_read.main(["--paths", big, "--question", "q"]) == 3
    assert "no backend answered" in capsys.readouterr().err
    assert run_guard(env, read(env, big)) is None and events(env)[-1]["reason"] == "bypass_token"


# ---- code-write ------------------------------------------------------------------------------------------------------

def test_code_write_strips_fences_and_prints_path_and_line_count(env, monkeypatch, capsys):
    lane(monkeypatch)
    ref, target = env["dir"] / "ref.py", env["dir"] / "out" / "new.py"
    ref.write_text("print(1)\n")
    STUB["content"] = FENCE + "python\nimport os\nprint(os.name)\n" + FENCE
    assert code_write.main(["write a script", str(ref), str(target)]) == 0
    assert target.read_text() == "import os\nprint(os.name)\n"
    assert capsys.readouterr().out.strip() == "%s 2 lines" % target
    assert code_write.main(["again", str(ref), str(target)]) == 2  # refuses an existing target
    assert code_write.main(["--force", "again", str(ref), str(target)]) == 0


def test_code_write_lane_uses_luna_when_inkling_is_down_and_pm_uses_claude(env, monkeypatch):
    ref = env["dir"] / "ref.py"
    ref.write_text("x\n")
    (env["dir"] / "fake-claude.mode").write_text(json.dumps({"result": "print('pm')"}))
    assert code_write.main(["spec", str(ref), str(env["dir"] / "pm.py")]) == 0
    assert (env["dir"] / "pm.py").read_text() == "print('pm')\n" and "requests" not in STUB
    lane(monkeypatch)
    monkeypatch.setenv("INKLING_HEALTH_URL", DEAD + "/health")
    STUB["content"] = "print('luna')"
    assert code_write.main(["spec", str(ref), str(env["dir"] / "lane.py")]) == 0
    assert (env["dir"] / "lane.py").read_text() == "print('luna')\n" and [r["model"] for r in STUB["requests"]] == ["gpt-6-luna"]


def test_code_write_failures_write_nothing(env, monkeypatch):
    lane(monkeypatch)
    ref, target = env["dir"] / "ref.py", env["dir"] / "new.py"
    ref.write_text("x\n")
    STUB["content"] = "<think>nothing</think>"
    assert code_write.main(["spec", str(ref), str(target)]) == 3 and not target.exists()  # both backends answered empty
    monkeypatch.setenv("INKLING_HEALTH_URL", DEAD + "/health")
    monkeypatch.setenv("CLIPROXY_BASE_URL", DEAD)
    for cached in env["state"].glob("health-*.json"):
        cached.unlink()
    assert code_write.main(["spec", str(ref), str(target)]) == 3 and not target.exists()


# ---- CTO BLOCK on 38bd1682: input boundary, egress, resource bound, disabled switch, hard deadline -------------------------

class Spy:
    """Records every path opened through os.open or builtins.open, and the bytes read through os.read."""

    def __init__(self, monkeypatch):
        self.opened, self.read_bytes = [], 0
        real_os_open, real_open, real_read = os.open, builtins.open, os.read

        def os_open(path, *args, **kwargs):
            self.opened.append(os.fspath(path))
            return real_os_open(path, *args, **kwargs)

        def b_open(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)):
                self.opened.append(os.fspath(file))
            return real_open(file, *args, **kwargs)

        def os_read(fd, n):
            data = real_read(fd, n)
            self.read_bytes += len(data)
            return data

        monkeypatch.setattr(os, "open", os_open)
        monkeypatch.setattr(builtins, "open", b_open)
        monkeypatch.setattr(os, "read", os_read)

    def touched(self, path):
        real = os.path.realpath(path)
        return any(os.path.realpath(p) == real for p in self.opened)


def test_input_boundary_synthetic_proof(env, monkeypatch, tmp_path_factory):
    """CTO proof: an approved source file reaches an approved backend; a restricted file and a resolved-outside-root symlink send zero
    bytes (asserted at the transport stub) and are never opened; an oversized input refuses before it is read."""
    lane(monkeypatch)
    root = env["dir"]
    approved = root / "src.ts"
    approved.write_text("export const answer = 'APPROVED-CANARY';\n" * 5)
    restricted = root / ".env.local"  # inside the approved root, restricted class
    restricted.write_text("TOKEN=RESTRICTED-CANARY\n")
    leak = tmp_path_factory.mktemp("outside") / "leak.txt"
    leak.write_text("OUTSIDE-CANARY\n")
    link = root / "notes.txt"  # looks like source, resolves outside the root
    link.symlink_to(leak)
    huge = root / "huge.ts"
    huge.write_text("x" * 300000)  # well over the lane cap
    assert bulk_read.main(["--paths", str(approved), "--question", "q"]) == 0
    assert len(STUB["requests"]) == 1 and "APPROVED-CANARY" in json.dumps(STUB["requests"][0])
    assert [r["model"] for r in STUB["requests"]] == ["inkling-2-mini"]
    spy = Spy(monkeypatch)
    for victim, code in ((restricted, 6), (link, 6), (huge, 4)):
        assert bulk_read.main(["--paths", str(approved), str(victim), "--question", "q"]) == code  # one bad input refuses the whole call
        assert len(STUB["requests"]) == 1, victim  # zero bytes at the transport stub
    assert not any(spy.touched(p) for p in (restricted, link, leak, huge, approved))  # nothing was even opened
    assert spy.read_bytes == 0
    blob = json.dumps(STUB["requests"])
    assert "RESTRICTED-CANARY" not in blob and "OUTSIDE-CANARY" not in blob


def test_oversized_input_stops_reading_at_the_cap_even_if_its_size_changes_after_the_check(env, monkeypatch):
    path = env["dir"] / "grows.ts"
    path.write_text("y" * 50000)
    ((real, meta),) = lib.approve_inputs([str(path)], 100000)
    spy = Spy(monkeypatch)
    real_fstat = os.fstat

    class Lie:  # the file is reported small, as if it grew after approval
        def __init__(self, st):
            self.st_mode, self.st_dev, self.st_ino, self.st_size = st.st_mode, st.st_dev, st.st_ino, 0

    monkeypatch.setattr(os, "fstat", lambda fd: Lie(real_fstat(fd)))
    with pytest.raises(lib.Refusal) as excinfo:
        lib.read_approved(real, meta, 1000)
    assert excinfo.value.reason == "too_large" and spy.read_bytes <= 1001  # streamed under the cap, never the whole file


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok\n")
    assert lib.input_refusal(os.path.realpath(path)) is None


@pytest.mark.parametrize("outside", ["/etc/hosts", "~/.ssh/config", "~/.codex/auth.json", "~/.cli-proxy-api/x.yaml", "~/.claude/state/pm-ops.db",
                                     "/tmp/some-evidence-dir/log.txt", "~/.config/cliproxyapi/client.env"])
def test_paths_outside_the_approved_roots_are_refused_and_send_nothing(env, monkeypatch, outside):
    lane(monkeypatch)
    assert bulk_read.main(["--paths", outside, "--question", "q"]) == 6
    assert "requests" not in STUB


def test_approved_roots_are_the_source_checkouts_and_clones_of_them(env, monkeypatch):
    projects = env["dir"] / "home" / "Downloads" / "projects"
    names = ("heydonna-app", "heydonna-app-3003", "heydonna-app-proxy", "master-of-panes", "heydonna-app-3002-evidence",
             "heydonna-app-3004-artifacts", "heydonna-app-3006-raw", "master-of-panes-capture-adapter", "heydonna-private-9828")
    for name in names:
        (projects / name).mkdir(parents=True)
    monkeypatch.delenv("MOP_BULK_READ_EXTRA_ROOTS")
    roots = {os.path.basename(root) for root in lib.source_roots()}
    assert {"heydonna-app", "heydonna-app-3003", "heydonna-app-proxy", "master-of-panes", "releases"} <= roots
    assert not roots & {"heydonna-app-3002-evidence", "heydonna-app-3004-artifacts", "heydonna-app-3006-raw", "master-of-panes-capture-adapter",
                        "heydonna-private-9828"}
    base = tempfile.mkdtemp(prefix="bulkread-clone-", dir="/tmp")
    try:
        def make_clone(name, git):
            top = os.path.join(base, name)
            os.makedirs(top)
            if git == "worktree":
                open(os.path.join(top, ".git"), "w").write("gitdir: %s/heydonna-app/.git/worktrees/%s\n" % (projects, name))
            elif git:
                os.makedirs(os.path.join(top, ".git"))
                open(os.path.join(top, ".git", "config"), "w").write('[remote "origin"]\n\turl = %s\n' % git)
            open(os.path.join(top, "app.ts"), "w").write("x\n")
            open(os.path.join(top, ".env"), "w").write("x\n")
            return os.path.realpath(top)

        wt = make_clone("lane-wt", "worktree")
        clone = make_clone("lane-clone", "https://github.com/heydonna-app/heydonna-app.git")
        mop = make_clone("lane-mop", "git@github.com:rajivpoddar/master-of-panes.git")
        other = make_clone("other-repo", "https://github.com/someone/else.git")
        plain = make_clone("plain-dir", None)
        wrong_tree = make_clone("stray-wt", None)
        open(os.path.join(wrong_tree, ".git"), "w").write("gitdir: /somewhere/else/.git/worktrees/x\n")
        evidence = make_clone("lane-evidence", "worktree")
        for top in (wt, clone, mop):
            assert lib.input_refusal(os.path.join(top, "app.ts")) is None, top
            assert lib.input_refusal(os.path.join(top, ".env")) == "restricted_data_class"
        for top in (other, plain, wrong_tree):
            assert lib.input_refusal(os.path.join(top, "app.ts")) == "outside_approved_roots", top
        assert lib.input_refusal(os.path.join(evidence, "app.ts")) == "restricted_data_class"  # an evidence dir is never a source root
    finally:
        shutil.rmtree(base)


def test_target_of_code_write_may_not_sit_in_a_restricted_place(env):
    assert lib.target_refusal(str(env["dir"] / ".ssh" / "authorized_keys")) == "restricted_data_class"
    assert lib.target_refusal(str(env["dir"] / "out" / "new.py")) is None


def test_code_write_refuses_bad_references_and_targets_with_zero_bytes(env, monkeypatch, tmp_path_factory):
    lane(monkeypatch)
    ok = env["dir"] / "ref.py"
    ok.write_text("x\n")
    secret_ref = env["dir"] / ".env"
    secret_ref.write_text("SECRET-CANARY\n")
    outside = tmp_path_factory.mktemp("outside") / "ref.py"
    outside.write_text("OUTSIDE-CANARY\n")
    spy = Spy(monkeypatch)
    assert code_write.main(["spec", str(secret_ref), str(env["dir"] / "a.py")]) == 6
    assert code_write.main(["spec", str(outside), str(env["dir"] / "b.py")]) == 6
    assert code_write.main(["spec", str(ok), str(env["dir"] / ".ssh" / "keys")]) == 6
    assert "requests" not in STUB and not spy.touched(secret_ref) and not spy.touched(outside)
    assert not (env["dir"] / "a.py").exists() and not (env["dir"] / "b.py").exists()


def test_guard_leaves_restricted_and_outside_root_files_to_the_native_read(env, tmp_path_factory):
    approved = make(env["dir"] / "big.ts", 900)
    restricted = make(env["dir"] / ".env.big", 900)
    outside = make(tmp_path_factory.mktemp("outside") / "big.txt", 900)
    assert run_guard(env, read(env, approved), **LANE) is not None  # an approved big file is still redirected
    assert run_guard(env, read(env, restricted), **LANE) is None  # bulk-read would refuse these, so the native Read stands
    assert run_guard(env, read(env, outside), **LANE) is None
    assert run_guard(env, bash(env, "cat " + restricted), **LANE) is None
    assert run_guard(env, read(env, restricted)) is None and run_guard(env, read(env, outside)) is None  # the PM session too


def test_egress_goes_only_to_the_loopback_router(env, monkeypatch):
    lane(monkeypatch)
    src = env["dir"] / "src.ts"
    src.write_text("export const x = 1;\n")
    sent = []

    def spy_open(request, timeout=None):
        sent.append((request.full_url, request.data))
        raise OSError("blocked by test")

    monkeypatch.setattr(lib.OPENER, "open", spy_open)
    for url in ("http://example.com:8317", "https://127.0.0.1:8317", "http://127.0.0.1.evil.example", "http://user:pw@127.0.0.1:8317",
                "http://192.168.68.113:8317", "ftp://127.0.0.1"):
        monkeypatch.setenv("CLIPROXY_BASE_URL", url)
        with pytest.raises(lib.InkError) as excinfo:
            lib.router_base()
        assert excinfo.value.reason == "egress_denied", url
        assert bulk_read.main(["--paths", str(src), "--question", "q"]) == 3
    assert [data for _, data in sent if data is not None] == []  # no request body went anywhere
    assert not [url for url, _ in sent if "example" in url or "192.168.68.113:8317" in url]
    for url in ("http://127.0.0.1:8317", "http://localhost:3000", "http://[::1]:8317"):
        monkeypatch.setenv("CLIPROXY_BASE_URL", url)
        assert lib.router_base().startswith("http://")
    for url in ("http://example.com/v1/models", "https://192.168.68.113/v1/models"):
        monkeypatch.setenv("INKLING_HEALTH_URL", url)
        with pytest.raises(lib.InkError):
            lib.health_url()
    monkeypatch.setenv("INKLING_HEALTH_URL", "http://192.168.68.113:18120/v1/models")
    assert lib.health_url().endswith("/v1/models")


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


def test_nested_claude_binary_cannot_be_swapped_by_env_outside_test_mode(env, monkeypatch):
    assert lib.claude_bin() == str(env["claude"])  # the test seam
    monkeypatch.delenv("MOP_BULK_READ_TEST_MODE")
    monkeypatch.setenv("PATH", str(env["dir"]) + os.pathsep + os.environ["PATH"])
    assert lib.claude_bin() != str(env["claude"]) and "fake-claude" not in lib.claude_bin()
    assert env["dir"] .as_posix() not in lib.source_roots()  # and the extra root is ignored too


@pytest.mark.parametrize("how", ["state_marker", "canonical_marker", "env_off"])
def test_disabled_refuses_at_entry_with_zero_bytes_and_no_file_opened(env, monkeypatch, capsys, how):
    """CTO add-on ts 1795291271.444299: bulk-read and code-write check the DISABLED marker and MOP_BULK_READ_GUARD=0 themselves."""
    lane(monkeypatch)
    src, ref, target = env["dir"] / "src.ts", env["dir"] / "ref.py", env["dir"] / "out.py"
    src.write_text("export const x = 1;\n")
    ref.write_text("x\n")
    if how == "state_marker":
        env["state"].mkdir(parents=True)
        (env["state"] / "DISABLED").write_text("")
    elif how == "canonical_marker":
        monkeypatch.setenv("MOP_BULK_READ_STATE_DIR", str(env["dir"] / "elsewhere"))  # the canonical marker still counts
        os.makedirs(os.path.dirname(lib.canonical_marker()))
        open(lib.canonical_marker(), "w").close()
    else:
        monkeypatch.setenv("MOP_BULK_READ_GUARD", "0")
    spy = Spy(monkeypatch)
    assert bulk_read.main(["--paths", str(src), "--question", "q"]) == 7
    assert code_write.main(["spec", str(ref), str(target)]) == 7
    err = capsys.readouterr().err
    assert "bulk-read: disabled" in err and "code-write: disabled" in err
    assert "requests" not in STUB and not target.exists()
    assert not any(spy.touched(p) for p in (src, ref, target)) and spy.read_bytes == 0  # no file opened, nothing read
    assert not (env["state"] / "events.jsonl").exists()


def test_guard_also_honors_the_canonical_marker_when_the_state_dir_is_elsewhere(env):
    big = make(env["dir"] / "big.ts", 900)
    assert run_guard(env, read(env, big), **LANE) is not None
    marker = env["dir"] / "home" / ".local" / "share" / "master-of-panes" / "bulk-read" / "DISABLED"
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    assert run_guard(env, read(env, big), **LANE) is None and run_guard(env, read(env, big)) is None


def test_one_hard_deadline_covers_reading_and_the_model_call(env, monkeypatch, capsys):
    lane(monkeypatch)
    monkeypatch.setenv("BULK_READ_TIMEOUT_S", "0.5")
    path = make(env["dir"] / "a.ts", 5)
    monkeypatch.setattr(lib, "approve_inputs", lambda paths, cap: time.sleep(10))  # a stalled read: slow disk, FIFO, hung mount
    started = time.time()
    assert bulk_read.main(["--paths", path, "--question", "q"]) == 3
    assert time.time() - started < 4 and "hard_deadline" in capsys.readouterr().err
    assert "requests" not in STUB and events(env)[-1]["event"] == "delegate_fail_open"


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
    assert "bulk-read *" in excluded and not any("code-write" in e for e in excluded)  # code-write writes arbitrary targets: stays fenced


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
