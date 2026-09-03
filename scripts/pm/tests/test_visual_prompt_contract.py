from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[3]
ASSETS = ROOT / "scripts" / "pm" / "shared-assets"
PM_STATUS = ASSETS / "codex" / "skills" / "heydonna-open-pr-status" / "SKILL.md"
PM_REVIEW = ASSETS / "claude" / "skills" / "pm-code-review" / "SKILL.md"
CTO_TRIGGER = ASSETS / "codex" / "skills" / "heydonna-cto-label-gated-ci" / "SKILL.md"
MANIFEST = ASSETS / "manifest.json"


def _text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


def test_live_prompt_sources_are_manifest_owned_and_cover_visual_boundary() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = {entry["source_path"]: entry for entry in manifest["entries"]}
    expected = {
        "codex/skills/heydonna-open-pr-status/SKILL.md": PM_STATUS,
        "claude/skills/pm-code-review/SKILL.md": PM_REVIEW,
        "codex/skills/heydonna-cto-label-gated-ci/SKILL.md": CTO_TRIGGER,
    }
    for source, path in expected.items():
        assert source in entries
        assert entries[source]["canonical_target"]
        assert entries[source]["mode"] == 0o644
        import hashlib
        assert entries[source]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    combined = " ".join(_text(path) for path in expected.values())
    for term in (
        "issue body",
        "body sha",
        "exact head",
        "visual acceptance contract",
        "deterministic",
        "approve_code_pending_qa_visual_proof",
        "qa_capability_blocked",
        "proof-only",
        "out of scope",
        "component tests",
        "production-shaped",
    ):
        assert term in combined


def test_missing_visual_proof_cannot_be_a_readiness_pass_and_non_ui_survives() -> None:
    pm = _text(PM_REVIEW)
    cto = _text(CTO_TRIGGER)
    assert "missing or malformed contract is blocked" in pm
    assert "not a readiness-bearing pass" in pm
    assert "missing, stale, malformed, head-mismatched, body-mismatched, or incomplete proof" in cto
    assert "before the first ci-trigger effect" in cto
    assert "non-ui prs with no visual contract retain" in pm
    assert "non-ui prs with no visual contract retain" in cto


def test_qa_capability_is_proof_only_and_legacy_paid_entry_is_not_a_caller() -> None:
    prompt_text = "\n".join(path.read_text(encoding="utf-8") for path in (PM_STATUS, PM_REVIEW, CTO_TRIGGER))
    assert "QA_CAPABILITY_BLOCKED" in prompt_text
    assert "proof-only" in prompt_text.lower()
    assert "pm-transition" not in prompt_text.lower()
    assert "request-label-gated-ci.sh" not in prompt_text
    assert "pr-ci-readiness-gate.py" not in prompt_text
    assert "raw workflow command" in _text(CTO_TRIGGER)


def test_authoritative_visual_gate_contract_is_present_and_not_replaced() -> None:
    gate = ASSETS / "claude" / "scripts" / "qa-visual-proof-gate.py"
    body = gate.read_text(encoding="utf-8")
    assert "def body_sha256" in body
    assert "issue_body_sha256" in body
    assert "required_screenshot_ac_ids" in body
    assert "heydonna_qa_visual_proof" in body
