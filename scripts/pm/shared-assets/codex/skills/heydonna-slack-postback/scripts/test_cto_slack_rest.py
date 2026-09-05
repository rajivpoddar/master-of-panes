#!/usr/bin/env python3
"""Focused regression tests for Slack postback text normalization."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import cto_slack_rest
from cto_slack_rest import normalize_prose_number_spacing, normalize_whitespace_escapes


class NormalizeWhitespaceEscapesTest(unittest.TestCase):
    def test_converts_visible_whitespace_escapes(self) -> None:
        self.assertEqual(
            normalize_whitespace_escapes("first\\nsecond\\r\\nthird\\tfourth"),
            "first\nsecond\nthird\tfourth",
        )

    def test_preserves_existing_real_whitespace(self) -> None:
        text = "first\nsecond\tthird"
        self.assertEqual(normalize_whitespace_escapes(text), text)

    def test_doubled_backslash_keeps_escape_visible(self) -> None:
        self.assertEqual(normalize_whitespace_escapes(r"show \\n literally"), r"show \n literally")

    def test_does_not_decode_unrelated_escapes(self) -> None:
        self.assertEqual(normalize_whitespace_escapes(r"path\value \d+"), r"path\value \d+")


class ProseNumberSpacingTest(unittest.TestCase):
    def test_spaces_known_prose_number_tokens_without_changing_suffix(self) -> None:
        self.assertEqual(
            normalize_prose_number_spacing("body11350 run33945611843 head6038f0df published1e742"),
            "body 11350 run 33945611843 head 6038f0df published 1e742",
        )

    def test_is_idempotent_and_preserves_short_identifiers(self) -> None:
        text = "body11350 S1 P1 AC1 R2 v2 SHA256"
        once = normalize_prose_number_spacing(text)
        self.assertEqual(normalize_prose_number_spacing(once), once)
        self.assertEqual(once, "body 11350 S1 P1 AC1 R2 v2 SHA256")

    def test_protected_markup_and_identifier_spans_are_unchanged(self) -> None:
        text = (
            "`body11350` ```run33945611843``` <@U123> <date^123^2026-09-05> "
            "https://example.test/body11350 user@example.com /tmp/body11350 "
            "abc123456789 550e8400-e29b-41d4-a716-446655440000 1.23 2026-09-05 12:30"
        )
        self.assertEqual(normalize_prose_number_spacing(text), text)

    def test_sender_uses_normalized_text_for_payload_and_hash(self) -> None:
        posted_payloads: list[dict[str, str]] = []

        def fake_slack_call(method: str, token: str, payload: dict[str, str] | None = None) -> dict:
            if method == "auth.test":
                return {"ok": True, "user_id": cto_slack_rest.EXPECTED_USER_ID}
            posted_payloads.append(payload or {})
            return {"ok": True, "ts": "123.456"}

        class ReplyResponse:
            def __enter__(self) -> "ReplyResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {"ok": True, "messages": [{"ts": "123.456", "user": cto_slack_rest.EXPECTED_USER_ID, "text": "body 11350"}]}
                ).encode()

        def invoke(argv: list[str], stdin: io.StringIO | None = None) -> dict:
            stdout = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SLACK_CTO_BOT_TOKEN": "token"}, clear=False),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(sys, "stdin", stdin or io.StringIO()),
                mock.patch.object(cto_slack_rest, "slack_call", side_effect=fake_slack_call),
                mock.patch.object(cto_slack_rest.urllib.request, "urlopen", return_value=ReplyResponse()),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(cto_slack_rest.main(), 0)
            return json.loads(stdout.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            text_file = Path(directory) / "message.txt"
            text_file.write_text("body11350", encoding="utf-8")
            results = [
                invoke(["cto_slack_rest.py", "--channel", "C123", "--text", "body11350"]),
                invoke(["cto_slack_rest.py", "--channel", "C123", "--text-file", str(text_file)]),
                invoke(["cto_slack_rest.py", "--channel", "C123"], io.StringIO("body11350")),
            ]

        self.assertEqual(
            posted_payloads,
            [
                {"channel": "C123", "text": "body 11350"},
                {"channel": "C123", "text": "body 11350"},
                {"channel": "C123", "text": "body 11350"},
            ],
        )
        self.assertEqual(
            [result["text_sha256"] for result in results],
            [hashlib.sha256(b"body 11350").hexdigest()] * 3,
        )


if __name__ == "__main__":
    unittest.main()
