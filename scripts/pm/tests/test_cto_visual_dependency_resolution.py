from __future__ import annotations

import hashlib
import importlib.util
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "scripts/pm/shared-assets/claude/scripts/qa-visual-proof-gate.py"
VALIDATOR = ROOT / "scripts/pm/shared-assets/claude/scripts/validate-issue-contract-ledger.py"
CLASSIFIER = ROOT / "scripts/pm/shared-assets/claude/scripts/ci/change_scope.py"
RULES = ROOT / "scripts/pm/shared-assets/claude/scripts/ci/change-scope-rules.json"
ADAPTER = ROOT / "scripts/pm/shared-assets/claude/scripts/ci/heydonna-cto-label-gated-ci.py"
READINESS = ROOT / "scripts/pm/shared-assets/claude/pr-ci-readiness-gate.py"

HEAD = "4604bc0d154f8fc8519be4c5547095d271bdd75f"
PR = 7639
ISSUE = 7638


def _body() -> str:
    return """<!-- qa-proof-schema: 1 -->
## Issue Contract Ledger

| Contract row | Requirement |
| --- | --- |
| Positive AC | The gate accepts the exact visual proof. |
| Negative AC | Missing proof is blocked. |
| Forbidden implementation | Do not bypass the visual gate. |
| Determinism | The proof tuple is deterministic. |
| Required proof | Screenshot proof. |

## Acceptance criteria

- AC-1: exact visual proof
  - Surface: visual
  - QA reachability: deterministic
  - Required proof: screenshot
  - QA scenario: one exact-head visual proof
"""


def _fake_gh(
    path: Path,
    body: str,
    *,
    changed_path: str = "app/page.tsx",
    pr_head: str = HEAD,
    api_head: str | None = None,
    include_receipt: bool = True,
    receipt_body: str | None = None,
) -> None:
    body_json = json.dumps(body)
    receipt_body = body if receipt_body is None else receipt_body
    receipt = {
        "schema": "heydonna_qa_visual_proof",
        "version": 1,
        "pr": PR,
        "issue": ISSUE,
        "head_sha": HEAD,
        "issue_body_sha256": hashlib.sha256(receipt_body.encode()).hexdigest(),
        "verdict": "pass",
        "scenarios": [{
            "ac_id": "AC-1",
            "artifact_kind": "local_tmp",
            "artifact_url": "/tmp/visual-proof.png",
            "sha256": "0" * 64,
            "viewport": "1440x900",
            "state": "passed",
            "captured_at": "2026-09-05T00:00:00Z",
        }],
    }
    comments_json = json.dumps(
        [{"body": "<!-- qa-visual-proof: " + json.dumps(receipt, sort_keys=True) + " -->"}]
        if include_receipt
        else []
    )
    issue_number = ISSUE
    issue_body_json = body_json
    api_head = pr_head if api_head is None else api_head
    script = f"""#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args[:2] == ["pr", "view"]:
    print(json.dumps({{
        "number": {PR},
        "title": "visual gate fixture",
        "body": "",
        "state": "OPEN",
        "headRefOid": "{pr_head}",
        "headRefName": "fix/{PR}-visual-gate",
        "closingIssuesReferences": [{{"number": {issue_number}}}],
        "comments": {comments_json}
    }}))
elif args[:2] == ["issue", "view"]:
    print(json.dumps({{"number": {issue_number}, "body": {issue_body_json}, "state": "OPEN"}}))
elif args[:1] == ["api"] and "files?per_page=100" in " ".join(args):
    print("{changed_path}")
elif args[:1] == ["api"] and "pulls/{PR}" in " ".join(args):
    print("{api_head}")
else:
    print("unexpected read-only gh invocation", file=sys.stderr)
    raise SystemExit(2)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _layout(tmp_path: Path, body: str, **gh_kwargs: object) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    scripts = runtime / ".claude" / "scripts"
    (scripts / "ci").mkdir(parents=True)
    (scripts / "pm").mkdir()
    shutil.copy2(GATE, scripts / "qa-visual-proof-gate.py")
    shutil.copy2(VALIDATOR, scripts / "validate-issue-contract-ledger.py")
    shutil.copy2(CLASSIFIER, scripts / "ci" / "change_scope.py")
    shutil.copy2(RULES, scripts / "ci" / "change-scope-rules.json")
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    _fake_gh(gh, body, **gh_kwargs)
    return runtime, gh


def _run_gate(gate: Path, gh: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{gh.parent}:{env['PATH']}"
    return subprocess.run(
        [sys.executable, str(gate), "--pr", str(PR), "--expect-head", HEAD,
         "--skip-artifact-availability", "--json"],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_real_gate_resolves_canonical_installed_layout_without_retired_resolver(tmp_path: Path) -> None:
    body = _body()
    runtime, gh = _layout(tmp_path, body)
    gate = runtime / ".claude" / "scripts" / "qa-visual-proof-gate.py"

    result = _run_gate(gate, gh, tmp_path)

    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["issue"] == ISSUE
    assert payload["head_sha"] == HEAD
    assert payload["issue_body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert payload["reason"] == "durable_screenshot_receipt_valid"
    assert not (runtime / "scripts" / "pm" / "resolve_pr_issue.py").exists()


def test_non_ui_gate_result_through_actual_adapter_preserves_issue_body(tmp_path: Path, monkeypatch) -> None:
    body = _body()
    runtime, gh = _layout(tmp_path, body, changed_path="scripts/pm/control-plane/example.py")
    gate = runtime / ".claude" / "scripts" / "qa-visual-proof-gate.py"
    validator = runtime / ".claude" / "scripts" / "validate-issue-contract-ledger.py"
    spec = importlib.util.spec_from_file_location("adapter", ADAPTER)
    assert spec and spec.loader
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    adapter.VISUAL_GATE = gate
    adapter.ISSUE_CONTRACT_VALIDATOR = validator
    assert adapter.VISUAL_GATE_SHA256 == "b9bbb10da4adc35c50e1820da1ccb3e87dd4e0468b823783360efe1314e82271"
    assert hashlib.sha256(gate.read_bytes()).hexdigest() == adapter.VISUAL_GATE_SHA256
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")

    adapter._visual_proof(argparse.Namespace(pr=PR, head=HEAD, gh=str(gh)), hashlib.sha256(body.encode()).hexdigest())


def test_stale_head_fails_closed_before_classification(tmp_path: Path) -> None:
    body = _body()
    runtime, gh = _layout(tmp_path, body, pr_head="0" * 40)
    result = _run_gate(runtime / ".claude" / "scripts" / "qa-visual-proof-gate.py", gh, tmp_path)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["reason"] == "gate_error"
    assert any("head mismatch" in error for error in payload["errors"])


@pytest.mark.parametrize(
    "include_receipt,receipt_body,expected_reason",
    [(False, None, "durable_screenshot_receipt_missing"), (True, "different body", "durable_screenshot_receipt_invalid")],
)
def test_ui_missing_or_wrong_body_receipt_refuses(
    tmp_path: Path, include_receipt: bool, receipt_body: str | None, expected_reason: str
) -> None:
    body = _body()
    runtime, gh = _layout(tmp_path, body, include_receipt=include_receipt, receipt_body=receipt_body)
    result = _run_gate(runtime / ".claude" / "scripts" / "qa-visual-proof-gate.py", gh, tmp_path)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == expected_reason


def test_missing_classifier_fails_closed_before_any_effect(tmp_path: Path) -> None:
    body = _body()
    runtime, gh = _layout(tmp_path, body)
    gate = runtime / ".claude" / "scripts" / "qa-visual-proof-gate.py"
    (runtime / ".claude" / "scripts" / "ci" / "change_scope.py").unlink()

    result = _run_gate(gate, gh, tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "cannot resolve repository root" in result.stderr


def test_missing_issue_validator_fails_closed_after_root_resolution(tmp_path: Path) -> None:
    body = _body()
    runtime, gh = _layout(tmp_path, body)
    gate = runtime / ".claude" / "scripts" / "qa-visual-proof-gate.py"
    (runtime / ".claude" / "scripts" / "validate-issue-contract-ledger.py").unlink()

    result = _run_gate(gate, gh, tmp_path)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "gate_error"
    assert any("cannot resolve issue-contract-ledger validator" in error for error in payload["errors"])


def test_adapter_pins_canonical_gate_and_validator() -> None:
    text = ADAPTER.read_text(encoding="utf-8")
    assert 'Path("/Users/rajiv/.claude/scripts/pr-ci-readiness-gate.py")' in text
    assert "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pr-ci-readiness-gate.py" not in text
    assert 'Path("/Users/rajiv/.claude/scripts/qa-visual-proof-gate.py")' in text
    assert 'Path("/Users/rajiv/.claude/scripts/validate-issue-contract-ledger.py")' in text
    assert "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/qa-visual-proof-gate.py" not in text


def _load_adapter():
    spec = importlib.util.spec_from_file_location("adapter_readiness", ADAPTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_readiness(path: Path, monkeypatch):
    policy = types.ModuleType("control_plane_issue_policy")
    policy.validate_live_followup_issue = lambda *args, **kwargs: []
    monkeypatch.setitem(sys.modules, "control_plane_issue_policy", policy)
    spec = importlib.util.spec_from_file_location("packaged_readiness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_readiness_uses_canonical_classifier_and_repo_root(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime" / ".claude" / "scripts"
    runtime.mkdir(parents=True)
    readiness = runtime / "pr-ci-readiness-gate.py"
    shutil.copy2(READINESS, readiness)
    classifier = runtime / "ci" / "change_scope.py"
    classifier.parent.mkdir()
    classifier.write_text(
        "import json, sys\n"
        "head = sys.argv[sys.argv.index('--expected-head') + 1]\n"
        "print(json.dumps({'head': head, 'rules_sha256': '1' * 64}))\n",
        encoding="utf-8",
    )
    classifier.chmod(classifier.stat().st_mode | stat.S_IXUSR)
    repo_root = runtime / "repo"
    repo_root.mkdir()
    module = _load_readiness(readiness, monkeypatch)
    module.CANONICAL_CHANGE_SCOPE = classifier
    module.CANONICAL_REPO_ROOT = repo_root

    result = module.exact_head_change_scope(PR, HEAD, "heydonna-app/heydonna-app")

    assert result["head"] == HEAD
    assert result["classifier_sha256"] == hashlib.sha256(classifier.read_bytes()).hexdigest()


def test_packaged_readiness_missing_classifier_or_repo_root_fails_closed(tmp_path: Path, monkeypatch) -> None:
    readiness = tmp_path / "pr-ci-readiness-gate.py"
    shutil.copy2(READINESS, readiness)
    module = _load_readiness(readiness, monkeypatch)
    module.CANONICAL_CHANGE_SCOPE = tmp_path / "missing-change_scope.py"
    with pytest.raises(FileNotFoundError, match="repository change_scope.py is unavailable"):
        module.change_scope_script()

    classifier = tmp_path / "change_scope.py"
    classifier.write_text("print('{}')\n", encoding="utf-8")
    module.CANONICAL_CHANGE_SCOPE = classifier
    module.CANONICAL_REPO_ROOT = tmp_path / "missing-repo"
    with pytest.raises(FileNotFoundError, match="canonical repository root is unavailable"):
        module.exact_head_change_scope(PR, HEAD, "heydonna-app/heydonna-app")


def test_headless_readiness_gate_error_is_not_reported_as_head_drift(monkeypatch) -> None:
    adapter = _load_adapter()
    captured: list[list[str]] = []
    monkeypatch.setattr(adapter, "_trusted_asset", lambda *args: None)
    monkeypatch.setattr(
        adapter,
        "_run",
        lambda command, **kwargs: (captured.append(command) or subprocess.CompletedProcess(
            command,
            1,
            json.dumps({"schema": "heydonna_pr_ci_readiness_gate", "version": 2, "ok": False, "reason": "gate_error", "errors": ["invalid JSON"]}),
            "",
        )),
    )
    with pytest.raises(adapter.Refusal, match=r"readiness_gate_error:gate_error"):
        adapter._gate(argparse.Namespace(pr=PR, head=HEAD), reentry=False)
    assert captured and str(adapter.READINESS_GATE) == "/Users/rajiv/.claude/scripts/pr-ci-readiness-gate.py"


def test_canonical_readiness_child_positive_uses_exact_head_and_inventory(tmp_path: Path, monkeypatch) -> None:
    adapter = _load_adapter()
    child = tmp_path / "pr-ci-readiness-gate.py"
    child.write_text(
        "import json\n"
        "print(json.dumps({\"headRefOid\": " + repr(HEAD) + ", \"ok\": True, \"artifacts\": {"
        "\"workflows\": {\"state\": \"green\"}, \"change_scope\": {"
        "\"head\": " + repr(HEAD) + ", \"ci_required\": True, \"e2e_required\": True, "
        "\"control_plane_only\": False}}}))\n",
        encoding="utf-8",
    )
    child.chmod(child.stat().st_mode | stat.S_IXUSR)
    adapter.READINESS_GATE = child
    adapter.READINESS_GATE_SHA256 = hashlib.sha256(child.read_bytes()).hexdigest()
    payload = adapter._gate(argparse.Namespace(pr=PR, head=HEAD), reentry=False)
    assert payload["headRefOid"] == HEAD
    assert payload["artifacts"]["workflows"]["state"] == "green"


def test_readiness_gate_malformed_child_is_distinct_and_head_mismatch_remains_drift(monkeypatch) -> None:
    adapter = _load_adapter()
    monkeypatch.setattr(adapter, "_trusted_asset", lambda *args: None)
    malformed = subprocess.CompletedProcess(["readiness"], 1, "not-json", "")
    monkeypatch.setattr(adapter, "_run", lambda *args, **kwargs: malformed)
    with pytest.raises(adapter.Refusal, match="readiness_gate_malformed"):
        adapter._gate(argparse.Namespace(pr=PR, head=HEAD), reentry=False)

    mismatch = subprocess.CompletedProcess(
        ["readiness"],
        0,
        json.dumps({"headRefOid": "0" * 40, "ok": True}),
        "",
    )
    monkeypatch.setattr(adapter, "_run", lambda *args, **kwargs: mismatch)
    with pytest.raises(adapter.Refusal, match="readiness_head_drift"):
        adapter._gate(argparse.Namespace(pr=PR, head=HEAD), reentry=False)
