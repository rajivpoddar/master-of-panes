"""Shared helpers for the MoP bulk-read PreToolUse guard, bulk-read and code-write.

Stdlib only. Every helper fails soft: the guard must never be the reason a tool call fails.
Backends: proxy lanes (PROXY_AGENT_RUN set) ask inkling-2-mini, then gpt-6-luna, both through the managed CLIProxyAPI origin; every
other session (the PM pane and its subagents) asks Claude Haiku (default) or Sonnet through a nested direct claude -p.

Trust model (CTO BLOCK on 38bd1682 and REVISE on fb6de68d6a35, C0ALZJHGE49 thread 1795291040.095359): nothing a caller can set decides
WHERE bytes may come from or WHERE they may go. Roots, origins, models, the Claude binary, the state dir and HOME are module constants
(HOME is taken from the account database, never from $HOME). The environment can only tighten: disable the helpers, or lower a cap or a
timeout. Tests replace the module constants through a test-only harness (scripts/pm/tests); no deployed entrypoint has a seam.
- DISABLED: the marker file (in STATE_DIR) or MOP_BULK_READ_GUARD=0 makes bulk-read and code-write refuse at entry, before any file is opened.
- SOURCE: every input is realpath-resolved and checked BEFORE it is opened. Approved: the fixed operator checkouts (REPO_NAMES), the
  worktrees those repos themselves register in their own .git/worktrees (with a back-link check), and the MoP releases. A git config or
  .git file in a writable directory such as /tmp grants nothing. Restricted data classes are denied even inside approved roots. Anything
  else sends zero bytes and the native Read stands.
- EGRESS: bytes go only to ROUTER_ORIGIN (127.0.0.1:8317) with the models inkling-2-mini and gpt-6-luna, or to the nested claude binary at a
  fixed, owner-checked install path with a scrubbed environment and no caller-selected config dir. No env proxy, no redirect.
- RESOURCE: each input is stat-checked and the total capped before any read; reads stream through a descriptor under the cap.
State (events log, health cache, circuit breaker, bypass tokens) lives under STATE_DIR. Logs hold paths, line counts and latencies only.
"""
import fcntl
import hashlib
import json
import os
import pwd
import re
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

# ---- managed constants. The harness in scripts/pm/tests replaces these; no environment variable does. ----------------------------
ACCOUNT = pwd.getpwuid(os.getuid())
HOME_DIR = ACCOUNT.pw_dir
STATE_DIR = os.path.join(HOME_DIR, ".local/share/master-of-panes/bulk-read")
RELEASES_ROOT = os.path.join(HOME_DIR, ".local/share/master-of-panes/releases")
PROJECTS_DIR = os.path.join(HOME_DIR, "Downloads", "projects")
REPO_NAMES = ("heydonna-app", "heydonna-app-proxy") + tuple("heydonna-app-300%d" % n for n in range(1, 9)) + ("master-of-panes",)
ROUTER_ORIGIN = "http://127.0.0.1:8317"
ROUTER_KEY_FILE = os.path.join(HOME_DIR, ".config/cliproxyapi/client.env")
INKLING_HEALTH_URL = "http://%s:18120/v1/models" % INKLING_HOST  # same URL as proxy-agent.sh's Inkling gate
INKLING_MODEL = "inkling-2-mini"
LUNA_MODEL = "gpt-6-luna"
CLAUDE_CANDIDATES = ("/opt/homebrew/bin/claude", os.path.join(HOME_DIR, ".local/opt/claude-code/claude"),
                     os.path.join(HOME_DIR, ".local/bin/claude"), "/usr/local/bin/claude")
CLAUDE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"


class InkError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Deadline(BaseException):
    """The hard overall deadline fired. A BaseException so no fallback logic can swallow it."""


class Refusal(Exception):
    """An input or destination was refused before it was opened or before more of it was read. Nothing was sent."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def cap_env(name, default, floor=1):
    """An env value may only LOWER a managed cap or timeout; anything unparsable or above the default is ignored."""
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    if not value > 0 or value == float("inf"):  # zero, negative, NaN and infinity are ignored
        return default
    return min(max(value, floor), default)


def state_dir():
    return STATE_DIR


def canonical_marker():
    return os.path.join(STATE_DIR, "DISABLED")


def disabled_by_file():
    return os.path.exists(canonical_marker())


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


# ---- egress: the managed loopback router, the Inkling host for its health probe, no proxy, no redirect -------------------------

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
    parts = _split(ROUTER_ORIGIN, LOOPBACK_HOSTS)
    host = "[%s]" % parts.hostname if ":" in parts.hostname else parts.hostname
    return "http://%s:%s" % (host, parts.port or 80)


def health_url():
    _split(INKLING_HEALTH_URL, LOOPBACK_HOSTS + (INKLING_HOST,))
    return INKLING_HEALTH_URL


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
    """Inkling's health URL (vLLM /v1/models), 3 s gate. A GET: no file bytes and no credential go with it."""
    def fetch():
        with OPENER.open(urllib.request.Request(health_url()), timeout=HEALTH_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    return _probe("inkling", fetch, now)


def router_up(now=None):
    """The managed CLIProxyAPI answers /v1/models: the path to gpt-6-luna, used when Inkling is down. 3 s gate."""
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
    """The managed nested direct-Claude binary: a fixed install path whose real file is mine or root's and not group/world writable."""
    for candidate in CLAUDE_CANDIDATES:
        try:
            real = os.path.realpath(candidate)
            meta = os.stat(real)
        except OSError:
            continue
        if stat.S_ISREG(meta.st_mode) and os.access(real, os.X_OK) and meta.st_uid in (0, os.getuid()) and not meta.st_mode & 0o022:
            return candidate
    return ""


def backend_available(route_name, now=None):
    """(ok, reason): is any backend for this route worth blocking for? Lanes: Inkling healthy, else the router (luna)."""
    if route_name == "claude":
        return (True, "claude") if claude_bin() else (False, "claude_missing")
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


# ---- source boundary --------------------------------------------------------------------------------------------------

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


def _expand(path):
    """~ means the account's home from the account database, not whatever $HOME says."""
    if path == "~" or path.startswith("~/"):
        return HOME_DIR + path[1:]
    return path


def fixed_roots():
    """Operator-approved checkouts (a fixed name list under ~/Downloads/projects) and the MoP releases, realpath'd."""
    roots = []
    for name in REPO_NAMES:
        path = os.path.join(PROJECTS_DIR, name)
        if os.path.isdir(path):
            roots.append(os.path.realpath(path))
    roots.append(os.path.realpath(RELEASES_ROOT))
    return roots


def registered_worktrees():
    """Worktrees the approved repos register in their OWN .git/worktrees. The registration holds the worktree path; the worktree's
    .git file must point back at that registration. A directory that merely looks like a clone (a /tmp .git or config) is not here."""
    found = []
    for root in fixed_roots()[:-1]:
        admin_root = os.path.join(root, ".git", "worktrees")
        try:
            names = os.listdir(admin_root)
        except OSError:
            continue
        for name in names:
            admin = os.path.join(admin_root, name)
            try:
                with open(os.path.join(admin, "gitdir"), "rb") as handle:
                    worktree_git = os.path.realpath(handle.read(4096).decode("utf-8", "replace").strip())
                with open(worktree_git, "rb") as handle:
                    back = re.match(r"gitdir:\s*(.+)", handle.read(512).decode("utf-8", "replace"))
            except OSError:
                continue
            if back and os.path.realpath(back.group(1).strip()) == os.path.realpath(admin):
                found.append(os.path.dirname(worktree_git))
    return found


def source_roots():
    return fixed_roots() + registered_worktrees()


def approved_root(real):
    """The approved root that contains this resolved path (longest match), else None."""
    matches = [root for root in source_roots() if real.startswith(root + os.sep)]
    return max(matches, key=len) if matches else None


def input_refusal(real, given=None):
    """Reason string when this resolved path may not be sent to a model, else None. Looks at the path only; opens nothing."""
    root = approved_root(real)
    if not root:
        return "outside_approved_roots"
    if _restricted_in(real[len(root):].split(os.sep)) or (given and restricted_component(os.path.basename(given.rstrip("/")))):
        return "restricted_data_class"
    return None


def target_refusal(target):
    """code-write output: never into a restricted place (.ssh, .env*, .git, ~/.claude/state, evidence dirs, ...)."""
    real = os.path.realpath(_expand(target))
    return "restricted_data_class" if _restricted_in(real.split(os.sep)) else None


def approve_inputs(paths, cap_total):
    """Phase A, nothing opened: realpath, approved root, restricted classes, regular file, and the summed size, for every input.
    Raises Refusal before any byte is read. Returns [(real, lstat)]."""
    if len(paths) > MAX_FILES:
        raise Refusal("too_many_files")
    items, total = [], 0
    for given in paths:
        real = os.path.realpath(_expand(given))
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


# ---- code-write destination: pin the directory, publish by exclusive temp file plus atomic rename or link at that directory --------

class Destination:
    """A pinned output directory. Everything after pin_destination() goes through this open directory descriptor, so renaming or
    re-pointing the path afterwards cannot move a write onto another file."""

    def __init__(self, fd, parent, name):
        self.fd, self.parent, self.name = fd, parent, name

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def publish(self, text, force):
        temp = ".%s.code-write.%d.%s" % (self.name, os.getpid(), os.urandom(4).hex())
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=self.fd)
        try:
            with os.fdopen(out, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if force:
                os.rename(temp, self.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            else:
                os.link(temp, self.name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)  # create-only: fails if present
                os.unlink(temp, dir_fd=self.fd)
        except FileExistsError:
            os.unlink(temp, dir_fd=self.fd)
            raise Refusal("target_exists")
        except BaseException:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except OSError:
                pass
            raise


def pin_destination(target):
    """Resolve and check the target directory once, open it, and verify the descriptor is that directory. Returns a Destination."""
    expanded = os.path.abspath(_expand(target))
    name = os.path.basename(expanded)
    if not name or name in (".", ".."):
        raise Refusal("bad_target_name")
    parent = os.path.realpath(os.path.dirname(expanded))
    if _restricted_in(parent.split(os.sep) + [name]):
        raise Refusal("restricted_data_class")
    os.makedirs(parent, exist_ok=True)
    try:
        fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    except OSError:
        raise Refusal("bad_destination")
    try:
        if hasattr(fcntl, "F_GETPATH"):  # macOS: the path the descriptor really is must still be the one that was checked
            actual = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0].decode("utf-8", "replace")
            if os.path.realpath(actual) != parent:
                raise Refusal("destination_changed")
    except BaseException:
        os.close(fd)
        raise
    return Destination(fd, parent, name)


# ---- backends ---------------------------------------------------------------------------------------------------------

def router_key():
    """The managed router key, from the managed file only (no env), sent only to ROUTER_ORIGIN."""
    try:
        with open(ROUTER_KEY_FILE) as handle:
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
    """One chat completion through the managed router with an approved model alias. Returns (text, usage); raises InkError(reason).
    The destination and the alias are checked before anything is built."""
    if model not in (INKLING_MODEL, LUNA_MODEL):
        raise InkError("alias_denied")
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


def claude_model():
    """Only Haiku (default) and Sonnet exist on direct Claude for the PM; the env can pick between those two and nothing else."""
    return "sonnet" if os.environ.get("BULK_READ_CLAUDE_MODEL", "haiku").strip().lower() == "sonnet" else "haiku"


def claude_env():
    """A fixed environment for the nested claude: the account's home and name, a fixed PATH, no config dir, no tokens, no session ids."""
    return {"HOME": HOME_DIR, "USER": ACCOUNT.pw_name, "LOGNAME": ACCOUNT.pw_name, "PATH": CLAUDE_PATH, "LANG": "en_US.UTF-8"}


def claude_chat(system, user, max_tokens, timeout):
    """One nested direct-Claude call on the account's own auth. No hooks, no settings, no tools, no session file, run from a fixed
    directory so no CLAUDE.md or project is loaded."""
    binary = claude_bin()
    if not binary:
        raise InkError("claude_missing")
    env = claude_env()
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
    try:
        up, why = inkling_up()
    except InkError as exc:
        up, why = False, exc.reason
    if up:
        try:
            text, usage = chat(INKLING_MODEL, system, user, max_tokens, min(timeout / 2.0, cap_env("BULK_READ_INKLING_TIMEOUT_S", 15, floor=0.1)))
            return text, usage, INKLING_MODEL
        except InkError as exc:
            failures.append("inkling_" + exc.reason)
    else:
        failures.append("inkling_" + why)
    try:
        text, usage = chat(LUNA_MODEL, system, user, max_tokens, max(timeout - (time.time() - started), min(timeout, 5.0)))
        return text, usage, LUNA_MODEL
    except InkError as exc:
        failures.append("luna_" + exc.reason)
    raise InkError(",".join(failures))
