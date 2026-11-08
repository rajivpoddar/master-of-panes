import importlib.util, pathlib, sys
d = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(d))
spec = importlib.util.spec_from_file_location("bt", d / "backlog-triage.py"); bt = importlib.util.module_from_spec(spec); spec.loader.exec_module(bt)
R = bt.runnability_refusals
PR = lambda n, body, at="2026-10-20T00:00:00Z": {"number": n, "body": body, "mergedAt": at}
C = lambda body, at: {"body": body, "createdAt": at}

def test_clean_issue_runnable(): assert R(5, "", [], [], []) == []
def test_merged_closing_pr_refused(): assert R(5, "", [], [PR(9, "Closes #5")], [])[0].startswith("merged_pr_closes_issue:#9")
def test_part_of_without_remaining_ac_refused():
    assert R(5, "", [], [PR(9, "Part of #5")], [])[0].startswith("merged_part_of_prs_without_named_remaining_ac")
def test_part_of_with_newer_remaining_ac_ok():
    assert R(5, "", [C("Remaining AC: AC-3 export path", "2026-10-21T00:00:00Z")], [PR(9, "Part of #5")], []) == []
def test_open_child_refused():
    r = R(5, "", [], [], [{"number": 7, "body": "Slice B of #5"}, {"number": 8, "body": "Parents: #4 and #5"}])
    assert r == ["umbrella_open_children:promote_child_instead:#7,#8"]
def test_unrelated_mention_not_child(): assert R(5, "", [], [], [{"number": 7, "body": "see #55 and #5"}]) == []
def test_latest_parked_refused(): assert R(5, "", [C("PM PARKED: not runnable", "2026-10-27T00:00:00Z")], [], [])[0].startswith("latest_pm_comment_parked")
def test_unparked_later_ok():
    assert R(5, "", [C("PM PARKED", "2026-10-26T00:00:00Z"), C("UNPARKED, ready", "2026-10-27T00:00:00Z")], [], []) == []

# 2026-10-31 promoter fix: a RESOLVED pm-blocked:dependency label must not make a
# valid dev contract non-dispatchable (#12183 was stuck in contract repair).
def test_resolved_dependency_label_not_tracking():
    meta = {"claimable_slot_type": "dev", "work_type": "app-code", "disposition": "READY"}
    issue = {"number": 1, "title": "fix(x): y", "body": "", "labels": [{"name": "pm-blocked:dependency"}, {"name": "status:backlog"}]}
    assert bt.is_non_dispatchable_contract(issue, meta) is True  # raw label still counts
    stripped = {**issue, "labels": [l for l in issue["labels"] if l["name"] != "pm-blocked:dependency"]}
    assert bt.is_non_dispatchable_contract(stripped, meta) is False

# "PM park" / "PM hold" comments must park too (#12183/#12206 holds, 2026-10-31).
def test_pm_hold_and_park_refused():
    assert R(5, "", [C("PM hold (31 Oct): depends on #12024", "2026-10-31T06:12:00Z")], [], [])[0].startswith("latest_pm_comment_parked")
    assert R(5, "", [C("PM park (31 Oct): stale scanner", "2026-10-31T06:13:00Z")], [], [])[0].startswith("latest_pm_comment_parked")

# 2026-11-02 (#12089): negated close phrasing in a Part-of skip PR is not a close.
def test_negated_close_not_counted():
    body = "Part of #5\n\nRemaining follow-up ACs: fix race. This PR must not close #5."
    later = C("Remaining AC: AC-1, AC-2 and AC-3, all still open.", "2026-10-21T00:00:00Z")
    assert R(5, "", [later], [PR(9, body)], []) == []
    for phrase in ("does not close #5", "doesn't close #5", "won't close #5", "should never fix #5"):
        assert not any(r.startswith("merged_pr_closes_issue") for r in R(5, "", [], [PR(9, phrase)], []))
def test_real_closes_still_refused():
    assert R(5, "", [], [PR(9, "Summary.\n\nCloses #5")], [])[0] == "merged_pr_closes_issue:#9"
    assert R(5, "", [], [PR(9, "Not related. Fixes #5")], [])[0] == "merged_pr_closes_issue:#9"

# S113 (2026-11-04) generalizes this earlier P0 exception to every priority.
# 2026-11-02 Rajiv: "the P0's can still be added to the ready pool, right? the gates can be
# checked while assigning" -> READY_GATED P0 todos are never dependency-migrated to backlog.
def _todo(n, prio, disposition):
    body = f"<!-- ready-pool:\npriority: {prio}\nblockers: #999\ndisposition: {disposition}\n-->"
    return {"number": n, "title": f"t{n}", "body": body, "updatedAt": "",
            "labels": [{"name": "status:todo"}, {"name": prio}, {"name": "pm-blocked:dependency"}]}

def test_ready_gated_p0_not_blocked_dependency():
    issues = [_todo(1, "P0", "READY_GATED"), _todo(2, "P1", "READY_GATED"), _todo(3, "P0", "READY")]
    from unittest.mock import patch
    with patch.object(bt, "issue_contract_ledger_errors", return_value=[]), patch.object(bt, "handoff_preflight_ok", return_value=(True,"")):
        b = bt.classify(issues, {1, 2, 3, 999})
    assert [r["number"] for r in b["todo_ready_gated_p0"]] == [1]
    assert b["todo_blocked_dependency"] == []
    assert sorted(r["number"] for r in b["todo_dependency_gated"]) == [1, 2, 3]

def test_migrate_skips_ready_gated_p0():
    calls = []
    orig = bt.run
    bt.run = lambda *a, **k: calls.append(a)
    try:
        bt.migrate_blocked_todo({"number": 1, "reason": "ready_gated_p0", "open_dependencies": [999]})
    finally:
        bt.run = orig
    assert calls == []

def test_audit_separates_parked_and_owned_from_missing_contract(tmp_path, monkeypatch):
    import json
    def issue(number, *extra):
        return {"number": number, "title": str(number), "body": "", "labels": [
            {"name": name} for name in ("status:todo", "P1", *extra)
        ]}
    issues = [issue(1, "pm-blocked:external"), issue(2, "slot:1"),
              issue(3), issue(4), issue(5, "tracking"), issue(6, "pm-blocked:dependency")]
    monkeypatch.setattr(bt, "gh_issue_list", lambda: issues)
    monkeypatch.setattr(bt, "issue_num_set", lambda rows: {row["number"] for row in rows})
    monkeypatch.setattr(bt, "open_pr_issue_numbers", lambda: {3})
    monkeypatch.setattr(bt, "prefetch_dependencies", lambda *args: None)
    monkeypatch.setattr(bt, "selection_candidate", lambda issue, kind: {})
    monkeypatch.setattr(bt, "evaluate_dependency", lambda *args: None)
    monkeypatch.setattr(bt, "is_gated", lambda selection: False)
    monkeypatch.setattr(bt, "issue_contract_ledger_errors", lambda *args, **kwargs: [])
    monkeypatch.setattr(bt, "OUT_DIR", tmp_path)
    assert bt.audit_ready_pool_main([]) == 0
    manifest = json.loads(next(tmp_path.glob("pm-ready-pool-audit-*.json")).read_text())
    assert {row["issue"] for row in manifest["parked_or_owned"]} == {1, 2, 3, 5, 6}
    assert [row["issue"] for row in manifest["contract_defects"]] == [4]
    assert any(reason.startswith("missing_ready_pool_fields:") for reason in manifest["contract_defects"][0]["reasons"])


def test_dependency_migration_contract_forms(monkeypatch):
    import ready_pool_select as selector
    monkeypatch.setattr(bt, "recent_promoter_rejections", lambda: {})
    monkeypatch.setattr(bt, "open_pr_issue_numbers", lambda: set())
    monkeypatch.setattr(bt, "issue_contract_ledger_errors", lambda *args, **kwargs: [])
    monkeypatch.setattr(bt, "handoff_preflight_ok", lambda *args: (True, ""))
    monkeypatch.setattr(bt, "issue_runnability_refusals", lambda *args: [])
    forms = [(["pm-blocked:dependency"], "dependency"),
             (["sub:waiting-on-issue"], "waiting-on-issue"),
             (["pm-blocked:dependency", "sub:waiting-on-issue"], "dependency, waiting-on-issue")]
    for form_labels, holds in forms:
        for other_hold, incomplete in ((False, False), (True, False), (False, True)):
            extra = ["pm-blocked:external"] if other_hold else []
            body = ("<!-- ready-pool:\npriority: P1\nlane: app\nbranch_slug: fix-thing\n"
                    "ac_summary: Fix thing\nclaimable_slot_type: dev\nwork_type: app-code\n"
                    f"blockers: #999\nholds: {holds}{', external' if other_hold else ''}\n-->")
            if incomplete:
                body = body.replace("branch_slug: fix-thing\n", "")
            issue = {"number": 123, "title": "fix thing", "body": body,
                     "labels": [{"name": n} for n in ["status:backlog", "P1", *form_labels, *extra]]}
            names = bt.labels(bt.contract_without_dependency_hold(issue))
            assert "pm-blocked:dependency" not in names
            assert ("pm-blocked:external" in names) == other_hold
            candidates, skipped = bt.classify_promoter([issue], {999})
            assert bool(candidates) == (not other_hold and not incomplete)
            if other_hold:
                assert skipped["missing_ready_pool_contract"] or skipped["blocked_label"]
                assert "pm-blocked:external" in selector.blocking_labels(selector._labels(issue), issue)
            if incomplete:
                assert skipped["missing_ready_pool_contract"]


def test_promotion_preserves_existing_legacy_dependency_label(monkeypatch):
    body = ("<!-- ready-pool:\npriority: P1\nlane: app\nbranch_slug: fix-thing\n"
            "ac_summary: Fix thing\nclaimable_slot_type: dev\nwork_type: app-code\n"
            "blockers: none\n-->")
    issue = {"number": 123, "title": "fix thing", "body": body,
             "labels": [{"name": n} for n in ("status:backlog", "P1", "pm-blocked:dependency")]}
    live = {**issue, "labels": [{"name": n} for n in ("status:todo", "P1", "pm-blocked:dependency")]}
    calls = []
    views = iter([issue, live])
    monkeypatch.setattr(bt, "gh_issue_view", lambda _: next(views))
    monkeypatch.setattr(bt, "dispatch_readiness_reasons", lambda *args, **kwargs: [])
    monkeypatch.setattr(bt, "issue_runnability_refusals", lambda *args: [])
    monkeypatch.setattr(bt, "open_pr_issue_numbers", lambda: set())
    monkeypatch.setattr(bt, "run", lambda cmd, **kwargs: calls.append(cmd))
    result = bt.promote_backlog_issue({"number": 123, "priority": "P1",
                                       "body_sha256": bt.issue_body_sha256(body)}, set())
    assert result["ok"]
    assert "pm-blocked:dependency" not in calls[0][calls[0].index("--remove-label") + 1]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and name != "test_audit_separates_parked_and_owned_from_missing_contract": fn()
    print("ok")
