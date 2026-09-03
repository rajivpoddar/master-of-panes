from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "scripts/pm/shared-assets/claude/scripts/pm-cleanup-pr.py"
SPEC = importlib.util.spec_from_file_location("pm_cleanup_pr", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


REQUEST = {
    "repository": "heydonna-app/heydonna-app",
    "pr": 7613,
    "issue": 7609,
    "head": "bc83a0c724810e14a05a6aef182d7066e7d9e4f3",
    "merge_commit": "a54166e439d25876deea4e9cf648732cbc47f7cc",
    "thread_ts": "1788434276.706249",
}

ISSUELESS_REQUEST = {
    "cleanup_mode": "merged_pr_issue_less",
    "repository": "heydonna-app/heydonna-app",
    "pr": 7626,
    "issue": None,
    "head": "d7f04ce21a7276c0f01220d349e84c0a4f28db37",
    "merge_commit": "7dc6320bf9e022d8458fd24f4b1301c9b250b95b",
}


class FakeExternal(MODULE.External):
    def __init__(self, *, slack_user: str = MODULE.CTO_USER_ID, ambiguous_step: str | None = None, crash_step: str | None = None, concurrent_label: str | None = None, close_reason: str | None = "COMPLETED"):
        self.slack_user = slack_user
        self.ambiguous_step = ambiguous_step
        self.crash_step = crash_step
        self.concurrent_label = concurrent_label
        self.close_reason = close_reason
        self.effects: list[tuple[str, object]] = []
        self.issue_reads = 0
        self.slack_calls = 0
        self.pr = {
            "number": 7613, "state": "MERGED", "mergedAt": "2026-09-03T10:00:00Z",
            "mergeCommit": {"oid": REQUEST["merge_commit"]}, "headRefOid": REQUEST["head"],
            "closingIssuesReferences": [{"number": 7609}],
            "labels": ["slot:1", "status:in-review", "ci-head:old", "customer-visible", "priority:P1"],
        }
        self.issue = {"number": 7609, "state": "OPEN", "stateReason": None, "labels": ["status:todo", "slot:1", "team:frontend"]}
        self.reply_texts: list[str] = []

    def read_pr(self, request):
        return self.pr

    def read_issue(self, request):
        self.issue_reads += 1
        return self.issue

    def _label_effect(self, scope, operation, label):
        name = f"{scope}_label_{operation}:{label}"
        self.effects.append((name, label))
        labels = self.pr["labels"] if scope == "pr" else self.issue["labels"]
        if scope == "pr" and operation == "remove" and self.concurrent_label and self.concurrent_label not in labels:
            labels.append(self.concurrent_label)
        if operation == "add":
            if label not in labels:
                labels.append(label)
        else:
            while label in labels:
                labels.remove(label)
        if self.crash_step == name:
            raise SystemExit("injected process death")
        if self.ambiguous_step in {scope + "_labels", name}:
            raise MODULE.CleanupError("github_effect_ambiguous", ambiguous=True)

    def add_pr_label(self, request, label):
        self._label_effect("pr", "add", label)

    def remove_pr_label(self, request, label):
        self._label_effect("pr", "remove", label)

    def add_issue_label(self, request, label):
        self._label_effect("issue", "add", label)

    def remove_issue_label(self, request, label):
        self._label_effect("issue", "remove", label)

    def close_issue(self, request):
        self.effects.append(("issue_close", None))
        if self.crash_step == "issue_close":
            raise SystemExit("injected process death")
        if self.ambiguous_step == "issue_close":
            raise MODULE.CleanupError("github_effect_ambiguous", ambiguous=True)
        self.issue["state"] = "CLOSED"
        if self.close_reason is None:
            self.issue.pop("stateReason", None)
        else:
            self.issue["stateReason"] = self.close_reason

    def slack_auth(self, token):
        self.slack_calls += 1
        return {"ok": True, "user_id": self.slack_user}

    def slack_replies(self, token, thread_ts):
        self.slack_calls += 1
        messages = [{"ts": REQUEST["thread_ts"], "user": MODULE.CTO_USER_ID, "channel": MODULE.SLACK_CHANNEL, "text": "canonical parent"}]
        messages.extend({"ts": f"reply-{i}", "user": MODULE.CTO_USER_ID, "thread_ts": thread_ts, "channel": MODULE.SLACK_CHANNEL, "text": text} for i, text in enumerate(self.reply_texts))
        return {"ok": True, "messages": messages}

    def slack_post(self, token, thread_ts, text):
        self.slack_calls += 1
        self.effects.append(("thread_reply", text))
        if self.ambiguous_step == "thread_reply":
            self.reply_texts.append(text)
            raise MODULE.CleanupError("slack_effect_ambiguous", ambiguous=True)
        self.reply_texts.append(text)
        return {"ok": True, "ts": "1789000000.000001", "channel": MODULE.SLACK_CHANNEL}


def mapping_file(path: Path) -> None:
    path.write_text(json.dumps({"assignment-generation": {
        "status": "parent_created", "repository_id": REQUEST["repository"], "issue": REQUEST["issue"],
        "pr": REQUEST["pr"], "head_sha": REQUEST["head"], "thread_ts": REQUEST["thread_ts"],
    }}), encoding="utf-8")


def test_merged_tuple_preserves_unrelated_labels_and_replies_once(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    external = FakeExternal(concurrent_label="owner-added-concurrently")
    result = MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert result["success"] is True
    assert result["idempotent"] is False
    assert set(external.pr["labels"]) == {
        "customer-visible", "pm-state:closed-clean", "priority:P1", "owner-added-concurrently",
    }
    assert set(external.issue["labels"]) == {"status:done", "team:frontend"}
    assert external.issue["state"] == "CLOSED"
    assert [name for name, _ in external.effects] == [
        "pr_label_remove:ci-head:old", "pr_label_remove:slot:1",
        "pr_label_remove:status:in-review", "pr_label_add:pm-state:closed-clean",
        "issue_label_remove:slot:1", "issue_label_remove:status:todo",
        "issue_label_add:status:done", "issue_close", "thread_reply",
    ]
    replay = MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert replay["idempotent"] is True
    assert len(external.effects) == 9


def test_stale_head_and_nonmerged_pr_fail_before_effect(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    external = FakeExternal()
    external.pr["headRefOid"] = "0" * 40
    with pytest.raises(MODULE.CleanupError, match="pr_head_mismatch"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert external.effects == []
    external.pr["headRefOid"] = REQUEST["head"]
    external.pr["state"] = "OPEN"
    with pytest.raises(MODULE.CleanupError, match="pr_not_merged"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert external.effects == []


def test_wrong_bot_and_stale_mapping_fail_before_effect(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    external = FakeExternal(slack_user="U-wrong")
    with pytest.raises(MODULE.CleanupError, match="slack_identity_mismatch"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert external.effects == []
    mapping.write_text("{}", encoding="utf-8")
    with pytest.raises(MODULE.CleanupError, match="thread_mapping_missing_or_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=FakeExternal(), cto_slack_token="cto")


def test_duplicate_tuple_mapping_is_rejected_before_slack(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping.write_text(json.dumps({
        "first": {
            "status": "parent_created", "repository_id": REQUEST["repository"], "issue": REQUEST["issue"],
            "pr": REQUEST["pr"], "head_sha": REQUEST["head"], "thread_ts": REQUEST["thread_ts"],
        },
        "second": {
            "status": "thread_replied", "repository_id": REQUEST["repository"], "issue": REQUEST["issue"],
            "pr": REQUEST["pr"], "head_sha": REQUEST["head"], "thread_ts": "1788434277.706249",
        },
    }), encoding="utf-8")
    external = FakeExternal()
    with pytest.raises(MODULE.CleanupError, match="thread_mapping_missing_or_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert external.effects == []


def test_caller_thread_cannot_select_a_different_authoritative_mapping(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    request = {**REQUEST, "thread_ts": "1788434277.706249"}
    with pytest.raises(MODULE.CleanupError, match="caller_thread_mapping_mismatch"):
        MODULE.run(request, mapping_path=mapping, receipt_path=receipt, external=FakeExternal(), cto_slack_token="cto")
    assert not receipt.exists()


def test_started_step_is_not_replayed_and_remaining_steps_resume(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    external = FakeExternal(crash_step="pr_label_remove:ci-head:old")
    with pytest.raises(SystemExit):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    names = [name for name, _ in external.effects]
    assert names.count("pr_label_remove:ci-head:old") == 1
    assert "thread_reply" in names


@pytest.mark.parametrize("close_reason", ["NOT_PLANNED", None])
def test_issue_close_requires_completed_reason_before_terminal_reply(tmp_path: Path, close_reason: str | None):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    external = FakeExternal(close_reason=close_reason)
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert [name for name, _ in external.effects].count("issue_close") == 1
    assert "thread_reply" not in [name for name, _ in external.effects]
    receipt_state = json.loads(receipt.read_text(encoding="utf-8"))
    cleanup_receipt = next(iter(receipt_state.values()))
    assert "thread_reply" not in cleanup_receipt["steps"]
    assert "thread_reply" not in cleanup_receipt["ambiguous_steps"]
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert "thread_reply" not in [name for name, _ in external.effects]


def test_response_loss_is_durable_ambiguous_and_never_replayed(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    external = FakeExternal(ambiguous_step="thread_reply")
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert [name for name, _ in external.effects].count("pr_label_remove:ci-head:old") == 1
    assert [name for name, _ in external.effects].count("thread_reply") == 1
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert len(external.effects) == 9


def test_github_response_loss_is_ambiguous_and_concurrent_replay_is_effect_free(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    external = FakeExternal(ambiguous_step="pr_labels")
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert [name for name, _ in external.effects].count("pr_label_remove:ci-head:old") == 1

    mapping_file(mapping)
    receipt.unlink()
    external = FakeExternal()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto"), range(2)))
    assert sorted(result["idempotent"] for result in results) == [False, True]
    assert [name for name, _ in external.effects].count("thread_reply") == 1


def test_stale_slot_label_never_calls_mop_and_manifest_is_exact(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    external = FakeExternal()
    MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert all(name not in {"mop_release", "mop_clear", "mop_send"} for name, _ in external.effects)
    manifest = json.loads((ROOT / "scripts/pm/shared-assets/manifest.json").read_text(encoding="utf-8"))
    entries = {entry["source_path"]: entry for entry in manifest["entries"]}
    assert entries["claude/scripts/pm-cleanup-pr.py"]["canonical_target"] == "/Users/rajiv/.claude/scripts/pm-cleanup-pr.py"
    assert entries["claude/skills/pm-cleanup-pr/SKILL.md"]["canonical_target"] == "/Users/rajiv/.claude/skills/pm-cleanup-pr/SKILL.md"
    assert entries["claude/scripts/pm-cleanup-pr.py"]["mode"] == 0o755
    assert entries["claude/skills/pm-cleanup-pr/SKILL.md"]["mode"] == 0o644


def test_issue_less_merged_pr_cleanup_preserves_labels_and_skips_issue_slack(tmp_path: Path):
    receipt = tmp_path / "receipt.json"
    external = FakeExternal(concurrent_label="owner-added-concurrently")
    external.pr.update({
        "number": 7626,
        "mergeCommit": {"oid": ISSUELESS_REQUEST["merge_commit"]},
        "headRefOid": ISSUELESS_REQUEST["head"],
        "closingIssuesReferences": [],
    })
    result = MODULE.run(ISSUELESS_REQUEST, receipt_path=receipt, external=external)
    assert result["success"] is True
    assert result["idempotent"] is False
    assert set(external.pr["labels"]) == {
        "customer-visible", "priority:P1", "owner-added-concurrently", "pm-state:closed-clean",
    }
    assert external.issue_reads == 0
    assert external.slack_calls == 0
    assert [name for name, _ in external.effects] == [
        "pr_label_remove:ci-head:old", "pr_label_remove:slot:1",
        "pr_label_remove:status:in-review", "pr_label_add:pm-state:closed-clean",
    ]
    replay = MODULE.run(ISSUELESS_REQUEST, receipt_path=receipt, external=external)
    assert replay["idempotent"] is True
    assert len(external.effects) == 4


def test_issue_less_cleanup_rejects_linked_issue_before_effect(tmp_path: Path):
    receipt = tmp_path / "receipt.json"
    external = FakeExternal()
    external.pr.update({
        "number": 7626,
        "mergeCommit": {"oid": ISSUELESS_REQUEST["merge_commit"]},
        "headRefOid": ISSUELESS_REQUEST["head"],
    })
    with pytest.raises(MODULE.CleanupError, match="issue_less_linked_issue_present"):
        MODULE.run(ISSUELESS_REQUEST, receipt_path=receipt, external=external)
    assert external.effects == []
    assert external.issue_reads == 0
    assert external.slack_calls == 0
    assert not receipt.exists()


@pytest.mark.parametrize("field", ["headRefOid", "mergeCommit"])
def test_issue_less_cleanup_rejects_stale_merge_tuple(tmp_path: Path, field: str):
    receipt = tmp_path / "receipt.json"
    external = FakeExternal()
    external.pr.update({
        "number": 7626,
        "mergeCommit": {"oid": ISSUELESS_REQUEST["merge_commit"]},
        "headRefOid": ISSUELESS_REQUEST["head"],
        "closingIssuesReferences": [],
    })
    if field == "headRefOid":
        external.pr[field] = "0" * 40
    else:
        external.pr[field] = {"oid": "0" * 40}
    with pytest.raises(MODULE.CleanupError, match="(pr_head_mismatch|merge_commit_mismatch)"):
        MODULE.run(ISSUELESS_REQUEST, receipt_path=receipt, external=external)
    assert external.effects == []


def test_issue_less_response_loss_is_permanently_ambiguous(tmp_path: Path):
    receipt = tmp_path / "receipt.json"
    external = FakeExternal(ambiguous_step="pr_label_remove:ci-head:old")
    external.pr.update({
        "number": 7626,
        "mergeCommit": {"oid": ISSUELESS_REQUEST["merge_commit"]},
        "headRefOid": ISSUELESS_REQUEST["head"],
        "closingIssuesReferences": [],
    })
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(ISSUELESS_REQUEST, receipt_path=receipt, external=external)
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(ISSUELESS_REQUEST, receipt_path=receipt, external=external)
    assert [name for name, _ in external.effects].count("pr_label_remove:ci-head:old") == 1
    assert external.issue_reads == 0
    assert external.slack_calls == 0


def test_issue_less_process_death_resumes_remaining_steps_without_redelivery(tmp_path: Path):
    receipt = tmp_path / "receipt.json"
    external = FakeExternal(crash_step="pr_label_remove:ci-head:old")
    external.pr.update({
        "number": 7626,
        "mergeCommit": {"oid": ISSUELESS_REQUEST["merge_commit"]},
        "headRefOid": ISSUELESS_REQUEST["head"],
        "closingIssuesReferences": [],
    })
    with pytest.raises(SystemExit):
        MODULE.run(ISSUELESS_REQUEST, receipt_path=receipt, external=external)
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(ISSUELESS_REQUEST, receipt_path=receipt, external=external)
    assert [name for name, _ in external.effects].count("pr_label_remove:ci-head:old") == 1
    assert [name for name, _ in external.effects].count("pr_label_remove:slot:1") == 1
    assert external.issue_reads == 0
    assert external.slack_calls == 0
