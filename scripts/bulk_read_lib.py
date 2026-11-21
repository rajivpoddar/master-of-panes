"""Shared helpers for the MoP bulk-read PreToolUse guard, bulk-read and code-write.

Stdlib only. Every helper fails soft: the guard must never be the reason a tool call fails.
Backends: proxy lanes (PROXY_AGENT_RUN set) ask inkling-2-mini, then gpt-6-luna, both through CLIProxyAPI on loopback; every other
session (the PM pane and its subagents) asks Claude Haiku (default) or Sonnet through a nested direct claude -p on its own auth.

Boundaries (CTO BLOCK on release 38bd1682, C0ALZJHGE49 ts 1795291197.575539 and ts 1795291271.444299):
- DISABLED: the marker file or MOP_BULK_READ_GUARD=0 makes bulk-read and code-write refuse at entry, before any file is opened.
- INPUT: every input is realpath-resolved and checked BEFORE it is opened. Only approved source roots (the heydonna-app checkouts, git
  clones or worktrees of them under /tmp, the MoP repo and its releases) are allowed, and restricted data classes are denied even
  inside them. A denied or outside-root input sends zero bytes.
- EGRESS: bytes go only to CLIProxyAPI on loopback or to the nested claude binary at a fixed install path; no env proxy, no redirect.
- RESOURCE: each input is stat-checked and the total capped before any read; reads stream through a descriptor under the cap.
State (events log, health cache, circuit breaker, bypass tokens) lives under MOP_BULK_READ_STATE_DIR (default
~/.local/share/master-of-panes/bulk-read). Logs hold paths, line counts and latencies only, never file content.
Test seam: MOP_BULK_READ_TEST_MODE=1 additionally honors MOP_BULK_READ_EXTRA_ROOTS (colon-separated source roots; the restricted-class
checks still apply) and BULK_READ_CLAUDE_BIN. Nothing else is relaxed.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

THRESHOLD_LINES = 400
HEALTH_TIMEOUT_S = 3
BYPASS_TTL_S = 600
BREAKER_FAILS = 3
BREAKER_SECONDS = 300
LOG_ROTATE_BYTES = 5 * 1024 * 1024
MAX_FILES = 20
OFF_VALUES = {"0", "off", "false", "no", "disabled"}
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
INKLING_HOST = "192.168.68.113"


class InkError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Deadline(BaseException):
    """The hard overall deadline fired. A BaseException so no fallback logic can swallow it."""


class Refusal(Exception):
    """An input was refused before it was opened or before more of it was read. Nothing was sent."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def test_mode():
    return os.environ.get("MOP_BULK_READ_TEST_MODE") == "1"


def state_dir():
    return os.environ.get("MOP_BULK_READ_STATE_DIR") or os.path.expanduser("~/.local/share/master-of-panes/bulk-read")


def canonical_marker():
    return os.path.join(os.path.expanduser("~/.local/share/master-of-panes/bulk-read"), "DISABLED")


def disabled_by_file():
    """The DISABLED marker in the canonical state dir always counts, even when MOP_BULK_READ_STATE_DIR points elsewhere."""
    return os.path.exists(canonical_marker()) or os.path.exists(os.path.join(state_dir(), "DISABLED"))


def disabled_reason(env=None):
    env = os.environ if env is None else env
    if env.get("MOP_BULK_READ_GUARD", "").strip().lower() in OFF_VALUES:
        return "MOP_BULK_READ_GUARD=0"
    if disabled_by_file():
        return "DISABLED marker"
    return None


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


# ---- egress: loopback router, the Inkling host for its health probe, no proxy, no redirect -------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _split(raw, hosts):
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "http" or (parts.hostname or "") not in hosts or parts.username or parts.password:
        raise InkError("egress_denied")
    return parts


def router_base():
    """CLIProxyAPI. Only a loopback http origin is accepted, so file bytes and the router key never go anywhere else."""
    parts = _split(os.environ.get("CLIPROXY_BASE_URL") or "http://127.0.0.1:8317", LOOPBACK_HOSTS)
    host = "[%s]" % parts.hostname if ":" in parts.hostname else parts.hostname
    return "http://%s:%s" % (host, parts.port or 80)


def health_url():
    """Same default as proxy-agent.sh's Inkling gate. /v1/models answers in about 1 s under load; /health timed out at 3 s while lanes were busy."""
    raw = os.environ.get("INKLING_HEALTH_URL") or "http://%s:18120/v1/models" % INKLING_HOST
    _split(raw, LOOPBACK_HOSTS + (INKLING_HOST,))
    return raw


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
    """Inkling's health URL (vLLM /v1/models), 3 s gate."""
    def fetch():
        with OPENER.open(urllib.request.Request(health_url()), timeout=HEALTH_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    return _probe("inkling", fetch, now)


def router_up(now=None):
    """CLIProxyAPI answers /v1/models: the path to gpt-6-luna, used when Inkling is down. 3 s gate."""
    def fetch():
        request = urllib.request.Request(router_base() + "/v1/models", headers={"Authorization": "Bearer " + router_key()})
        with OPENER.open(request, timeout=HEALTH_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    return _probe("router", fetch, now)


def route(env=None):
    """'lane' for a proxy lane (Inkling, then luna); 'claude' for every other session (PM pane: Haiku/Sonnet on direct Claude)."""
    value = (os.environ if env is None else env).get("PROXY_AGENT_RUN", "").strip().lower()
    return "claude" if not value or value in OFF_VALUES else "lane"


def claude_bin():
    """The nested direct-Claude binary. Fixed install paths only: an env or PATH override would be an egress hole."""
    if test_mode() and os.environ.get("BULK_READ_CLAUDE_BIN"):
        return os.environ["BULK_READ_CLAUDE_BIN"]
    home = os.path.expanduser("~")
    for candidate in ("/opt/homebrew/bin/claude", os.path.join(home, ".local/opt/claude-code/claude"),
                      os.path.join(home, ".local/bin/claude"), "/usr/local/bin/claude"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def backend_available(route_name, now=None):
    """(ok, reason): is any backend for this route worth blocking for? Lanes: Inkling healthy, else the router (luna)."""
    if route_name == "claude":
        found = claude_bin()
        return (True, "claude") if found and os.access(found, os.X_OK) else (False, "claude_missing")
    try:
        up, why = inkling_up(now)
    except InkError:
        up, why = False, "egress_denied"
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


def line_count(path, max_bytes=64 * 1024 * 1024):
    """(lines, is_binary). Binary = a NUL byte in the first 8 KiB. Counts the unterminated last line. Used by the hook only."""
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


# ---- input boundary --------------------------------------------------------------------------------------------------

SOURCE_CHECKOUT = re.compile(r"^(heydonna-app(-(2|300[1-8]|proxy))?|master-of-panes)$")
RESTRICTED_NAMES = {".ssh", ".codex", ".cli-proxy-api", ".gnupg", ".aws", ".git", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
                    ".htpasswd", ".pgpass", "settings.local.json", "client.env", "auth.json"}
RESTRICTED_SUFFIXES = (".env", ".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".ppk", ".kdbx", ".db", ".sqlite", ".sqlite3",
                       ".db-wal", ".db-shm")
RESTRICTED_PREFIXES = (".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
RESTRICTED_WORDS = re.compile(r"secret|credential|evidence|customer[-_ ]?artifact|customer[-_ ]?data|"
                              r"slack[-_ ]?(image|file|download|upload|attachment)")


def restricted_component(name):
    low = name.lower()
    return (low.startswith(RESTRICTED_PREFIXES) or low in RESTRICTED_NAMES or low.endswith(RESTRICTED_SUFFIXES)
            or bool(RESTRICTED_WORDS.search(low)))


def _restricted_in(parts):
    if any(restricted_component(part) for part in parts if part):
        return True
    return any(a == ".claude" and b == "state" for a, b in zip(parts, parts[1:]))  # ~/.claude/state (pm-ops.db and friends)


def source_roots():
    """Realpath'd approved source roots: the heydonna-app checkouts, the MoP repo and its releases, plus test roots in test mode."""
    home = os.path.expanduser("~")
    projects = os.path.join(home, "Downloads", "projects")
    roots = []
    try:
        for name in sorted(os.listdir(projects)):
            if SOURCE_CHECKOUT.match(name) and os.path.isdir(os.path.join(projects, name)):
                roots.append(os.path.realpath(os.path.join(projects, name)))
    except OSError:
        pass
    roots.append(os.path.realpath(os.path.join(home, ".local/share/master-of-panes/releases")))
    if test_mode():
        roots += [os.path.realpath(p) for p in os.environ.get("MOP_BULK_READ_EXTRA_ROOTS", "").split(":") if p]
    return roots


def _approved_git_top(top):
    """A /tmp clone of an approved repo: a worktree whose gitdir sits under an approved checkout, or a clone whose remote is one."""
    marker = os.path.join(top, ".git")
    try:
        if os.path.isfile(marker):
            with open(marker, "rb") as handle:
                match = re.match(r"gitdir:\s*(.+)", handle.read(512).decode("utf-8", "replace"))
            if not match:
                return False
            gitdir = os.path.realpath(os.path.join(top, match.group(1).strip()))
            return any(gitdir.startswith(os.path.join(root, ".git", "worktrees") + os.sep) for root in source_roots())
        if os.path.isdir(marker):
            with open(os.path.join(marker, "config"), "rb") as handle:
                urls = re.findall(r"url\s*=\s*(\S+)", handle.read(16384).decode("utf-8", "replace"))
            return any(re.search(r"[/:](heydonna-app|Scribie)/heydonna-app(\.git)?/?$|[/:]rajivpoddar/master-of-panes(\.git)?/?$", url)
                       for url in urls)
    except OSError:
        pass
    return False


def approved_root(real):
    """(root, deny_base) when the resolved path is inside an approved root, else None. deny_base is where restricted-name checks begin."""
    for root in source_roots():
        if real.startswith(root + os.sep):
            return root, root
    tmp = os.path.realpath("/tmp")
    if real.startswith(tmp + os.sep):
        parts = real[len(tmp) + 1:].split(os.sep)
        for depth in (1, 2):
            if len(parts) > depth:
                top = os.path.join(tmp, *parts[:depth])
                if _approved_git_top(top):
                    return top, tmp
    return None


def input_refusal(real, given=None):
    """Reason string when this resolved path may not be sent to a model, else None. Looks at the path only; opens nothing."""
    found = approved_root(real)
    if not found:
        return "outside_approved_roots"
    root, base = found
    if _restricted_in(real[len(base):].split(os.sep)) or (given and restricted_component(os.path.basename(given.rstrip("/")))):
        return "restricted_data_class"
    return None


def target_refusal(target):
    """code-write output: never into a restricted place (.ssh, .env*, .git, ~/.claude/state, evidence dirs, ...)."""
    real = os.path.realpath(os.path.expanduser(target))
    return "restricted_data_class" if _restricted_in(real.split(os.sep)) else None


def approve_inputs(paths, cap_total):
    """Phase A, nothing opened: realpath, approved root, restricted classes, regular file, and the summed size, for every input.
    Raises Refusal before any byte is read. Returns [(real, lstat)]."""
    if len(paths) > MAX_FILES:
        raise Refusal("too_many_files")
    items, total = [], 0
    for given in paths:
        real = os.path.realpath(os.path.expanduser(given))
        reason = input_refusal(real, given)
        if reason:
            raise Refusal(reason)
        try:
            meta = os.lstat(real)
        except OSError:
            raise Refusal("not_found")
        if not stat.S_ISREG(meta.st_mode):
            raise Refusal("not_a_regular_file")
        total += meta.st_size
        items.append((real, meta))
    if total > cap_total:
        raise Refusal("too_large")
    return items


def read_approved(real, meta, cap):
    """Phase B: open the resolved path without following a final symlink, verify it is the file that was approved, then stream
    at most cap bytes. Refuses too_large without reading when the descriptor already reports more."""
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise Refusal("not_found")
    try:
        now = os.fstat(fd)
        if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != (meta.st_dev, meta.st_ino):
            raise Refusal("changed_during_check")
        if now.st_size > cap:
            raise Refusal("too_large")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                raise Refusal("too_large")
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if b"\0" in data[:8192]:
        raise Refusal("binary_file")
    return data.decode("utf-8", "replace")


# ---- backends ---------------------------------------------------------------------------------------------------------

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
    """One chat completion through CLIProxyAPI. Returns (text, usage); raises InkError(reason). The destination is checked first."""
    base = router_base()
    payload = {"model": model, "temperature": 0, "max_tokens": max_tokens, "reasoning_effort": "low",
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    request = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + router_key()})
    try:
        with OPENER.open(request, timeout=timeout) as response:
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
    """(text, usage, backend). Lanes: inkling-2-mini (gated by its health URL), then gpt-6-luna; raises InkError only when every
    backend failed. Other sessions: Haiku or Sonnet on direct Claude."""
    if route() == "claude":
        text, usage = claude_chat(system, user, max_tokens, timeout)
        return text, usage, "claude-" + claude_model()
    started, failures = time.time(), []
    inkling = os.environ.get("BULK_READ_MODEL", "inkling-2-mini")
    luna = os.environ.get("BULK_READ_FALLBACK_MODEL", "gpt-6-luna")
    try:
        up, why = inkling_up()
    except InkError as exc:
        up, why = False, exc.reason
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
