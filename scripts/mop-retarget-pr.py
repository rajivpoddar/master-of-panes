#!/usr/bin/env python3
"""Retarget PR metadata on an occupied MoP assignment without reassigning it."""
import argparse
import json
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("slot", "issue", "epoch", "old-pr", "pr"):
        parser.add_argument("--" + flag, type=int, required=True)
    for flag in ("branch", "head", "reason"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args()
    url = f"http://127.0.0.1:3100/slots/{args.slot}"
    with urllib.request.urlopen(url, timeout=10) as response:
        current = json.load(response)
    if not current.get("occupied") or current.get("issue") != args.issue or current.get("assignment_epoch") != args.epoch or current.get("pr") != args.old_pr:
        raise SystemExit("REFUSE: occupied assignment, issue, epoch or old PR differs")
    fields = ("repository_id", "issue", "pr", "branch", "head_sha", "work_kind", "handoff_id", "claimed_at")
    payload = {"expected_current_" + name: current.get(name) for name in fields}
    payload.update(expected_epoch=args.epoch, issue=args.issue, pr=args.pr, branch=args.branch, head_sha=args.head, reason=args.reason)
    request = urllib.request.Request(url + "/retarget-pr", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            print(response.read().decode())
    except urllib.error.HTTPError as error:
        raise SystemExit("REFUSE: " + error.read().decode()) from error


if __name__ == "__main__":
    main()
