#!/usr/bin/env python3
"""Post one HeyDonna CTO Slack message through REST with identity verification."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SLACK_API = "https://slack.com/api"
EXPECTED_USER_ID = "U0BNFGX2UAX"


def slack_call(method: str, token: str, payload: dict[str, str] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        f"{SLACK_API}/{method}", data=data, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"{method}: {result.get('error') or 'unknown_error'}")
    return result


def read_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    if args.text_file is not None:
        return Path(args.text_file).read_text(encoding="utf-8")
    return sys.stdin.read()


def normalize_whitespace_escapes(text: str) -> str:
    """Turn single-escaped whitespace into real whitespace without decoding other escapes.

    A doubled backslash remains a literal backslash, so ``\\\\n`` can still be
    used when the Slack message intentionally needs to display ``\\n``.
    """
    normalized: list[str] = []
    index = 0
    replacements = {"n": "\n", "r": "\r", "t": "\t"}
    while index < len(text):
        if text[index] != "\\":
            normalized.append(text[index])
            index += 1
            continue

        run_end = index
        while run_end < len(text) and text[run_end] == "\\":
            run_end += 1
        slash_count = run_end - index
        normalized.append("\\" * (slash_count // 2))

        if run_end < len(text) and text[run_end] in replacements and slash_count % 2:
            normalized.append(replacements[text[run_end]])
            index = run_end + 1
        else:
            if slash_count % 2:
                normalized.append("\\")
            index = run_end

    return "".join(normalized).replace("\r\n", "\n").replace("\r", "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--thread-ts")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text")
    source.add_argument("--text-file")
    parser.add_argument(
        "--preserve-literal-escapes",
        action="store_true",
        help="Do not convert visible \\n, \\r, or \\t sequences to real whitespace",
    )
    args = parser.parse_args()

    token = os.environ.get("SLACK_CTO_BOT_TOKEN")
    if not token:
        raise SystemExit("SLACK_CTO_BOT_TOKEN is not set")

    text = read_text(args)
    if not args.preserve_literal_escapes:
        text = normalize_whitespace_escapes(text)
    if not text.strip():
        raise SystemExit("Slack message text is empty")

    identity = slack_call("auth.test", token)
    auth_user_id = identity.get("user_id")
    if auth_user_id != EXPECTED_USER_ID:
        raise SystemExit(
            f"cto_identity_mismatch: expected={EXPECTED_USER_ID} actual={auth_user_id}"
        )

    payload = {"channel": args.channel, "text": text}
    if args.thread_ts:
        payload["thread_ts"] = args.thread_ts
    posted = slack_call("chat.postMessage", token, payload)
    posted_ts = str(posted["ts"])
    root_ts = args.thread_ts or posted_ts

    query = urllib.parse.urlencode(
        {"channel": args.channel, "ts": root_ts, "limit": "100"}
    )
    request = urllib.request.Request(
        f"{SLACK_API}/conversations.replies?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        replies = json.loads(response.read().decode("utf-8"))
    if not replies.get("ok"):
        raise RuntimeError(
            f"conversations.replies: {replies.get('error') or 'unknown_error'}"
        )

    stored = next(
        (message for message in replies.get("messages", []) if str(message.get("ts")) == posted_ts),
        None,
    )
    if stored is None:
        raise SystemExit(f"posted_message_not_found: ts={posted_ts}")
    stored_user = stored.get("user")
    if stored_user != EXPECTED_USER_ID:
        raise SystemExit(
            f"stored_author_mismatch: expected={EXPECTED_USER_ID} actual={stored_user}"
        )
    if stored.get("text") != text:
        raise SystemExit("stored_text_mismatch")
    if args.thread_ts and str(stored.get("thread_ts")) != args.thread_ts:
        raise SystemExit(
            f"stored_thread_mismatch: expected={args.thread_ts} actual={stored.get('thread_ts')}"
        )

    print(
        json.dumps(
            {
                "ok": True,
                "channel": args.channel,
                "thread_ts": root_ts,
                "ts": posted_ts,
                "auth_user_id": auth_user_id,
                "stored_user": stored_user,
                "text_sha256": __import__("hashlib").sha256(text.encode("utf-8")).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
