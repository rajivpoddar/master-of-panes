#!/usr/bin/env python3
"""Fail closed when Slack text offers unsupported transcript/job splitting."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SAFE_POLICY_LANGUAGE = re.compile(
    r"\b(?:do\s+not|don['’]t|never|must\s+not|cannot|can['’]t|"
    r"not\s+supported|unsupported|forbidden|prohibit(?:ed)?|withdraw(?:n)?|"
    r"cancel(?:led|ed)?|avoid)\b",
    re.IGNORECASE,
)

CUSTOMER_ARTIFACT = (
    r"(?:transcript|job|file|audio|project)"
    r"(?!\s+(?:parser|component|module|test|issue|pr|contract|implementation|code)\b)"
)

UNSUPPORTED_SPLIT = re.compile(
    rf"(?:"
    rf"\b(?:split|splitting|divide|dividing|break|breaking)\b.{{0,90}}\b"
    rf"{CUSTOMER_ARTIFACT}\b.{{0,90}}\b(?:into|section|part|chunk|smaller)\b"
    rf"|\b{CUSTOMER_ARTIFACT}\b.{{0,70}}\b(?:split|divide)\b.{{0,70}}"
    rf"\b(?:section|part|chunk|smaller)\b"
    rf"|\b(?:set\s+up|arrange|prepare|create|working\s+out)\b.{{0,80}}"
    rf"\b(?:the\s+)?split\b"
    rf"|\b(?:way\s+to\s+)?split\s+this\b.{{0,100}}"
    rf"\b(?:edit|audio|section|smaller|work|safe|reliable)\w*\b"
    rf")",
    re.IGNORECASE | re.DOTALL,
)


def block_text(blocks: Any) -> str:
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"text", "fallback", "initial_option"} and isinstance(item, str):
                    found.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(blocks)
    return "\n".join(found)


def load_blocks(raw: str) -> str:
    if not raw:
        return ""
    try:
        if raw.startswith("@"):
            raw = Path(raw[1:]).read_text(encoding="utf-8")
        return block_text(json.loads(raw))
    except (OSError, ValueError, TypeError):
        return raw


def candidate_sentences(text: str) -> list[str]:
    compact = re.sub(r"[ \t]+", " ", text)
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|[\r\n]+", compact) if part.strip()]


def unsafe_sentence(text: str) -> str | None:
    for sentence in candidate_sentences(text):
        if SAFE_POLICY_LANGUAGE.search(sentence):
            continue
        if UNSUPPORTED_SPLIT.search(sentence):
            return sentence
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", default="")
    parser.add_argument("--blocks", default="")
    args = parser.parse_args()

    visible_text = "\n".join(part for part in (args.message, load_blocks(args.blocks)) if part)
    matched = unsafe_sentence(visible_text)
    if matched is None:
        return 0

    print(
        "[UNSUPPORTED_TRANSCRIPT_SPLIT_BLOCKED]\n"
        "This Slack message appears to offer, promise, or arrange splitting a customer "
        "transcript/job/file as a workaround. HeyDonna does not support that operation. "
        "It can break audio/text synchronization, version history, editor state, and "
        "export continuity. Do not send the message and do not perform the split.\n\n"
        "Required action:\n"
        "1. Diagnose and fix the actual runtime failure.\n"
        "2. Communicate only verified, supported mitigations.\n"
        "3. If a customer was already promised a split, withdraw the promise in the same "
        "thread and escalate it to Rajiv.\n\n"
        "A policy correction is allowed when it is explicit, for example: "
        "'We do not support splitting transcripts; do not split this file.'\n"
        f"Matched sentence: {matched[:500]}",
        file=sys.stderr,
    )
    return 23


if __name__ == "__main__":
    raise SystemExit(main())
