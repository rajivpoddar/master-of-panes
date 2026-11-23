#!/usr/bin/env python3
"""Post proxy launch/stop status lines into each issue's assignment thread.

Rajiv, DM D0AMF0XE6TS thread 1794720837.453299, ts 1794720957.200479: "go. track the issue
thread in pm ops db to save lookups." (PM proposal ts 1794720867.996749).

proxy-agent.sh calls this helper twice per hd-planner / hd-builder / hd-proof-qa /
hd-investigator run whose brief carries an ISSUE: or PR: line:

  launch  ->  "<role> started on #N (PR #M, head <sha8>, model <m>, run <name>)"
  stop    ->  "<role> <DONE|HANDOFF|BLOCKED> on #N: <first 200 chars of the result line>"
              (an hd-planner HANDOFF with a committed plan also carries the plan path and a
              GitHub blob link at the pushed head)

The thread comes from `pm-ops-legacy.py issue-thread get`, which resolves and persists it.
Posts go out as the HeyDonna Alerts bot (SLACK_SEND_AS=alerts), never with the PM token.

FAIL-OPEN: every lookup or post failure is logged to stderr and swallowed; the process
always exits 0 so it can never block a proxy launch or stop. A total time budget bounds
the worst case. Customer channels are never a target, and nothing is posted without a
stored thread (a DM thread is used only when the issue's stored thread is that DM).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
REPO = os.environ.get("PROXY_ASSIGNMENT_REPO", "Scribie/heydonna-app")
ROLES = frozenset({"hd-planner", "hd-builder", "hd-proof-qa", "hd-investigator"})
CUSTOMER_CHANNELS = frozenset({"C0A56RX6FNW"})
CHANNEL_RE = re.compile(r"^[CD][0-9A-Z]{8,}$")
THREAD_RE = re.compile(r"^\d{9,11}\.\d{6}$")
MARK = re.compile(r"^\s*\**\s*(DONE|BLOCKED|HANDOFF)\s*:\s*(.*)$", re.I)
PLAN_RE = re.compile(r"\bdocs/plans/issue-\d+-[A-Za-z0-9._-]+\.md\b")
HEX40 = re.compile(r"\b[0-9a-f]{40}\b")
RESULT_CHARS = 200
BUDGET_S = float(os.environ.get("PROXY_NOTIFY_BUDGET_S", "40"))
_START = time.monotonic()


def log(message: str) -> None:
    print(f"PROXY_NOTIFY {message}", file=sys.stderr)


def remaining(cap: float) -> float:
    """Seconds left for one subprocess: the smaller of its own cap and the total budget."""
    left = BUDGET_S - (time.monotonic() - _START)
    return max(0.0, min(cap, left))


def run(args: list[str], *, timeout: float, stdin: str | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str] | None:
    limit = remaining(timeout)
    if limit <= 0.5:
        log(f"skip reason=budget_exhausted cmd={Path(args[0]).name}")
        return None
    try:
        return subprocess.run(args, input=stdin, capture_output=True, text=True,
                              timeout=limit, check=False, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"cmd_failed cmd={Path(args[0]).name} error={type(exc).__name__}")
        return None


def text_part(value: str) -> str:
    """Slack-escape a plain-text fragment. No backticks, no control characters."""
    value = "".join(c if c >= " " and c != "\x7f" else " " for c in value)
    value = value.replace("`", "'")
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def redact(line: str) -> str:
    """Same redaction the proxy stop relay applies to a result line."""
    for key, value in os.environ.items():
        if len(value) >= 6 and re.search(r"token|secret|password|api.?key", key, re.I):
            line = line.replace(value, "[REDACTED]")
    line = re.sub(r"(?i)(bearer\s+)[\w.~/+\-=]+", r"\1[REDACTED]", line)
    line = re.sub(r"(?i)((?:[\w-]*(?:token|secret|password|api[_-]?key)[\w-]*)[\"']?\s*[:=]\s*)[\"']?[^\s,;\"']+",
                  r"\1[REDACTED]", line)
    line = re.sub(r"\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]+", "[REDACTED]", line)
    return line


def parse_refs(brief: str) -> tuple[str | None, str | None]:
    refs = []
    for key in ("ISSUE", "PR"):
        m = re.search(r"^\s*" + key + r"\s*:\s*#?(\d{3,6})\b", brief, re.I | re.M)
        refs.append(m.group(1) if m else None)
    return refs[0], refs[1]


def gh_json(args: list[str], *, timeout: float = 15) -> dict:
    gh = os.environ.get("PROXY_NOTIFY_GH", "gh")
    done = run([gh, *args], timeout=timeout)
    if done is None or done.returncode != 0:
        return {}
    try:
        data = json.loads(done.stdout or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def resolve_issue(issue: str | None, pr: str | None) -> str | None:
    if issue:
        return issue
    if not pr:
        return None
    refs = gh_json(["pr", "view", pr, "--repo", REPO, "--json", "closingIssuesReferences"]).get("closingIssuesReferences") or []
    for ref in refs:
        number = str(ref.get("number", "")) if isinstance(ref, dict) else ""
        if number.isdigit():
            return number
    return None


def thread_target_ok(channel: str, thread_ts: str) -> bool:
    return (bool(CHANNEL_RE.match(channel)) and bool(THREAD_RE.match(thread_ts))
            and channel not in CUSTOMER_CHANNELS)


def lookup_thread(issue: str, pr: str | None) -> tuple[str, str] | None:
    """Stored (or freshly resolved and persisted) assignment thread, else None (= skip)."""
    pm_ops = os.environ.get("PROXY_NOTIFY_PM_OPS", str(HOME / ".claude/scripts/pm-ops-legacy.py"))
    cmd = [sys.executable, "-I", pm_ops, "issue-thread", "get", "--issue", issue, "--format", "tsv"]
    if pr:
        cmd += ["--pr", pr]
    done = run(cmd, timeout=30)
    if done is None or done.returncode != 0:
        log(f"skip reason=no_thread issue={issue}")
        return None
    fields = (done.stdout.strip().splitlines() or [""])[0].split("\t")
    if len(fields) < 2 or not thread_target_ok(fields[0], fields[1]):
        log(f"skip reason=thread_refused issue={issue}")
        return None
    return fields[0], fields[1]


def post(channel: str, thread_ts: str, body: str) -> bool:
    # Rajiv DM D0AMF0XE6TS ts 1794992065.308419: "remove the slack alert from the proxy assign
    # script. it's causing too much noise." Proxy launch/stop lines are no longer posted to Slack.
    log("skip reason=slack_alerts_removed_rajiv_1794992065")
    return False
    if not thread_target_ok(channel, thread_ts):
        log("skip reason=target_refused")
        return False
    sender = os.environ.get("PROXY_NOTIFY_SLACK_SEND", str(HOME / ".claude/scripts/slack-send.sh"))
    done = run([sender, "-c", channel, "-t", thread_ts, "-f"], timeout=20, stdin=body,
               env={**os.environ, "SLACK_SEND_AS": "alerts"})
    if done is None:
        return False
    receipt = next((line for line in done.stdout.splitlines() if line.startswith("OK ts=")), "")
    fields = dict(part.split("=", 1) for part in receipt.split()[1:] if "=" in part)
    if (done.returncode == 0 and re.fullmatch(r"[0-9]+\.[0-9]+", fields.get("ts", ""))
            and fields.get("channel") == channel):
        log(f"delivery=sent channel={channel} thread={thread_ts}")
        return True
    log(f"delivery=unknown rc={done.returncode}")
    return False


def head_for_launch(brief: str, pr: str | None) -> str:
    m = re.search(r"^\s*HEAD\s*:\s*`?([0-9a-f]{7,40})\b", brief, re.I | re.M)
    if m:
        return m.group(1).lower()[:8]
    if pr:
        oid = str(gh_json(["pr", "view", pr, "--repo", REPO, "--json", "headRefOid"]).get("headRefOid", ""))
        if re.fullmatch(r"[0-9a-f]{40}", oid):
            return oid[:8]
    return ""


def launch_message(role: str, issue: str, pr: str | None, head: str, model: str, name: str) -> str:
    parts = []
    if pr:
        parts.append(f"PR #{pr}")
    if head:
        parts.append(f"head {head}")
    parts.append(f"model {model or 'auto'}")
    parts.append(f"run {name}")
    return text_part(f"{role} started on #{issue} (" + ", ".join(parts) + ")")


def result_text(jsonl: str, out: str) -> str:
    """Text of the LAST result event in the stream-json file; falls back to the rendered out file."""
    text = ""
    try:
        with open(jsonl, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if '"type":"result"' not in raw and '"type": "result"' not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    text = str(event.get("result") or "")
    except OSError:
        pass
    if text.strip():
        return text
    try:
        return Path(out).read_text(errors="replace")
    except OSError:
        return ""


def final_marker(text: str) -> tuple[str, str] | None:
    """The LAST DONE:/BLOCKED:/HANDOFF: line, as (STATUS, remainder)."""
    for line in reversed(text.splitlines()):
        m = MARK.match(line)
        if m:
            return m.group(1).upper(), m.group(2).strip()
    return None


def pushed_head(jsonl: str, text: str) -> str:
    observe = os.environ.get("PROXY_NOTIFY_OBSERVE", str(HOME / ".claude/scripts/proxy-observe.py"))
    done = run([sys.executable, "-I", observe, "--stop-info", jsonl], timeout=15)
    if done is not None and done.returncode == 0:
        m = re.search(r"last_pushed_head=([0-9a-f]{40})\b", done.stdout)
        if m:
            return m.group(1)
    found = HEX40.findall(text)
    return found[-1] if found else ""


def plan_link(text: str, jsonl: str) -> str:
    """' Plan: <path> <blob link>' for a committed plan, or '' when none is named."""
    paths = PLAN_RE.findall(text)
    if not paths:
        return ""
    plan = paths[-1]
    head = pushed_head(jsonl, text)
    suffix = f" Plan: {text_part(plan)}"
    if not head:
        return suffix
    # Only claim a blob link when the plan is really at that head.
    seen = gh_json(["api", f"repos/{REPO}/contents/{plan}?ref={head}"])
    if seen.get("path") != plan:
        log("plan_link_unverified")
        return suffix
    return f"{suffix} <https://github.com/{REPO}/blob/{head}/{plan}|plan at {head[:8]}>"


def stop_message(role: str, issue: str, jsonl: str, out: str) -> str | None:
    text = result_text(jsonl, out)
    found = final_marker(text)
    if not found:
        log("skip reason=no_terminal_marker")
        return None
    status, rest = found
    line = redact(rest)
    line = " ".join("".join(c if c >= " " and c != "\x7f" else " " for c in line).split())
    clipped = line[:RESULT_CHARS] + ("…" if len(line) > RESULT_CHARS else "")
    message = text_part(f"{role} {status} on #{issue}: {clipped}")
    if role == "hd-planner" and status == "HANDOFF":
        message += plan_link(redact(text), jsonl)
    return message


def notify(args: argparse.Namespace) -> None:
    role = args.type
    if role not in ROLES:
        return
    try:
        brief = Path(args.brief).read_text(errors="replace")
    except OSError:
        log("skip reason=brief_unreadable")
        return
    issue_ref, pr = parse_refs(brief)
    if not (issue_ref or pr):
        return
    issue = resolve_issue(issue_ref, pr)
    if not issue:
        log("skip reason=no_issue")
        return
    target = lookup_thread(issue, pr)
    if target is None:
        return
    if args.cmd == "launch":
        body = launch_message(role, issue, pr, head_for_launch(brief, pr), args.model, args.name)
    else:
        body = stop_message(role, issue, args.jsonl, args.out)
        if body is None:
            return
    post(target[0], target[1], body)


# ---- Structured exit result (Rajiv D0AMF0XE6TS thread 1795436469.682079, ts 1795438159.967189) --------------------------------------
# A lane ends with ONE block placed BEFORE its final DONE:/BLOCKED:/HANDOFF: marker line (the marker contract is unchanged):
#   RESULT_BEGIN
#   {"lane": ..., "type": ..., "issue": ..., "pr": ..., "head": "<40 hex>", "verdict": "DONE|BLOCKED|FAILED|HANDOFF",
#    "proof_links": [...], "next_stage": ..., "notes": ...}
#   RESULT_END
# Missing or invalid -> UNSTRUCTURED: today's result line is kept and the head is derived from git ls-remote (PR head ref) instead.
RESULT_BEGIN = re.compile(r"^\s*\**\s*RESULT_BEGIN\s*\**\s*$")
RESULT_END = re.compile(r"^\s*\**\s*RESULT_END\s*\**\s*$")
VERDICTS = ("DONE", "BLOCKED", "FAILED", "HANDOFF")
SCHEMA_KEYS = ("lane", "type", "issue", "pr", "head", "verdict", "proof_links", "next_stage", "notes")
HEX40_FULL = re.compile(r"^[0-9a-f]{40}$")
MESSAGE_JSON_CHARS = 1400


def extract_block(text: str) -> str | None:
    """Body of the LAST RESULT_BEGIN ... RESULT_END block in `text`, else None."""
    lines = text.splitlines()
    end = None
    for index in range(len(lines) - 1, -1, -1):
        if end is None:
            if RESULT_END.match(lines[index]):
                end = index
        elif RESULT_BEGIN.match(lines[index]):
            return "\n".join(lines[index + 1:end])
    return None


def _ref(value) -> tuple[bool, int | None]:
    if value is None or value == "":
        return True, None
    if isinstance(value, bool):
        return False, None
    if isinstance(value, int) and value > 0:
        return True, value
    if isinstance(value, str) and re.fullmatch(r"#?\d{1,7}", value.strip()):
        return True, int(value.strip().lstrip("#"))
    return False, None


def _clean(value: str, limit: int) -> str:
    value = redact("".join(c if c >= " " and c != "\x7f" else " " for c in value))
    return value[:limit]


def validate_block(body: str, name: str = "") -> tuple[dict | None, str]:
    """(clean block, "") or (None, reason). Unknown keys are dropped; strings are redacted; a lane-name mismatch is normalised, not fatal."""
    lines = [line for line in body.strip().splitlines() if not line.strip().startswith("```")]
    try:
        data = json.loads("\n".join(lines))
    except ValueError:
        return None, "json_parse"
    if not isinstance(data, dict):
        return None, "not_object"
    missing = [key for key in SCHEMA_KEYS if key not in data]
    if missing:
        return None, "missing:" + ",".join(missing)
    for key in ("lane", "type"):
        if not isinstance(data[key], str) or not data[key].strip():
            return None, key + "_invalid"
    refs = {}
    for key in ("issue", "pr"):
        ok, number = _ref(data[key])
        if not ok:
            return None, key + "_invalid"
        refs[key] = number
    head = data["head"].strip().lower() if isinstance(data["head"], str) else ""
    if not HEX40_FULL.match(head):
        return None, "head_invalid"
    verdict = data["verdict"].strip().upper() if isinstance(data["verdict"], str) else ""
    if verdict not in VERDICTS:
        return None, "verdict_invalid"
    links = data["proof_links"]
    if not isinstance(links, list) or len(links) > 20 or not all(isinstance(x, str) and x.strip() and len(x) <= 500 for x in links):
        return None, "proof_links_invalid"
    stage = data["next_stage"]
    if stage is not None and (not isinstance(stage, str) or len(stage) > 100):
        return None, "next_stage_invalid"
    notes = data["notes"]
    if notes is not None and not isinstance(notes, str):
        return None, "notes_invalid"
    clean = {"lane": _clean(data["lane"].strip(), 80), "type": _clean(data["type"].strip(), 60), "issue": refs["issue"], "pr": refs["pr"],
             "head": head, "verdict": verdict, "proof_links": [_clean(x.strip(), 500) for x in links],
             "next_stage": _clean(stage.strip(), 100) if isinstance(stage, str) and stage.strip() else None,
             "notes": _clean((notes or "").strip(), 2000)}
    if name and clean["lane"] != name:
        clean["lane_declared"] = clean["lane"]
        clean["lane"] = name
    return clean, ""


def ls_remote_pr_head(pr: str, cwd: str) -> str:
    """PR head sha from the remote's refs/pull/<n>/head (git ls-remote), or ''."""
    git = os.environ.get("PROXY_NOTIFY_GIT", "git")
    where = cwd if cwd and Path(cwd).is_dir() else "."
    done = run([git, "-C", where, "ls-remote", "origin", f"refs/pull/{pr}/head"], timeout=20)
    if done is not None and done.returncode == 0:
        found = re.match(r"([0-9a-f]{40})\s", done.stdout)
        if found:
            return found.group(1)
    return ""


def derive_head(brief: str, cwd: str, jsonl: str, _text: str = "") -> str:
    """Head for an UNSTRUCTURED result: the brief PR's remote head ref first, else the lane's last observed push; '' = unknown."""
    try:
        _issue, pr = parse_refs(Path(brief).read_text(errors="replace")) if brief else (None, None)
    except OSError:
        pr = None
    if pr:
        head = ls_remote_pr_head(pr, cwd)
        if head:
            return head
    # No bare-sha scraping of the lane's own prose: only the observed git push output (proxy-observe --stop-info) counts.
    return pushed_head(jsonl, "") if jsonl else ""


def structured_result(text: str, name: str, brief: str, cwd: str, jsonl: str) -> dict:
    body = extract_block(text)
    clean, reason = (None, "no_result_block") if body is None else validate_block(body, name)
    if clean:
        return {"status": "STRUCTURED", "block": clean, "head": clean["head"], "reason": "", "verdict": clean["verdict"]}
    reason = reason if reason == "no_result_block" else "invalid:" + reason
    return {"status": "UNSTRUCTURED", "block": None, "head": derive_head(brief, cwd, jsonl, text), "reason": reason, "verdict": ""}


def message_json(block: dict) -> str:
    """Compact one-line JSON for the PROXY_STOP message; trims notes/proof links when needed (the run row keeps the full block)."""
    text = json.dumps(block, separators=(",", ":"), ensure_ascii=True)
    if len(text) > MESSAGE_JSON_CHARS:
        short = dict(block, notes=block["notes"][:200], proof_links=block["proof_links"][:5])
        text = json.dumps(short, separators=(",", ":"), ensure_ascii=True)
    return text


def result_command(args: argparse.Namespace) -> None:
    text = result_text(args.jsonl, args.out)
    found = structured_result(text, args.name, args.brief, args.cwd or os.getcwd(), args.jsonl)
    block = found["block"]
    values = {"RESULT_STATUS": found["status"], "RESULT_HEAD": found["head"], "RESULT_VERDICT": found["verdict"],
              "RESULT_REASON": found["reason"], "RESULT_JSON": json.dumps(block, separators=(",", ":")) if block else "",
              "RESULT_MSGJSON": message_json(block) if block else ""}
    if args.format == "json":
        print(json.dumps({**found, "message_json": values["RESULT_MSGJSON"]}))
    else:
        for key, value in values.items():
            print(f"{key}={shlex.quote(value)}")


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("cmd", choices=("launch", "stop", "result"))
    parser.add_argument("--format", default="shell")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--type", default="")
    parser.add_argument("--brief", default="")
    parser.add_argument("--name", default=os.environ.get("PROXY_AGENT_NAME", "proxy"))
    parser.add_argument("--model", default=os.environ.get("PROXY_AGENT_MODEL_ID", ""))
    parser.add_argument("--out", default="")
    parser.add_argument("--jsonl", default="")
    parser.add_argument("--rc", default="")
    try:
        parsed = parser.parse_args()
        if parsed.cmd == "result":
            result_command(parsed)
        else:
            notify(parsed)
    except SystemExit:
        pass
    except Exception as exc:  # fail open: never block a launch or stop
        log(f"error={type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
