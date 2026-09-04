import hashlib
import json
import stat
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ASSETS = ROOT / "scripts" / "pm" / "shared-assets"
MANIFEST = ASSETS / "manifest.json"


def _text(name: str) -> str:
    return (ASSETS / "claude" / "skills" / name / "SKILL.md").read_text()


def test_direct_assign_contract_covers_three_shapes_and_single_native_edge():
    text = _text("direct-assign")
    for marker in ("repro", "rework", "new_issue", "issue", "repository_id", "task"):
        assert marker in text
    assert "POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/assign" in text
    assert "JSON {issue, repository_id, task}" in text
    assert "delivery_verified=true" in text
    assert "one new top-level" in text
    assert "never create a\nnew-issue parent" in text
    assert "Never send a second message-slot request" in text


def test_direct_release_contract_is_one_existing_release_call():
    text = _text("direct-release")
    assert "Switch to main and pull the latest origin/main." in text
    assert "exactly once through" in text
    assert "message-slot/direct-send path" in text
    assert "same slot" in text
    assert "pinned epoch" in text
    assert "complete assignment tuple" in text
    assert "clean" in text
    assert "checkout" in text
    assert "no unpushed work" in text
    assert "HEAD` equal to the current" in text
    assert "POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release" in text
    assert "exactly once" in text
    for forbidden in ("reset", "force", "retry"):
        assert forbidden in text
    assert "send another pane message" in text
    assert "Never send pane input" not in text


def test_nudge_contract_releases_first_and_prioritizes_work():
    text = _text("pm-nudge-processing")
    assert "at least 20 minutes" in text
    assert "Skill(direct-release)" in text
    assert "exact `Switch to main and pull the latest origin/main.` instruction once" in text
    assert "same complete assignment" in text
    assert "same epoch" in text
    assert "occupied=false" in text
    assert "repro`, `rework`, then `new_issue" in text
    assert text.count("POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release") == 1
    assert "delivery_verified=true" in text
    assert "no retry" in text


def test_manifest_owns_exact_skill_sources_and_modes():
    manifest = json.loads(MANIFEST.read_text())
    entries = {entry["source_path"]: entry for entry in manifest["entries"]}
    expected = {
        "claude/skills/direct-assign/SKILL.md": "/Users/rajiv/.claude/skills/direct-assign/SKILL.md",
        "claude/skills/direct-release/SKILL.md": "/Users/rajiv/.claude/skills/direct-release/SKILL.md",
        "claude/skills/pm-nudge-processing/SKILL.md": "/Users/rajiv/.claude/skills/pm-nudge-processing/SKILL.md",
    }
    for source, target in expected.items():
        entry = entries[source]
        source_path = ASSETS / entry["source_path"]
        assert entry["canonical_target"] == target
        assert entry["mode"] == 0o644
        assert entry["dependency_status"] == "closed"
        assert entry["sha256"] == hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert stat.S_IMODE(source_path.stat().st_mode) == 0o644
