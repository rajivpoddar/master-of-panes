"""Shared helpers for the MoP bulk-read PreToolUse guard, bulk-read and code-write.

Stdlib only. Every helper fails soft: the guard must never be the reason a tool call fails.
Backends: proxy lanes (PROXY_AGENT_RUN set) ask inkling-2-mini, then gpt-6-luna, both through CLIProxyAPI; every other session
(the PM pane and its subagents) asks Claude Haiku (default) or Sonnet through a nested direct `claude -p` on its own auth.
State (events log, health cache, circuit breaker, bypass tokens) lives under
MOP_BULK_READ_STATE_DIR (default ~/.local/share/master-of-panes/bulk-read). Logs hold paths, line counts
and latencies only, never file content.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

THRESHOLD_LINES = 400
HEALTH_TIMEOUT_S = 3
BYPASS_TTL_S = 600
BREAKER_FAILS = 3
BREAKER_SECONDS = 300
LOG_ROTATE_BYTES = 5 * 1024 * 1024
OFF_VALUES = {"0", "off", "false", "no", "disabled"}


def state_dir():
    return os.environ.get("MOP_BULK_READ_STATE_DIR") or os.path.expanduser("~/.local/share/master-of-panes/bulk-read")


def log_event(event, **fields):
    """Append one JSON line (path, counts, latency; no file content). Never raises."""
    try:
        directory = state_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "events.jsonl")
        try:
            if os.path.getsize(path) > LOG_ROTATE_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event,
                  "lane": os.environ.get("PROXY_AGENT_NAME", "")}
        record.update({k: v for k, v in fields.items() if v is not None})
        with open(path, "a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass


def disabled_by_file():
    return os.path.exists(os.path.join(state_dir(), "DISABLED"))


def _read_json(name):
    try:
        with open(os.path.join(state_dir(), name)) as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_json(name, value):
    try:
        os.makedirs(state_dir(), exist_ok=True)
        tmp = os.path.join(state_dir(), name + ".tmp.%d" % os.getpid())
        with open(tmp, "w") as handle:
            json.dump(value, handle)
        os.replace(tmp, os.path.join(state_dir(), name))
    except Exception:
        pass


def health_url():
    return os.environ.get("INKLING_HEALTH_URL") or "http://192.168.68.113:18120/health"


def router_base():
    return (os.environ.get("CLIPROXY_BASE_URL") or "http://127.0.0.1:8317").rstrip("/")


def _probe(name, fetch, now):
    """(ok, reason) from fetch(), cached 30 s (up) / 15 s (down) so a burst of reads never waits twice."""
    now = time.time() if now is None else now
    cached = _read_json("health-%s.json" % name)
    try:
        if 0 <= now - float(cached["ts"]) < (30 if cached.get("ok") else 15):
            return bool(cached["ok"]), str(cached.get("reason", "cached"))
    except (KeyError, TypeError, ValueError):
        pass
    try:
        ok = fetch()
        reason = "up" if ok else "unhealthy"
    except Exception as exc:
        ok, reason = False, "down_" + type(exc).__name__
    _write_json("health-%s.json" % name, {"ts": now, "ok": ok, "reason": reason})
    return ok, reason


def inkling_up(now=None):
    """Inkling's own /health, 3 s gate."""
    def fetch():
        with urllib.request.urlopen(health_url(), timeout=HEALTH_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    return _probe("inkling", fetch, now)


def router_up(now=None):
    """CLIProxyAPI answers /v1/models: the path to gpt-6-luna, used when Inkling is down. 3 s gate."""
    def fetch():
        request = urllib.request.Request(router_base() + "/v1/models", headers={"Authorization": "Bearer " + router_key()})
        with urllib.request.urlopen(request, timeout=HEALTH_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    return _probe("router", fetch, now)


def route(env=None):
    """'lane' for a proxy lane (Inkling, then luna); 'claude' for every other session (PM pane: Haiku/Sonnet on direct Claude)."""
    value = (os.environ if env is None else env).get("PROXY_AGENT_RUN", "").strip().lower()
    return "claude" if not value or value in OFF_VALUES else "lane"


def claude_bin():
    return os.environ.get("BULK_READ_CLAUDE_BIN") or shutil.which("claude") or ""


def backend_available(route_name, now=None):
    """(ok, reason): is any backend for this route worth blocking for? Lanes: Inkling healthy, else the router (luna)."""
    if route_name == "claude":
        found = claude_bin()
        return (True, "claude") if found and os.access(found, os.X_OK) else (False, "claude_missing")
    up, why = inkling_up(now)
    if up:
        return True, "inkling"
    rup, rwhy = router_up(now)
    return (True, "luna") if rup else (False, "no_backend_%s_%s" % (why, rwhy))


def breaker_open(now=None, name="lane"):
    """True while recent calls keep failing (3 in a row open it for 5 minutes): stop blocking, fail open."""
    now = time.time() if now is None else now
    try:
        return float(_read_json("breaker-%s.json" % name).get("until", 0)) > now
    except (TypeError, ValueError):
        return False


def breaker_record(ok, now=None, name="lane"):
    now = time.time() if now is None else now
    if ok:
        _write_json("breaker-%s.json" % name, {"fails": 0, "until": 0})
        return
    try:
        fails = int(_read_json("breaker-%s.json" % name).get("fails", 0)) + 1
    except (TypeError, ValueError):
        fails = 1
    _write_json("breaker-%s.json" % name, {"fails": fails, "until": now + BREAKER_SECONDS if fails >= BREAKER_FAILS else 0})


def _bypass_name(path):
    return hashlib.sha256(os.path.realpath(path).encode()).hexdigest()[:24]


def bypass_grant(paths, now=None):
    """Allow the next plain Read of these files for BYPASS_TTL_S (bulk-read could not answer; fail open). True when recorded."""
    now = time.time() if now is None else now
    try:
        directory = os.path.join(state_dir(), "bypass")
        os.makedirs(directory, exist_ok=True)
        for path in paths:
            with open(os.path.join(directory, _bypass_name(path)), "w") as handle:
                handle.write(str(now))
        return True
    except Exception:
        return False


def bypass_active(path, now=None):
    now = time.time() if now is None else now
    try:
        with open(os.path.join(state_dir(), "bypass", _bypass_name(path))) as handle:
            return 0 <= now - float(handle.read().strip()) < BYPASS_TTL_S
    except Exception:
        return False


def line_count(path, max_bytes=256 * 1024 * 1024):
    """(lines, is_binary). Binary = a NUL byte in the first 8 KiB. Counts the unterminated last line."""
    with open(path, "rb") as handle:
        head = handle.read(8192)
        if b"\0" in head:
            return 0, True
        lines, last, total = head.count(b"\n"), head[-1:], len(head)
        while total < max_bytes:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            lines, last, total = lines + chunk.count(b"\n"), chunk[-1:], total + len(chunk)
    if last and last != b"\n":
        lines += 1
    return lines, False


class InkError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def router_key():
    key = os.environ.get("CLIPROXY_API_KEY", "")
    if key:
        return key
    try:
        with open(os.path.expanduser("~/.config/cliproxyapi/client.env")) as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if line.startswith("CLIPROXY_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def strip_think(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return text.rsplit("</think>", 1)[-1].strip()


def chat(model, system, user, max_tokens, timeout):
    """One chat completion through CLIProxyAPI. Returns (text, usage); raises InkError(reason)."""
    payload = {"model": model, "temperature": 0, "max_tokens": max_tokens, "reasoning_effort": "low",
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    request = urllib.request.Request(router_base() + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + router_key()})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise InkError("http_%s" % exc.code)
    except (TimeoutError, OSError) as exc:  # socket.timeout and URLError are OSError subclasses
        raise InkError("timeout" if "timed out" in str(exc) else "unreachable")
    except ValueError:
        raise InkError("bad_json")
    try:
        text = body["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        raise InkError("bad_shape")
    text = strip_think(text)
    if not text:
        raise InkError("no_content")
    return text, body.get("usage") or {}


CLAUDE_ENV_KEYS = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")


def claude_model():
    """Only Haiku (default) and Sonnet exist on direct Claude for the PM."""
    return "sonnet" if os.environ.get("BULK_READ_CLAUDE_MODEL", "haiku").strip().lower() == "sonnet" else "haiku"


def claude_chat(system, user, max_tokens, timeout):
    """One nested direct-Claude call on the caller's own auth. No hooks, no settings, no tools, no session file, a scrubbed env
    (no CLAUDE_CODE_* session ids, no router credentials), run from the temp dir so no CLAUDE.md or project is loaded."""
    binary = claude_bin()
    if not binary:
        raise InkError("claude_missing")
    env = {key: os.environ[key] for key in CLAUDE_ENV_KEYS if key in os.environ}
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
    args = [binary, "-p", "--model", claude_model(), "--setting-sources", "", "--settings", '{"disableAllHooks": true}',
            "--no-session-persistence", "--tools", "", "--disable-slash-commands", "--strict-mcp-config",
            "--output-format", "json", "--system-prompt", system]
    try:
        done = subprocess.run(args, input=user, capture_output=True, text=True, env=env, cwd=tempfile.gettempdir(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise InkError("timeout")
    except OSError:
        raise InkError("claude_unavailable")
    try:
        body = json.loads(done.stdout)
        text = strip_think(body.get("result") or "")
    except (ValueError, AttributeError):
        raise InkError("bad_json")
    if done.returncode != 0 or body.get("is_error"):
        raise InkError("claude_error")
    if not text:
        raise InkError("no_content")
    usage = body.get("usage") or {}
    return text, {"prompt_tokens": sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                  "completion_tokens": usage.get("output_tokens"), "cost_usd": body.get("total_cost_usd")}


def complete(system, user, max_tokens, timeout):
    """(text, usage, backend). Lanes: inkling-2-mini (gated by its /health), then gpt-6-luna; raises InkError only when every
    backend failed. Other sessions: Haiku or Sonnet on direct Claude."""
    if route() == "claude":
        text, usage = claude_chat(system, user, max_tokens, timeout)
        return text, usage, "claude-" + claude_model()
    started, failures = time.time(), []
    inkling = os.environ.get("BULK_READ_MODEL", "inkling-2-mini")
    luna = os.environ.get("BULK_READ_FALLBACK_MODEL", "gpt-6-luna")
    up, why = inkling_up()
    if up:
        try:
            text, usage = chat(inkling, system, user, max_tokens, min(timeout / 2.0, float(os.environ.get("BULK_READ_INKLING_TIMEOUT_S", "15"))))
            return text, usage, inkling
        except InkError as exc:
            failures.append("inkling_" + exc.reason)
    else:
        failures.append("inkling_" + why)
    try:
        text, usage = chat(luna, system, user, max_tokens, max(timeout - (time.time() - started), min(timeout, 5.0)))
        return text, usage, luna
    except InkError as exc:
        failures.append("luna_" + exc.reason)
    raise InkError(",".join(failures))
