#!/usr/bin/env python3
"""PR-wide completed review budget, shared by both launch entry points."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

STAGES = {"pm-admission-reviewer": "12b", "hd-proof-qa": "qa", "pm-proof-qa": "qa"}
SHA = r"[0-9a-fA-F]{7,40}"
PM = re.compile(rf"^#{{0,6}} ?PM 12b: (PASS|REVISE|BLOCK|FAIL) at {SHA}(?=$|[\s.,;)])")
QA = re.compile(
    rf"^#{{0,6}} ?(?:QA(?: verdict)?|hd-proof-qa(?: QA)?)(?::[ \t]*|[ \t]+)"
    rf"(?:PASS|REVISE|BLOCK|FAIL|NEEDS_REVISION)(?: (?:at |head=){SHA})?$"
)
QA_PASS = re.compile(rf"^DONE: proof complete at {SHA} - .*\bCodex QA (?:PASS|APPROVE), ready for PM 12b$")
QA_RECEIPT = ("If the brief identifies a PR and this QA review round produces a verdict, post exactly one PR comment with "
              "first line `hd-proof-qa: <PASS|REVISE|BLOCK|FAIL> at <reviewed SHA>`. "
              "Do not post a verdict for setup errors, cap refusals, or evidence-only carry-forwards.")


def count_reviews(stage, pages):
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise ValueError("unexpected comment pages")
    reviews = set()
    for page in pages:
        for comment in page:
            if not isinstance(comment, dict) or not isinstance(comment.get("body"), str) or type(comment.get("id")) is not int:
                raise ValueError("unexpected comment record")
            lines = comment["body"].rstrip().splitlines()
            if not lines:
                continue
            first, last = lines[0], lines[-1]
            if stage == "12b":
                verdict = PM.match(first) and "(S153 disposition)" not in first
            else:
                # A cap refusal isn't a verdict, and Codex companion comments aren't the QA stage.
                verdict = QA.fullmatch(first) or QA.fullmatch(last) or QA_PASS.fullmatch(last)
            if verdict:
                reviews.add(comment["id"])
    return len(reviews)


def check(agent_type, brief, timeout=12, report_ok=False):
    stage = STAGES.get(agent_type)
    if not stage:
        return 0, ""
    match = re.search(r"^[ \t]*PR:[ \t]*#?([1-9][0-9]*)\b", brief, re.M)
    match = match or re.search(r"\bPR(?:[ \t]+#|:[ \t]*#?)([1-9][0-9]*)\b", brief)
    if not match:
        return 0, f"warning: review cap skipped stage={stage}: no PR number in brief"
    pr = match[1]
    try:
        response = subprocess.run(
            ["gh", "api", "--paginate", "--slurp", f"repos/Scribie/heydonna-app/issues/{pr}/comments?per_page=100"],
            check=True, capture_output=True, timeout=timeout,
        )
        rounds = count_reviews(stage, json.loads(response.stdout))
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0, f"warning: review cap skipped stage={stage} pr={pr}: GitHub comments unavailable"
    # Keep revise_rounds for existing launcher log readers; it now counts all full verdicts.
    summary = f"stage={stage} pr={pr} revise_rounds={rounds}"
    if rounds >= 2:
        override = os.environ.get("PROXY_AGENT_REVIEW_CAP_OVERRIDE", "")
        if re.fullmatch(r"rajiv:[0-9]+\.[0-9]+", override):
            return 0, f"REVIEW_CAP_OVERRIDE {summary} override={override}"
        return 76, f"REVIEW_CAP_2 {summary}"
    return 0, f"REVIEW_CAP_OK {summary}" if report_ok else ""


if __name__ == "__main__":
    if sys.argv[1:] == ["--qa-receipt"]:
        print(QA_RECEIPT)
        sys.exit(0)
    agent_type, brief_path = sys.argv[1:]
    try:
        brief = Path(brief_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        sys.exit(0)  # Launcher validates the brief separately.
    rc, note = check(agent_type, brief, report_ok=os.environ.get("PROXY_AGENT_DRY_RUN") == "1")
    if note:
        print(f"proxy-agent: {note}", file=sys.stderr)
    sys.exit(rc)
