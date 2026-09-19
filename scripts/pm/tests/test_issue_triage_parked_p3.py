"""Focused proof for the parked-P3 promotion guard (#7905 twice-promoted).

Governing defect: the triage sweep PROMOTEs a P3 carrying intentional-park
metadata (status:backlog/deferred + complete Ready Pool frontmatter + a
quoted CTO/Rajiv park instruction) to status:todo, because the PROMOTE path
and the "no eligible backlog candidate remains parked" terminal condition
treat it as an ordinary eligible candidate. Rajiv re-parked #7905 twice.

The repair carves parked P3s out of PROMOTE eligibility in the SOP's PROMOTE
/ Todo-invariant / terminal-condition / status-hygiene path, with P0-P2
promotion byte-identical. These tests pin the guard clauses in the canonical
source plus manifest parity for the adopted entry.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SOP = SHARED / "codex" / "monitors" / "heydonna-issue-triage" / "WAKE_SOP.md"
MANIFEST_PATH = SHARED / "manifest.json"
INSTALLER_PATH = ROOT / "scripts" / "install-release.py"
SPEC = importlib.util.spec_from_file_location("install_release_parked_p3", INSTALLER_PATH)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


def _flat() -> str:
    assert SOP.is_file(), "canonical SOP source missing"
    return re_normalize(SOP.read_text(encoding="utf-8"))


def re_normalize(text: str) -> str:
    return " ".join(text.split())


def test_promote_carries_parked_p3_exclusion() -> None:
    flat = _flat()
    assert "Parked-P3 exclusion (mandatory before any PROMOTE)" in flat
    assert "a P3 is parked — never set `status:todo` — when ALL hold" in flat
    assert "it carries `status:backlog` or `status:deferred`" in flat
    assert "complete Ready Pool frontmatter (`disposition: PROMOTE` with `application_state: ready_for_dispatch`)" in flat
    assert "its body quotes an explicit CTO/Rajiv park instruction" in flat
    assert "no slot now" in flat
    assert "queue it per priority behind the active lanes" in flat
    assert "Keep its current status" in flat


def test_exclusion_requires_quoted_instruction_and_new_promotion() -> None:
    flat = _flat()
    assert "record the quoted instruction as the structured reason" in flat
    assert "absent any quoted park instruction, normal promotion applies" in flat
    assert "Only a NEW explicit CTO/Rajiv promotion, issued after the park instruction, may move a parked P3 to `status:todo`" in flat


def test_p0_p2_promotion_unchanged_and_todo_invariants_cover_parked_p3() -> None:
    flat = _flat()
    assert "P0–P2 promotion is unchanged by this exclusion." in flat
    assert "a parked P3 per the exclusion above is never PROMOTE-eligible" in flat
    assert "terminal holding states for P3s until an explicit CTO/Rajiv promotion" in flat


def test_terminal_condition_and_hygiene_exclude_parked_p3() -> None:
    flat = _flat()
    assert "(a parked P3 per the exclusion above is not eligible)" in flat
    assert "a parked P3 already in `status:backlog` or `status:deferred` keeps that status" in flat
    assert 'never "repair" it to `status:todo`' in flat


def test_manifest_covers_sop_with_parity() -> None:
    manifest = INSTALLER._load_shared_manifest(ROOT)
    by_path = {item["source_path"]: item for item in manifest["entries"]}
    relative = "codex/monitors/heydonna-issue-triage/WAKE_SOP.md"
    assert relative in by_path, relative
    entry = by_path[relative]
    assert entry["canonical_target"] == "/Users/rajiv/.codex/monitors/heydonna-issue-triage/WAKE_SOP.md"
    assert entry["dependency_status"] == "closed"
    assert entry["dependencies"] == []
    assert entry["sha256"] == hashlib.sha256(SOP.read_bytes()).hexdigest()
    assert entry["mode"] == (os.stat(SOP).st_mode & 0o777) == 0o644
    assert manifest["inventory"]["selected_count"] == len(manifest["entries"])


def test_required_zero_reconciles_parked_p3_classifier_rows() -> None:
    """Complete operational contract for the required-zero point: a parked-P3
    classifier row is reconciled (row + quoted directive recorded, terminal
    unblocked, row left visible) instead of forcing promotion. This is the
    executable proxy for a prose-owned control point: it pins every operative
    sentence of the reconciliation rule in decision order. Prose limitation
    disclosed: no code path executes the sweep decision; the SOP text is the
    enforcement surface, so presence-plus-order of the complete rule is the
    proof."""
    flat = _flat()
    assert "Parked-P3 required-zero reconciliation" in flat
    # 1. Trigger: a backlog_promote_candidate row matching the FULL exclusion.
    assert "a `backlog_promote_candidate` classifier row matching the FULL parked-P3 exclusion above" in flat
    # 2. Effect: not unresolved, terminal unblocked — for every important-label family.
    assert "does not count as an unresolved promote candidate and does not prevent terminal completion" in flat
    for family in ("bug", "customer-feedback", "public-beta", "pmf", "post-beta"):
        assert f"`{family}`" in flat, family
    # 3. Recording duty: row + quoted directive as structured reason; row stays visible.
    assert "record that classifier row plus the quoted park directive as the structured reason for excluding it" in flat
    assert "row itself stays visible and is never hidden or deleted" in flat
    # 4. Scope: all conjuncts; P0-P2, deferred/blocked, unparked, and
    #    directive-less P3s stay under existing rules.
    assert "This exception applies only when ALL parked-P3 conjuncts hold" in flat
    assert "P0–P2 rows, deferred/blocked dependency handling, unparked P3s, and P3s lacking an attributable quoted directive remain governed by the existing classifier and promotion rules" in flat
    # 5. Override: only a newer explicit promotion cancels.
    assert "Only a newer post-park explicit CTO/Rajiv promotion instruction cancels the exclusion." in flat


def test_strict_form_never_todo_and_no_slot_narrowing() -> None:
    """Amendment pin: the rule is NEVER status:todo (not merely 'not promoted
    to a slot'); the withdrawn narrower phrasing must not appear."""
    flat = _flat()
    assert "never set `status:todo`" in flat
    assert "not promoted to a slot" not in flat


def test_restoration_returns_parked_p3_at_todo_to_backlog() -> None:
    """A parked P3 found carrying status:todo is moved back to backlog with
    the quoted directive as reason — this ends the live flip-flop. The
    trigger is satisfiable: every parked conjunct EXCEPT the status-label
    conjunct, with single status:todo instead (exactly one status always
    holds — the live #7905 shape carries status:todo with no simultaneous
    backlog/deferred label). A newer explicit promotion keeps todo; unparked
    P3s are never demoted here."""
    flat = _flat()
    assert "Restoration (ends flip-flopping)" in flat
    assert "when a P3 satisfying every parked conjunct above EXCEPT the status-label conjunct" in flat
    assert "carrying single `status:todo` instead of `status:backlog`/`status:deferred`" in flat
    assert "(exactly one status label always holds)" in flat
    assert "with the complete Ready Pool frontmatter and the attributable quoted CTO/Rajiv park instruction still required" in flat
    assert "move it back to `status:backlog` (remove `status:todo`)" in flat
    assert "quoting the park instruction as the structured reason" in flat
    assert "no newer explicit CTO/Rajiv promotion instruction exists anywhere on the issue (body, comments, linked threads)" in flat
    assert "a newer promotion keeps `status:todo` and cancels the exclusion for that issue" in flat
    assert "Never remove `status:todo` from an unparked P3 under this rule." in flat


def test_parked_p3_excluded_from_ready_pool_membership() -> None:
    flat = _flat()
    assert "A parked P3 is not a Ready Pool member" in flat
    assert "the dispatchable Ready Pool carries P0–P2, and a P3 enters it only unparked under the existing promotion rules" in flat
