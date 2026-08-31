from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
MANIFEST = SHARED / "manifest.json"
NEW_TASK = "01a04154-c9c1-7bc1-8f7b-009a87bc7628"
CTO_TASK = "01a03236-2e61-71f3-a6a8-3dc24d8c8917"
RETIRED_GENERIC_TASK = "01a0324b-68e0-7491-988f-e7da9abd26ab"


def _text(relative: str) -> str:
    return (SHARED / relative).read_text(encoding="utf-8")


def test_process_routes_pm_report_to_cto_diagnosis() -> None:
    monitor = _text("codex/monitors/heydonna-pm-chat/MONITOR.md")
    wake = _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md")
    assert "PM reports the first literal" in monitor
    assert "control-plane blocker and exact tuple" in monitor
    assert "CTO Decisions" in monitor
    assert "PM does not diagnose" in monitor
    assert "CTO decisions performs the causal diagnosis" in wake


def test_verified_control_plane_routes_to_one_mop_candidate_owner() -> None:
    texts = [
        _text("codex/monitors/heydonna-pm-chat/MONITOR.md"),
        _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md"),
        _text("codex/skills/_shared/release-conveyor-contract.md"),
        _text("codex/skills/heydonna-control-plane-repair/SKILL.md"),
    ]
    combined = "\n".join(texts)
    assert NEW_TASK in combined
    assert "candidate-only" in combined
    assert "exactly one inline review" in combined
    assert "BLOCK returns rework to the same" in combined
    assert RETIRED_GENERIC_TASK not in combined


def test_approval_belongs_to_cto_and_pm_gets_only_post_deploy_terminal() -> None:
    contract = _text("codex/skills/_shared/release-conveyor-contract.md")
    skill = _text("codex/skills/heydonna-control-plane-repair/SKILL.md")
    wake = _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md")
    assert "\"owner\": \"CTO_DECISIONS\"" in contract
    assert "publish_rollout_verify_and_notify_pm" in contract
    assert "CTO Decisions performs" in skill
    assert "single PM notification" in skill
    assert "MoP stops" in skill
    assert "never deploys after approval" in skill
    assert "The packet is an actionable CTO review wake" in skill
    assert "CTO_INLINE_APPROVE" in wake
    assert "approved bounded control-plane publication/install/activation" in wake
    assert "must not wait, poll, investigate broadly" in wake
    assert "Never implement or continuously monitor" in wake
    assert "Never implement, deploy, or monitor any bounded control-plane repair" not in wake


def test_open_pr_ownership_has_only_two_pm_responsibilities() -> None:
    contract = _text("codex/skills/_shared/release-conveyor-contract.md")
    monitor = _text("codex/monitors/heydonna-pm-chat/MONITOR.md")
    wake = _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md")
    skill = _text("codex/skills/heydonna-control-plane-repair/SKILL.md")
    combined = "\n".join((contract, monitor, wake, skill))

    assert "ci_failure_investigation" in contract
    assert "cto_routed_rework_or_repro_slot_assignment" in contract
    assert "PM has exactly two operational responsibilities" in combined
    assert "one bounded CI/E2E failure investigation" in combined
    assert "CTO-authorized rework" in combined
    for scenario in (
        "code_ready_without_admission",
        "capture_decision_or_dispatch",
        "pr_label_or_state_transition",
        "workflow_terminal",
        "rerun_or_retry_decision",
        "rescue_or_release_routing",
        "sync_integration_and_merge",
    ):
        assert f'"{scenario}"' in contract
    assert '"code_ready_without_admission": {"owner": "CTO_DECISIONS"' in contract
    assert '"capture_decision_or_dispatch": {"owner": "CTO_DECISIONS"' in contract
    assert '"pr_label_or_state_transition": {"owner": "CTO_DECISIONS"' in contract
    assert '"rerun_or_retry_decision": {"owner": "CTO_DECISIONS"' in contract
    assert '"rescue_or_release_routing": {"owner": "CTO_DECISIONS"' in contract
    assert '"sync_integration_and_merge": {"owner": "CTO_DECISIONS"' in contract
    assert "PM owns routine free-slot refill" not in combined
    assert "PM performs routine free-compatible-slot refill" not in combined
    assert "PM owns" not in combined
    assert "direct PM to fire label-gated" not in combined
    assert "PM releases the stuck slot" not in combined
    assert "PM slot assignment only after an explicit CTO-routed rework/repro" in combined
    assert "PM may assign a numbered slot only when CTO explicitly routes" in combined


def test_canonical_asset_manifest_has_exact_source_digests_and_modes() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = {
        "codex/monitors/heydonna-pm-chat/MONITOR.md",
        "codex/monitors/heydonna-pm-chat/WAKE_SOP.md",
        "codex/skills/_shared/release-conveyor-contract.md",
        "codex/skills/heydonna-control-plane-repair/SKILL.md",
    }
    entries = {entry["source_path"]: entry for entry in manifest["entries"]}
    assert expected <= entries.keys()
    for source_path in expected:
        source = SHARED / source_path
        entry = entries[source_path]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == entry["sha256"]
        assert entry["mode"] == 0o644
        assert entry["canonical_target"].startswith("/Users/rajiv/")


def test_unrelated_asset_is_unchanged_from_candidate_base() -> None:
    relative = "scripts/pm/shared-assets/claude/scripts/launch-dev-slot-claude.sh"
    current = (ROOT / relative).read_bytes()
    import subprocess

    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    original = subprocess.check_output(["git", "show", f"{base}:{relative}"], cwd=ROOT)
    assert current == original


def test_cto_return_transport_is_explicit() -> None:
    skill = _text("codex/skills/heydonna-control-plane-repair/SKILL.md")
    wake = _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md")
    assert CTO_TASK in skill
    assert "$codex-stdio-send-message" in skill
    assert "renderer-free" in wake
    assert "$codex-stdio-send-message" in wake
    assert "PM may provide the initial" in wake
    assert "blocker/context only" in wake


def test_native_bypass_contract_is_single_fenced_and_ordered() -> None:
    contract = _text("codex/skills/_shared/release-conveyor-contract.md")
    wake = _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md")
    skill = _text("codex/skills/heydonna-control-plane-repair/SKILL.md")
    assert "high-level typed/control-plane path is attempted once" in contract
    assert "do not retry it" in contract
    assert "one direct GitHub complete-set label" in contract
    assert "It performs no MoP or" in contract
    assert "MoP -> GitHub ->" in contract
    assert "failure at any step stops before later effects" in contract
    assert "exactly one" in contract
    assert "literal `message-slot` continuation packet" in contract
    assert "raw `workflow_dispatch`" in contract
    assert "blind rerun" in contract
    assert "shared native bypass contract" in wake
    assert "Native bypass contract (CTO-only, after one high-level refusal)" in skill
