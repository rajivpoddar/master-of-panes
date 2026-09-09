#!/usr/bin/env python3
"""Compatibility no-op for the retired Codex-to-PM reporter gate.

The path remains registered so existing hook configurations stay valid.  The
retired reporter requirement is no longer enforced here; unrelated review,
truthfulness, and lifecycle protections remain owned by their current paths.
"""

from __future__ import annotations

import sys


def main() -> int:
    # Consume the hook payload without classifying its text or writing state.
    try:
        sys.stdin.buffer.read()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
