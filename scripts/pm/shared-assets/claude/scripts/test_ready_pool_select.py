#!/usr/bin/env python3
"""Unit tests for ready_pool_select.py (Rajiv C0ALZJHGE49 ts 1793202340.562609).

Run: python3 -m unittest ~/.claude/scripts/test_ready_pool_select.py  (stdlib only)
"""

import importlib.util
import pathlib
import unittest
import json
import subprocess
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "ready_pool_select", pathlib.Path(__file__).with_name("ready_pool_select.py"))
rps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rps)


def issue(n, labels, created="2026-10-01T00:00:00Z", body=""):
    # Ranking fixtures are valid contracts; individual tests override fields
    # to exercise the actual exclusion/priority behavior.
    import re
    priority = next((p for p in labels if p in rps.PRIORITY_RANK), "P1")
    fields = {"priority": priority, "lane": "editor", "branch_slug": "bounded-repair",
              "ac_summary": "Preserve current work through the owning boundary.",
              "claimable_slot_type": "dev", "work_type": "app-code",
              "blockers": "#999999" if "pm-blocked:dependency" in labels else "none"}
    match = rps.READY_POOL_RE.search(body)
    if match:
        fields.update(dict(re.findall(r"(?m)^\s*([a-z_]+):\s*(.*?)\s*$", match.group(1))))
        body = body[:match.start()] + body[match.end():]
    body = "<!-- ready-pool:\n" + "\n".join(f"{k}: {v}" for k, v in fields.items()) + "\n-->\n" + body + """
<!-- qa-proof-schema: 1 -->
## Acceptance criteria
1. AC-1: Preserve current work through the owning boundary.
   - Surface: nonvisual
   - QA reachability: deterministic
   - Required proof: integration
## Issue Contract Ledger
| Contract row | Requirement |
| --- | --- |
| Positive AC | Preserve current work through the owning boundary. |
| Negative AC | Never lose valid edits or newer bytes. |
| Forbidden implementation | Never weaken the save admission guard. |
| Determinism | Fixed identity and controlled boundary ordering. |
| Required proof | Owning integration covers accepted and refused saves. |
"""
    return {"number": n, "title": f"t{n}", "labels": [{"name": l} for l in labels],
            "createdAt": created, "body": body}


class HoldCompatibilityTests(unittest.TestCase):
    def test_old_new_and_mixed_holds_across_dependency_states(self):
        forms = (
            (["pm-blocked:dependency"], "dependency"),
            (["sub:waiting-on-issue"], "waiting-on-issue"),
            (["pm-blocked:dependency", "sub:waiting-on-issue"], "sub:waiting-on-issue"),
        )
        for labels, hold in forms:
            for state in ("open", "cleared_other_hold", "incomplete_contract"):
                with self.subTest(labels=labels, state=state):
                    extra = "; external" if state == "cleared_other_hold" else ""
                    row = issue(901, ["P1", "status:todo", *labels], body=(
                        f"<!-- ready-pool:\nblockers: #902\nholds: {hold}{extra}\n-->"))
                    if state == "incomplete_contract":
                        row["body"] = "<!-- ready-pool:\nclaimable_slot_type: dev\n-->"
                    if state == "incomplete_contract":
                        self.assertIsNone(rps.classify(row))
                        continue
                    candidate = rps.candidate(row, "new_issue")
                    resolved = state == "cleared_other_hold"
                    rps.evaluate_dependency(candidate, {('native', 901): [], (902, "default"): (resolved, "CLOSED" if resolved else "OPEN")}, offline=False)
                    self.assertFalse(candidate.get("dependency_unblocked", False))
                    if resolved:
                        self.assertIn("pm-blocked:external", candidate["blocking_labels"])
                        self.assertEqual(candidate.get("dependency_reason"), "other_blocking_labels")
                        self.assertIsNone(rps.classify(row))
                    else:
                        self.assertEqual(rps.classify(row), "new_issue")
                        self.assertTrue(candidate.get("open_dependencies"))


class DispatchContentTests(unittest.TestCase):
    def test_pm_owned_incidents_and_non_dev_need_investigation(self):
        for header in (
            "claimable_slot_type: pm-hotfix\nowner: PM\ndisposition: PM_OWNED BLOCKED_INVESTIGATION",
            "claimable_slot_type: dev\nlane: PM\ndisposition: READY",
            "claimable_slot_type: dev\ndisposition: PARKED_NO_CAUSAL_CHAIN",
            "claimable_slot_type: dev\ndisposition: DEFERRED_FOR_DECISION",
            "claimable_slot_type: none\ndisposition: NEEDS_INVESTIGATION",
        ):
            with self.subTest(header=header):
                row = issue(11310, ["P1", "status:todo"], body=f"<!-- ready-pool:\n{header}\n-->")
                self.assertIsNone(rps.classify(row))

    def test_dev_with_pm_routing_owner_remains_eligible(self):
        row = issue(12944, ["P1", "status:todo"], body="<!-- ready-pool:\nowner: Dhruva PM\ndisposition: READY\n-->")
        self.assertEqual(rps.classify(row), "new_issue")

    def test_dev_no_product_code_change_is_eligible(self):
        row = issue(11890, ["P1", "status:todo"])
        row["body"] = row["body"].replace("AC-1: Preserve current work through the owning boundary.", "AC-1: No product code change in this slice.")
        self.assertEqual(rps.classify(row), "new_issue")

    def test_quoted_and_aliased_dev_is_eligible(self):
        row = issue(11891, ["P1", "status:todo"])
        row["body"] = row["body"].replace("claimable_slot_type: dev", 'slot_type: "dev"')
        self.assertEqual(rps.classify(row), "new_issue")

    def test_last_header_controls_dispatch(self):
        row = issue(11892, ["P1", "status:todo"])
        row["body"] = '<!-- ready-pool:\nclaimable_slot_type: pm\ndisposition: PARKED\n-->\n' + row["body"]
        self.assertEqual(rps.classify(row), "new_issue")
        row["body"] += '\n<!-- ready-pool:\nclaimable_slot_type: pm\ndisposition: PARKED\n-->'
        self.assertIsNone(rps.classify(row))

    def test_ready_header_not_blocked_by_stale_or_negated_comments(self):
        row = issue(12063, ["P1", "status:todo"])
        row["comments"] = [{"body": "PM parked this previously"}, {"body": "not blocked"}]
        self.assertEqual(rps.dispatch_content_refusals(row), [])
        self.assertEqual(rps.classify(row), "new_issue")

    def test_clean_dev_and_light_candidate_remain_eligible(self):
        row = issue(12052, ["P1", "status:todo"])
        self.assertEqual(rps.classify(row), "new_issue")
        for body in (None, "", "   "):
            row["body"] = body
            self.assertIsNone(rps.classify(row))
        del row["body"]
        self.assertIsNone(rps.classify(row))
        self.assertEqual(rps.classify(row, light=True), "new_issue")


class RankTests(unittest.TestCase):
    def test_priority_beats_class_and_age(self):
        issues = [
            issue(1, ["P2", "status:qa-rework"], "2026-01-01T00:00:00Z"),  # old P2 rework
            issue(2, ["P1", "status:todo"], "2026-10-20T00:00:00Z"),      # newer P1 new_issue
            issue(3, ["P0", "status:todo"], "2026-10-27T00:00:00Z"),      # newest P0
        ]
        self.assertEqual([c["issue"] for c in rps.rank_candidates(issues)], [3, 2, 1])

    def test_tie_rework_before_new_then_age(self):
        issues = [
            issue(10, ["P1", "status:todo"], "2026-09-01T00:00:00Z"),
            issue(11, ["P1", "pm-state:qa-failed-rework"], "2026-10-25T00:00:00Z"),
            issue(12, ["P1", "status:todo"], "2026-08-01T00:00:00Z"),
        ]
        self.assertEqual([c["issue"] for c in rps.rank_candidates(issues)], [11, 12, 10])

    def test_exclusions(self):
        issues = [
            issue(20, ["P0", "status:todo", "pm-blocked:product"]),
            issue(21, ["P0", "status:todo", "slot:3"]),
            issue(22, ["P0", "status:in-progress"]),
            issue(23, ["P0", "status:todo", "pm-nonclaimable:tracking"]),
            issue(24, ["P0", "status:backlog"]),
            issue(25, ["P0", "status:todo"], body="<!-- ready-pool:\nclaimable_slot_type: pm-direct\n-->"),
            issue(26, ["P2", "status:todo"]),
        ]
        self.assertEqual([c["issue"] for c in rps.rank_candidates(issues)], [26])

    def test_unlabelled_priority_falls_back_to_ready_pool_then_last(self):
        issues = [
            issue(30, ["status:todo"]),
            issue(31, ["status:todo"], body="<!-- ready-pool:\npriority: P1\n-->"),
            issue(32, ["P2", "status:todo"]),
        ]
        self.assertEqual([c["issue"] for c in rps.rank_candidates(issues)], [32])

    def test_label_wins_over_stale_ready_pool_priority(self):
        issues = [issue(40, ["P2", "status:todo"], body="<!-- ready-pool:\npriority: P1\n-->"),
                  issue(41, ["P1", "status:todo"], "2026-10-28T00:00:00Z")]
        self.assertEqual([c["issue"] for c in rps.rank_candidates(issues)], [41])

    def test_rework_only_and_extra_rows(self):
        issues = [issue(50, ["P0", "status:todo"]), issue(51, ["P2", "status:qa-rework"]),
                  issue(52, ["P1"])]  # live source for the unlabelled explicit rework lane
        extra = [{"issue": 52, "class": "rework", "priority": "P1", "pr": 900}]
        ranked = rps.rank_candidates(issues, extra, rework_only=True)
        self.assertEqual([c["issue"] for c in ranked], [52, 51])
        self.assertEqual(ranked[0]["pr"], 900)


class CheckAndReasonTests(unittest.TestCase):
    def test_check_refuses_p2_new_issue_while_p1_ready(self):
        ranked = rps.rank_candidates([issue(60, ["P2", "status:todo"]), issue(61, ["P1", "status:todo"])])
        res = rps.check_pick(60, ranked)
        self.assertEqual(res["status"], "PRIORITY_INVERSION")
        self.assertEqual(res["should_take"]["issue"], 61)

    def test_check_allows_p2_when_no_p0_p1_and_logs_reason(self):
        ranked = rps.rank_candidates([issue(70, ["P2", "status:todo"]), issue(71, ["P3", "status:todo"])])
        res = rps.check_pick(70, ranked)
        self.assertEqual(res["status"], "ok")
        self.assertIn("no ready P0/P1", res["priority_reason"])

    def test_check_allows_same_tier_older_skip_and_p1_has_no_reason(self):
        ranked = rps.rank_candidates([issue(80, ["P1", "status:todo"], "2026-01-01T00:00:00Z"),
                                      issue(81, ["P1", "status:todo"], "2026-10-01T00:00:00Z")])
        res = rps.check_pick(81, ranked)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["priority_reason"], "")

    def test_check_not_eligible(self):
        self.assertEqual(rps.check_pick(999, [])["status"], "not_eligible")


GATED_BODY = ("<!-- ready-pool:\npriority: P0\ngate: dependency:#12000\n"
              "wake_condition: #12000 merged\ndisposition: READY_GATED\n-->")


class GatedDependencyTests(unittest.TestCase):
    def setUp(self):
        real_run = subprocess.run
        def lookup(cmd, **kwargs):
            if any(str(part).endswith("validate-issue-contract-ledger.py") for part in cmd):
                return real_run(cmd, **kwargs)
            return subprocess.CompletedProcess([], 0, json.dumps({"state": "open"}), "")
        for p in (patch.object(rps, "load_open_prs", return_value=[]),
                  patch.object(rps.subprocess, "run", side_effect=lookup)):
            p.start()
            self.addCleanup(p.stop)
        self.issues = [
            issue(100, ["P0", "status:todo", "pm-blocked:dependency"], body=GATED_BODY),
            issue(101, ["P1", "status:todo", "pm-blocked:dependency"]),   # non-P0 dependency wait: visible, not assignable
            issue(102, ["P0", "status:todo", "pm-blocked:pm-gate", "slot:2"]),  # on a slot: dropped
            issue(103, ["P0", "status:backlog", "pm-blocked:dependency"]),      # EXCLUDE_LABELS: dropped
            issue(104, ["P2", "status:todo"]),
        ]

    def test_gated_p0_listed_not_picked(self):
        ranked = rps.rank_candidates(self.issues)
        gated = rps.gated_p0_candidates(self.issues)
        self.assertEqual([c["issue"] for c in ranked], [104])
        self.assertEqual([c["issue"] for c in gated], [100, 101])
        self.assertEqual(gated[0]["gate"], "dependency:#12000")
        self.assertEqual(gated[0]["wake_condition"], "#12000 merged")
        self.assertEqual(gated[0]["blocking_labels"], ["pm-blocked:dependency"])

    def test_check_gated_p0_returns_gated(self):
        res = rps.check_pick(100, rps.rank_candidates(self.issues), rps.gated_p0_candidates(self.issues))
        self.assertEqual(res["status"], "DEPENDENCY_OPEN")
        self.assertEqual(res["gate"], "dependency:#12000")

    def test_lower_pick_with_only_gated_p0_is_not_inversion(self):
        ranked = rps.rank_candidates(self.issues)
        res = rps.check_pick(104, ranked, rps.gated_p0_candidates(self.issues))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(rps.priority_inversions([{"slot": 1, "occupied": True, "issue": 104}],
                                                 {104: 2}, ranked), [])

    def test_extra_row_cannot_ungate(self):
        ranked = rps.rank_candidates(self.issues, [{"issue": 100, "class": "rework", "priority": "P0"}])
        self.assertNotIn(100, [c["issue"] for c in ranked])

    def test_main_exit_codes(self):
        import json, tempfile, io, contextlib
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(self.issues, f)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rps.main(["--issues-json", f.name, "--check", "100"]), 4)
            self.assertEqual(rps.main(["--issues-json", f.name, "--check", "104"]), 0)
            self.assertEqual(rps.main(["--issues-json", f.name]), 0)
        last = json.loads(out.getvalue().split("\n", 2)[2])
        self.assertEqual(last["pick"]["issue"], 104)
        self.assertEqual([g["issue"] for g in last["gated_p0"]], [100])


class InversionTests(unittest.TestCase):
    def test_flags_p2_slot_while_p0_ready(self):
        ranked = rps.rank_candidates([issue(90, ["P0", "status:todo"])])
        slots = [{"slot": 1, "occupied": True, "issue": 11462},
                 {"slot": 2, "occupied": True, "issue": 500},
                 {"slot": 3, "occupied": False}]
        out = rps.priority_inversions(slots, {11462: 2, 500: 1}, ranked)
        self.assertEqual(len(out), 1)
        self.assertIn("PRIORITY_INVERSION S1 on #11462 (P2)", out[0])
        self.assertIn("P0 #90", out[0])

    def test_no_flag_when_p0_already_on_a_slot_or_none_ready(self):
        ranked = rps.rank_candidates([issue(90, ["P0", "status:todo"])])
        slots = [{"slot": 1, "occupied": True, "issue": 90}, {"slot": 2, "occupied": True, "issue": 11462}]
        self.assertEqual(rps.priority_inversions(slots, {11462: 2, 90: 0}, ranked), [])
        self.assertEqual(rps.priority_inversions(slots, {11462: 2}, []), [])

    def test_unknown_priority_lane_not_flagged(self):
        ranked = rps.rank_candidates([issue(90, ["P0", "status:todo"])])
        slots = [{"slot": 4, "occupied": True, "issue": 777}]
        self.assertEqual(rps.priority_inversions(slots, {}, ranked), [])


class ParseDependenciesTests(unittest.TestCase):
    """Regression tests for the two defects that gated 27 issues as `unparsed_wake_condition`.

    Defect 1: `fullmatch` was applied to `blockers` AND `wake_condition`, but `wake_condition`
              is prose, so it always returned None and hid a well-formed blockers list.
    Defect 2: a prose blocker naming a real issue (`#11785 slice A merged; ...`) was treated as
              unparseable instead of yielding that issue.
    """

    def test_prose_wake_does_not_hide_structured_blockers(self):
        self.assertEqual(rps.parse_dependencies("#12773", "Merge of #12773"), [(12773, "default")])

    def test_prose_blocker_yields_its_references(self):
        refs = rps.parse_dependencies(
            "#11785 slice A merged; #11891 billing design (CTO review)", "")
        self.assertEqual(refs, [(11785, "merge"), (11891, "default")])

    def test_parent_umbrella_ref_is_context_not_dependency(self):
        refs = rps.parse_dependencies(
            "PR #12507 (Part of #12504) merge; PM split ruling 2026-11-02", "")
        self.assertEqual(refs, [(12507, "merge")])

    def test_wake_prose_without_refs_parses_empty(self):
        self.assertEqual(rps.parse_dependencies("", "none"), [])

    def test_blocker_prose_naming_no_ref_stays_unparseable(self):
        # A real dependency we cannot see must not silently become assignable.
        self.assertIsNone(rps.parse_dependencies(
            "CTO product decision ts 1793358878.091759 unanswered", ""))

    def test_blocked_label_with_no_declared_dependency_is_not_assignable(self):
        for blockers, wake in (("", ""), ("none", "none")):
            c = {"blockers": blockers, "wake_condition": wake, "blocking_labels": ["pm-blocked:dependency"]}
            rps.evaluate_dependency(c, {}, offline=True)
            self.assertEqual(c["dependency_reason"], "no_dependency_declared")
            self.assertNotIn("dependency_unblocked", c)


class ShippedProseWakeCaseTests(unittest.TestCase):
    """The exact blocker text from issue #12362 and #12791 that gated them before the fix."""

    def setUp(self):
        for p in (patch.object(rps, "load_open_prs", return_value=[]),
                  patch.object(rps.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, json.dumps({"state": "open"}), ""))):
            p.start()
            self.addCleanup(p.stop)

    def test_12362_shape_is_evaluated_not_unparsed(self):
        c = {"blockers": "PR #12507 (Part of #12504) merge; PM split ruling 2026-11-02",
             "wake_condition": "#12507 server-fencing gate ACTIVATED",
             "blocking_labels": ["pm-blocked:dependency"]}
        rps.evaluate_dependency(c, {})
        self.assertNotEqual(c.get("dependency_reason"), "unparsed_wake_condition")
        self.assertEqual([d["number"] for d in c["dependencies"]], [12507])


class IssueLabelCompatibilityTests(unittest.TestCase):
    def test_dependency_only_old_and_new_representations_match(self):
        old = issue(300, ["P0", "status:todo", "pm-blocked:dependency"])
        new = issue(300, ["P0", "status:todo", "sub:waiting-on-issue"],
                    body="<!-- ready-pool:\nblockers: #999999\nholds: dependency\n-->")
        self.assertEqual(rps.classify(old), rps.classify(new))
        self.assertEqual(rps.blocking_labels(rps._labels(old), old),
                         rps.blocking_labels(rps._labels(new), new))

    def test_dependency_cleared_but_another_hold_remains(self):
        item = issue(301, ["P0", "status:blocked", "sub:external", "pm-blocked:dependency"],
                     body="<!-- ready-pool:\nholds: dependency, external\n-->")
        self.assertIsNone(rps.classify(item))
        self.assertEqual(rps.blocking_labels(rps._labels(item), item),
                         ["pm-blocked:dependency", "pm-blocked:external"])

    def test_dependency_cleared_pm_gate_still_blocks(self):
        before = issue(304, ["P0", "status:blocked", "sub:pm-gate", "pm-blocked:dependency",
                             "pm-blocked:pm-gate"],
                       body="<!-- ready-pool:\nholds: dependency, pm-gate\n-->")
        after = issue(304, ["P0", "status:blocked", "sub:pm-gate", "pm-blocked:pm-gate"],
                      body="<!-- ready-pool:\nholds: pm-gate\n-->")
        self.assertIsNone(rps.classify(before))
        self.assertIsNone(rps.classify(after))
        self.assertEqual(rps.blocking_labels(rps._labels(after), after), ["pm-blocked:pm-gate"])

    def test_incomplete_contract_stays_unassignable(self):
        item = issue(302, ["P1", "status:todo", "sub:waiting-on-issue"], body="")
        self.assertEqual(rps.rank_candidates([item]), [])

    def test_dependency_only_hold_must_be_todo_not_blocked(self):
        item = issue(303, ["P0", "status:blocked", "sub:waiting-on-issue"],
                     body="<!-- ready-pool:\nholds: dependency\n-->")
        self.assertIsNone(rps.classify(item))


if __name__ == "__main__":
    unittest.main()
