"""code-write logic. Not an entrypoint: the launchers scripts/code-write (direct Claude, the PM) and scripts/code-write-lane (router) set
lib.BOUND_ROUTE and call main(). Spec + reference file -> target file, written by a cheap model.

  code-write [--force] "<spec>" <reference-file> <target-output-path>

Wraps the retired ./scripts/codex-write.sh contract: boilerplate only (tests, configs, repetitive patterns), review the
result before committing. Writes the raw file (code fences stripped), refuses an existing target without --force, and
prints ONLY "<target> <N> lines".

Backend, bound by the launcher (no environment variable selects it): code-write-lane uses inkling-2-mini then gpt-6-luna through the managed
router; code-write uses Claude Haiku, or Sonnet with BULK_READ_CLAUDE_MODEL=sonnet, through a nested direct claude -p and refuses inside a proxy lane.

Boundaries (CTO BLOCK on 38bd1682 ts 1795291197.575539 and ts 1795291271.444299; REVISE on fb6de68d6a35 ts 1795292716.429489):
  - Nothing in the environment chooses where bytes come from or go to (see bulk_read_lib.py). The environment can only disable the
    helper or LOWER a cap or timeout.
  - At entry, before any file is opened or any request made: refuses when the DISABLED marker exists or MOP_BULK_READ_GUARD=0.
  - The reference file gets the same source check as bulk-read inputs (realpath, operator-approved checkouts and the worktrees their own
    .git registers, restricted data classes, regular file, size) before it is opened; refused means zero bytes sent.
  - The destination directory is checked once and PINNED (an open directory descriptor) before the model is called. The result is written to
    an exclusive temp file in that directory and published there: an atomic link without --force (create-only, an existing file is never
    touched), an atomic rename with --force. Re-pointing the path afterwards cannot move the write onto another file.
  - One hard deadline covers reading, the model call and the write.
Exit codes: 8 no launcher binding, or a lane session using the direct-Claude launcher; 0 written; 2 usage, missing input or an existing target without --force; 3 every backend unavailable or failed (nothing
written: write the file yourself); 4 empty output (nothing written); 5 spec + reference over CODE_WRITE_MAX_CHARS (at most 80000);
6 input or destination refused by the boundary (nothing sent); 7 disabled.
Env (lowering only): CODE_WRITE_TIMEOUT_S (at most 120), CODE_WRITE_MAX_CHARS (at most 80000), BULK_READ_CLAUDE_MODEL (haiku or sonnet),
MOP_BULK_READ_GUARD=0 (disable). Each run appends one line (sizes, backend, latency; no content) to the guard's events.jsonl.
"""
import argparse
import os
import re
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import bulk_read_lib as lib  # noqa: E402

SYSTEM = ("You write one file. Output ONLY the raw file contents that satisfy the spec, using the reference for context. "
          "No code fences, no explanation, no prose, no leading or trailing commentary.")
FENCE_OPEN = re.compile(r"^" + chr(96) * 3 + r"[\w+-]*\s*$")


def strip_fences(text):
    lines = text.strip("\n").split("\n")
    if len(lines) >= 2 and FENCE_OPEN.match(lines[0]) and lines[-1].strip() == chr(96) * 3:
        lines = lines[1:-1]
    return "\n".join(lines).rstrip("\n") + "\n"


def run(args, started, timeout, dest_holder):
    cap = int(lib.cap_env("CODE_WRITE_MAX_CHARS", 80000))
    if not args.spec.strip():
        print("code-write: spec must not be empty", file=sys.stderr)
        return 2
    reason = lib.target_refusal(args.target)
    if reason:
        lib.log_event("code_write_refused", reason="target_" + reason)
        print("code-write: refused the target (%s); nothing was sent." % reason, file=sys.stderr)
        return 6
    if os.path.lexists(args.target) and not args.force:
        print("code-write: target already exists: %s (use --force)" % args.target, file=sys.stderr)
        return 2
    try:
        (real, meta), = lib.approve_inputs([args.reference], cap)
        dest_holder.append(lib.pin_destination(args.target))  # the destination is fixed here, before the model is called
        reference = lib.read_approved(real, meta, cap)
    except lib.Refusal as exc:
        lib.log_event("code_write_refused", reason=exc.reason)
        if exc.reason == "too_large":
            print("code-write: spec + reference over the size cap; trim the reference.", file=sys.stderr)
            return 5
        if exc.reason in ("not_found", "binary_file"):
            print("code-write: reference file not usable: %s (%s)" % (args.reference, exc.reason), file=sys.stderr)
            return 2
        print("code-write: refused (%s); nothing was sent. Use Read with offset/limit." % exc.reason, file=sys.stderr)
        return 6
    user = "<spec>\n%s\n</spec>\n\n<reference path=\"%s\">\n%s\n</reference>" % (args.spec, real, reference)
    if len(user) > cap:
        print("code-write: spec + reference over the size cap; trim the reference.", file=sys.stderr)
        return 5

    def failed(why, code):
        lib.log_event("code_write_fail", reason=why, chars_in=len(user), latency_ms=int((time.time() - started) * 1000))
        print("code-write: no backend answered (%s); write the file directly." % why, file=sys.stderr)
        return code

    available, why = lib.backend_available(lib.route())
    if not available:
        return failed(why, 3)
    try:
        text, usage, backend = lib.complete(SYSTEM, user, 12000, timeout)
    except lib.InkError as exc:
        return failed(exc.reason, 4 if exc.reason == "no_content" else 3)
    except Exception as exc:
        return failed("error_" + type(exc).__name__, 3)
    body = strip_fences(text)
    if not body.strip():
        return failed("no_content", 4)
    try:
        dest_holder[0].publish(body, args.force)
    except lib.Refusal as exc:
        lib.log_event("code_write_refused", reason=exc.reason)
        print("code-write: not written (%s): %s" % (exc.reason, args.target), file=sys.stderr)
        return 2
    count = body.count("\n")
    lib.log_event("code_write", chars_in=len(user), chars_out=len(body), lines=count, backend=backend,
                  latency_ms=int((time.time() - started) * 1000), prompt_tokens=usage.get("prompt_tokens"),
                  completion_tokens=usage.get("completion_tokens"), cost_usd=usage.get("cost_usd"))
    print("%s %d lines" % (args.target, count))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="code-write", description="Write a file from a spec with a cheap model.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("spec")
    parser.add_argument("reference")
    parser.add_argument("target")
    args = parser.parse_args(argv)
    off = lib.disabled_reason()
    if off:  # first action after parsing: no file is opened, nothing is sent
        print("code-write: disabled (%s); nothing was read or sent. Write the file directly." % off, file=sys.stderr)
        return 7
    if lib.BOUND_ROUTE not in ("lane", "claude"):
        print("code-write: not started through a launcher (code-write or code-write-lane); nothing was read or sent.", file=sys.stderr)
        return 8
    if lib.BOUND_ROUTE == "claude" and os.environ.get("PROXY_AGENT_RUN", "").strip().lower() not in ("",) + tuple(lib.OFF_VALUES):
        print("code-write: a proxy lane uses code-write-lane; nothing was read or sent.", file=sys.stderr)
        return 8
    started = time.time()
    timeout = lib.cap_env("CODE_WRITE_TIMEOUT_S", 120.0, floor=0.1)
    dest_holder = []

    def on_alarm(signum, frame):
        raise lib.Deadline()

    signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout + min(5.0, timeout))  # one hard deadline over reading, the model call and the write
    try:
        return run(args, started, timeout, dest_holder)
    except lib.Refusal as exc:  # pin_destination refused before the reference was read
        lib.log_event("code_write_refused", reason=exc.reason)
        print("code-write: refused (%s); nothing was sent." % exc.reason, file=sys.stderr)
        return 6
    except lib.Deadline:
        lib.log_event("code_write_fail", reason="hard_deadline", latency_ms=int((time.time() - started) * 1000))
        print("code-write: hard deadline passed; nothing written. Write the file directly.", file=sys.stderr)
        return 3
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for dest in dest_holder:
            dest.close()

