#!/usr/bin/env python3
"""Validate a new issue using the installed canonical Ready Pool policy."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys


REQUIRED_HEADINGS = (
    "Problem", "Original directive and intended capability",
    "Production failure mode", "Runtime control point", "Scope",
    "Scribie integration contract impact", "Acceptance criteria",
    "Negative acceptance criteria", "Testing requirements", "Issue Contract Ledger",
)
EVIDENCE_FIELDS = (
    "source_commit", "runtime_symbol", "duplicate_search_receipt",
    "overlapping_work_receipt",
)
SCRIBIE_DETAILS = (
    "External-facing error code or response change", "Caller remediation steps",
    "Customer-notification owner, channel, and timing", "Compatibility or rollout requirement",
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load canonical validator: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate_body(body: str, title: str, priority: str, triage, policy) -> list[str]:
    errors = []
    meta = triage.ready_pool_metadata({"body": body})
    if len(triage.ready_pool_blocks(body)) != 1:
        errors.append("expected_one_ready_pool_block")
    for heading in REQUIRED_HEADINGS:
        if not re.search(rf"(?im)^##\s+{re.escape(heading)}\s*$", body):
            errors.append("missing_heading:" + heading)
    if re.search(r"\bTBD\b|<[a-z_]*(?:SHA|SLUG)[a-z_]*>", body, re.I):
        errors.append("unfilled_placeholder")
    blockers = triage.clean_meta_value(meta.get("blockers"))
    named = triage.has_dependency_contract(meta)
    labels = [{"name": "status:todo"}, {"name": priority}]
    if named:
        labels.append({"name": "pm-blocked:dependency"})
    issue = {"number": 0, "title": title, "body": body, "labels": labels}
    errors.extend(triage.ready_pool_contract_reasons(
        issue, set(), allow_backlog=False, allow_dependencies=True,
    ))
    if not triage.metadata_blockers_clear(meta) and not named:
        errors.append("unparseable_or_non_dependency_gate:" + blockers)
    for field in ("owner", "next_action", "wake_condition", "required_validation", "application_state"):
        if not triage.clean_meta_value(meta.get(field)):
            errors.append("missing_ready_pool_field:" + field)
    if triage.clean_meta_value(meta.get("disposition")).upper() not in {
        "PROMOTE", "READY", "READY_GATED", "DEPENDENCY_HELD",
    }:
        errors.append("invalid_admission_disposition")
    for field in EVIDENCE_FIELDS:
        value = triage.clean_meta_value(meta.get(field))
        if not value or value.lower() in {"none", "n/a", "unknown", "not checked"}:
            errors.append("missing_or_invalid_evidence:" + field)
    if len(triage.clean_meta_value(meta.get("ac_summary"))) > 140:
        errors.append("ac_summary_exceeds_140_chars")
    reason = policy.internal_control_plane_reason(title, body)
    if reason:
        errors.append("internal_control_plane_issue_forbidden:" + reason)
    choices = re.findall(r"(?im)^-\s*\[([ xX])\]\s*(N/A|Applies)\s+—\s+this issue", body)
    checked = [name for mark, name in choices if mark.lower() == "x"]
    if sorted(name for _, name in choices) != ["Applies", "N/A"] or len(checked) != 1:
        errors.append("scribie_impact_select_exactly_one")
    else:
        section = re.search(r"(?ims)^##\s+Scribie integration contract impact\s*$\n(.*?)(?=^##\s+|\Z)", body)
        for field in SCRIBIE_DETAILS:
            match = re.search(rf"(?im)^-\s*{re.escape(field)}:\s*(.*?)\s*$", section.group(1) if section else "")
            value = match.group(1).strip() if match else ""
            if not value or (checked == ["N/A"] and value.upper() != "N/A") or (checked == ["Applies"] and value.upper() in {"N/A", "TBD"}):
                errors.append("invalid_scribie_detail:" + field)
    return list(dict.fromkeys(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--priority", required=True)
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument("--control-plane-dir", type=Path, default=Path.home() / ".claude/scripts")
    args = parser.parse_args()
    try:
        body = args.body_file.read_text(encoding="utf-8")
        directory = args.control_plane_dir.expanduser().resolve()
        sys.path.insert(0, str(directory))
        triage = load_module("filing_canonical_triage", directory / "backlog-triage.py")
        policy = load_module("filing_canonical_control_plane_policy", directory / "control_plane_issue_policy.py")
        priority = args.priority.upper().strip()
        errors = validate_body(body, args.title, priority, triage, policy)
        meta = triage.ready_pool_metadata({"body": body})
        source = triage.clean_meta_value(meta.get("source_commit"))
        if not re.fullmatch(r"[0-9a-f]{40}", source):
            errors.append("invalid_source_commit")
        else:
            head = subprocess.check_output(
                ["git", "-C", args.repo_root, "rev-parse", "origin/main"],
                text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
            if source != head:
                errors.append("source_commit_drift")
        result = {"ok": not errors, "errors": errors, "priority": priority,
                  "membership": "dependency-held" if triage.has_dependency_contract(meta) else "unblocked",
                  "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                  "canonical_validator_dir": str(directory)}
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
        result = {"ok": False, "errors": ["canonical_validation_failed:" + str(exc)]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 13


if __name__ == "__main__":
    raise SystemExit(main())
