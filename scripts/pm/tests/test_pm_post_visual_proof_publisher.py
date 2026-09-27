from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts/pm/shared-assets"
PUBLISHER_SOURCE = SHARED / "claude/scripts/pm-post-visual-proof.py"
GATE_SOURCE = SHARED / "claude/scripts/qa-visual-proof-gate.py"
MANIFEST = SHARED / "manifest.json"
HEAD = "a" * 40
BODY = """<!-- qa-proof-schema:1 -->
## Issue Contract Ledger

## Acceptance criteria

| ID | Criterion | Surface | QA reachability | Required proof |
| --- | --- | --- | --- | --- |
| AC-1 | The page matches the approved layout. | visual | deterministic | screenshot |
"""
ISSUE_BODY_SHA256 = hashlib.sha256(BODY.encode("utf-8")).hexdigest()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publisher_and_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = tmp_path / "fixture-repo"
    (repo / "scripts/ci").mkdir(parents=True)
    (repo / "scripts/ci/change_scope.py").write_text("# fixture\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    return (
        load_module("pm_post_visual_proof_publisher", PUBLISHER_SOURCE),
        load_module("qa_visual_proof_gate", GATE_SOURCE),
    )


def resolved_issue_binding(publisher, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    responses = [
        subprocess.CompletedProcess(
            ["gh"],
            0,
            json.dumps({
                "number": 77,
                "title": "Implement layout for issue #88",
                "body": "Implementation for #88",
                "headRefOid": HEAD,
                "headRefName": "fix/88-layout",
                "closingIssuesReferences": [{"number": 88}],
            }),
            "",
        ),
        subprocess.CompletedProcess(
            ["gh"], 0, json.dumps({"number": 88, "body": BODY}), ""
        ),
    ]
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return responses.pop(0)

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    binding = publisher.resolve_issue_binding(77, HEAD)
    assert commands[0][:3] == ["gh", "pr", "view"]
    assert commands[1][:3] == ["gh", "issue", "view"]
    return binding


def slack_comment(publisher, *, issue: int = 88, body: str = BODY) -> dict[str, str]:
    receipt = publisher.build_slack_receipt(
        pr=77,
        issue=issue,
        issue_body=body,
        head=HEAD,
        channel="C0ALZJHGE49",
        thread_ts="1790477819.230219",
        items=[{
            "ac_id": "AC-1",
            "slack_file_id": "FTEST12345",
            "name": "ac-1.png",
            "sha256": "b" * 64,
        }],
    )
    return {
        "createdAt": "2026-09-27T00:00:00Z",
        "body": "<!-- qa-visual-proof-slack: " + json.dumps(receipt) + " -->",
    }


def install_gate_contract(monkeypatch: pytest.MonkeyPatch, gate) -> None:
    monkeypatch.setattr(
        gate,
        "proof_contract",
        lambda _body: ([{
            "id": "AC-1",
            "fields": {
                "surface": "visual",
                "qa_reachability": "deterministic",
                "required_proof": "screenshot",
            },
        }], []),
    )


def evaluate(gate, *, issue: int = 88, body: str = BODY, comments: list[dict[str, str]]):
    return gate.evaluate(
        pr=77,
        issue=issue,
        head=HEAD,
        issue_body=body,
        ui_changed=True,
        comments=comments,
        repo="heydonna-app/heydonna-app",
        verify_remote=False,
    )


def test_publisher_is_adopted_as_one_manifest_mapped_executable() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = [
        entry for entry in manifest["entries"]
        if entry["source_path"] == "claude/scripts/pm-post-visual-proof.py"
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["canonical_target"] == "/Users/rajiv/.claude/scripts/pm-post-visual-proof.py"
    assert row["mode"] == 0o755
    assert row["sha256"] == hashlib.sha256(PUBLISHER_SOURCE.read_bytes()).hexdigest()
    assert row["dependencies"] == []
    assert row["dependency_status"] == "closed"
    assert manifest["entries"] == sorted(manifest["entries"], key=lambda item: item["source_path"])
    assert manifest["inventory"]["selected_count"] == len(manifest["entries"])


def test_synthetic_publisher_receipt_passes_managed_gate(monkeypatch, tmp_path) -> None:
    publisher, gate = publisher_and_gate(monkeypatch, tmp_path)
    install_gate_contract(monkeypatch, gate)
    pr_data = {
        "number": 77,
        "title": "Implement layout for issue #88",
        "body": "Implementation for #88",
        "headRefOid": HEAD,
        "headRefName": "fix/88-layout",
        "closingIssuesReferences": [{"number": 88}],
    }
    gate_issue = gate.resolve_pr_issue_from_metadata(pr_data, pr_number=77)
    assert publisher.resolve_implementation_issue(pr_data, pr_number=77) == gate_issue == 88
    resolved_issue, resolved_body = resolved_issue_binding(publisher, monkeypatch)
    assert (resolved_issue, resolved_body) == (88, BODY)

    result = evaluate(
        gate,
        comments=[slack_comment(publisher, issue=resolved_issue, body=resolved_body)],
    )

    assert result["ok"] is True
    assert result["reason"] == "durable_screenshot_receipt_valid"
    assert result["issue"] == 88
    assert result["issue_body_sha256"] == ISSUE_BODY_SHA256


@pytest.mark.parametrize(
    ("issue", "body", "expected_error"),
    [
        (89, BODY, "receipt_issue_mismatch"),
        (88, BODY + "\nChanged acceptance criteria.\n", "receipt_issue_body_mismatch"),
    ],
)
def test_same_head_wrong_issue_or_edited_body_refuses(
    monkeypatch, tmp_path, issue: int, body: str, expected_error: str
) -> None:
    publisher, gate = publisher_and_gate(monkeypatch, tmp_path)
    install_gate_contract(monkeypatch, gate)

    result = evaluate(gate, issue=issue, body=body, comments=[slack_comment(publisher)])

    assert result["ok"] is False
    assert expected_error in result["errors"]


def test_publisher_refuses_unresolved_issue_body_before_upload(monkeypatch, tmp_path) -> None:
    publisher, _gate = publisher_and_gate(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    responses = [
        subprocess.CompletedProcess(
            ["gh"], 0,
            json.dumps({
                "number": 77,
                "title": "Implement layout for issue #88",
                "body": "Implementation for #88",
                "headRefOid": HEAD,
                "headRefName": "fix/88-layout",
                "closingIssuesReferences": [{"number": 88}],
            }),
            "",
        ),
        subprocess.CompletedProcess(["gh"], 1, "", "not found"),
    ]

    def fake_run(command, **kwargs):
        calls.append(command)
        return responses.pop(0)

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="issue body unavailable"):
        publisher.resolve_issue_binding(77, HEAD)
    assert len(calls) == 2


def test_publisher_refuses_when_issue_cannot_be_resolved(monkeypatch, tmp_path) -> None:
    publisher, _gate = publisher_and_gate(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    response = subprocess.CompletedProcess(
        ["gh"],
        0,
        json.dumps({
            "number": 77,
            "title": "Improve layout",
            "body": "No implementation issue reference.",
            "headRefOid": HEAD,
            "headRefName": "feature/layout-refresh",
            "closingIssuesReferences": [],
        }),
        "",
    )

    def fake_run(command, **kwargs):
        calls.append(command)
        return response

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="cannot resolve implementation issue"):
        publisher.resolve_issue_binding(77, HEAD)
    assert len(calls) == 1
