#!/usr/bin/env python3
"""Reserve proxy review rounds per PR and stage, independent of head SHA.

Stage 12b: a reservation is a round (the verdict reaches GitHub only via PM), so receipts + reservations count.
Stage qa (Rajiv C0ALZJHGE49 thread 1795010709.991409, ts 1795138738.993769): only POSTED QA verdict receipts
occupy rounds. A launch still gets a receipt token (so a posted verdict pairs with its launch) but a launch that
ends without a verdict consumes nothing, so proof/debug runs can never lock QA debugging out. When the qa verdict
cap is reached, a brief whose first non-empty line is exactly `MODE: DEBUG` is still allowed (no reservation, no token).
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
PM = re.compile(rf"^#{{0,6}} ?PM 12b( delta)?: (PASS|REVISE|BLOCK|FAIL) at {SHA}(?=$|[\s.,;)])")
PM_RECEIPT_FIELD = re.compile(r"^(?:PR|HEAD_SHA|VERDICT|REVIEWER|TIMESTAMP):")
QA = re.compile(
    rf"^#{{0,6}} ?(?:QA(?: verdict)?|hd-proof-qa(?: QA)?)(?::[ \t]*|[ \t]+)"
    rf"(?:PASS|REVISE|BLOCK|FAIL|NEEDS_REVISION)(?: (?:at |head=){SHA})?$"
)
QA_PASS = re.compile(rf"^DONE: proof complete at {SHA} - .*\bCodex QA (?:PASS|APPROVE), ready for PM 12b$")
TOKEN = re.compile(r"^REVIEW_ROUND_RECEIPT: ([0-9a-f]{32})$", re.M)
S153_ROUND_TWO = "s153:round-2"  # Cached history evidence, not a receipt ID.
S153_DISPOSITION = re.compile(rf"^PM 12b: PASS at {SHA} \(S153 disposition\)$")
S153_REVISE = "Reviewer verdict at this head was REVISE with REVIEW_CAP_2 (round 2)"


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
            if not lines:
                continue
            first, last = lines[0], lines[-1]
            if "(S153 disposition)" in first:
                if (stage == "12b" and S153_DISPOSITION.fullmatch(first)
                        and len(lines) > 1 and lines[1].startswith(S153_REVISE)):
                    receipts[S153_ROUND_TWO] = None
                continue
            tokens = set(TOKEN.findall(body))
            if stage == "12b":
                verdict = PM.match(first)
                if not verdict:
                    continue
                # Only evidence-only PASS markers are carry-forwards. A negative
                # verdict can cite carried proof without ceasing to be a review.
                details = [line.strip() for line in lines[1:]
                           if line.strip() and not PM_RECEIPT_FIELD.match(line)]
                if verdict[2] == "PASS" and not tokens and (
                    verdict[1] or (details and all(line.startswith("CARRY_FORWARD:") for line in details))
                ):
                    continue
            else:
                # Codex companion comments and REVIEW_CAP_2 summaries are not QA verdicts.
                if not (QA.fullmatch(first) or QA.fullmatch(last) or QA_PASS.fullmatch(last)):
                    continue
            receipts[str(comment["id"])] = tokens.pop() if len(tokens) == 1 else None
    return receipts


def fetch_receipts(stage, pr):
    response = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "--paginate", "--slurp",
         f"repos/Scribie/heydonna-app/issues/{pr}/comments?per_page=100"],
        check=True, capture_output=True, timeout=12,
    )
    return read_receipts(stage, json.loads(response.stdout))


def is_debug_brief(brief):
    """True when the brief's first non-empty line is exactly `MODE: DEBUG` (trailing blanks only are tolerated)."""
    for line in brief.splitlines():
        if line.strip():
            return line.rstrip() == "MODE: DEBUG"
    return False


def check(conn, stage, pr, reserve, debug=False):
    key = (pr, stage)
    label = f"stage={stage} pr={pr}"
    saved = conn.execute("SELECT receipts, reservations FROM rounds WHERE pr=? AND stage=?", key).fetchone() if conn else None
    receipts, reservations = (json.loads(saved[0]), json.loads(saved[1])) if saved else ({}, [])
    try:
        # The transaction spans lookup, decision and reservation: another launcher
        # cannot reuse this snapshot while we're claiming a slot.
        current = fetch_receipts(stage, pr)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 76, f"REVIEW_CAP_UNKNOWN {label} reason=github_history_unavailable", ""
    receipts.update(current)

    # A matching receipt and reservation are one review, including reposts of
    # that receipt. An untagged receipt cannot silently free a reserved slot.
    reserved = set(reservations)
    tokens = [token for key, token in receipts.items() if key != S153_ROUND_TWO]
    if stage == "qa":
        # Posted verdicts only. A reposted receipt (same token) is still one review; reservations are
        # kept for pairing and diagnostics but never occupy a QA round.
        occupied = tokens.count(None) + len({token for token in tokens if token is not None})
    else:
        receipt_count = tokens.count(None) + len({token for token in tokens if token is not None} - reserved)
        # The disposition attests that round two already happened. It sets a floor
        # rather than adding another receipt if the verdict is also in history.
        occupied = max(receipt_count + len(reserved), 2 if S153_ROUND_TWO in receipts else 0)
    summary = f"{label} revise_rounds={occupied} receipts={len(tokens)} reservations={len(reserved)}"
    token = ""
    if occupied >= 2 and stage == "qa" and debug:
        # MODE: DEBUG never posts a verdict, so it takes no reservation and gets no receipt token.
        rc, note = 0, f"REVIEW_CAP_DEBUG_ALLOWED {label} revise_rounds={occupied}"
    elif occupied >= 2:
        rc, note = 76, f"REVIEW_CAP_2 {summary}"
    else:
        rc = 0
        note = f"REVIEW_CAP_OK {summary} round={occupied + 1}"
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
    if "PROXY_AGENT_REVIEW_CAP_OVERRIDE" in os.environ:
        print(f"proxy-agent: REVIEW_CAP_OVERRIDE_DISABLED stage={stage}; "
              "independent approval custody is not available; unset PROXY_AGENT_REVIEW_CAP_OVERRIDE",
              file=sys.stderr)
        return 76
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
        rc, note, token = check(conn, stage, pr, reserve, is_debug_brief(brief))
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
