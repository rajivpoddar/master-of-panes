#!/usr/bin/env python3
"""MoP bulk-read guard: a PreToolUse hook (matchers Read and Bash) that blocks bulk reads before the main model pays for them.

Rajiv D0AMF0XE6TS thread 1795287257.526309, ts 1795287629.063499 ("do it with mop main repair agent"); PM amendments of 22 Nov 00:42,
00:54 and 01:00 IST (Rajiv ts 1795289208.297109, 1795289295.887739). Not the UserPromptSubmit chain (rule 50): it only ever looks at one Read,
or one lone cat/less/more/head/tail on a single path, and nothing else.

BLOCKS (deny with the redirect message) a Read, or a lone Bash cat/less/more/head/tail of one path, of a text file over 400 lines when
no offset/limit is given. The message names the alternatives: `bulk-read --paths <files> --question "<q>"` (understanding) or Read with
offset/limit of at most 200 lines (editing). bulk-read answers with Inkling, then gpt-6-luna, in a proxy lane, and with Claude Haiku
(Sonnet via BULK_READ_CLAUDE_MODEL) on direct Claude in the PM session.

LOADED BY: every proxy lane (proxy-agent.sh adds it to the generated --settings file) and the PM pane (install-relay-hooks.py adds it to the
PM checkout settings next to the MoP relay hooks). The PM pane is NOT exempt.

EXEMPT (never blocked, no log line), checked in this order:
  1. MOP_BULK_READ_GUARD=0|off|false|no|disabled in the session env, or the DISABLED marker (global kill switch) in the canonical state dir
     ~/.local/share/master-of-panes/bulk-read or in MOP_BULK_READ_STATE_DIR. bulk-read and code-write check both too, at entry.
  2. BULK_READ_EXEMPT=1 (proxy-agent.sh exports it for judgment lanes).
  3. Per-read escape hatch for evidence reads: any Read with offset or limit (offset=1 and limit=<line count> returns the whole file),
     or a Bash command prefixed BULK_READ_ALLOW=1. The block message for the PM session spells this out.
  4. Proxy lanes only (PROXY_AGENT_RUN set): judgment lanes, i.e. PROXY_AGENT_TYPE (else the type parsed from PROXY_AGENT_NAME) in
     JUDGMENT_TYPES or PROXY_AGENT_CAP_OVERRIDE starting with P0:, and lanes whose type cannot be determined. Same list as proxy-agent.sh
     proxy_agent_is_judgment_lane. Debugging and root-cause work stays on the main model (CLAUDE.md: never delegate it).
  5. Carve-outs: images, PDF and notebooks; binary files; files of 400 lines or fewer; Bash head/tail printing 400 lines or fewer;
     this hook's own helper files and state dir.
  6. Inputs bulk-read would refuse (CTO BLOCK on 38bd1682, ts 1795291197.575539): a resolved path outside the approved source roots or
     in a restricted data class (.env*, *secret*, *credential*, keys, ~/.ssh, ~/.codex, evidence dirs, ...) is never redirected;
     the native Read stands. See bulk_read_lib.py.

FAILS OPEN (allows the original read, logs a fail_open line): any hook error or the 6 s watchdog; no backend (lane: Inkling health URL (vLLM
/v1/models, as in proxy-agent.sh) not answering in 3 s AND the CLIProxyAPI router down; PM session: no claude binary); the circuit breaker (3 failed bulk-read runs in a row mean
5 minutes of no blocking, per route); or a bypass token that bulk-read leaves for a file when every backend failed or timed out (30 s cap).
The PM session never waits on a network gate: its check is a local file test.

LOGS one JSON line per block or fail-open to <state dir>/events.jsonl (path, lines, latency; never file content).
Disable: MOP_BULK_READ_GUARD=0 for a session; touch <state dir>/DISABLED for everyone; remove the guard entries from the settings files above.
State dir: MOP_BULK_READ_STATE_DIR, default ~/.local/share/master-of-panes/bulk-read.
"""
import json
import os
import re
import shlex
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import bulk_read_lib as lib  # noqa: E402

JUDGMENT_TYPES = ("ci-repair-agent", "e2e-failure-investigator", "hd-investigator", "customer-artifact-investigator",
                  "pm-admission-reviewer", "hd-planner")
SKIP_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico", ".heic", ".avif",
                   ".pdf", ".ipynb"}
HELPER_FILES = {"bulk_read_guard.py", "bulk_read_lib.py", "bulk-read", "code-write", "install-bulk-read-guard.py"}
WATCHDOG_S = 6
SHELL_META = re.compile(r"[|;&<>`$\n\\]")


def lane_type(env):
    declared = env.get("PROXY_AGENT_TYPE", "").strip()
    if declared:
        return declared
    match = re.match(r"^(?:px-)?proxy-(.+?)-\d{8}T\d{6}Z-\d+$", env.get("PROXY_AGENT_NAME", ""))
    return match.group(1) if match else ""


def exemption(env):
    """Reason string when this session is never blocked, else None. Only proxy lanes have type or P0 exemptions."""
    if env.get("MOP_BULK_READ_GUARD", "").strip().lower() in lib.OFF_VALUES:
        return "opt_out_env"
    if lib.disabled_by_file():
        return "opt_out_file"
    if env.get("BULK_READ_EXEMPT", "").strip().lower() not in ("", "0", "false", "no", "off"):
        return "exempt_env"
    if lib.route(env) == "claude":
        return None
    kind = lane_type(env)
    if env.get("PROXY_AGENT_CAP_OVERRIDE", "").startswith("P0:"):
        return "p0_lane"
    if kind in JUDGMENT_TYPES:
        return "judgment_lane"
    if not kind:
        return "unclassified_lane"
    return None


def _count_arg(value, program):
    """head/tail -n value to ('first'|'from'|'all_but', n); None when it is not a plain line count."""
    if value is None:
        return None
    try:
        if program == "tail" and value.startswith("+"):
            return ("from", int(value[1:]))
        if program == "head" and value.startswith("-"):
            return ("all_but", int(value[1:]))
        return ("first", int(value.lstrip("+-")))
    except ValueError:
        return None


def parse_bash(command):
    """A lone cat/less/more/head/tail of one path -> dict(tool, path, cd, count); anything else -> None."""
    text = (command or "").strip()
    cd = None
    match = re.match(r"^cd\s+(\S+)\s+&&\s+(.*)$", text, re.S)
    if match:
        cd, text = match.group(1), match.group(2).strip()
    if not text or SHELL_META.search(text):
        return None
    try:
        argv = shlex.split(text)
    except ValueError:
        return None
    program, args = os.path.basename(argv[0]), argv[1:]
    if program in ("cat", "less", "more"):
        paths = [a for a in args if not a.startswith("-")]
        return {"tool": "Bash:" + program, "path": paths[0], "cd": cd, "count": None} if len(paths) == 1 else None
    if program not in ("head", "tail"):
        return None
    count, paths, i = ("first", 10), [], 0
    while i < len(args):
        arg = args[i]
        if arg in ("-n", "--lines"):
            count, i = _count_arg(args[i + 1] if i + 1 < len(args) else None, program), i + 2
            if count is None:
                return None
            continue
        if arg.startswith("--lines="):
            count = _count_arg(arg[8:], program)
        elif re.fullmatch(r"-n\S+", arg):
            count = _count_arg(arg[2:], program)
        elif re.fullmatch(r"-\d+", arg):
            count = _count_arg(arg[1:], program)
        elif arg.startswith("-"):
            return None
        else:
            paths.append(arg)
        if count is None:
            return None
        i += 1
    return {"tool": "Bash:" + program, "path": paths[0], "cd": cd, "count": count} if len(paths) == 1 else None


def printed_lines(count, total):
    if count is None:
        return total
    kind, n = count
    return {"first": n, "from": max(total - n + 1, 0), "all_but": max(total - n, 0)}[kind]


def resolve(path, cwd):
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd or os.getcwd(), expanded)
    return os.path.realpath(expanded)


def is_own_helper(real):
    state = os.path.realpath(lib.state_dir())
    return os.path.basename(real) in HELPER_FILES or real == state or real.startswith(state + os.sep)


def deny_message(display, lines, what, route_name):
    text = ("MoP bulk-read guard: %s has %d lines (limit %d) and %s has no offset/limit.\n"
            "- To understand it: bulk-read --paths <files> --question \"<q>\" (a cheap model; prints bullets with file:line refs).\n"
            "- To edit it: Read with offset/limit (200 lines or fewer) around the lines you need."
            % (display, lines, lib.THRESHOLD_LINES, what))
    if route_name == "claude":
        text += ("\n- Evidence read of the whole file: Read it with offset=1 and limit=%d, or prefix the Bash command with BULK_READ_ALLOW=1."
                 % lines)
    return text


def decide(data, env):
    """('allow'|'deny'|'fail_open', reason, info). Reads the filesystem; logs nothing."""
    reason = exemption(env)
    if reason:
        return "allow", reason, {}
    tool, tool_input, cwd = data.get("tool_name"), data.get("tool_input") or {}, data.get("cwd") or ""
    if tool == "Read":
        raw = tool_input.get("file_path")
        if not raw or tool_input.get("offset") is not None or tool_input.get("limit") is not None:
            return "allow", "range_given" if raw else "no_path", {}
        label, count, what = "Read", None, "this Read"
    elif tool == "Bash":
        if re.match(r"^\s*BULK_READ_ALLOW=1\s", tool_input.get("command") or ""):
            return "allow", "escape_hatch", {}
        parsed = parse_bash(tool_input.get("command"))
        if not parsed:
            return "allow", "not_a_lone_file_read", {}
        raw = parsed["path"] if not parsed["cd"] else os.path.join(resolve(parsed["cd"], cwd), parsed["path"])
        label, count = parsed["tool"], parsed["count"]
        what = "this command"
    else:
        return "allow", "other_tool", {}
    real = resolve(raw, cwd)
    info = {"tool": label, "path": real}
    if os.path.splitext(real)[1].lower() in SKIP_EXTENSIONS:
        return "allow", "carve_out_type", info
    if is_own_helper(real) or not os.path.isfile(real):
        return "allow", "own_helper_or_missing", info
    refusal = lib.input_refusal(real, raw)  # path only, nothing opened: bulk-read would refuse this input, so leave the native Read alone
    if refusal:
        return "allow", "not_delegable_" + refusal, info
    lines, binary = lib.line_count(real)
    info["lines"] = lines
    if binary:
        return "allow", "carve_out_binary", info
    if lines <= lib.THRESHOLD_LINES or printed_lines(count, lines) <= lib.THRESHOLD_LINES:
        return "allow", "within_limit", info
    route_name = lib.route(env)
    if lib.bypass_active(real):
        return "fail_open", "bypass_token", info
    if lib.breaker_open(name=route_name):
        return "fail_open", "breaker_open", info
    available, why = lib.backend_available(route_name)
    if not available:
        return "fail_open", why, info
    info["message"] = deny_message(real, lines, what, route_name)
    info["backend"] = why
    return "deny", "bulk_read", info


def main():
    started = time.time()
    state = {"tool": None, "path": None}

    def watchdog(signum, frame):
        lib.log_event("fail_open", reason="hook_timeout", tool=state["tool"], path=state["path"],
                      latency_ms=int((time.time() - started) * 1000))
        os._exit(0)

    try:
        signal.signal(signal.SIGALRM, watchdog)
        signal.alarm(WATCHDOG_S)
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return 0
        state["tool"] = data.get("tool_name")
        action, reason, info = decide(data, dict(os.environ))
        latency = int((time.time() - started) * 1000)
        if action == "allow":
            return 0
        lib.log_event("block" if action == "deny" else "fail_open", reason=reason, tool=info.get("tool"),
                      path=info.get("path"), lines=info.get("lines"), latency_ms=latency, backend=info.get("backend"),
                      session=(data.get("session_id") or "")[:12] or None)
        if action == "deny":
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                                 "permissionDecision": "deny",
                                                                 "permissionDecisionReason": info["message"]}}))
        return 0
    except BaseException as exc:  # never be the reason a tool call fails
        lib.log_event("fail_open", reason="hook_error_" + type(exc).__name__, tool=state["tool"],
                      latency_ms=int((time.time() - started) * 1000))
        return 0


if __name__ == "__main__":
    sys.exit(main())
