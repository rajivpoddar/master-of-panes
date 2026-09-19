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
