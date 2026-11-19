#!/usr/bin/env python3
"""Reserve proxy review rounds per PR and stage, independent of head SHA.

For an exception, a trusted host operator must verify Rajiv's explicit approval
in Slack and record it in ~/.claude/state/proxy-review-cap-approvals.json as a
list of {"pr": 123, "stage": "12b", "round": 3, "ts": "1234567890.123456"}.
Keep that file operator-managed (0600); the launcher never writes it. A request
using rajiv:<ts> can redeem only that next round, once. A PM-written GitHub
comment is not approval: the PM uses Rajiv's GitHub account to post verdicts.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid

STAGES = {"pm-admission-reviewer": "12b", "hd-proof-qa": "qa"}
SHA = r"[0-9a-fA-F]{7,40}"
PM = re.compile(rf"^#{{0,6}} ?PM 12b: (PASS|REVISE|BLOCK|FAIL) at {SHA}(?=$|[\s.,;)])")
QA = re.compile(
    rf"^#{{0,6}} ?(?:QA(?: verdict)?|hd-proof-qa(?: QA)?)(?::[ \t]*|[ \t]+)"
    rf"(?:PASS|REVISE|BLOCK|FAIL|NEEDS_REVISION)(?: (?:at |head=){SHA})?$"
)
QA_PASS = re.compile(rf"^DONE: proof complete at {SHA} - .*\bCodex QA (?:PASS|APPROVE), ready for PM 12b$")
TOKEN = re.compile(r"^REVIEW_ROUND_RECEIPT: ([0-9a-f]{32})$", re.M)


def read_receipts(stage, pages):
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise ValueError("unexpected comment pages")
    receipts = {}
    for page in pages:
        for comment in page:
            if not isinstance(comment, dict) or not isinstance(comment.get("body"), str) or type(comment.get("id")) is not int:
                raise ValueError("unexpected comment record")
            body = comment["body"].rstrip()
            lines = body.splitlines()
            if not lines or "CARRY_FORWARD" in body or "(S153 disposition)" in body:
                continue
            first, last = lines[0], lines[-1]
            # Codex companion comments and REVIEW_CAP_2 summaries are not QA verdicts.
            verdict = PM.match(first) if stage == "12b" else (
                QA.fullmatch(first) or QA.fullmatch(last) or QA_PASS.fullmatch(last)
            )
            if verdict:
                tokens = set(TOKEN.findall(body))
                receipts[str(comment["id"])] = tokens.pop() if len(tokens) == 1 else None
    return receipts


def fetch_receipts(stage, pr):
    response = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "--paginate", "--slurp",
         f"repos/Scribie/heydonna-app/issues/{pr}/comments?per_page=100"],
        check=True, capture_output=True, timeout=12,
    )
    return read_receipts(stage, json.loads(response.stdout))


def approved(path, pr, stage, round_number, override):
    if not re.fullmatch(r"rajiv:[0-9]+\.[0-9]{6}", override):
        return False
    try:
        with path.open() as stream:
            fd_stat = os.fstat(stream.fileno())
            if fd_stat.st_uid != os.getuid() or fd_stat.st_mode & 0o077:
                return False
            approvals = json.load(stream)
    except (OSError, ValueError):
        return False
    return isinstance(approvals, list) and {
        "pr": pr, "stage": stage, "round": round_number, "ts": override[6:],
    } in approvals


def check(conn, root, stage, pr, reserve):
    key = (pr, stage)
    label = f"stage={stage} pr={pr}"
    saved = conn.execute("SELECT receipts, reservations FROM rounds WHERE pr=? AND stage=?", key).fetchone() if conn else None
    receipts, reservations = (json.loads(saved[0]), json.loads(saved[1])) if saved else ({}, [])
    try:
        # The transaction spans lookup, decision and reservation: another launcher
        # cannot reuse this snapshot while we're claiming a slot.
        current = fetch_receipts(stage, pr)
    except (OSError, subprocess.SubprocessError, ValueError):
        if not saved:
            return 76, f"REVIEW_CAP_UNKNOWN {label} reason=no_trusted_history", ""
        print(f"proxy-agent: warning: using saved review history {label}", file=sys.stderr)
    else:
        receipts.update(current)

    # A matching receipt and reservation are one review, including reposts of
    # that receipt. An untagged receipt cannot silently free a reserved slot.
    reserved = set(reservations)
    receipt_count = sum(token not in reserved for token in receipts.values())
    occupied = receipt_count + len(reserved)
    summary = f"{label} revise_rounds={occupied} receipts={len(receipts)} reservations={len(reserved)}"
    override = os.environ.get("PROXY_AGENT_REVIEW_CAP_OVERRIDE", "")
    token = ""
    if occupied >= 2 and not approved(root / "proxy-review-cap-approvals.json", pr, stage, occupied + 1, override):
        rc, note = 76, f"REVIEW_CAP_2 {summary}"
    else:
        rc = 0
        note = (f"REVIEW_CAP_OVERRIDE {summary} round={occupied + 1} override={override}"
                if occupied >= 2 else f"REVIEW_CAP_OK {summary} round={occupied + 1}")
        if reserve:
            token = uuid.uuid4().hex
            reservations.append(token)
    if reserve:
        conn.execute("INSERT OR REPLACE INTO rounds VALUES (?, ?, ?, ?)",
                     (*key, json.dumps(receipts), json.dumps(reservations)))
        conn.commit()
    if token:
        note += f" receipt={token}"
    return rc, note, token


def main(mode, agent_type, brief_path):
    stage = STAGES[agent_type]
    root = Path.home() / ".claude/state"
    conn = None
    try:
        brief = Path(brief_path).read_text(encoding="utf-8")
        candidates = re.findall(r"^[ \t]*PR:[ \t]*#?([1-9][0-9]*)\b", brief, re.M)
        candidates = candidates or re.findall(r"\bPR(?:[ \t]+#|:[ \t]*#?)([1-9][0-9]*)\b", brief)
        if len(set(candidates)) != 1:
            print(f"proxy-agent: REVIEW_CAP_UNKNOWN stage={stage} reason=missing_or_ambiguous_pr", file=sys.stderr)
            return 76
        pr = int(candidates[0])
        database = root / "proxy-review-cap.sqlite3"
        reserve = mode == "reserve"
        if reserve:
            os.umask(0o077)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            conn = sqlite3.connect(database, timeout=20)
            conn.execute("CREATE TABLE IF NOT EXISTS rounds (pr INTEGER, stage TEXT, receipts TEXT, reservations TEXT, PRIMARY KEY(pr, stage))")
            conn.execute("BEGIN IMMEDIATE")
        elif database.exists():
            conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        rc, note, token = check(conn, root, stage, pr, reserve)
        print(f"proxy-agent: {note}", file=sys.stderr)
        if token:
            print(token)
        return rc
    except (OSError, ValueError, sqlite3.Error, TypeError, OverflowError):
        print(f"proxy-agent: REVIEW_CAP_UNKNOWN stage={stage} reason=state_or_brief_unavailable", file=sys.stderr)
        return 76
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
