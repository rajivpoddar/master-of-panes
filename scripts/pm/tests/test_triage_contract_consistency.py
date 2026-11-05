from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ASSETS = Path(os.environ.get("TRIAGE_TEST_ASSETS", str(Path(__file__).parents[1] / "shared-assets")))
SCRIPTS = ASSETS / "claude/scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


triage = load("triage_consistency", SCRIPTS / "backlog-triage.py")
selector = load("selector_consistency", SCRIPTS / "ready_pool_select.py")
filing = load("filing_consistency", ASSETS / "codex/skills/heydonna-file-promote-issue/scripts/validate_candidate.py")
policy = load("filing_policy_consistency", SCRIPTS / "control_plane_issue_policy.py")


def issue(number=101, priority="P1", blockers="none", suffix=""):
    body = f"""<!-- ready-pool:
priority: {priority}
lane: editor
branch_slug: preserve-current-content
ac_summary: Preserve current content at the save boundary.
claimable_slot_type: dev
blockers: {blockers}
work_type: app-code
disposition: READY_GATED
owner: PM
next_action: Select after live dependency checks.
wake_condition: Existing dependency capability is available.
application_state: ready
required_validation: Focused owning integration with fixed candidate identity.
source_commit: {'a' * 40}
runtime_symbol: hooks/use-transcript-save.ts::performSave
duplicate_search_receipt: Current issue and PR bodies checked by runtime capability.
overlapping_work_receipt: No active implementation owns this candidate.
-->
<!-- qa-proof-schema: 1 -->
## Problem
Stale content can replace the current document.
## Original directive and intended capability
Preserve current customer edits.
## Production failure mode
A stale producer overwrites current work.
## Runtime control point
The candidate save boundary in hooks/use-transcript-save.ts.
## Scope
One bounded producer repair using existing save controls.
## Scribie integration contract impact
- [x] N/A — this issue changes only editor persistence.
- [ ] Applies — this issue changes the integration contract.
- External-facing error code or response change: N/A
- Caller remediation steps: N/A
- Customer-notification owner, channel, and timing: N/A
- Compatibility or rollout requirement: N/A
## Acceptance criteria
1. AC-1: Current content survives the actual candidate save path.
   - Surface: nonvisual
   - QA reachability: deterministic
   - Required proof: integration
## Negative acceptance criteria
Preserve genuine edits and exact rejected bytes.
## Testing requirements
Use the actual owning boundary with fixed candidate identities.
## Issue Contract Ledger
| Contract row | Requirement |
| --- | --- |
| Positive AC | Current content survives the actual candidate save path. |
| Negative AC | No stale overwrite and no genuine edit loss. |
| Forbidden implementation | No heuristic word-count or similarity gate. |
| Determinism | Fixed candidate identity and controlled persistence order. |
| Required proof | Owning integration covers stale refusal and valid saves. |
{suffix}
"""
    labels = [{"name": "status:todo"}, {"name": priority}]
    if blockers != "none":
        labels.append({"name": "pm-blocked:dependency"})
    return {"number": number, "title": "Preserve current content", "body": body,
            "labels": labels, "createdAt": "2026-11-01T00:00:00Z"}


class ContractConsistencyTests(unittest.TestCase):
    def test_validator_failure_or_unknown_payload_never_means_valid(self):
        for rc, output in [(1, '{"errors":[]}'), (0, '{"state":"open"}'), (0, '[]')]:
            with patch.object(triage.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, output, "")):
                self.assertEqual(triage.issue_contract_ledger_errors(issue()["body"]), ["icl_validator_failed"])

    def test_historical_and_quoted_descriptions_do_not_classify_app_work(self):
        for prose in ("## Transfer (administrative tracking only)\nArchived #400 owns separate bookkeeping.",
                      '> That old task was "not claimable" and "docs-only".',
                      "Do not turn this runtime repair into documentation-only work."):
            row = issue(suffix=prose)
            self.assertEqual(triage.ready_pool_contract_reasons(row, set(), allow_backlog=False, allow_dependencies=True), [])
            self.assertEqual([c["issue"] for c in selector.rank_candidates([row], offline=True)], [101])

    def test_real_tracking_and_product_holds_still_refuse(self):
        for field in ("work_type: tracking", "claimable_slot_type: pm-direct", "disposition: TRACKING_ONLY"):
            row = issue()
            key = field.split(":")[0]
            row["body"] = __import__("re").sub(r"(?m)^" + key + r":.*$", field, row["body"])
            self.assertTrue(triage.ready_pool_contract_reasons(row, set(), allow_backlog=False, allow_dependencies=True))
            self.assertEqual(selector.rank_candidates([row], offline=True), [])
        row = issue()
        row["labels"].append({"name": "pm-blocked:product"})
        self.assertEqual(selector.rank_candidates([row], offline=True), [])

    def test_gated_p0_membership_never_authorizes_assignment(self):
        row = issue(priority="P0")
        row["labels"].append({"name": "pm-blocked:product"})
        self.assertEqual(triage.ready_pool_contract_reasons(row, set(), allow_backlog=False, allow_dependencies=True), [])
        self.assertTrue(triage.dispatch_readiness_reasons(row, set(), allow_backlog=False))
        self.assertEqual(selector.rank_candidates([row], offline=True), [])
        self.assertEqual([c["issue"] for c in selector.gated_p0_candidates([row], offline=True)], [101])

    def test_missing_ledger_or_branch_is_neither_claimable_nor_an_extra_row_bypass(self):
        for row in (issue(), issue(number=102)):
            if row["number"] == 101:
                row["body"] = row["body"].split("## Issue Contract Ledger")[0]
            else:
                row["body"] = row["body"].replace("branch_slug: preserve-current-content\n", "")
            self.assertTrue(triage.ready_pool_contract_reasons(row, set(), allow_backlog=False, allow_dependencies=True))
            self.assertEqual(selector.rank_candidates([row], [{"issue": row["number"], "class": "new_issue", "priority": "P1"}], offline=True), [])

    def test_new_issue_extra_cannot_override_source_priority_age_or_dependencies(self):
        rows = [issue(101, "P1"), issue(102, "P2")]
        extra = [{"issue": 102, "class": "new_issue", "priority": "P0", "created_at": "2000-01-01T00:00:00Z"}]
        ranked = selector.rank_candidates(rows, extra, offline=True)
        self.assertEqual([(r["issue"], r["priority"]) for r in ranked], [(101, "P1"), (102, "P2")])
        self.assertEqual(ranked[1]["created_at"], rows[1]["createdAt"])
        rows[1] = issue(102, "P2", "#999")
        self.assertEqual([r["issue"] for r in selector.rank_candidates(rows, extra, cache={(999, "default"): (False, "OPEN")})], [101])

    def test_p3_dependency_is_a_member_but_never_picked_unresolved(self):
        row = issue(priority="P3", blockers="#999")
        self.assertEqual(triage.ready_pool_contract_reasons(row, {999}, allow_backlog=False, allow_dependencies=True), [])
        self.assertEqual(selector.rank_candidates([row], cache={(999, "default"): (False, "OPEN")}), [])
        held = selector.gated_p0_candidates([row], cache={(999, "default"): (False, "OPEN")})
        self.assertEqual([r["issue"] for r in held], [101])
        self.assertEqual([r["issue"] for r in selector.rank_candidates([row], cache={(999, "default"): (True, "COMPLETED")})], [101])

    def test_canonical_filing_accepts_named_dependency_without_architecture_marker(self):
        row = issue(priority="P2", blockers="#999")
        self.assertEqual(filing.validate_body(row["body"], row["title"], "P2", triage, policy), [])
        row["body"] = row["body"].replace("blockers: #999", "blockers: product approval")
        self.assertTrue(filing.validate_body(row["body"], row["title"], "P2", triage, policy))

    def test_pr_ownership_has_one_definition_in_selector_and_promoter(self):
        prs = [{"number": 8, "body": "Fixes #101. Part of #102.\nRemaining ACs: more proof", "closingIssuesReferences": [{"number": 103}]},
               {"number": 9, "body": "Must not close #104; refs #105", "closingIssuesReferences": []}]
        owned, _ = selector.owned_issues(prs)
        self.assertEqual(set(owned), {101, 102, 103, 105})
        with patch.object(triage, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(prs), "")):
            self.assertEqual(triage.open_pr_issue_numbers(), set(owned))

    def test_selector_and_pool_audit_agree_on_contract_defects_and_ownership(self):
        rows = [issue(), issue(number=102), issue(number=103, priority="P3", blockers="#999"), issue(number=104)]
        rows[1]["body"] = rows[1]["body"].replace("branch_slug: preserve-current-content\n", "")
        prs = [{"number": 8, "isDraft": False, "body": "Fixes #104", "closingIssuesReferences": []}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "issues.json"
            path.write_text(json.dumps(rows))
            with patch.object(selector, "load_open_prs", return_value=prs), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(selector.main(["--issues-json", str(path), "--no-dependency-check"]), 0)
            result = json.loads(out.getvalue())
            self.assertEqual(result["eligible_count"], 1)
            self.assertEqual(result["pool_count"], 2)
            self.assertEqual(result["contract_defects"][0]["issue"], 102)
            self.assertEqual(result["dependency_gated_count"], 1)
            def dependency_states(_rows, cache):
                cache[(999, "default")] = (False, "OPEN")
            with patch.object(triage, "gh_issue_list", return_value=rows), patch.object(triage, "open_pr_issue_numbers", return_value={104}), patch.object(triage, "prefetch_dependencies", side_effect=dependency_states), patch.object(triage, "OUT_DIR", Path(directory)), contextlib.redirect_stdout(io.StringIO()):
                triage.audit_ready_pool_main([])
            audit = json.loads(next(Path(directory).glob("pm-ready-pool-audit-*.json")).read_text())
            self.assertNotIn(102, audit["clean_issues"])
            self.assertNotIn(104, audit["clean_issues"])

    def test_closed_not_planned_dependency_is_not_delivery_in_pool_audit(self):
        rows = [issue(blockers="#999")]
        def dependency_states(_rows, cache):
            cache[(999, "default")] = (False, "CLOSED")
        with tempfile.TemporaryDirectory() as directory, patch.object(triage, "gh_issue_list", return_value=rows), patch.object(triage, "open_pr_issue_numbers", return_value=set()), patch.object(triage, "prefetch_dependencies", side_effect=dependency_states), patch.object(triage, "OUT_DIR", Path(directory)), contextlib.redirect_stdout(io.StringIO()):
            triage.audit_ready_pool_main([])
            audit = json.loads(next(Path(directory).glob("pm-ready-pool-audit-*.json")).read_text())
        self.assertEqual(audit["clean_todo_count"], 0)
        self.assertEqual(audit["dependency_gated_count"], 1)

    def test_candidate_validator_cli_uses_canonical_tools_not_retired_app_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "fixture"], check=True)
            head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", head], check=True)
            body = repo / "issue.md"
            body.write_text(issue(blockers="#999")["body"].replace("a" * 40, head))
            result = subprocess.run([sys.executable, str(Path(filing.__file__)), "--repo-root", str(repo), "--title", "Preserve current content", "--priority", "P1", "--body-file", str(body), "--control-plane-dir", str(SCRIPTS)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["membership"], "dependency-held")
            body.write_text(body.read_text().replace(head, "b" * 40))
            result = subprocess.run([sys.executable, str(Path(filing.__file__)), "--repo-root", str(repo), "--title", "Preserve current content", "--priority", "P1", "--body-file", str(body), "--control-plane-dir", str(SCRIPTS)], capture_output=True, text=True)
            self.assertIn("source_commit_drift", json.loads(result.stdout)["errors"])


if __name__ == "__main__":
    unittest.main()
