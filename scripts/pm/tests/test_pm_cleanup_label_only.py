"""Focused proof for the label-only terminal cleanup repair.

Governing defect (PR #7769 recurrence): `pm-cleanup-pr.py` refused a
merged PR with one CLOSED/COMPLETED linked issue because the retired
`pm-transition-parent-receipts.json` mapping is absent. The repair allows
label-only terminal cleanup (no threaded Slack reply) from live GitHub
without that historical mapping, while a requested thread reply still
fails closed without it.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SPEC = importlib.util.spec_from_file_location(
    "pm_cleanup_pr", SHARED / "claude" / "scripts" / "pm-cleanup-pr.py"
)
assert SPEC and SPEC.loader
CLEANUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLEANUP)

HEAD = "c" * 40
MERGE = "5" * 40
THREAD_TS = "17899.0001"


def base_request(**overrides):
    request = {
        "repository": "heydonna-app/heydonna-app",
        "pr": 7769,
        "issue": 7768,
        "head": HEAD,
        "merge_commit": MERGE,
    }
    request.update(overrides)
    return request


def merged_pr_payload(labels=("pm-state:stale-x", "ci-passed"), refs=("single",), state="MERGED"):
    if refs == ("single",):
        closing = [{"number": 7768}]
    elif refs == ("multiple",):
        closing = [{"number": 7768}, {"number": 7767}]
    elif refs == ("none",):
        closing = []
    else:
        closing = refs
    return {
        "number": 7769,
        "state": state,
        "mergeCommit": {"oid": MERGE},
        "headRefOid": HEAD,
        "closingIssuesReferences": closing,
        "labels": list(labels),
    }


def closed_issue_payload(labels=("status:todo",), state="CLOSED", reason="COMPLETED"):
    return {"number": 7768, "state": state, "stateReason": reason, "labels": list(labels)}


class FakeExternal:
    def __init__(self, pr, issue):
        self._pr = pr
        self._issue = issue
        self.effects: list[str] = []
        self.slack_posts: list[str] = []

    def read_pr(self, request):
        return json.loads(json.dumps(self._pr))

    def read_issue(self, request):
        return json.loads(json.dumps(self._issue))

    def _mut(self, scope, label, op):
        target = self._pr if scope == "pr" else self._issue
        labels = target.setdefault("labels", [])
        if op == "add":
            if label not in labels:
                labels.append(label)
        else:
            target["labels"] = [item for item in labels if item != label]
        self.effects.append(f"{scope}:{op}:{label}")

    def add_pr_label(self, request, label):
        self._mut("pr", label, "add")

    def remove_pr_label(self, request, label):
        self._mut("pr", label, "remove")

    def add_issue_label(self, request, label):
        self._mut("issue", label, "add")

    def remove_issue_label(self, request, label):
        self._mut("issue", label, "remove")

    def close_issue(self, request):
        self._issue["state"] = "CLOSED"
        self._issue["stateReason"] = "COMPLETED"
        self.effects.append("issue:close")

    def slack_auth(self, token):
        assert token == "x"
        return {"ok": True, "user_id": "U0BNFGX2UAX"}

    def slack_replies(self, token, thread_ts):
        messages = [
            {"ts": thread_ts, "user": "U0BNFGX2UAX", "channel": "C0ALZJHGE49", "text": "parent"},
        ]
        messages.extend(
            {"ts": f"{thread_ts}.{i}", "user": "U0BNFGX2UAX", "thread_ts": thread_ts, "text": text}
            for i, text in enumerate(self.slack_posts)
        )
        return {"ok": True, "messages": messages}

    def slack_post(self, token, thread_ts, text):
        self.slack_posts.append(text)
        self.effects.append("slack:reply")


def run_cleanup(request, tmp_path, external, mapping_path=None, token=None):
    return CLEANUP.run(
        request,
        mapping_path=Path(mapping_path) if mapping_path is not None else tmp_path / "absent.json",
        receipt_path=tmp_path / "receipts.json",
        external=external,
        cto_slack_token=token,
    )


def test_threaded_request_without_mapping_refuses_like_pr7769_recurrence(tmp_path):
    external = FakeExternal(merged_pr_payload(), closed_issue_payload())
    with pytest.raises(CLEANUP.CleanupError) as excinfo:
        run_cleanup(base_request(), tmp_path, external, token="x")
    assert excinfo.value.reason == "thread_mapping_missing_or_ambiguous"
    assert external.effects == []


def test_label_only_completes_without_mapping_and_posts_no_reply(tmp_path):
    external = FakeExternal(merged_pr_payload(), closed_issue_payload())
    result = run_cleanup(base_request(thread_reply=False), tmp_path, external, mapping_path=tmp_path)
    assert result["success"] is True and result["status"] == "completed"
    assert result["thread_ts"] is None and result["thread_reply"] is False
    assert "slack:reply" not in external.effects and "issue:close" not in external.effects
    assert "pr:add:pm-state:closed-clean" in external.effects
    assert "issue:add:status:done" in external.effects


def test_label_only_receipt_plan_is_label_ops_only(tmp_path):
    external = FakeExternal(merged_pr_payload(), closed_issue_payload())
    run_cleanup(base_request(thread_reply=False), tmp_path, external, mapping_path=tmp_path)
    receipts = json.loads((tmp_path / "receipts.json").read_text())
    assert len(receipts) == 1
    (receipt,) = receipts.values()
    assert receipt["thread_reply"] is False and receipt["thread_ts"] is None
    names = [step["name"] for step in receipt["plan"]]
    assert names and all(name.startswith(("pr_label_", "issue_label_")) for name in names)


def test_label_only_replay_is_idempotent_without_duplicate_effects(tmp_path):
    external = FakeExternal(merged_pr_payload(), closed_issue_payload())
    first = run_cleanup(base_request(thread_reply=False), tmp_path, external, mapping_path=tmp_path)
    assert first["idempotent"] is False
    count = len(external.effects)
    second = run_cleanup(base_request(thread_reply=False), tmp_path, external, mapping_path=tmp_path)
    assert second["success"] is True and second["idempotent"] is True
    assert second["cleanup_key"] == first["cleanup_key"]
    assert len(external.effects) == count


def test_label_only_multiple_linked_issues_refuses(tmp_path):
    external = FakeExternal(merged_pr_payload(refs=("multiple",)), closed_issue_payload())
    with pytest.raises(CLEANUP.CleanupError) as excinfo:
        run_cleanup(base_request(thread_reply=False), tmp_path, external, mapping_path=tmp_path)
    assert excinfo.value.reason == "linked_issue_not_unique"
    assert external.effects == []


def test_label_only_open_issue_refuses(tmp_path):
    external = FakeExternal(merged_pr_payload(), closed_issue_payload(state="OPEN", reason=None))
    with pytest.raises(CLEANUP.CleanupError) as excinfo:
        run_cleanup(base_request(thread_reply=False), tmp_path, external, mapping_path=tmp_path)
    assert excinfo.value.reason == "linked_issue_not_terminal"
    assert external.effects == []


def test_label_only_unmerged_pr_refuses(tmp_path):
    external = FakeExternal(merged_pr_payload(state="OPEN"), closed_issue_payload())
    with pytest.raises(CLEANUP.CleanupError) as excinfo:
        run_cleanup(base_request(thread_reply=False), tmp_path, external, mapping_path=tmp_path)
    assert excinfo.value.reason == "pr_not_merged"
    assert external.effects == []


def test_label_only_head_mismatch_refuses(tmp_path):
    external = FakeExternal(merged_pr_payload(), closed_issue_payload())
    with pytest.raises(CLEANUP.CleanupError) as excinfo:
        run_cleanup(base_request(thread_reply=False, head="d" * 40), tmp_path, external, mapping_path=tmp_path)
    assert excinfo.value.reason == "pr_head_mismatch"
    assert external.effects == []


def test_label_only_malformed_refs_refuses(tmp_path):
    payload = merged_pr_payload()
    payload["closingIssuesReferences"] = "not-a-list"
    external = FakeExternal(payload, closed_issue_payload())
    with pytest.raises(CLEANUP.CleanupError) as excinfo:
        run_cleanup(base_request(thread_reply=False), tmp_path, external, mapping_path=tmp_path)
    assert excinfo.value.reason == "linked_issue_mismatch"
    assert external.effects == []


def test_label_only_rejects_thread_ts_and_bad_flag_shapes(tmp_path):
    external = FakeExternal(merged_pr_payload(), closed_issue_payload())
    with pytest.raises(CLEANUP.CleanupError) as excinfo:
        run_cleanup(base_request(thread_reply=False, thread_ts=THREAD_TS), tmp_path, external)
    assert excinfo.value.reason == "label_only_thread_ts_forbidden"
    with pytest.raises(CLEANUP.CleanupError) as excinfo:
        run_cleanup(base_request(thread_reply="yes"), tmp_path, external)
    assert excinfo.value.reason == "thread_reply_invalid"
    assert external.effects == []


def test_threaded_request_after_label_only_still_requires_mapping(tmp_path):
    external = FakeExternal(merged_pr_payload(), closed_issue_payload())
    done = run_cleanup(base_request(thread_reply=False), tmp_path, external, mapping_path=tmp_path)
    assert done["status"] == "completed"
    with pytest.raises(CLEANUP.CleanupError) as excinfo:
        run_cleanup(base_request(), tmp_path, external, token="x")
    assert excinfo.value.reason == "thread_mapping_missing_or_ambiguous"
    assert "slack:reply" not in external.effects


def test_threaded_request_with_mapping_completes_with_reply(tmp_path):
    mapping = {
        "k1": {
            "repository": "heydonna-app/heydonna-app",
            "pr": 7769,
            "issue": 7768,
            "head": HEAD,
            "thread_ts": THREAD_TS,
            "status": "parent_created",
        }
    }
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping))
    external = FakeExternal(merged_pr_payload(), closed_issue_payload(state="OPEN", reason=None))
    result = run_cleanup(base_request(thread_ts=THREAD_TS), tmp_path, external, mapping_path=mapping_path, token="x")
    assert result["success"] is True and result["thread_ts"] == THREAD_TS
    assert result["thread_reply"] is True
    assert "slack:reply" in external.effects and "issue:close" in external.effects
