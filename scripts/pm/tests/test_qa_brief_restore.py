from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SKILL = SHARED / "claude" / "skills" / "qa-brief" / "SKILL.md"
MANIFEST = SHARED / "manifest.json"


def test_historical_qa_brief_is_manifest_bound_and_read_only() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["entries"] if item["source_path"] == "claude/skills/qa-brief/SKILL.md")

    assert entry["canonical_target"] == "/Users/rajiv/.claude/skills/qa-brief/SKILL.md"
    assert entry["mode"] == 0o644
    assert entry["sha256"] == hashlib.sha256(SKILL.read_bytes()).hexdigest()
    assert stat.S_IMODE(SKILL.stat().st_mode) == 0o644
    text = SKILL.read_text(encoding="utf-8")
    for required in ("exact-head", "customer artifact", "qa-artifact-evidence", "unconditional", "no PM-side override"):
        assert required in text
    for forbidden in ("ProofShot", "PM Operator", "kernel-assignment-boundary", "pm-transition"):
        assert forbidden not in text
