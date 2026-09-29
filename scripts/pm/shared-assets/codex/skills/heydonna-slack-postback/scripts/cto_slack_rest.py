#!/usr/bin/env python3
"""Post one HeyDonna CTO Slack message through REST with identity verification."""

from __future__ import annotations

import argparse
import json
import hashlib
import os
import re
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SLACK_API = "https://slack.com/api"
EXPECTED_USER_ID = "U0BNFGX2UAX"


def slack_normalize_stored_text(text: str) -> str:
    """Slack stores &, < and > HTML-escaped and rewrites bare email addresses
    into <mailto:addr|addr> links. Normalize those two documented display
    transformations before the delivery readback comparison so a literal '>'
    or an email in the sent text is not reported as a delivery failure while
    the author check stays strict."""
    normalized = re.sub(r"<mailto:([^|>]+)\|[^>]*>", r"\1", text)
    normalized = normalized.replace("&lt;", "<").replace("&gt;", ">")
    return normalized.replace("&amp;", "&")


MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # Slack upload guard; far below external-share size
SLACK_UPLOAD_HOST_SUFFIX = ".slack.com"


def read_image_bytes(image_file: str) -> bytes:
    """Read the local image bytes up front so a missing/unreadable/oversized
    file fails closed before any Slack write is attempted."""
    path = Path(image_file)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"image_file_unreadable: {image_file}: {exc}")
    if not data:
        raise SystemExit("image_file_empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise SystemExit(f"image_file_too_large: {len(data)} > {MAX_UPLOAD_BYTES}")
    return data


def slack_form_call(method: str, token: str, fields: dict[str, str]) -> dict[str, Any]:
    """Form-encoded Slack Web API call, matching the published contract for
    files.getUploadURLExternal."""
    request = urllib.request.Request(
        f"{SLACK_API}/{method}",
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"{method}: {result.get('error') or 'unknown_error'}")
    return result


def assert_trusted_upload_host(upload_url: str) -> None:
    """The signed upload URL is posted to without our bearer token, so refuse
    any host that is not Slack's own HTTPS files host."""
    parsed = urllib.parse.urlparse(upload_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "slack.com" or host.endswith(SLACK_UPLOAD_HOST_SUFFIX)
    ):
        raise RuntimeError(
            f"files.getUploadURLExternal: refusing untrusted upload host {host!r}"
        )


def post_image_bytes(upload_url: str, filename: str, data: bytes) -> None:
    """POST the raw image bytes to Slack's signed upload URL. Deliberately sends
    no Authorization header and follows no redirects with credentials."""
    boundary = "----CTO-SLACK-UPLOAD-" + secrets.token_hex(16)
    body = bytearray()
    body += f"--{boundary}\r\n".encode("utf-8")
    body += (
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
    ).encode("utf-8")
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += bytes(data)
    body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        upload_url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        response.read()


def files_upload_external(token: str, channel: str, root_ts: str, filename: str,
                          data: bytes) -> str:
    """Attach the local image with the supported Slack external-upload API.

    Simple-upload ``files.upload`` now returns ``method_deprecated``, so this
    follows the supported three-step flow: reserve a signed upload URL, POST the
    bytes to it, then share the finished file into the exact channel/thread. The
    bearer token is only ever sent to slack.com API endpoints. No public link is
    created and no initial_comment is attached, because the guarded text message
    already carries the content."""
    ticket = slack_form_call(
        "files.getUploadURLExternal",
        token,
        {"filename": filename, "length": str(len(data))},
    )
    upload_url = ticket.get("upload_url")
    file_id = ticket.get("file_id")
    if not upload_url or not file_id:
        raise RuntimeError("files.getUploadURLExternal: missing upload_url/file_id")
    assert_trusted_upload_host(str(upload_url))
    post_image_bytes(str(upload_url), filename, data)
    slack_call(
        "files.completeUploadExternal",
        token,
        {
            "files": [{"id": str(file_id), "title": filename}],
            "channel_id": channel,
            "thread_ts": root_ts,
        },
    )
    return str(file_id)


def files_info(token: str, file_id: str, channel: str) -> dict[str, Any]:
    """Authoritative readback: the exact uploaded file must resolve, it must be
    authored by the CTO identity, and it must be shared to the destination. No
    URL is fetched or printed."""
    # files.info takes its arguments from the query string or a form-encoded
    # body; a JSON body is answered with `invalid_arguments` (observed live at
    # the F0C1461012R upload). Post the form shape instead of the JSON shape.
    result = slack_form_call("files.info", token, {"file": file_id})
    file = result.get("file")
    if not isinstance(file, dict) or str(file.get("id")) != str(file_id):
        raise RuntimeError("files.info: file_id mismatch after upload")
    author = file.get("user")
    if author != EXPECTED_USER_ID:
        raise RuntimeError(
            f"files.info: author mismatch expected={EXPECTED_USER_ID} actual={author}"
        )
    shared_to: set[str] = set()
    for key in ("channels", "groups", "ims"):
        for value in file.get(key) or []:
            shared_to.add(str(value))
    if channel not in shared_to:
        raise RuntimeError(
            f"files.info: destination mismatch expected={channel} actual={sorted(shared_to)}"
        )
    return file


def slack_call(method: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
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


_PROTECTED_SPANS = (
    re.compile(r"```[\s\S]*?```"),
    re.compile(r"`[^`\n]*`"),
    re.compile(r"<[^>\n]*>"),
    re.compile(r"(?i)https?://[^\s<>]+"),
    re.compile(r"(?i)\bwww\.[^\s<>]+"),
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\w)(?:~|/|\./|\.\./)[^\s<>]+"),
    re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"),
    re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z0-9_-]+\.)+[A-Za-z]{1,8}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])(?:python|node|bun|deno)\d+(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])(?:[0-9A-Fa-f]{8,}|[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{5,})(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])(?:v?\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?)(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])\d{1,4}[-/]\d{1,2}[-/]\d{1,4}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])\d+\.\d+(?![A-Za-z0-9])"),
)
_PROSE_NUMBER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])((?:body|run|head|published))(\d[0-9A-Fa-f]{2,})(?![A-Za-z0-9_-])"
)
_PROSE_WORD_NUMBER_WORD = re.compile(
    r"(?<![A-Za-z0-9_-])((?:last|next|first|past))(\d{1,2})"
    r"(days?|hours?|minutes?|items?|issues?|runs?|steps?)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def normalize_prose_number_spacing(text: str) -> str:
    """Separate long prose prefixes from attached numeric/hash identifiers.

    Slack markup, code, links, paths, addresses, hashes, versions, dates, times,
    decimals, and short identifiers are protected. The conservative rule only
    handles only the recognized prose prefixes followed by a multi-character
    numeric/hex suffix, making the operation idempotent.
    """
    protected: list[tuple[int, int]] = []
    for pattern in _PROTECTED_SPANS:
        protected.extend(match.span() for match in pattern.finditer(text))
    protected = _merge_ranges(protected)

    def transform(segment: str) -> str:
        segment = _PROSE_NUMBER_TOKEN.sub(r"\1 \2", segment)
        return _PROSE_WORD_NUMBER_WORD.sub(r"\1 \2 \3", segment)

    output: list[str] = []
    cursor = 0
    for start, end in protected:
        if cursor < start:
            output.append(transform(text[cursor:start]))
        output.append(text[start:end])
        cursor = end
    if cursor < len(text):
        output.append(transform(text[cursor:]))
    return "".join(output)


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
    parser.add_argument(
        "--image-file",
        help="Optional local image to attach to the message via the Slack external-upload API",
    )
    args = parser.parse_args()

    token = os.environ.get("SLACK_CTO_BOT_TOKEN")
    if not token:
        raise SystemExit("SLACK_CTO_BOT_TOKEN is not set")

    text = read_text(args)
    if not args.preserve_literal_escapes:
        text = normalize_whitespace_escapes(text)
    text = normalize_prose_number_spacing(text)
    if not text.strip():
        raise SystemExit("Slack message text is empty")

    identity = slack_call("auth.test", token)
    auth_user_id = identity.get("user_id")
    if auth_user_id != EXPECTED_USER_ID:
        raise SystemExit(
            f"cto_identity_mismatch: expected={EXPECTED_USER_ID} actual={auth_user_id}"
        )

    image_result: dict[str, Any] = {}
    image_bytes: bytes | None = None
    if args.image_file:
        image_bytes = read_image_bytes(args.image_file)

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
    if slack_normalize_stored_text(stored.get("text") or "") != text:
        raise SystemExit("stored_text_mismatch")
    if args.thread_ts and str(stored.get("thread_ts")) != args.thread_ts:
        raise SystemExit(
            f"stored_thread_mismatch: expected={args.thread_ts} actual={stored.get('thread_ts')}"
        )

    # Attach the optional image into the same thread after the text is readback.
    if args.image_file:
        assert image_bytes is not None
        filename = Path(args.image_file).name
        file_id = files_upload_external(
            token, args.channel, root_ts, filename, image_bytes
        )
        files_info(token, file_id, args.channel)
        image_result = {
            "image_file_id": file_id,
            "image_name": filename,
            "image_bytes": len(image_bytes),
            "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
            "image_channel": args.channel,
            "image_thread_ts": root_ts,
        }

    print(
        json.dumps(
            {
                "ok": True,
                "channel": args.channel,
                "thread_ts": root_ts,
                "ts": posted_ts,
                "auth_user_id": auth_user_id,
                "stored_user": stored_user,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "image": image_result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
