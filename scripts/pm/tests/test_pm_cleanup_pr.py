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


class FakeExternal(MODULE.External):
    def __init__(self, *, slack_user: str = MODULE.CTO_USER_ID, ambiguous_step: str | None = None):
        self.slack_user = slack_user
        self.ambiguous_step = ambiguous_step
        self.effects: list[tuple[str, object]] = []
        self.pr = {
            "number": 7613, "state": "MERGED", "mergedAt": "2026-09-03T10:00:00Z",
            "mergeCommit": {"oid": REQUEST["merge_commit"]}, "headRefOid": REQUEST["head"],
            "closingIssuesReferences": [{"number": 7609}],
            "labels": ["slot:1", "status:in-review", "ci-head:old", "customer-visible", "priority:P1"],
        }
        self.issue = {"number": 7609, "state": "OPEN", "labels": ["status:todo", "slot:1", "team:frontend"]}
        self.reply_texts: list[str] = []

    def read_pr(self, request):
        return self.pr

    def read_issue(self, request):
        return self.issue

    def replace_pr_labels(self, request, labels):
        self.effects.append(("pr_labels", labels))
        if self.ambiguous_step == "pr_labels":
            raise MODULE.CleanupError("github_effect_ambiguous", ambiguous=True)
        self.pr["labels"] = labels

    def replace_issue_labels(self, request, labels):
        self.effects.append(("issue_labels", labels))
        if self.ambiguous_step == "issue_labels":
            raise MODULE.CleanupError("github_effect_ambiguous", ambiguous=True)
        self.issue["labels"] = labels

    def close_issue(self, request):
        self.effects.append(("issue_close", None))
        if self.ambiguous_step == "issue_close":
            raise MODULE.CleanupError("github_effect_ambiguous", ambiguous=True)
        self.issue["state"] = "CLOSED"

    def slack_auth(self, token):
        return {"ok": True, "user_id": self.slack_user}

    def slack_replies(self, token, thread_ts):
        messages = [{"ts": REQUEST["thread_ts"], "user": MODULE.CTO_USER_ID, "channel": MODULE.SLACK_CHANNEL, "text": "canonical parent"}]
        messages.extend({"ts": f"reply-{i}", "user": MODULE.CTO_USER_ID, "thread_ts": thread_ts, "channel": MODULE.SLACK_CHANNEL, "text": text} for i, text in enumerate(self.reply_texts))
        return {"ok": True, "messages": messages}

    def slack_post(self, token, thread_ts, text):
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
    external = FakeExternal()
    result = MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert result["success"] is True
    assert result["idempotent"] is False
    assert external.pr["labels"] == ["customer-visible", "pm-state:closed-clean", "priority:P1"]
    assert external.issue["labels"] == ["status:done", "team:frontend"]
    assert external.issue["state"] == "CLOSED"
    assert [name for name, _ in external.effects] == ["pr_labels", "issue_labels", "issue_close", "thread_reply"]
    replay = MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert replay["idempotent"] is True
    assert len(external.effects) == 4


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


def test_response_loss_is_durable_ambiguous_and_never_replayed(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    external = FakeExternal(ambiguous_step="thread_reply")
    with pytest.raises(MODULE.CleanupError, match="slack_effect_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert [name for name, _ in external.effects] == ["pr_labels", "issue_labels", "issue_close", "thread_reply"]
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert len(external.effects) == 4


def test_github_response_loss_is_ambiguous_and_concurrent_replay_is_effect_free(tmp_path: Path):
    mapping = tmp_path / "mapping.json"
    receipt = tmp_path / "receipt.json"
    mapping_file(mapping)
    external = FakeExternal(ambiguous_step="pr_labels")
    with pytest.raises(MODULE.CleanupError, match="github_effect_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    with pytest.raises(MODULE.CleanupError, match="cleanup_ambiguous"):
        MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto")
    assert [name for name, _ in external.effects] == ["pr_labels"]

    mapping_file(mapping)
    receipt.unlink()
    external = FakeExternal()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: MODULE.run(REQUEST, mapping_path=mapping, receipt_path=receipt, external=external, cto_slack_token="cto"), range(2)))
    assert sorted(result["idempotent"] for result in results) == [False, True]
    assert [name for name, _ in external.effects] == ["pr_labels", "issue_labels", "issue_close", "thread_reply"]


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
