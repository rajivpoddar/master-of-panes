"""Offline regression proof for promotion versus assignment (Rajiv S113)."""
import importlib.util
import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
import os
import time
from copy import deepcopy
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bt = load("triage_policy_backlog", "backlog-triage.py")
repair = load("triage_policy_repair", "ready-pool-contract-repair.py")
selector = load("triage_policy_selector", "ready_pool_select.py")


def issue(number=1234, priority="P1", status="backlog", blockers="none", extra=()):
    return {
        "number": number, "title": "fix(editor): preserve saved text", "state": "OPEN",
        "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-11-04T00:00:00Z",
        "labels": [{"name": n} for n in [f"status:{status}", priority, "public-beta", *extra]],
        "body": f"""<!-- ready-pool:
priority: {priority}
lane: editor
branch_slug: preserve-saved-text
ac_summary: Preserve saved text after reconnect.
claimable_slot_type: any
blockers: {blockers}
work_type: bug_fix
-->

## Acceptance Criteria
- AC-1: Preserve saved text after reconnect.
  - Surface: nonvisual
  - QA reachability: not-applicable
  - Required proof: integration
""",
    }


class TriagePolicyTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(bt, "handoff_preflight_ok", return_value=(True, "")),
            patch.object(bt, "issue_contract_ledger_errors", return_value=[]),
            patch.object(bt, "issue_runnability_refusals", return_value=[]),
            patch.object(bt, "recent_promoter_rejections", return_value={}),
            patch.object(bt, "open_pr_issue_numbers", return_value=set()),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_dependency_membership_does_not_authorize_dispatch(self):
        for priority in ("P0", "P1", "P2", "P3"):
            held = issue(priority=priority, status="todo", blockers="[#9999]", extra=("pm-blocked:dependency",))
            self.assertEqual(bt.dispatch_readiness_reasons(held, {1234, 9999}, allow_backlog=False, allow_dependencies=True), [])
            self.assertTrue(bt.dispatch_readiness_reasons(held, {1234, 9999}, allow_backlog=False))

    def test_held_backlog_and_blocked_issues_can_enter_pool(self):
        for status in ("backlog", "blocked", "deferred"):
            held = issue(status=status, blockers="[#9999]", extra=("pm-blocked:dependency",))
            rows, _ = bt.classify_promoter([held], {1234, 9999})
            self.assertEqual([r["number"] for r in rows], [1234])
            self.assertEqual(rows[0]["open_dependencies"], [9999])

    def test_other_holds_and_non_slot_work_stay_out(self):
        for extra in (("pm-blocked:product",), ("slot:2",), ("pm-nonclaimable:tracking",)):
            rows, _ = bt.classify_promoter([issue(extra=extra)], {1234})
            self.assertEqual(rows, [])
        with patch.object(bt, "open_pr_issue_numbers", return_value={1234}):
            rows, _ = bt.classify_promoter([issue()], {1234})
            self.assertEqual(rows, [])
        malformed = issue(blockers="awaiting an answer")
        self.assertTrue(bt.dispatch_readiness_reasons(malformed, {1234}, allow_backlog=True, allow_dependencies=True))

    def test_dependency_todos_are_waits_not_contract_defects(self):
        todos = [issue(1200+i, p, "todo", "[#9999]", ("pm-blocked:dependency",)) for i,p in enumerate(("P0", "P1", "P2", "P3"))]
        buckets = bt.classify(todos, {9999, 1200, 1201, 1202, 1203})
        self.assertEqual(buckets["todo_blocked_dependency"], [])
        self.assertEqual(len(buckets["todo_dependency_gated"]), 4)
        self.assertEqual(buckets["todo_dispatch_readiness_failed"], [])
        self.assertEqual(buckets["todo_missing_ready_pool"], [])

    def test_legacy_migration_never_demotes_dependency_todos(self):
        with patch.object(bt, "run") as run, patch.object(bt, "pm_ops_upsert") as upsert:
            bt.migrate_blocked_todo({"number": 1234, "reason": "open_dependency", "open_dependencies": [9999]})
        run.assert_not_called()
        upsert.assert_not_called()

    def test_p3_refills_only_deficit_after_all_higher_priorities(self):
        rows = [bt.promoter_row(issue(i, p), "clean_ready_pool") for i,p in [(1234,"P1"),(1235,"P2"),(1236,"P3"),(1237,"P3")]]
        self.assertEqual([r["number"] for r in bt.select_promotion_rows(rows, 8)], [1234,1235])
        self.assertEqual([r["number"] for r in bt.select_promotion_rows(rows, 7)], [1234,1235,1236])
        self.assertEqual([r["number"] for r in bt.select_promotion_rows(rows, 17)], [1234,1235])
        held = dict(rows[0], open_dependencies=[9999])
        self.assertEqual(len(bt.select_promotion_rows([held, *rows[1:]], 7)), 4)
        held_p3 = dict(rows[2], open_dependencies=[9999])
        self.assertEqual([r["number"] for r in bt.select_promotion_rows([held_p3], 17)], [1236])

    def test_promotion_preserves_dependency_metadata_and_label(self):
        held = issue(blockers="[#9999]", extra=("pm-blocked:dependency",))
        live = {**held, "labels": [{"name": "status:todo"}, {"name": "P1"}, {"name": "pm-blocked:dependency"}]}
        with patch.object(bt, "gh_issue_view", side_effect=[held, live]), patch.object(bt, "run") as run, patch.object(bt, "pm_ops_upsert"):
            result = bt.promote_backlog_issue(bt.promoter_row(held, "clean_ready_pool"), {1234,9999})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["live_ready_pool"]["blockers"], "[#9999]")
        self.assertNotIn("pm-blocked:dependency", run.call_args_list[0].args[0][run.call_args_list[0].args[0].index("--remove-label")+1])

    def test_contract_repair_allows_p3_and_preserves_dependencies(self):
        with patch.object(repair, "_triage", return_value=bt), patch.object(repair, "ledger_errors", return_value=[]):
            for p in ("P1", "P3"):
                held = issue(priority=p, blockers="[#9999]", extra=("pm-blocked:dependency",))
                result = repair.repair_contract(held)
                self.assertIn(result["status"], ("REPAIRED", "ALREADY_COMPLETE"), result)
                self.assertIn("blockers: [#9999]", result["body"])

    def test_selector_still_refuses_unknown_and_open_dependencies(self):
        held = issue(status="todo", blockers="[#9999]", extra=("pm-blocked:dependency",))
        ranked = selector.rank_candidates([held], offline=True)
        self.assertEqual(ranked, [])  # offline: no lookup means no dispatch authority

    def test_canonical_dependency_without_label_is_still_gated(self):
        held = issue(status="todo", blockers="[#9999]")
        self.assertEqual(selector.rank_candidates([held], offline=True), [])
        gated = selector.gated_p0_candidates([held], offline=True)
        self.assertEqual(gated[0]["issue"], 1234)
        self.assertIn("pm-blocked:dependency", gated[0]["blocking_labels"])

    def test_unmerged_pr_and_incomplete_issue_do_not_satisfy_bare_dependency(self):
        held = issue(status="todo", blockers="[#9999]")
        def response(cmd, **kwargs):
            data = {"state":"closed","pull_request":{}} if cmd[1] == "api" else {"state":"CLOSED","mergedAt":None}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")
        with patch.object(selector.subprocess, "run", side_effect=response):
            self.assertEqual(selector.rank_candidates([held]), [])
        with patch.object(selector.subprocess, "run", return_value=subprocess.CompletedProcess([],0,json.dumps({"state":"closed","state_reason":"not_planned"}),"")):
            self.assertEqual(selector.rank_candidates([held]), [])

    def test_promotion_adds_dependency_label_and_does_not_clear_it_from_snapshot(self):
        for original_extra in ((), ("pm-blocked:dependency",)):
            held = issue(blockers="[#9999]", extra=original_extra)
            live = issue(status="todo", blockers="[#9999]", extra=("pm-blocked:dependency",))
            row = bt.promoter_row(held, "clean_ready_pool")
            with patch.object(bt, "gh_issue_view", side_effect=[held,live]), patch.object(bt, "run") as run, patch.object(bt, "pm_ops_upsert"):
                result = bt.promote_backlog_issue(row, {1234})  # predecessor closed in snapshot, not proof of merge
            self.assertTrue(result["ok"], result)
            cmd = run.call_args_list[0].args[0]
            self.assertIn("pm-blocked:dependency", cmd[cmd.index("--add-label")+1])
            self.assertNotIn("pm-blocked:dependency", cmd[cmd.index("--remove-label")+1])

    def test_existing_todo_missing_slug_repairs_in_place_with_dependency_preserved(self):
        held = issue(status="todo", blockers="[#9999]", extra=("pm-blocked:dependency",))
        held["body"] = held["body"].replace("branch_slug: preserve-saved-text\n", "").replace("work_type: bug_fix\n", "work_type: bug_fix\nwake_condition: PR #9999 merged\n")
        current = deepcopy(held)
        proposal = {**bt.ready_pool_metadata(held), "issue":1234,"branch_slug":"preserve-saved-text",
                    "blockers":"none", "review_proof":"Existing issue AC-1 and original directive; metadata repair only.",
                    "expected_body_sha256":bt.issue_body_sha256(held["body"])}
        def fake_run(cmd, **kwargs):
            if "--body-file" in cmd:
                current["body"] = Path(cmd[cmd.index("--body-file")+1]).read_text()
            return subprocess.CompletedProcess(cmd,0,"","")
        with tempfile.TemporaryDirectory() as tmp, patch.object(bt,"OUT_DIR",Path(tmp)), \
             patch.object(bt,"gh_issue_list",return_value=[deepcopy(held)]), patch.object(bt,"issue_num_set",return_value={1234,9999}), \
             patch.object(bt,"gh_issue_view",side_effect=lambda n:deepcopy(current)), patch.object(bt,"run",side_effect=fake_run), patch.object(bt,"pm_ops_upsert"):
            path = Path(tmp)/"proposal.json";path.write_text(json.dumps([proposal]))
            self.assertEqual(bt.repair_ready_pool_main(["--apply","--metadata-json",str(path),"--issues","1234"]),0)
        meta = bt.ready_pool_metadata(current)
        self.assertEqual(meta["branch_slug"],"preserve-saved-text")
        self.assertEqual(meta["blockers"],"[#9999]")
        self.assertEqual(meta["wake_condition"],"PR #9999 merged")
        self.assertEqual(bt.strip_ready_pool(current["body"]) if hasattr(bt,"strip_ready_pool") else repair.strip_ready_pool(current["body"]), repair.strip_ready_pool(held["body"]))

    def test_full_stop_hook_accepts_full_pool_with_only_surplus_p3(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(bt,"OUT_DIR",Path(tmp)):
            root=Path(tmp);hourly=root/"hourly.stdout"
            hourly.write_text("READY_POOL_LOW clean_todo=4 threshold=10 total_status_todo=4 assigned_todo=0 blocked_todo=0\n")
            os.utime(hourly,(time.time()-10,time.time()-10))
            p3=bt.promoter_row(issue(priority="P3"),"clean_ready_pool")
            manifest=bt.write_promoter_manifest([p3],{"p3_refill_not_needed":[p3]},[],applied=False,current_ready_pool_count=10)
            self.assertEqual(json.loads(manifest.read_text())["counts"]["auto_promotable"],0)
            env={**os.environ,"READY_POOL_LOW_HOURLY_GLOB":str(hourly),"READY_POOL_LOW_PROMOTER_GLOB":str(manifest),
                 "READY_POOL_LOW_FANOUT_GLOB":str(root/"none-fanout*"),"READY_POOL_LOW_DETERMINISTIC_GLOB":str(root/"none-deterministic*"),
                 "READY_POOL_REPAIR_ACTIVE_FILE":str(root/"none-active.json"),"READY_POOL_REPAIR_LEGACY_SCAN":"0","READY_POOL_LOW_STOP_LOG":str(root/"hook.log")}
            result=subprocess.run(["bash",str(SCRIPTS.parent/"hooks/ready-pool-low-stop-validator.sh")],input="",text=True,capture_output=True,env=env,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn('"decision": "block"',result.stdout,result.stdout)

    def test_release_gate_requires_exactly_one_actual_release_label(self):
        self.assertFalse(bt.has_release_gate({"P1", "bug"}))
        self.assertFalse(bt.has_release_gate({"public-beta", "post-beta"}))
        self.assertTrue(bt.has_release_gate({"P1", "public-beta"}))

    def test_repair_counts_only_primary_acs_and_requires_split(self):
        self.assertEqual(len(repair.extract_acs(issue()["body"])), 1)
        wide = issue()
        wide["body"] += "\n- AC-2: Keep speaker marks intact.\n- AC-3: Reject stale updates safely.\n- AC-4: Preserve undo after reconnect.\n"
        with patch.object(repair, "_triage", return_value=bt):
            self.assertEqual(repair.repair_contract(wide)["reasons"], ["split_required_more_than_three_acs"])

    def test_same_run_repair_continues_through_verified_promotion_without_reviewer(self):
        candidate = issue()
        with tempfile.TemporaryDirectory() as tmp, patch.object(repair, "OUT_DIR", Path(tmp)), \
             patch.object(repair, "_gh_json_issue", return_value=candidate), \
             patch.object(repair, "repair_contract", return_value={"status": "ALREADY_COMPLETE", "body": candidate["body"], "reasons": [], "add_label": []}), \
             patch.object(repair, "promote_exact", return_value=Path(tmp)/"promoted.json") as promote, \
             patch.object(repair, "live_deficit", return_value=(10,0,Path(tmp)/"scan.json")):
            (Path(tmp)/"scan.json").write_text(json.dumps({"would_promote": [], "needs_ready_pool_metadata": []}))
            self.assertEqual(repair.run_deterministic(Path(tmp)/"source.json", {"needs_ready_pool_metadata":[{"number":1234}]}, None, True), 0)
            promote.assert_called_once_with(1234, 1800)
            result = json.loads(next(Path(tmp).glob("pm-ready-pool-contract-repair-deterministic-*.json")).read_text())
            self.assertEqual(result["promoted"][0]["status"], "PROMOTED")
            self.assertEqual(result["status"], "complete")

    def test_stale_promotion_body_never_mutates_labels(self):
        candidate = issue()
        row = bt.promoter_row(candidate, "clean_ready_pool")
        changed = dict(candidate, body=candidate["body"]+"\nNew constraint.")
        with patch.object(bt, "gh_issue_view", return_value=changed), patch.object(bt, "run") as run:
            result = bt.promote_backlog_issue(row, {1234})
        self.assertFalse(result["ok"])
        self.assertIn("stale_issue_body_before_promotion", result["readiness_reasons"])
        run.assert_not_called()

    def test_failed_ownership_lookup_cannot_prove_a_free_issue(self):
        from subprocess import CompletedProcess
        # setUp deliberately patches the lookup for other tests; exercise the
        # real function from a fresh module without any live command.
        fresh = load("triage_failed_lookup", "backlog-triage.py")
        with patch.object(fresh, "run", return_value=CompletedProcess([],1,"","offline")):
            with self.assertRaisesRegex(RuntimeError, "ownership_lookup_failed"):
                fresh.open_pr_issue_numbers()

    def test_stop_hook_counts_claimable_capacity_separately_from_membership(self):
        hook = SCRIPTS.parent/"hooks/ready-pool-low-stop-validator.sh"
        body = hook.read_text().split("python3 <<'PYEOF'\n",1)[1].rsplit("\nPYEOF",1)[0]
        tree = ast.parse(body)
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        namespace = {"re": __import__("re"), "os": __import__("os"), "json": json}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(hook), "exec"), namespace)
        result = namespace["apply_reaches_target"]({"ready_pool_target":10, "current_ready_pool_count":9,
                    "promoted":[{"ok":True,"dependency_gated":True,"live_labels":["status:todo","P2","pm-blocked:dependency"]}]})
        self.assertFalse(result[0])
        self.assertEqual(result[2],0)


if __name__ == "__main__":
    unittest.main()
