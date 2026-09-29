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

    def test_spaces_conservative_word_number_word_prose(self) -> None:
        text = "last7days next2steps first3issues past1hour"
        expected = "last 7 days next 2 steps first 3 issues past 1 hour"
        self.assertEqual(normalize_prose_number_spacing(text), expected)
        self.assertEqual(normalize_prose_number_spacing(expected), expected)
        for value in ("last7days_backup", "next10steps-v2"):
            self.assertEqual(normalize_prose_number_spacing(value), value)

    def test_protected_markup_and_identifier_spans_are_unchanged(self) -> None:
        text = (
            "`body11350` ```run33945611843``` <@U123> <date^123^2026-09-05> "
            "https://example.test/body11350 user@example.com /tmp/body11350 "
            "scripts/run339.py feature/body11350 release20260905.json python311 "
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
                posted_text = posted_payloads[-1]["text"]
                return json.dumps(
                    {"ok": True, "messages": [{"ts": "123.456", "user": cto_slack_rest.EXPECTED_USER_ID, "text": posted_text}]}
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
            prose_file = Path(directory) / "prose.txt"
            text_file.write_text("body11350", encoding="utf-8")
            prose_file.write_text("last7days", encoding="utf-8")
            results = [
                invoke(["cto_slack_rest.py", "--channel", "C123", "--text", "body11350"]),
                invoke(["cto_slack_rest.py", "--channel", "C123", "--text-file", str(text_file)]),
                invoke(["cto_slack_rest.py", "--channel", "C123"], io.StringIO("body11350")),
                invoke(["cto_slack_rest.py", "--channel", "C123", "--text", "last7days"]),
                invoke(["cto_slack_rest.py", "--channel", "C123", "--text-file", str(prose_file)]),
                invoke(["cto_slack_rest.py", "--channel", "C123"], io.StringIO("last7days")),
            ]

        self.assertEqual(
            posted_payloads,
            [
                {"channel": "C123", "text": "body 11350"},
                {"channel": "C123", "text": "body 11350"},
                {"channel": "C123", "text": "body 11350"},
                {"channel": "C123", "text": "last 7 days"},
                {"channel": "C123", "text": "last 7 days"},
                {"channel": "C123", "text": "last 7 days"},
            ],
        )
        self.assertEqual(
            [result["text_sha256"] for result in results],
            [hashlib.sha256(b"body 11350").hexdigest()] * 3
            + [hashlib.sha256(b"last 7 days").hexdigest()] * 3,
        )

    def test_writer_preserves_ambiguous_tokens_in_payload_and_hash(self) -> None:
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
                    {
                        "ok": True,
                        "messages": [
                            {
                                "ts": "123.456",
                                "user": cto_slack_rest.EXPECTED_USER_ID,
                                "text": posted_payloads[-1]["text"],
                            }
                        ],
                    }
                ).encode()

        def invoke(text: str) -> dict:
            stdout = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"SLACK_CTO_BOT_TOKEN": "token"}, clear=False),
                mock.patch.object(sys, "argv", ["cto_slack_rest.py", "--channel", "C123", "--text", text]),
                mock.patch.object(cto_slack_rest, "slack_call", side_effect=fake_slack_call),
                mock.patch.object(cto_slack_rest.urllib.request, "urlopen", return_value=ReplyResponse()),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(cto_slack_rest.main(), 0)
            return json.loads(stdout.getvalue())

        values = [
            "scripts/run339.py",
            "feature/body11350",
            "release20260905.json",
            "python311",
            "last_run339",
            "run339_result",
            "run339-fix",
            "head6038f0df_backup",
        ]
        results = [invoke(value) for value in values]
        self.assertEqual([payload["text"] for payload in posted_payloads], values)
        self.assertEqual(
            [result["text_sha256"] for result in results],
            [hashlib.sha256(value.encode()).hexdigest() for value in values],
        )


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class ImageAttachmentTest(unittest.TestCase):
    """Focused RED/GREEN proof for the optional guarded image attachment."""

    EXPECTED = cto_slack_rest.EXPECTED_USER_ID
    IMAGE_NAME = "ac1-ac2-signup-paused.png"
    UPLOAD_PREFIX = "https://files.slack.com/upload/"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.image_path = Path(self._tmp.name) / self.IMAGE_NAME
        self.image_bytes = b"\x89PNG\r\n\x1a\n" + b"safe-visual-proof" * 4
        self.image_path.write_bytes(self.image_bytes)

    def _image_argv(self, *extra: str) -> list[str]:
        return [
            "cto_slack_rest.py", "--channel", "C123",
            "--text", "body11350", "--image-file", str(self.image_path), *extra,
        ]

    def _run(self, argv, *, auth_user=None, ticket=None, complete_error=None,
             info=None, stdin=None):
        record = {"slack": [], "form": [], "http": [], "posted": None}

        def fake_slack_call(method, token, payload=None):
            record["slack"].append((method, payload))
            if method == "auth.test":
                return {"ok": True, "user_id": auth_user or self.EXPECTED}
            if method == "chat.postMessage":
                record["posted"] = dict(payload)
                return {"ok": True, "ts": "111.222"}
            if method == "files.completeUploadExternal":
                if complete_error is not None:
                    raise complete_error
                return {"ok": True, "files": list(payload["files"])}
            # files.info must NOT arrive here: it takes form/query arguments, so
            # a JSON body is answered by Slack with invalid_arguments.
            raise AssertionError(f"unexpected slack_call: {method}")

        def fake_form_call(method, token, fields):
            record["form"].append((method, dict(fields)))
            if method == "files.getUploadURLExternal":
                if ticket is not None:
                    return ticket
                return {"ok": True, "upload_url": self.UPLOAD_PREFIX + "v1/TICKET",
                        "file_id": "F123456"}
            if method == "files.info":
                if info is not None:
                    return {"ok": True, "file": info}
                return {"ok": True, "file": {
                    "id": fields["file"], "user": self.EXPECTED,
                    "channels": ["C123"],
                }}
            raise AssertionError(f"unexpected slack_form_call: {method}")

        def fake_urlopen(request, timeout=None):
            url = request.full_url
            record["http"].append((url, dict(request.header_items()), request.data))
            if url.startswith(self.UPLOAD_PREFIX):
                return _FakeResponse(b"")
            posted = record["posted"] or {}
            message = {"ts": "111.222", "user": self.EXPECTED, "text": posted.get("text")}
            if posted.get("thread_ts"):
                message["thread_ts"] = posted["thread_ts"]
            return _FakeResponse(json.dumps({"ok": True, "messages": [message]}).encode())

        stdout = io.StringIO()
        error = None
        rc = None
        with (
            mock.patch.dict(os.environ, {"SLACK_CTO_BOT_TOKEN": "token"}, clear=False),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(sys, "stdin", stdin or io.StringIO()),
            mock.patch.object(cto_slack_rest, "slack_call", side_effect=fake_slack_call),
            mock.patch.object(cto_slack_rest, "slack_form_call",
                              side_effect=fake_form_call, create=True),
            mock.patch.object(cto_slack_rest.urllib.request, "urlopen", side_effect=fake_urlopen),
            contextlib.redirect_stdout(stdout),
        ):
            try:
                rc = cto_slack_rest.main()
            except BaseException as exc:  # SystemExit is not an Exception subclass
                error = exc
        return rc, stdout.getvalue(), error, record

    def _uploads(self, record):
        return [entry for entry in record["http"] if entry[0].startswith(self.UPLOAD_PREFIX)]

    def _completes(self, record):
        return [payload for method, payload in record["slack"]
                if method == "files.completeUploadExternal"]

    def test_image_success_uses_external_upload_and_reads_back(self) -> None:
        rc, out, error, record = self._run(self._image_argv("--thread-ts", "999.001"))
        self.assertIsNone(error)
        self.assertEqual(rc, 0)
        result = json.loads(out)
        self.assertEqual(result["thread_ts"], "999.001")
        self.assertEqual(result["image"], {
            "image_file_id": "F123456",
            "image_name": self.IMAGE_NAME,
            "image_bytes": len(self.image_bytes),
            "image_sha256": hashlib.sha256(self.image_bytes).hexdigest(),
            "image_channel": "C123",
            "image_thread_ts": "999.001",
        })

        # Step 1: reserve the signed upload URL with the exact filename/length.
        # Step 4: read the file back. Both go through the form-encoded path;
        # files.info is never sent as a JSON body (that shape is rejected with
        # invalid_arguments).
        self.assertEqual([m for m, _ in record["form"]],
                         ["files.getUploadURLExternal", "files.info"])
        self.assertEqual(record["form"][0][1], {
            "filename": self.IMAGE_NAME, "length": str(len(self.image_bytes)),
        })
        self.assertEqual(record["form"][1], ("files.info", {"file": "F123456"}))

        # Step 2: bytes go to Slack's signed host with NO bearer token.
        uploads = self._uploads(record)
        self.assertEqual(len(uploads), 1)
        self.assertNotIn("Authorization", uploads[0][1])
        self.assertIn(self.image_bytes, uploads[0][2])

        # Step 3: shared into the exact channel/thread, no public link, no extra comment.
        completes = self._completes(record)
        self.assertEqual(len(completes), 1)
        self.assertEqual(completes[0]["channel_id"], "C123")
        self.assertEqual(completes[0]["thread_ts"], "999.001")
        self.assertEqual(completes[0]["files"],
                         [{"id": "F123456", "title": self.IMAGE_NAME}])
        self.assertNotIn("initial_comment", completes[0])
        self.assertEqual([m for m, _ in record["slack"]], [
            "auth.test", "chat.postMessage", "files.completeUploadExternal",
        ])

    def test_image_without_thread_attaches_to_the_posted_message(self) -> None:
        rc, out, error, record = self._run(self._image_argv())
        self.assertIsNone(error)
        self.assertEqual(rc, 0)
        self.assertEqual(self._completes(record)[0]["thread_ts"], "111.222")
        self.assertEqual(json.loads(out)["image"]["image_thread_ts"], "111.222")

    def test_untrusted_upload_host_is_refused_before_any_byte_is_posted(self) -> None:
        rc, out, error, record = self._run(
            self._image_argv(),
            ticket={"ok": True, "upload_url": "https://evil.example.com/upload/v1/x",
                    "file_id": "F1"},
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("untrusted upload host", str(error))
        self.assertEqual(self._uploads(record), [])
        # Only the guarded text readback reached slack.com; no bytes went anywhere else.
        self.assertEqual(
            [url for url, _, _ in record["http"]],
            ["https://slack.com/api/conversations.replies?channel=C123&ts=111.222&limit=100"],
        )
        self.assertEqual(self._completes(record), [])

    def test_wrong_identity_has_zero_slack_effects(self) -> None:
        rc, out, error, record = self._run(self._image_argv(), auth_user="U0NOTTHECT0")
        self.assertIsInstance(error, SystemExit)
        self.assertIn("cto_identity_mismatch", str(error))
        self.assertIsNone(record["posted"])
        self.assertEqual(record["form"], [])
        self.assertEqual(record["http"], [])

    def test_missing_image_fails_closed_before_any_write(self) -> None:
        missing = Path(self._tmp.name) / "does-not-exist.png"
        argv = ["cto_slack_rest.py", "--channel", "C123", "--text", "body11350",
                "--image-file", str(missing)]
        rc, out, error, record = self._run(argv)
        self.assertIsInstance(error, SystemExit)
        self.assertIn("image_file_unreadable", str(error))
        self.assertIsNone(record["posted"])
        self.assertEqual(record["form"], [])
        self.assertEqual(record["http"], [])

    def test_empty_image_fails_closed(self) -> None:
        empty = Path(self._tmp.name) / "empty.png"
        empty.write_bytes(b"")
        argv = ["cto_slack_rest.py", "--channel", "C123", "--text", "body11350",
                "--image-file", str(empty)]
        rc, out, error, record = self._run(argv)
        self.assertIsInstance(error, SystemExit)
        self.assertIn("image_file_empty", str(error))
        self.assertIsNone(record["posted"])

    def test_oversized_image_fails_closed(self) -> None:
        big = Path(self._tmp.name) / "big.png"
        big.write_bytes(b"x" * 64)
        argv = ["cto_slack_rest.py", "--channel", "C123", "--text", "body11350",
                "--image-file", str(big)]
        with mock.patch.object(cto_slack_rest, "MAX_UPLOAD_BYTES", 32):
            rc, out, error, record = self._run(argv)
        self.assertIsInstance(error, SystemExit)
        self.assertIn("image_file_too_large", str(error))
        self.assertIsNone(record["posted"])

    def test_share_failure_is_not_blindly_replayed(self) -> None:
        rc, out, error, record = self._run(
            self._image_argv("--thread-ts", "999.001"),
            complete_error=RuntimeError("files.completeUploadExternal: ratelimited"),
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual([m for m, _ in record["form"]], ["files.getUploadURLExternal"])
        self.assertEqual(len(self._uploads(record)), 1)
        self.assertEqual(len(self._completes(record)), 1)
        # The readback never ran, so no files.info call of either shape exists.
        self.assertEqual([m for m, _ in record["slack"] if m == "files.info"], [])
        self.assertNotIn("files.info", [m for m, _ in record["form"]])

    def test_readback_mismatch_fails_closed_without_replay(self) -> None:
        rc, out, error, record = self._run(
            self._image_argv("--thread-ts", "999.001"),
            info={"id": "F999999", "user": self.EXPECTED, "channels": ["C123"]},
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("file_id mismatch", str(error))
        self.assertEqual([m for m, _ in record["form"]],
                         ["files.getUploadURLExternal", "files.info"])
        self.assertEqual(len(self._uploads(record)), 1)
        self.assertEqual(len(self._completes(record)), 1)

    def test_readback_author_mismatch_fails_closed(self) -> None:
        rc, out, error, record = self._run(
            self._image_argv("--thread-ts", "999.001"),
            info={"id": "F123456", "user": "U0SOMEONEELSE", "channels": ["C123"]},
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("author mismatch", str(error))

    def test_readback_destination_mismatch_fails_closed(self) -> None:
        rc, out, error, record = self._run(
            self._image_argv("--thread-ts", "999.001"),
            info={"id": "F123456", "user": self.EXPECTED, "channels": ["C999"]},
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("destination mismatch", str(error))

    def test_files_info_uses_form_encoded_call_not_json(self) -> None:
        """files.info takes form/query arguments. A JSON body is answered with
        invalid_arguments (the live F0C1461012R failure), so the readback must
        go through slack_form_call and must never touch the JSON path."""
        calls: list[tuple[str, dict[str, str]]] = []

        def form(method: str, token: str, fields: dict[str, str]) -> dict:
            calls.append((method, dict(fields)))
            return {"ok": True, "file": {"id": "F123456", "user": self.EXPECTED,
                                         "channels": ["C123"]}}

        def json_call(method: str, token: str, payload=None) -> dict:
            raise AssertionError(f"files.info must not use the JSON path: {method}")

        with (
            mock.patch.object(cto_slack_rest, "slack_form_call", side_effect=form),
            mock.patch.object(cto_slack_rest, "slack_call", side_effect=json_call),
        ):
            file = cto_slack_rest.files_info("token", "F123456", "C123")
        self.assertEqual(calls, [("files.info", {"file": "F123456"})])
        self.assertEqual(str(file.get("id")), "F123456")

    def test_text_only_path_is_unchanged_and_uses_no_upload(self) -> None:
        rc, out, error, record = self._run(
            ["cto_slack_rest.py", "--channel", "C123", "--text", "body11350"]
        )
        self.assertIsNone(error)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["image"], {})
        self.assertEqual(record["form"], [])
        self.assertEqual(self._uploads(record), [])
        self.assertEqual([m for m, _ in record["slack"]], ["auth.test", "chat.postMessage"])


if __name__ == "__main__":
    unittest.main()
