"""Focused proof for the missing-@ mention guard in slack-send.sh.

Rajiv directive 2026-09-19: `<U...>` written in angle brackets without the
at-sign does not resolve; block it, allow the canonical `<@U...>` form.

The guard sits before the curl POST. Executing the full script in proof
would POST to live Slack and inject routed text into live MoP panes via
/api/slack-route, so the harness executes ONLY the exact guard bytes
(extracted from the canonical source by marker) with a stub PAYLOAD — zero
network, zero live effects. A byte-identity check plus placement ordering
prove the landed file carries the verified behavior in the effective
position.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SOURCE = SHARED / "claude" / "skills" / "slack-message" / "scripts" / "slack-send.sh"
MANIFEST = SHARED / "manifest.json"
INSTALLER_PATH = ROOT / "scripts" / "install-release.py"
SPEC = importlib.util.spec_from_file_location("install_release_mention_guard", INSTALLER_PATH)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)

GUARD_START = "# Pre-send mention-ID guard (2026-08-12"
LIVE_FILE = Path("/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/slack-message-real/scripts/slack-send.sh")


def _guard_snippet() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    start = text.index(GUARD_START)
    end = text.index("RESULT=$(curl", start)
    return text[start:end]


def _decision(payload: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(prefix="mention-guard-") as tmp:
        probe = Path(tmp) / "probe.sh"
        probe.write_text(
            "#!/bin/bash\nset -euo pipefail\nPAYLOAD=${1:-}\n" + _guard_snippet() + 'echo "ALLOW"\n',
            encoding="utf-8",
        )
        completed = subprocess.run(
            ["bash", str(probe), payload], capture_output=True, text=True, timeout=15
        )
    return completed.returncode, completed.stdout + completed.stderr


def test_malformed_angle_bracket_id_without_at_is_blocked() -> None:
    code, output = _decision("ping <U0BNFGX2UAX> status green")
    assert code != 0
    assert "WITHOUT the '@'" in output


def test_canonical_at_form_is_allowed() -> None:
    code, output = _decision("ping <@U0BNFGX2UAX> status green")
    assert code == 0, output
    assert output.strip().endswith("ALLOW")


def test_non_mention_markup_still_passes() -> None:
    for payload in (
        "ping <!channel> deploy done",
        "see <#C0ALZJHGE49|heydonna-dev> thread",
        "plain status update, no ids",
    ):
        code, output = _decision(payload)
        assert code == 0, (payload, output)


def test_landed_guard_is_byte_identical_to_verified_live() -> None:
    live = LIVE_FILE.read_text(encoding="utf-8")
    start = live.index(GUARD_START)
    end = live.index("RESULT=$(curl", start)
    assert _guard_snippet() == live[start:end]


def test_guard_sits_before_post_and_aborts_send() -> None:
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    guard_exit = next(
        index for index, line in enumerate(lines)
        if line.strip() == "exit 1" and "Missing-@ guard" in "\n".join(lines[max(0, index - 14):index])
    )
    post = next(index for index, line in enumerate(lines) if "RESULT=$(curl" in line)
    assert guard_exit < post
    assert "set -euo pipefail" in lines


def test_manifest_covers_sender_with_parity() -> None:
    manifest = INSTALLER._load_shared_manifest(ROOT)
    by_path = {item["source_path"]: item for item in manifest["entries"]}
    relative = "claude/skills/slack-message/scripts/slack-send.sh"
    assert relative in by_path, relative
    entry = by_path[relative]
    assert entry["canonical_target"] == "/Users/rajiv/.claude/skills/slack-message/scripts/slack-send.sh"
    assert entry["dependency_status"] == "closed"
    assert entry["dependencies"] == []
    assert entry["sha256"] == hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert entry["mode"] == (os.stat(SOURCE).st_mode & 0o777) == 0o755
    assert manifest["inventory"]["selected_count"] == len(manifest["entries"])
