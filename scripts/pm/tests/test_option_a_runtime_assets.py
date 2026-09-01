from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
MANIFEST = SHARED / "manifest.json"


def test_option_a_clients_are_mapped_directly_and_have_expected_modes() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = {entry["source_path"]: entry for entry in manifest["entries"]}
    expected = {
        "claude/scripts/sakshi-heartbeat.sh": "/Users/rajiv/.claude/scripts/sakshi-heartbeat.sh",
        "claude/scripts/backlog-triage.py": "/Users/rajiv/.claude/scripts/backlog-triage.py",
        "claude/scripts/heartbeat-session-age-clear.py": "/Users/rajiv/.claude/scripts/heartbeat-session-age-clear.py",
        "claude/scripts/mop-clear-slot.sh": "/Users/rajiv/.claude/scripts/mop-clear-slot.sh",
    }
    assert manifest["inventory"]["selected_count"] == len(manifest["entries"])
    for source_path, target in expected.items():
        source = SHARED / source_path
        entry = entries[source_path]
        assert entry["canonical_target"] == target
        assert entry["mode"] == 0o755
        assert stat.S_IMODE(source.stat().st_mode) == 0o755
        assert hashlib.sha256(source.read_bytes()).hexdigest() == entry["sha256"]
        contents = source.read_text(encoding="utf-8").lower()
        assert "pm-operator" not in contents
        assert "kernel-assignment" not in contents


def test_clients_have_no_cli_or_effect_recursion() -> None:
    launcher = (SHARED / "claude/scripts/sakshi-heartbeat.sh").read_text(encoding="utf-8")
    backlog = (SHARED / "claude/scripts/backlog-triage.py").read_text(encoding="utf-8")
    session = (SHARED / "claude/scripts/heartbeat-session-age-clear.py").read_text(encoding="utf-8")
    release = (SHARED / "claude/scripts/mop-clear-slot.sh").read_text(encoding="utf-8")
    assert 'exec python3 /Users/rajiv/.claude/scripts/sakshi-heartbeat.py "$@"' in launcher
    assert "--ready-pool-audit" in backlog and "runpy.run_path" in backlog
    assert "urllib.request" in session and "subprocess" not in session and "tmux" not in session
    assert "/slots/$slot/release" in release and "curl" in release
