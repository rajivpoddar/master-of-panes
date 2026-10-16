"""Exact-head classifier decisions are shared by admission and final merge."""
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets"
sys.path.insert(0, str(ASSETS / "codex/scripts"))
import classifier_requirements as reader

HEAD = "a" * 40


def load(name, skill, filename):
    path = ASSETS / f"codex/skills/{skill}/scripts/{filename}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("result,expected", [
    ({"head": HEAD, "e2e_required": False}, False),
    ({"head": "b" * 40, "e2e_required": False}, True),
    ({"head": HEAD}, True),
    ({"head": HEAD, "e2e_required": True}, True),
    ({"head": HEAD, "e2e_required": "false"}, True),
    ({"head": HEAD, "e2e_required": 0}, True),
    (None, True), ([], True),
])
def test_shared_decision_and_both_consumers(result, expected):
    decision = reader.requirements(result, HEAD)
    assert decision == {"head": HEAD, "ci_required": True, "e2e_required": expected}
    pr = {"number": 10047, "head": {"sha": HEAD, "ref": "fix/example"}, "labels": []}
    admission = load("admit_requirements", "heydonna-cto-label-gated-ci", "admit")
    workflows = []
    def command(*args, **kwargs):
        workflows.append(args[-1])
        return json.dumps([{"workflow_runs": []}])
    with patch.object(admission, "workflow_requirements", return_value=decision), \
            patch.object(admission, "command", side_effect=command):
        assert admission.existing(pr) is None
    assert any("ci.yml" in item for item in workflows)
    assert any("e2e.yml" in item for item in workflows) is expected

    merge = load("merge_requirements", "heydonna-open-pr-status", "merge")
    calls = []
    def proof(pr, workflow, names):
        calls.append(workflow)
        return {"head": HEAD, "run": 1}
    def api(path):
        if path == "git/ref/heads/main":
            return {"object": {"sha": HEAD}}
        return {"merge_base_commit": {"sha": HEAD}}
    with patch.object(merge, "pr_at", return_value=pr), \
            patch.object(merge, "site_classifier_exemption", return_value=None), \
            patch.object(merge, "control_plane_classifier_exemption", return_value=None), \
            patch.object(merge, "workflow_requirements", return_value=decision), \
            patch.object(merge, "workflow_proof", side_effect=proof), \
            patch.object(merge, "api", side_effect=api):
        assert merge.merge(10047, HEAD)["status"] == "READY_TO_MERGE"
    assert calls == (["ci.yml", "e2e.yml"] if expected else ["ci.yml"])


def test_classifier_failure_requires_e2e():
    with patch.object(reader, "command", return_value="not JSON"):
        assert reader.classify(10047, HEAD)["e2e_required"] is True


def test_ci_failure_still_blocks_without_e2e():
    merge = load("merge_ci_required", "heydonna-open-pr-status", "merge")
    pr = {"number": 10047, "head": {"sha": HEAD}}
    with patch.object(merge, "pr_at", return_value=pr), \
            patch.object(merge, "site_classifier_exemption", return_value=None), \
            patch.object(merge, "control_plane_classifier_exemption", return_value=None), \
            patch.object(merge, "workflow_requirements", return_value=reader.requirements(
                {"head": HEAD, "e2e_required": False}, HEAD)), \
            patch.object(merge, "workflow_proof", side_effect=merge.Refusal("CI_NOT_GREEN")):
        with pytest.raises(merge.Refusal, match="CI_NOT_GREEN"):
            merge.merge(10047, HEAD)
