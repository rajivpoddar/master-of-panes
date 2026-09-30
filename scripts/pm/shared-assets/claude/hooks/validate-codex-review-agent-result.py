#!/usr/bin/env python3
"""Reject silent fallback results for custom Codex reviewer agents.

Claude Code may resolve an unavailable custom Agent type to general-purpose.
That fallback must never be treated as a Codex review because it does not run
the companion or produce a canonical marker.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


REVIEWERS = {
    "codex-arch-reviewer",
    "codex-code-reviewer",
    "codex-diag-reviewer",
    "codex-plan-reviewer",
    "codex-qa-reviewer",
}
PM_PANE_CWD = "/Users/rajiv/Downloads/projects/heydonna-app"
PM_SLOT_HOOK_URL = "http://127.0.0.1:3100/hooks/slot/0"
PM_REVIEW_WINDOW_SECONDS = 30 * 60


def emit_feedback(message: str) -> None:
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": message,
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": message,
                },
            }
        )
    )


def response_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(response_text(item) for item in value)
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("text", "content", "stdout", "output"):
            if key in value:
                parts.append(response_text(value[key]))
        return "\n".join(part for part in parts if part)
    return ""


def marker_target(reviewer: str, prompt: str) -> tuple[Path | None, str | None]:
    pr_match = re.search(r"\bPR\s*#?(\d+)\b", prompt, re.IGNORECASE)
    issue_match = re.search(r"\bissue\s*#?(\d+)\b", prompt, re.IGNORECASE)
    if reviewer == "codex-code-reviewer":
        if pr_match:
            return Path(f"/tmp/codex-app-code-review-{pr_match.group(1)}.txt"), pr_match.group(1)
        if issue_match:
            return Path(f"/tmp/codex-app-code-review-{issue_match.group(1)}.txt"), None
    if reviewer == "codex-qa-reviewer" and pr_match:
        return Path(f"/tmp/codex-app-qa-review-{pr_match.group(1)}.txt"), pr_match.group(1)
    if reviewer == "codex-plan-reviewer" and issue_match:
        return Path(f"/tmp/codex-app-plan-review-{issue_match.group(1)}.txt"), None
    return None, None


def field(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\s*([^\n]+?)\s*$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else ""


def pm_arch_review_event(payload: dict[str, Any], *, now: int | None = None) -> dict[str, Any] | None:
    """Build a slot-0 event only for the PM's completed, issue-bound arch review."""
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Agent":
        return None
    if Path(str(payload.get("cwd") or "")).resolve() != Path(PM_PANE_CWD).resolve():
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or tool_input.get("subagent_type") != "codex-arch-reviewer":
        return None
    prompt = "\n".join(
        str(value) for value in (tool_input.get("prompt"), tool_input.get("description")) if isinstance(value, str)
    )
    result = payload.get("tool_response") or payload.get("tool_result") or payload.get("tool_output") or {}
    if not isinstance(result, dict) or result.get("agentType") != "codex-arch-reviewer":
        return None
    output = response_text(result)
    marker_match = re.search(
        r"CANONICAL_MARKER:\s*(/tmp/codex-review-companion/arch-issue-[A-Za-z0-9._-]+-\d+\.md)",
        output,
        re.IGNORECASE,
    )
    if not marker_match:
        return None

    marker = Path(marker_match.group(1))
    try:
        if marker.is_symlink() or marker.resolve().parent != Path("/tmp/codex-review-companion").resolve():
            return None
        marker_text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    issue = field(marker_text, "ISSUE").lstrip("#").strip()
    baseline = field(marker_text, "BASELINE_SHA").lower()
    timestamp = field(marker_text, "TIMESTAMP")
    if (
        field(marker_text, "MARKER_PROVENANCE").upper() != "CODEX-REVIEW-COMPANION"
        or field(marker_text, "TYPE").lower() != "arch-review"
        or not issue
        or not re.fullmatch(r"[0-9a-f]{40}", baseline)
        or issue.casefold() not in prompt.casefold()
        or baseline not in prompt.casefold()
        or not re.fullmatch(r"\d{10}", timestamp)
    ):
        return None
    completed_at = int(timestamp)
    observed_now = int(time.time()) if now is None else now
    if completed_at > observed_now or observed_now - completed_at > PM_REVIEW_WINDOW_SECONDS:
        return None

    # Keep the event small and private: preserve the parent-session identity,
    # exact issue/base binding, and marker path without forwarding the full prompt.
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "cwd": PM_PANE_CWD,
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "codex-arch-reviewer",
            "prompt": (
                f"codex-arch-reviewer issue {issue} baseline_sha {baseline}; "
                f"codex-app-arch-review validated marker={marker} timestamp={timestamp}"
            ),
        },
    }


def post_pm_arch_review(payload: dict[str, Any]) -> bool:
    event = pm_arch_review_event(payload)
    if event is None:
        return False
    request = urllib.request.Request(
        PM_SLOT_HOOK_URL,
        data=json.dumps(event).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return 200 <= response.status < 300
    except (OSError, TimeoutError):
        return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    cwd = str(payload.get("cwd") or os.getcwd())
    if Path(cwd).resolve() == Path(PM_PANE_CWD).resolve():
        post_pm_arch_review(payload)
        return 0
    if not re.search(r"/heydonna-app-300[1-6](?:/|$)", cwd):
        return 0

    tool_input = payload.get("tool_input") or {}
    requested = str(tool_input.get("agentType") or tool_input.get("subagent_type") or "")
    if requested not in REVIEWERS:
        return 0

    result = payload.get("tool_response") or payload.get("tool_result") or {}
    resolved = str(result.get("agentType") or "") if isinstance(result, dict) else ""
    if resolved != requested:
        emit_feedback(
            "[CODEX_REVIEW_AGENT_FALLBACK_BLOCKED] "
            f"Requested Agent type {requested!r}, but Claude resolved {resolved or 'unknown'!r}. "
            "This is not a Codex companion review and its verdict/marker packet is invalid. "
            "Run the foreground codex-review-companion command from the slot checkout, then read "
            "the canonical /tmp/codex-app-*-review marker. Do not report this Agent result to PM."
        )
        return 0

    prompt = str(tool_input.get("prompt") or "")
    marker, pr_number = marker_target(requested, prompt)
    if marker is None:
        return 0
    if not marker.is_file():
        emit_feedback(
            "[CODEX_REVIEW_MARKER_MISSING] "
            f"{requested} completed without canonical marker {marker}. "
            "Do not hand-author marker fields or report a verdict to PM; rerun the companion."
        )
        return 0

    marker_text = marker.read_text(errors="replace")
    provenance = field(marker_text, "MARKER_PROVENANCE").upper()
    if provenance != "CODEX-REVIEW-COMPANION":
        emit_feedback(
            "[CODEX_REVIEW_MARKER_PROVENANCE_INVALID] "
            f"{marker} has MARKER_PROVENANCE={provenance or 'missing'}. "
            "Only a codex-review-companion marker may be reported to PM."
        )
        return 0

    if pr_number:
        marker_head = field(marker_text, "HEAD_SHA")
        try:
            checkout_head = subprocess.check_output(
                ["git", "-C", cwd, "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).strip()
            live_head = subprocess.check_output(
                ["/opt/homebrew/bin/gh", "pr", "view", pr_number, "--json", "headRefOid", "--jq", ".headRefOid"],
                cwd=cwd,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            emit_feedback(
                "[CODEX_REVIEW_HEAD_UNVERIFIED] Could not resolve checkout/live PR head after "
                f"{requested}. Do not report the verdict to PM; verify PR #{pr_number} and rerun."
            )
            return 0
        if not marker_head or marker_head.lower() != live_head.lower() or checkout_head.lower() != live_head.lower():
            emit_feedback(
                "[CODEX_REVIEW_MARKER_HEAD_MISMATCH] "
                f"marker={marker_head or 'missing'} checkout={checkout_head or 'missing'} live_pr={live_head or 'missing'}. "
                "The reviewer result is stale or ran in the wrong checkout. Rerun the companion on the live PR head "
                "before any PM verdict report."
            )
            return 0

    output = response_text(result)
    if str(marker) not in output:
        emit_feedback(
            "[CODEX_REVIEW_MARKER_NOT_READ] "
            f"{requested} did not return canonical marker path {marker}. "
            "Read the marker and report its verdict fields exactly; do not synthesize a Marker Packet."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
