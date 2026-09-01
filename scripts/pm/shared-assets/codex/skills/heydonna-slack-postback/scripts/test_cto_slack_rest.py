#!/usr/bin/env python3
"""Focused regression tests for Slack postback text normalization."""

from __future__ import annotations

import unittest

from cto_slack_rest import normalize_whitespace_escapes


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


if __name__ == "__main__":
    unittest.main()
