from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
MANIFEST = SHARED / "manifest.json"
# Rebaselined 2026-10-01: the Sept-1 pins (task ids 01a04154/01a03236/01a0324b-
# ...e7da/01a03265 and the pre-prune WAKE_SOP prose) were superseded by 9ad9e00
# (2026-09-20, live Decisions SOP adopted + pruned; old text kept in
# WAKE_SOP.archive-2026-09-20.md) and 3fdc245 (2026-09-29, task roster + Rajiv's
# 09-27/09-28 "PM owns PR terminal processing" direction synced back).
CTO_DECISIONS_TASK = "01a09112-a09c-7361-9a2a-0ada6a4e9dfb"
NEW_TASK = CTO_DECISIONS_TASK
CTO_TASK = CTO_DECISIONS_TASK
CP_REPAIRS_TASK = "01a08f69-45db-71c2-b433-678419139ed7"
MOP_TASK = "01a0d779-9a74-7f52-89ad-167910930d27"
PR_REVIEWS_TASK = "01a0b53e-3316-77d3-9610-1c0c58d4ba5b"


def _text(relative: str) -> str:
    return (SHARED / relative).read_text(encoding="utf-8")


def test_process_routes_pm_report_to_cto_diagnosis() -> None:
    monitor = _text("codex/monitors/heydonna-pm-chat/MONITOR.md")
    wake = _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md")
    contract = _text("codex/skills/_shared/release-conveyor-contract.md")
    assert "PM reports the first literal" in monitor
    assert "control-plane blocker and exact tuple" in monitor
    assert "CTO Decisions" in monitor
    assert "PM does not diagnose" in monitor
    # Causal diagnosis moved from WAKE_SOP prose to the shared contract (9ad9e00).
    assert "CTO Decisions performs causal diagnosis" in contract
    assert f"Sole consumer: CTO Decisions\n`{CTO_DECISIONS_TASK}`" in wake


def test_verified_control_plane_routes_by_affinity_then_to_pr_reviews() -> None:
    texts = [
        _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md"),
        _text("codex/skills/_shared/release-conveyor-contract.md"),
        _text("codex/skills/heydonna-control-plane-repair/SKILL.md"),
    ]
    combined = "\n".join(texts)
    assert CTO_DECISIONS_TASK in combined
    assert CP_REPAIRS_TASK in combined
    assert MOP_TASK in combined
    assert PR_REVIEWS_TASK in combined
    assert "same implementation task" not in texts[0]  # pruned wording (9ad9e00)
    assert "approval back to that same task" in combined
    assert "functionality-first independent review" in combined
    assert "CTO Decisions does not perform the review" in combined
    assert "CTO_INLINE_APPROVE" not in combined


def test_approval_returns_rollout_to_same_owner_and_cto_only_notifies_pm() -> None:
    contract = _text("codex/skills/_shared/release-conveyor-contract.md")
    skill = _text("codex/skills/heydonna-control-plane-repair/SKILL.md")
    wake = _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md")
    assert "\"owner\": \"CTO_DECISIONS\"" in contract
    assert "return_approval_to_same_implementation_owner" in contract
    assert "approval back to that same task" in skill
    assert "never investigates deeply, implements, reviews, publishes" in skill
    assert "PR Reviews" in skill
    assert "rework/approval returns to that same owner" in wake
    assert "same-owner rollout" in wake
    assert "CTO Decisions never implements" in contract
    assert "CTO_INLINE_APPROVE" not in wake


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
    # Rajiv 2026-09-27/28 (thread 1790563991.676189): PM owns PR CI/E2E terminal
    # processing; CTO only for exact-head admission and merge (d133914, 3fdc245).
    # This replaces the old blanket `"PM owns" not in combined` pin.
    assert "PM owns PR CI/E2E diagnosis and fixes" in wake
    assert "PM owns the complete PR CI/E2E terminal loop" in monitor
    assert "PM requests CTO Decisions only at an exact-head pair" in wake
    assert "PR Merges executes admission/merge" in wake
    assert "direct PM to fire label-gated" not in combined
    assert "PM releases the stuck slot" not in combined
    assert "PM must force terminalization" not in monitor
    assert "PM does\n  not commit/push terminalization" in monitor
    assert "park/release" in monitor
    assert "choose the\n  next work" in monitor
    assert "CTO-authorized rework/repro/proof slot assignment" in monitor
    assert CP_REPAIRS_TASK in skill
    assert PR_REVIEWS_TASK in skill


def test_canonical_asset_manifest_has_exact_source_digests_and_modes() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = {
        "codex/monitors/heydonna-pm-chat/MONITOR.md",
        "codex/monitors/heydonna-pm-chat/WAKE_SOP.md",
        "codex/skills/_shared/release-conveyor-contract.md",
        "codex/skills/heydonna-control-plane-repair/SKILL.md",
        "codex/monitors/heydonna-issue-triage/WAKE_SOP.md",
        "claude/control_plane/runtime_observation.py",
        "claude/scripts/pm/control-plane/sakshi-heartbeat.py",
    }
    entries = {entry["source_path"]: entry for entry in manifest["entries"]}
    assert expected <= entries.keys()
    assert "~/Downloads/projects/heydonna-app/scripts/pm/control-plane/sakshi-heartbeat.py" not in manifest["inventory"]["source_roots"]
    for source_path in expected:
        source = SHARED / source_path
        entry = entries[source_path]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == entry["sha256"]
        expected_mode = 0o755 if source_path.endswith("sakshi-heartbeat.py") else 0o644
        assert entry["mode"] == expected_mode
        assert entry["canonical_target"].startswith("/Users/rajiv/")
    assert entries["claude/control_plane/runtime_observation.py"]["canonical_target"] == "/Users/rajiv/.claude/control_plane/runtime_observation.py"
    assert entries["claude/scripts/pm/control-plane/sakshi-heartbeat.py"]["canonical_target"] == "/Users/rajiv/.claude/scripts/sakshi-heartbeat.py"


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
    contract = _text("codex/skills/_shared/release-conveyor-contract.md")
    assert CTO_TASK in skill
    assert "$codex-stdio-send-message" in skill
    assert "renderer-free stdio" in contract
    assert "installed\n  `codex-stdio-send-message` with exact destination" in wake
    assert "Never poll the\n  recipient" in wake


def test_slack_backed_wake_reconciles_latest_thread_before_processing() -> None:
    # The full pre-wake Slack reconciliation section was pruned to a compact
    # "Slack (only for concrete ambiguity)" rule in 9ad9e00; the archive keeps
    # the old 6-step text as non-operative evidence.
    wake = _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md")
    mechanics = wake[wake.index("## Wake mechanics"):]
    assert "- Slack (only for concrete ambiguity)" in mechanics
    assert "read via postback skill + bot token" in mechanics
    assert "authoritative newer replies count" in mechanics
    assert "freeze `observed_through_ts` + digest" in mechanics
    assert "stop typed on incomplete/conflict" in mechanics
    assert "Action + receipt first; new events queue next" in mechanics
    assert mechanics.index("dedupe fingerprint") < mechanics.index("- Slack (only")


def test_slack_postback_is_a_managed_global_codex_skill() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = {entry["source_path"]: entry for entry in manifest["entries"]}
    files = {
        "codex/skills/heydonna-slack-postback/SKILL.md": (
            "/Users/rajiv/.codex/skills/heydonna-slack-postback/SKILL.md",
            0o644,
        ),
        "codex/skills/heydonna-slack-postback/agents/openai.yaml": (
            "/Users/rajiv/.codex/skills/heydonna-slack-postback/agents/openai.yaml",
            0o644,
        ),
        "codex/skills/heydonna-slack-postback/scripts/cto_slack_rest.py": (
            "/Users/rajiv/.codex/skills/heydonna-slack-postback/scripts/cto_slack_rest.py",
            0o755,
        ),
        "codex/skills/heydonna-slack-postback/scripts/render_slack_blocks.py": (
            "/Users/rajiv/.codex/skills/heydonna-slack-postback/scripts/render_slack_blocks.py",
            0o755,
        ),
        "codex/skills/heydonna-slack-postback/scripts/test_cto_slack_rest.py": (
            "/Users/rajiv/.codex/skills/heydonna-slack-postback/scripts/test_cto_slack_rest.py",
            0o644,
        ),
    }

    for source_path, (target, mode) in files.items():
        source = SHARED / source_path
        entry = entries[source_path]
        assert entry["canonical_target"] == target
        assert entry["mode"] == mode
        assert hashlib.sha256(source.read_bytes()).hexdigest() == entry["sha256"]

    skill = _text("codex/skills/heydonna-slack-postback/SKILL.md")
    wake = _text("codex/monitors/heydonna-pm-chat/WAKE_SOP.md")
    global_root = "/Users/rajiv/.codex/skills/heydonna-slack-postback"
    assert global_root in skill
    # 9ad9e00 replaced the inline cto_slack_rest.py path with a reference to
    # the managed postback skill.
    assert "read via postback skill + bot token" in wake
    assert ".agents/skills/heydonna-slack-postback" not in skill
    assert ".agents/skills/heydonna-slack-postback" not in wake


def test_native_bypass_contract_is_single_fenced_and_ordered() -> None:
    contract = _text("codex/skills/_shared/release-conveyor-contract.md")
    monitor = _text("codex/monitors/heydonna-pm-chat/MONITOR.md")
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
    # The WAKE_SOP pointer was pruned in 9ad9e00; MONITOR.md carries it now.
    assert "`Native bypass contract (CTO-only, after one high-level refusal)`" in monitor
    assert "No direct CTO implementation, review, publication" in skill