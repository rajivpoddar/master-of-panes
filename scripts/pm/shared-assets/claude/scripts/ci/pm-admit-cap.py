#!/usr/bin/env python3
"""Read-only admission round breaker for pm-admit-ci.sh.

Rajiv approval C0ALZJHGE49 thread 1795669948.123989, ts 1795676115.108879 ("ok"):
count every exact ci-head label addition on this PR, including same-head re-admissions.
CI/E2E conclusions and failure classifications do not change the count. Unrelated
labels and workflow runs (including reruns without a new admission) do not count.

Exit: 0 under the cap | 10 cap reached | 11 UNAVAILABLE | 64 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

CAP = 2
DEFAULT_REPO = "Scribie/heydonna-app"
HEAD_LABEL = re.compile(r"ci-head:[0-9a-f]{40}")


class Unavailable(Exception):
    pass


def count_rounds(events):
    rounds = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("event"), str) or not event["event"]:
            raise Unavailable("invalid_event")
        if event["event"] != "labeled":
            continue
        label = event.get("label")
        if not isinstance(label, dict) or not isinstance(label.get("name"), str) or not label["name"]:
            raise Unavailable("invalid_label_event")
        name = label["name"]
        if not HEAD_LABEL.fullmatch(name):
            continue
        event_id = event.get("id")
        if type(event_id) is not int or event_id <= 0 or rounds.setdefault(event_id, name) != name:
            raise Unavailable("invalid_admission_event_id")
    return len(rounds)  # de-duplicate a repeated page row, not separate admissions of the same head


def fetch_rounds(repo: str, pr: int) -> int:
    path = f"issues/{pr}/events"
    cmd = ["gh", "api", f"repos/{repo}/{path}?per_page=100", "--paginate", "--slurp"]
    for attempt in (1, 2):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode:
                raise Unavailable(f"gh_rc_{proc.returncode}")
            pages = json.loads(proc.stdout)
            if not isinstance(pages, list) or not pages or not all(isinstance(page, list) for page in pages):
                raise Unavailable("bad_response_shape")
            return count_rounds(event for page in pages for event in page)
        except subprocess.TimeoutExpired:
            reason = "timeout"
        except OSError as exc:
            reason = f"gh_not_runnable:{type(exc).__name__}"
        except ValueError:
            reason = "bad_json"
        except Unavailable as exc:
            reason = str(exc)
        if attempt == 1:
            try:
                time.sleep(float(os.environ.get("PM_ADMIT_RETRY_SLEEP", "3")))
            except ValueError:
                time.sleep(3)
    raise Unavailable(f"{path}: {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--cap", type=int, default=CAP)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 64
    if args.pr <= 0 or args.cap <= 0:
        print("ADMISSION_ROUND_BREAKER: usage error")
        return 64
    try:
        rounds = fetch_rounds(args.repo, args.pr)
    except Unavailable as exc:
        print(f"ADMISSION_ROUND_BREAKER: UNAVAILABLE reason={exc}")
        return 11
    status = "cap_reached" if rounds >= args.cap else "ok"
    print(f"ADMISSION_ROUND_BREAKER: {status} pr={args.pr} rounds={rounds} cap={args.cap}")
    return 10 if rounds >= args.cap else 0


if __name__ == "__main__":
    sys.exit(main())
