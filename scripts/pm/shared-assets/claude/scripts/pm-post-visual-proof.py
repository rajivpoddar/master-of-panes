#!/usr/bin/env python3
"""Post exact-head UI proof screenshots to a PR's Slack thread + write the PR receipt.

Rajiv directive 2026-09-27 08:37/08:44 (thread 1790476865.865109): UI screenshots
go to the PR's Slack thread as part of PM PR review, before CI admission. CTO
08:43 flow: PM posts the images in the Slack thread AND writes ONE structured PR
comment (Slack message ts + file ids, head, ACs, sha256) that the visual-proof
gate verifies. Rajiv's reply is NOT a prerequisite for admission; a concrete
layout objection blocks merge.

Usage:
  pm-post-visual-proof.py --pr 8363 --thread-ts 1790477819.230219 \
      --head <40-hex> --file AC-1=/abs/path/a.png --file AC-2=/abs/path/b.png \
      [--channel C0ALZJHGE49] [--note "extra context"] [--dry-run]
"""
import argparse, hashlib, json, os, re, subprocess, sys, urllib.parse, urllib.request

REPO = "heydonna-app/heydonna-app"
RAJIV = "<@UEQTTB97A>"
BRANCH_ISSUE_RE = re.compile(
    r"^(?:.*/)?(?:(?:fix|feat|feature|bug|test|chore|perf|refactor|enhance)/)?"
    r"(?P<issue>[0-9]{3,6})(?:[-_/].*)?$"
)
PROSE_ISSUE_RE = re.compile(r"#([0-9]+)")


def resolve_implementation_issue(pr_data, *, pr_number):
    self_ref = pr_number if type(pr_number) is int and pr_number > 0 else 0
    closing = {
        ref["number"]
        for ref in pr_data.get("closingIssuesReferences") or []
        if isinstance(ref, dict) and type(ref.get("number")) is int and ref["number"] > 0
    }
    closing.discard(self_ref)
    if len(closing) == 1:
        return next(iter(closing))
    if len(closing) > 1:
        raise RuntimeError("ambiguous implementation issue: multiple closing issue references")

    branch = pr_data.get("headRefName")
    if isinstance(branch, str):
        match = BRANCH_ISSUE_RE.fullmatch(branch.strip())
        if match:
            return int(match.group("issue"))

    text = f"{pr_data.get('title') or ''}\n{pr_data.get('body') or ''}"
    prose = {int(number) for number in PROSE_ISSUE_RE.findall(text)}
    prose.discard(self_ref)
    if len(prose) == 1:
        return next(iter(prose))
    if len(prose) > 1:
        raise RuntimeError("ambiguous implementation issue: multiple issue references in PR metadata")
    raise RuntimeError("cannot resolve implementation issue from read-only PR metadata")


def _gh_json(args, *, failure):
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
    except OSError:
        raise RuntimeError(failure) from None
    if result.returncode != 0:
        raise RuntimeError(failure)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(failure) from None
    if not isinstance(payload, dict):
        raise RuntimeError(failure)
    return payload


def resolve_issue_binding(pr, head):
    if type(pr) is not int or pr <= 0 or not re.fullmatch(r"[0-9a-f]{40}", str(head)):
        raise RuntimeError("invalid PR/head binding")
    pr_data = _gh_json(
        [
            "pr", "view", str(pr), "-R", REPO, "--json",
            "number,title,body,headRefOid,headRefName,closingIssuesReferences",
        ],
        failure="PR metadata unavailable",
    )
    if type(pr_data.get("number")) is not int or pr_data["number"] != pr:
        raise RuntimeError("PR metadata number mismatch")
    if pr_data.get("headRefOid") != head:
        raise RuntimeError("live PR head does not match --head")
    issue = resolve_implementation_issue(pr_data, pr_number=pr)
    issue_data = _gh_json(
        ["issue", "view", str(issue), "-R", REPO, "--json", "number,body"],
        failure="issue body unavailable",
    )
    if type(issue_data.get("number")) is not int or issue_data["number"] != issue:
        raise RuntimeError("issue body number mismatch")
    issue_body = issue_data.get("body")
    if not isinstance(issue_body, str) or not issue_body.strip():
        raise RuntimeError("issue body unavailable")
    return issue, issue_body


def build_slack_receipt(*, pr, issue, issue_body, head, channel, thread_ts, items):
    if type(pr) is not int or pr <= 0 or type(issue) is not int or issue <= 0:
        raise RuntimeError("invalid PR/issue binding")
    if not isinstance(issue_body, str) or not issue_body.strip():
        raise RuntimeError("issue body unavailable")
    if not re.fullmatch(r"[0-9a-f]{40}", str(head)):
        raise RuntimeError("invalid PR/head binding")
    return {
        "schema": "heydonna_qa_visual_proof",
        "version": 2,
        "artifact_kind": "slack",
        "pr": pr,
        "issue": issue,
        "head_sha": head,
        "issue_body_sha256": hashlib.sha256(issue_body.encode("utf-8")).hexdigest(),
        "slack_channel": channel,
        "slack_thread_ts": thread_ts,
        "scenarios": [
            {
                "ac_id": item["ac_id"],
                "slack_file_id": item["slack_file_id"],
                "file_name": item["name"],
                "sha256": item["sha256"],
            }
            for item in items
        ],
    }


def slack_token():
    for f in ("/Users/rajiv/Downloads/projects/heydonna-app/.env.local", os.path.expanduser("~/.claude/.env")):
        try:
            for line in open(f):
                m = re.match(r"^SLACK_BOT_TOKEN=(.*)$", line.strip())
                if m:
                    return m.group(1).strip().strip('"')
        except FileNotFoundError:
            pass
    sys.exit("REFUSED: SLACK_BOT_TOKEN not found")


def api(tok, method, data):
    req = urllib.request.Request("https://slack.com/api/" + method,
                                 data=urllib.parse.urlencode(data).encode(),
                                 headers={"Authorization": "Bearer " + tok})
    return json.load(urllib.request.urlopen(req))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--thread-ts", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--file", action="append", required=True, help="AC-id=/abs/path.png")
    ap.add_argument("--channel", default="C0ALZJHGE49")
    ap.add_argument("--note", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not re.fullmatch(r"[0-9a-f]{40}", a.head):
        sys.exit("REFUSED: --head must be a full 40-hex SHA")
    try:
        issue, issue_body = resolve_issue_binding(a.pr, a.head)
    except RuntimeError as exc:
        sys.exit(f"REFUSED: {exc}")

    items = []
    for spec in a.file:
        ac, _, path = spec.partition("=")
        if not ac or not os.path.isabs(path) or not os.path.isfile(path):
            sys.exit(f"REFUSED: bad --file {spec!r} (need AC-id=/abs/path that exists)")
        data = open(path, "rb").read()
        if not data.startswith(b"\x89PNG"):
            sys.exit(f"REFUSED: {path} is not a PNG")
        items.append({"ac_id": ac, "path": path, "name": os.path.basename(path), "bytes": data,
                      "sha256": hashlib.sha256(data).hexdigest()})

    lines = "\n".join(f"• {i['ac_id']}: {i['name']} (sha256 {i['sha256'][:12]}…)" for i in items)
    comment = (f"{RAJIV} UI proof for PR #{a.pr} at head `{a.head[:9]}` for layout review. "
               "Admission does not wait on your reply; a concrete objection blocks merge.\n" + lines +
               (f"\n{a.note}" if a.note else ""))
    if a.dry_run:
        print("DRY RUN\n" + comment)
        return

    tok = slack_token()
    files = []
    for i in items:
        r = api(tok, "files.getUploadURLExternal", {"filename": i["name"], "length": len(i["bytes"])})
        if not r.get("ok"):
            sys.exit(f"REFUSED: getUploadURLExternal {r}")
        urllib.request.urlopen(urllib.request.Request(r["upload_url"], data=i["bytes"], method="POST"))
        i["slack_file_id"] = r["file_id"]
        files.append({"id": r["file_id"], "title": f"{i['ac_id']} {i['name']}"})
    r = api(tok, "files.completeUploadExternal", {"files": json.dumps(files), "channel_id": a.channel,
                                                 "thread_ts": a.thread_ts, "initial_comment": comment})
    if not r.get("ok"):
        sys.exit(f"REFUSED: completeUploadExternal {r}")

    receipt = build_slack_receipt(
        pr=a.pr,
        issue=issue,
        issue_body=issue_body,
        head=a.head,
        channel=a.channel,
        thread_ts=a.thread_ts,
        items=items,
    )
    body = (f"<!-- qa-visual-proof-slack: {json.dumps(receipt, separators=(',', ':'))} -->\n"
            f"UI proof posted to Slack thread `{a.channel}/{a.thread_ts}` at head `{a.head}` for Rajiv's layout "
            "review (PM PR review step, Rajiv 2026-09-27).\n\n" +
            "\n".join(f"- {i['ac_id']}: `{i['name']}` sha256 `{i['sha256']}` (Slack file `{i['slack_file_id']}`)" for i in items))
    out = subprocess.run(["gh", "pr", "comment", str(a.pr), "-R", REPO, "--body", body], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"PARTIAL: Slack posted, PR comment failed: {out.stderr.strip()}")
    print(json.dumps({"ok": True, "pr_comment": out.stdout.strip(), "slack_files": [i["slack_file_id"] for i in items]}))


if __name__ == "__main__":
    main()
