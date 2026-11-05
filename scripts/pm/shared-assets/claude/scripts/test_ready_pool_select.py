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
    return {"number": n, "title": f"t{n}", "labels": [{"name": l} for l in labels],
            "createdAt": created, "body": body}


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
        self.assertEqual([c["issue"] for c in rps.rank_candidates(issues)], [31, 32, 30])

    def test_label_wins_over_stale_ready_pool_priority(self):
        issues = [issue(40, ["P2", "status:todo"], body="<!-- ready-pool:\npriority: P1\n-->"),
                  issue(41, ["P1", "status:todo"], "2026-10-28T00:00:00Z")]
        self.assertEqual([c["issue"] for c in rps.rank_candidates(issues)], [41, 40])

    def test_rework_only_and_extra_rows(self):
        issues = [issue(50, ["P0", "status:todo"]), issue(51, ["P2", "status:qa-rework"])]
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
        for p in (patch.object(rps, "load_open_prs", return_value=[]),
                  patch.object(rps.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"state":"open"}), ""))):
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


if __name__ == "__main__":
    unittest.main()
