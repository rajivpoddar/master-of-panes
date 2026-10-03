"""Post-merge cleanup closes the issue a merged PR delivered via MoP tuples.

Rajiv 2026-10-03 08:25 IST (C0ALZJHGE49/1790995117.258909, "do it"): when a
merged PR has no GitHub closing reference, resolve the issue from MoP's own
issue<->PR assignment tuples and close it authoritatively. "Part of #N" and
pm-nonclaimable:tracking issues keep today's labels-only cleanup.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[3]
SPEC = importlib.util.spec_from_file_location(
    "pm_cleanup_pr_mop", ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "pm-cleanup-pr.py"
)
assert SPEC and SPEC.loader
CLEANUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLEANUP)

HEAD = "a" * 40
MERGE = "b" * 40


def issue_less_request():
    return {
        "repository": "heydonna-app/heydonna-app", "pr": 8801, "issue": None,
        "head": HEAD, "merge_commit": MERGE, "cleanup_mode": "merged_pr_issue_less",
    }


def pr_payload(body="Implements the thing.", refs=()):
    return {
        "number": 8801, "state": "MERGED", "mergeCommit": {"oid": MERGE}, "headRefOid": HEAD,
        "headRefName": "feat/8800-thing", "body": body,
        "closingIssuesReferences": [{"number": n} for n in refs],
        "labels": ["ci-passed", "keep-me"],
    }


class Fake:
    def __init__(self, pr, issue_labels=("status:in-progress",), mop=None):
        self.pr = pr
        self.issue = {"number": 8800, "state": "OPEN", "stateReason": None, "labels": list(issue_labels)}
        self.mop = mop if mop is not None else {"issue": 8800, "source": "pr", "ambiguous": False}
        self.effects: list[str] = []
        self.mop_calls: list[tuple] = []

    def read_pr(self, request):
        return json.loads(json.dumps(self.pr))

    def read_issue(self, request):
        assert request["issue"] == 8800
        return json.loads(json.dumps(self.issue))

    def mop_issue_for_pr(self, repository, pr, branch):
        self.mop_calls.append((repository, pr, branch))
        return self.mop

    def _mut(self, target, scope, label, op):
        labels = target["labels"]
        if op == "add" and label not in labels:
            labels.append(label)
        if op == "remove":
            target["labels"] = [x for x in labels if x != label]
        self.effects.append(f"{scope}:{op}:{label}")

    def add_pr_label(self, r, label): self._mut(self.pr, "pr", label, "add")
    def remove_pr_label(self, r, label): self._mut(self.pr, "pr", label, "remove")
    def add_issue_label(self, r, label): self._mut(self.issue, "issue", label, "add")
    def remove_issue_label(self, r, label): self._mut(self.issue, "issue", label, "remove")

    def close_issue(self, r):
        self.issue["state"], self.issue["stateReason"] = "CLOSED", "COMPLETED"
        self.effects.append("issue:close")


def run(request, tmp_path, fake):
    return CLEANUP.run(request, mapping_path=tmp_path / "absent.json",
                       receipt_path=tmp_path / "r.json", external=fake)


def test_unique_mop_match_closes_issue_authoritatively(tmp_path):
    fake = Fake(pr_payload())
    result = run(issue_less_request(), tmp_path, fake)
    assert result["success"] is True
    assert fake.mop_calls == [("heydonna-app/heydonna-app", 8801, "feat/8800-thing")]
    assert "issue:close" in fake.effects
    assert "issue:add:status:done" in fake.effects
    assert "issue:remove:status:in-progress" in fake.effects
    assert "pr:add:pm-state:closed-clean" in fake.effects
    (receipt,) = json.loads((tmp_path / "r.json").read_text()).values()
    assert receipt["request"]["issue"] == 8800
    assert receipt["request"]["issue_authoritative"] is True


def test_branch_fallback_match_closes_issue(tmp_path):
    fake = Fake(pr_payload(), mop={"issue": 8800, "source": "branch", "ambiguous": False})
    assert run(issue_less_request(), tmp_path, fake)["success"] is True
    assert "issue:close" in fake.effects


def test_ambiguous_mop_result_keeps_labels_only(tmp_path):
    fake = Fake(pr_payload(), mop={"issue": None, "source": "pr", "ambiguous": True})
    assert run(issue_less_request(), tmp_path, fake)["success"] is True
    assert not [e for e in fake.effects if e.startswith("issue:")]
    assert "pr:add:pm-state:closed-clean" in fake.effects


def test_part_of_body_keeps_labels_only(tmp_path):
    fake = Fake(pr_payload(body="Part of #8800 (slice 2)"))
    assert run(issue_less_request(), tmp_path, fake)["success"] is True
    assert not [e for e in fake.effects if e.startswith("issue:")]


def test_tracking_label_keeps_labels_only(tmp_path):
    fake = Fake(pr_payload(), issue_labels=("pm-nonclaimable:tracking",))
    assert run(issue_less_request(), tmp_path, fake)["success"] is True
    assert not [e for e in fake.effects if e.startswith("issue:")]


def test_mop_unreachable_keeps_labels_only(tmp_path):
    fake = Fake(pr_payload())

    def boom(*a):
        raise OSError("down")
    fake.mop_issue_for_pr = boom
    assert run(issue_less_request(), tmp_path, fake)["success"] is True
    assert not [e for e in fake.effects if e.startswith("issue:")]


def test_existing_closing_ref_unchanged_and_mop_not_consulted(tmp_path):
    fake = Fake(pr_payload(refs=(8800,)))
    request = {
        "repository": "heydonna-app/heydonna-app", "pr": 8801, "issue": 8800,
        "head": HEAD, "merge_commit": MERGE, "thread_reply": False, "post_merge_terminal": True,
    }
    assert run(request, tmp_path, fake)["success"] is True
    assert fake.mop_calls == []
    assert "issue:close" in fake.effects
    (receipt,) = json.loads((tmp_path / "r.json").read_text()).values()
    assert receipt["request"]["issue_authoritative"] is False
