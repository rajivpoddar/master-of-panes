#!/usr/bin/env python3
"""Canonical Ready Pool claimability classification."""

from __future__ import annotations

import json
import re
import sys
from typing import Any


NON_DISPATCHABLE_CLAIMABLE_TYPES = frozenset(
    {
        "none",
        "no",
        "no-slot",
        "no_slot",
        "not-claimable",
        "not_claimable",
        "unclaimable",
        "pm",
        "pm-direct",
        "pm_direct",
        "direct-main",
        "infra",
        "infrastructure",
        "docs",
        "docs-only",
        "documentation",
        "documentation-only",
        "rajiv",
        "external",
    }
)

READY_POOL_RE = re.compile(
    r"<!--\s*ready-pool:\s*(.*?)-->",
    re.DOTALL | re.IGNORECASE,
)


def ready_pool_value(body: str, field: str) -> str:
    match = READY_POOL_RE.search(body)
    if not match:
        return ""
    field_match = re.search(
        rf"(?mi)^\s*{re.escape(field)}:\s*(.*?)\s*$",
        match.group(1),
    )
    return field_match.group(1).strip() if field_match else ""


def issue_rescope_forward_state(issue: dict[str, Any]) -> str:
    body = str(issue.get("body") or "")
    labels = {str(item.get("name") or "") for item in issue.get("labels", [])}
    claimable_type = ready_pool_value(body, "claimable_slot_type").lower()
    disposition = ready_pool_value(body, "disposition").upper()
    if (
        claimable_type in NON_DISPATCHABLE_CLAIMABLE_TYPES
        or disposition == "TRACKING_ONLY"
        or "pm-nonclaimable:tracking" in labels
        or "sub:pm-owned" in labels
    ):
        return "status:deferred"
    return "status:todo"


def main() -> int:
    if sys.argv[1:] != ["forward-state"]:
        print("usage: issue-claimability.py forward-state", file=sys.stderr)
        return 2
    issue = json.load(sys.stdin)
    print(issue_rescope_forward_state(issue))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
