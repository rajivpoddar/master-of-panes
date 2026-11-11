"""Merge-only dependency decision and its mandatory assignment caller; no live I/O."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ASSETS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def selector(monkeypatch):
    module = load("merged_pr_selector", "ready_pool_select.py")
    monkeypatch.setattr(module, "run_lookup", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected network")))
    return module


def issue(n, priority="P1", blockers="none", labels=()):
    # Reuse the existing canonical valid-contract fixture, not a copied policy.
    fixtures = load("existing_ready_pool_fixtures", "test_ready_pool_select.py")
    body = "<!-- ready-pool:\nblockers: " + blockers + "\n-->"
    return fixtures.issue(n, [priority, "status:todo", *labels], body=body)


def linked_issue(prs=(), more=False, state="OPEN"):
    return {"state": state, "closedByPullRequestsReferences": {
        "nodes": list(prs), "pageInfo": {"hasNextPage": more}}}


MERGED = {"number": 77, "state": "MERGED", "mergedAt": "2026-11-05T10:00:00Z"}


def github_fixture(selector, monkeypatch, states):
    def lookup(command, **kwargs):
        query = next(s[6:] for s in command if s.startswith("query="))
        assert "deployment" not in query.lower() and "verification" not in query.lower()
        numbers = [int(n) for n in re.findall(r"i(\d+): issueOrPullRequest", query)]
        data = {"data": {"repository": {f"i{n}": states.get(n) for n in numbers}}}
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")
    monkeypatch.setattr(selector, "run_lookup", lookup)


def test_live_non_slot_owner_excluded_from_ranking_and_inversion(selector, monkeypatch):
    monkeypatch.setattr(selector, 'non_slot_owners', lambda: ({'px-owner': {100}}, {}))
    p0 = issue(100, 'P0')
    p0['body'] = p0['body'].replace('blockers: none', 'blockers: none\nowner: px-owner\ndisposition: IN_PROGRESS')
    rows = [p0, issue(101)]
    ranked = selector.rank_candidates(rows, offline=True)
    assert [row['issue'] for row in ranked] == [101]
    assert selector.check_pick(101, ranked)['status'] == 'ok'
    assert selector.classify(p0, light=True) is not None  # fast scan fetches full details
    assert selector.classify(p0) is None


def test_unverified_owner_stays_in_inversion_and_live_obligation_excludes(selector, monkeypatch):
    p0 = issue(100, 'P0')
    p0['body'] = p0['body'].replace('blockers: none', 'blockers: none\nowner: px-owner\ndisposition: IN_PROGRESS')
    monkeypatch.setattr(selector, 'non_slot_owners', lambda: ({}, {}))
    ranked = selector.rank_candidates([p0, issue(101)], offline=True)
    assert selector.check_pick(101, ranked)['status'] == 'PRIORITY_INVERSION'
    p0['body'] = p0['body'].replace('owner: px-owner\ndisposition: IN_PROGRESS', 'owner: none\ndisposition: READY')
    monkeypatch.setattr(selector, 'non_slot_owners', lambda: ({'px-owner': {100}}, {100: {'px-owner'}}))
    assert selector.classify(p0) is None
    monkeypatch.setattr(selector, 'non_slot_owners', lambda: ({'px-owner': {100}}, {}))
    assert selector.classify(p0) == 'new_issue'


def test_real_producer_binds_reparented_peer_to_current_issue(selector, monkeypatch, tmp_path):
    import sqlite3
    db = tmp_path / 'ops.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE obligations (issue, target_type, target_id, owner, status)')
        conn.execute("INSERT INTO obligations VALUES (100, 'github_issue', '100', 'px-owner', 'open')")
    connect = sqlite3.connect
    monkeypatch.setattr(selector.sqlite3, 'connect', lambda *a, **kw: connect(db))
    job = tmp_path / 'proxy-agent-job.test'
    job.mkdir()
    brief = tmp_path / 'brief.md'
    runner = job / 'run.sh'
    runner.write_text(f'claude --name px-owner --model luna < {brief} 2> /tmp/x | python3 -I -u /x/proxy-agent-stream.py /tmp/out /tmp/out.jsonl\n')
    tmux = f'0\tpx-owner\t1794405600\t0\t100\tbash {runner}\n'
    ps = '100 1 00:01:00 bash run.sh\n101 1 00:00:59 /usr/bin/claude --name px-owner --model luna\n'
    monkeypatch.setattr(selector.subprocess, 'run', lambda cmd, **kw: SimpleNamespace(stdout=tmux if cmd[0] == 'tmux' else ps))
    p0 = issue(100, 'P0')
    lower = issue(101)
    for text, expected in [('ISSUE: 200\n', 'PRIORITY_INVERSION'), ('ISSUE: 100\n', 'ok')]:
        brief.write_text(text)
        selector.non_slot_owners.cache_clear()
        ranked = selector.rank_candidates([p0, lower], offline=True)
        assert selector.check_pick(101, ranked)['status'] == expected
    p0['body'] = p0['body'].replace('blockers: none', 'blockers: none\nowner: px-owner\ndisposition: IN_PROGRESS')
    brief.write_text('ISSUE: 200\n')
    selector.non_slot_owners.cache_clear()
    assert selector.classify(p0) == 'new_issue'
    brief.write_text('ISSUE: 100\n')
    selector.non_slot_owners.cache_clear()
    assert selector.classify(p0) is None


def test_open_issue_and_merged_associated_pr_unblock_before_p1(selector, monkeypatch):
    rows = [issue(100, "P0", "#10", ["pm-blocked:dependency"]), issue(101)]
    github_fixture(selector, monkeypatch, {10: linked_issue([MERGED])})
    cache = {}
    selector.prefetch_dependencies(rows, cache)
    ranked = selector.rank_candidates(rows, cache=cache)
    assert [r["issue"] for r in ranked] == [100, 101]
    assert selector.check_pick(101, ranked)["status"] == "PRIORITY_INVERSION"
    assert cache[(10, "default")][0] is True


@pytest.mark.parametrize("node", [
    {"state": "CLOSED", "mergedAt": None},
    {"state": "OPEN", "mergedAt": None},
    linked_issue([]), linked_issue([MERGED, {**MERGED, "number": 78}]),
    linked_issue([MERGED], more=True), None, {"state": "CLOSED", "stateReason": "COMPLETED"},
    {"state": "MERGED", "mergedAt": "malformed"},
])
def test_unmerged_missing_ambiguous_or_failed_link_keeps_p0_gated(selector, monkeypatch, node):
    rows = [issue(100, "P0", "#10", ["pm-blocked:dependency"]), issue(101)]
    github_fixture(selector, monkeypatch, {10: node})
    cache = {}
    selector.prefetch_dependencies(rows, cache)
    ranked = selector.rank_candidates(rows, cache=cache)
    assert [r["issue"] for r in ranked] == [101]
    assert selector.check_pick(101, ranked)["status"] == "ok"
    assert selector.check_pick(100, ranked, selector.gated_p0_candidates(rows, cache=cache))["status"] == "DEPENDENCY_OPEN"


def test_direct_pr_closed_condition_still_requires_merge(selector):
    assert not selector.dependency_satisfaction({"state": "CLOSED", "mergedAt": None})[0]
    assert selector.dependency_satisfaction(MERGED)[0]
    for state in ["OPEN", "CLOSED"]:
        assert selector.dependency_satisfaction(linked_issue([MERGED], state=state))[0]


def test_other_hold_occupied_and_offline_remain_excluded(selector, monkeypatch):
    rows = [issue(100, "P0", "#10", ["pm-blocked:dependency", "pm-blocked:product"]),
            issue(102, "P0", labels=["slot:2"]), issue(101)]
    github_fixture(selector, monkeypatch, {10: linked_issue([MERGED])})
    cache = {}; selector.prefetch_dependencies(rows, cache)
    assert [r["issue"] for r in selector.rank_candidates(rows, cache=cache)] == [101]
    assert [r["issue"] for r in selector.rank_candidates([issue(100, "P0", "#10"), issue(101)], offline=True)] == [101]


def test_own_merged_implementation_does_not_become_new_issue(selector):
    done = issue(100, "P0")
    raw = {**done, **linked_issue([MERGED]), "labels": {"nodes": done["labels"], "pageInfo": {"hasNextPage": False}}}
    assert [r["issue"] for r in selector.rank_candidates([selector.candidate_node(raw), issue(101)])] == [101]


def test_extra_rework_keeps_source_priority_and_structured_dependency(selector, monkeypatch):
    rows = [issue(100, "P2", "#10"), issue(101)]
    extra = [{"issue": 100, "class": "rework", "priority": "P0"}]
    github_fixture(selector, monkeypatch, {10: linked_issue([])})
    cache = {}; selector.prefetch_dependencies(rows, cache)
    assert [r["issue"] for r in selector.rank_candidates(rows, extra, cache=cache)] == [101]
    github_fixture(selector, monkeypatch, {10: linked_issue([MERGED])})
    cache = {}; selector.prefetch_dependencies(rows, cache)
    ranked = selector.rank_candidates(rows, extra, cache=cache)
    assert [r["issue"] for r in ranked] == [101, 100] and ranked[1]["priority"] == "P2"


def test_loader_is_complete_label_union_with_merged_metadata(selector, monkeypatch):
    calls = []
    def lookup(command, **kwargs):
        calls.append(command)
        query = next(s[6:] for s in command if s.startswith("query="))
        assert "closedByPullRequestsReferences" in query and "states:OPEN,labels:" in query
        rows = [issue(100)] if 'labels:["status:todo"]' in query else []
        nodes = [{**r, **linked_issue(), "labels": {"nodes": r["labels"], "pageInfo": {"hasNextPage": False}}} for r in rows]
        return subprocess.CompletedProcess(command, 0, json.dumps({"data": {"repository": {"issues": {"nodes": nodes, "totalCount": len(nodes), "pageInfo": {"hasNextPage": False}}}}}), "")
    monkeypatch.setattr(selector, "run_lookup", lookup)
    assert [r["number"] for r in selector.load_open_issues()] == [100]
    assert len(calls) == 5 and all(c[:3] == ["gh", "api", "graphql"] for c in calls)


def drive_caller(selector, monkeypatch, requested, rows, states, resume=False, unavailable=False):
    client = load("canonical_assignment_client", "mop-assign-slot.py")
    github_fixture(selector, monkeypatch, states)
    monkeypatch.setattr(selector, "load_open_issues", lambda: rows)
    monkeypatch.setattr(selector, "load_open_prs", lambda: [])
    checks, http = [], []
    def run(command, **kwargs):
        checks.append(command)
        if unavailable:
            raise subprocess.TimeoutExpired(command, 90)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = selector.main(command[2:])
        return subprocess.CompletedProcess(command, code, output.getvalue(), "")
    monkeypatch.setattr(client, "subprocess", SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError), raising=False)
    def api(method, url, body=None):
        http.append((method, url, body))
        if "assignment-effect-receipt" in url:
            if not resume:
                return 404, {"code": "assignment_effect_receipt_not_found"}
            return 200, {"success": True, "slot": 3, "effect_id": parse_qs(urlparse(url).query)["effect_id"][0],
                         "state": "pending_delivery", "selection_class": "new_issue",
                         "task_digest": hashlib.sha256(b"").hexdigest(), "desired_tuple": {"issue": requested}}
        if method == "GET":
            return 200, {"slot": 3, "occupied": resume, "issue": requested if resume else None, "assignment_epoch": 4}
        return 200, {"status": "assigned", "slot": 3, "assignment_epoch": 5}
    monkeypatch.setattr(client, "http_json", api)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = client.main(["--slot", "3", "--issue", str(requested)])
    return code, json.loads(output.getvalue()), checks, http


def test_actual_caller_refuses_inversion_before_any_assignment_effect(selector, monkeypatch):
    rows = [issue(100, "P0", "#10"), issue(101)]
    code, result, checks, http = drive_caller(selector, monkeypatch, 101, rows, {10: linked_issue([MERGED])})
    assert code == 2 and result["reason"] == "PRIORITY_INVERSION"
    assert len(checks) == 1 and not any(method == "POST" for method, _, _ in http)


def test_actual_caller_accepts_the_same_merged_dependency_decision(selector, monkeypatch):
    rows = [issue(100, "P0", "#10"), issue(101)]
    code, result, checks, http = drive_caller(selector, monkeypatch, 100, rows, {10: linked_issue([MERGED])})
    assert code == 0 and result["selection_receipt"]["pick"]["issue"] == 100
    assert len(checks) == 1 and sum(method == "POST" for method, _, _ in http) == 1


def test_unknown_check_has_no_assignment_effect(selector, monkeypatch):
    code, result, _, http = drive_caller(selector, monkeypatch, 100, [issue(100)], {}, unavailable=True)
    assert code == 2 and result["reason"] == "LOOKUP_FAILED"
    assert not any(method == "POST" for method, _, _ in http)


def test_pre_upgrade_default_receipt_resumes_original_effect_without_notice(monkeypatch):
    client = load("legacy_assignment_resume", "mop-assign-slot.py")
    binding = {"slot": 3, "selection_class": "new_issue", "repository_id": "github:heydonna-app/heydonna-app",
               "issue": 100, "pr": None, "branch": None, "head_sha": None, "work_kind": None,
               "handoff_id": None, "claimed_at": None, "task_file": ""}
    digest = hashlib.sha256(b"").hexdigest()
    original = client.compute_effect_id(binding, digest)
    requests = []
    monkeypatch.setattr(client, "check_selection", lambda args: pytest.fail("fresh selection"))
    monkeypatch.setattr(client, "post_assignment_notice", lambda *args: pytest.fail("duplicate/unknown notice retry"))
    def api(method, url, body=None):
        requests.append((method, url, body))
        if "assignment-effect-receipt" in url:
            effect = parse_qs(urlparse(url).query)["effect_id"][0]
            if effect != original:
                return 404, {"code": "assignment_effect_receipt_not_found"}
            return 200, {"success": True, "slot": 3, "effect_id": original, "state": "pending_delivery",
                         "selection_class": "new_issue", "task_digest": digest,
                         "desired_tuple": {"issue": 100, "repository_id": binding["repository_id"]}}
        if method == "GET":
            return 200, {"slot": 3, "occupied": True, "issue": 100, "assignment_epoch": 4}
        assert body["effect_id"] == original and body["repository_id"] == binding["repository_id"]
        assert body["expected_epoch"] == 4
        return 200, {"status": "assigned", "slot": 3, "assignment_epoch": 4, "idempotent": True}
    monkeypatch.setattr(client, "http_json", api)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = client.main(["--slot", "3", "--issue", "100"])
    result = json.loads(output.getvalue())
    assert code == 0 and result["selection_receipt"] == {"status": "existing_effect_resume", "effect_id": original}
    assert result["assignment_epoch"] == 4 and result["assignment_notice"]["delivery"] == "not_retried"
    assert sum(method == "POST" for method, _, _ in requests) == 1


def test_known_pending_delivery_resumes_without_re_admission(selector, monkeypatch):
    code, result, checks, http = drive_caller(selector, monkeypatch, 100, [], {}, resume=True)
    assert code == 0 and result["selection_receipt"]["status"] == "existing_effect_resume"
    assert not checks and sum(method == "POST" for method, _, _ in http) == 1
