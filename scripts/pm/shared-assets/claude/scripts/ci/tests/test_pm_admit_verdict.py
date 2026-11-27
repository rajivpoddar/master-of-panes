#!/usr/bin/env python3
"""Tests for pm-admit-verdict.py (PM brief 2026-11-27, Rajiv OK C0ALZJHGE49 thread 1795754821.658339, ts 1795761962.862319).

`gate`: GitHub is replaced by a fake `gh` on PATH that serves fixture JSON per endpoint (events, comments, runs per head_sha, jobs per run id).
`descendant`: a real temp git repo (rule 60: the property is checked on built history, no list of the real repo).
Rework 2026-11-27 (CTO REVISE, thread 1795754821.658339, ts 1795766409.314279): the gate selects the latest SUBSTANTIVE pull_request run per workflow
(label-wrapper runs are ignored) and accepts only producer-shape verdicts bound to head + run id + CURRENT attempt (no head-only fallback).
Rework 2026-11-27 #2 (CTO REVISE, ts 1795769102.876159): ONE shared reader accepts both deployed envelopes (investigation marker first, or the standard heading
first with the marker anywhere after it; `head` or `head_sha`), using the real producer comment 7212581439 as a literal fixture, and only the trusted
posting account's comments count (the authenticated `gh` login, the account every PM agent posts as; no hardcoded list).
Run: python3 -I ~/.claude/scripts/ci/tests/test_pm_admit_verdict.py
"""
import json
import os
import subprocess
import tempfile
import textwrap
import unittest

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pm-admit-verdict.py")
H = "a" * 40      # the head under test
OLD = "b" * 40    # an earlier head of the same PR
REPO = "Scribie/heydonna-app"

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, re, sys
    d = os.environ["FAKE_GH_DIR"]
    a = sys.argv[2] if len(sys.argv) > 2 else ""
    if os.environ.get("FAKE_GH_FAIL") and re.search(os.environ["FAKE_GH_FAIL"], a):
        sys.stderr.write("boom\\n"); sys.exit(1)
    if a == "user":  # `gh api user --jq .login`: the trusted PM account
        sys.stdout.write(os.environ.get("FAKE_GH_LOGIN", "pm-bot") + "\\n"); sys.exit(0)
    if "/events" in a: f = "events.json"
    elif "/comments" in a: f = "comments.json"
    elif "/jobs" in a: f = "jobs-" + re.search(r"/runs/(\\d+)/jobs", a).group(1) + ".json"
    elif "/actions/runs" in a: f = "runs-" + re.search(r"head_sha=([0-9a-f]{40})", a).group(1) + ".json"
    else: sys.exit(2)
    p = os.path.join(d, f)
    empty = {"runs-": '[{"workflow_runs": []}]', "jobs-": '[{"jobs": []}]'}.get(f[:5], "[[]]")
    sys.stdout.write(open(p).read() if os.path.exists(p) else empty)
    """)

PAID_STEP = {"ci.yml": "Confirm current paid CI success", "e2e.yml": "Confirm current paid E2E success"}
GATE_STEP = {"ci.yml": "Require exemption or exact-head real CI success", "e2e.yml": "Require exemption or exact-head real E2E success"}


def label_event(eid, head):
    return {"id": eid, "event": "labeled", "label": {"name": f"ci-head:{head}"}}


def run(rid, wf, conclusion, head=H, started="2026-11-27T10:00:00Z", attempt=1, event="pull_request"):
    return {"id": rid, "path": f".github/workflows/{wf}", "name": {"ci.yml": "CI", "e2e.yml": "E2E"}.get(wf, "Perf"), "head_sha": head,
            "conclusion": conclusion, "run_started_at": started, "created_at": started, "event": event, "run_attempt": attempt,
            "html_url": f"https://example.invalid/runs/{rid}"}


def jobs_page(wf, wrapper, gate_conclusion="success"):
    """One /jobs page. A substantive paid run skips the gate step; a label-wrapper run skips the paid step and runs the gate step."""
    steps = [{"name": "Set up job", "conclusion": "success"},
             {"name": PAID_STEP[wf], "conclusion": "skipped" if wrapper else "success"},
             {"name": GATE_STEP[wf], "conclusion": gate_conclusion if wrapper else "skipped"}]
    return [{"jobs": [{"name": "test" if wf == "ci.yml" else "e2e", "steps": steps}]}]


TRUSTED = "pm-bot"  # what the fake `gh api user` answers; the account all PM agents post as


def verdict_comment(cid, run_id, head, cls, created="2026-11-27T11:00:00Z", attempt=1, login=TRUSTED, **extra):
    """Legacy envelope (marker first, `head` key): marker line first (run id, attempt, full head), then the ci-verdict JSON."""
    body = {"run_id": str(run_id), "head": head, "classification": cls, "evidence": "ev", "follow_up": "#900"}
    body.update(extra)
    return {"id": cid, "created_at": created, "html_url": f"https://example.invalid/c/{cid}", "user": {"login": login},
            "body": f"<!-- ci-failure-investigation:run={run_id} attempt={attempt} head={head} -->\ntext\n<!-- ci-verdict: " + json.dumps(body) + " -->"}


def heading_comment(cid, run_id, head, cls, created="2026-11-27T11:00:00Z", attempt=1, login=TRUSTED, head_key="head_sha", **extra):
    """Deployed heading envelope, the shape of #14917 comment 7212581439: heading, blank line, marker, JSON keyed `head_sha` (no `head`), free text."""
    body = {"attempt": attempt, "blocking_for_merge": True, "classification": cls, "current_for_pr": True, "current_pr_head_sha": head,
            head_key: head, "pr": 14917, "run_head_sha": head, "run_id": int(run_id), "sha": head}
    body.update(extra)
    return {"id": cid, "created_at": created, "html_url": f"https://example.invalid/c/{cid}", "user": {"login": login},
            "body": ("## CI failure investigation verdict\n\n"
                     f"<!-- ci-failure-investigation:run={run_id} attempt={attempt} head={head} -->\n"
                     "<!-- ci-verdict: " + json.dumps(body, sort_keys=True, separators=(",", ":")) + " -->\n\nFree text after the machine block.\n")}


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        gh = os.path.join(self.bin, "gh")
        with open(gh, "w") as fh:
            fh.write(FAKE_GH)
        os.chmod(gh, 0o755)
        self.put("events.json", [[]])  # gh --paginate --slurp wraps even an empty listing in one page
        self.put("comments.json", [[]])

    def put(self, name, data):
        with open(os.path.join(self.dir, name), "w") as fh:
            json.dump(data, fh)

    def runs(self, head, runs):
        self.put(f"runs-{head}.json", [{"workflow_runs": runs}])

    def jobs(self, run_id, wf, wrapper, gate_conclusion="success"):
        self.put(f"jobs-{run_id}.json", jobs_page(wf, wrapper, gate_conclusion))

    def gate(self, scope="head", fail=None, login=TRUSTED, head=H):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], FAKE_GH_DIR=self.dir, PM_ADMIT_RETRY_SLEEP="0", FAKE_GH_LOGIN=login)
        if fail:
            env["FAKE_GH_FAIL"] = fail
        r = subprocess.run(["python3", "-I", HELPER, "gate", "--repo", REPO, "--pr", "14950", "--head", head, "--scope", scope],
                           capture_output=True, text=True, env=env)
        return r.returncode, r.stdout

    def admit(self, *heads):
        self.put("events.json", [[label_event(100 + i, h) for i, h in enumerate(heads)]])


class GateTest(Fixture):
    def test_never_admitted_head_proceeds_without_reading_runs(self):
        self.runs(H, [run(1, "ci.yml", "failure")])
        rc, out = self.gate()
        self.assertEqual(rc, 0, out)
        self.assertIn("proceed", out)

    def test_admitted_head_with_no_red_proceeds(self):
        self.admit(H)
        self.runs(H, [run(1, "ci.yml", "success"), run(2, "e2e.yml", "success")])
        self.assertEqual(self.gate()[0], 0)

    def test_cancelled_and_in_progress_are_not_red(self):
        self.admit(H)
        self.runs(H, [run(1, "ci.yml", "cancelled"), run(2, "e2e.yml", None)])
        self.assertEqual(self.gate()[0], 0)

    def test_red_without_any_verdict_is_classify_first(self):
        self.admit(H)
        self.runs(H, [run(11, "e2e.yml", "failure")])
        rc, out = self.gate()
        self.assertEqual(rc, 20, out)
        self.assertIn("REFUSE: ci_verdict_required #14950", out)
        self.assertIn("e2e.yml:11", out)
        self.assertIn("classify first", out)

    def test_each_flaky_class_prints_the_admin_ask_packet(self):
        for cls in ("flaky", "infra", "inherited-main", "out-of-diff", "Out_Of_Diff", "INHERITED_MAIN", "flake"):
            with self.subTest(cls=cls):
                self.admit(H)
                self.runs(H, [run(11, "e2e.yml", "failure")])
                self.put("comments.json", [[verdict_comment(5, 11, H, cls, failing_tests=["spec/a.spec.ts:12", "spec/b.spec.ts:7"])]])
                rc, out = self.gate()
                self.assertEqual(rc, 21, out)
                self.assertIn("ADMIN_ASK_PACKET (S171/S157) #14950 head=" + H, out)
                self.assertIn("run e2e.yml:11", out)
                self.assertIn("spec/a.spec.ts:12; spec/b.spec.ts:7", out)
                self.assertIn("follow_up_issue: #900", out)
                self.assertIn("evidence: ev", out)
                self.assertIn("--match-head-commit", out)

    def test_packet_marks_missing_failing_tests_unavailable_never_invented(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky", follow_up="")]])
        rc, out = self.gate()
        self.assertEqual(rc, 21, out)
        self.assertIn("failing_tests: UNAVAILABLE (not in the verdict", out)
        self.assertIn("follow_up_issue: UNAVAILABLE", out)

    def test_pr_caused_verdict_proceeds(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 11, H, "pr-caused")]])
        rc, out = self.gate()
        self.assertEqual(rc, 0, out)
        self.assertIn("class=pr-caused", out)

    def test_unknown_class_is_not_treated_as_flaky(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 11, H, "test")]])
        self.assertEqual(self.gate()[0], 0)

    def test_latest_verdict_for_the_run_wins(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky", created="2026-11-27T11:00:00Z"),
                                    verdict_comment(6, 11, H, "pr-caused", created="2026-11-27T12:00:00Z")]])
        self.assertEqual(self.gate()[0], 0)
        self.put("comments.json", [[verdict_comment(5, 11, H, "pr-caused", created="2026-11-27T11:00:00Z"),
                                    verdict_comment(6, 11, H, "infra", created="2026-11-27T12:00:00Z")]])
        self.assertEqual(self.gate()[0], 21)

    def test_verdict_bound_to_another_head_does_not_count(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 11, OLD, "flaky")]])
        self.assertEqual(self.gate()[0], 20)

    def test_verdict_json_without_head_does_not_count_even_with_the_marker_head(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        c = verdict_comment(5, 11, H, "flaky")
        c["body"] = c["body"].replace(f'"head": "{H}", ', "")
        self.put("comments.json", [[c]])
        self.assertEqual(self.gate()[0], 20)

    def test_investigation_marker_alone_is_not_a_verdict(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        c = verdict_comment(5, 11, H, "flaky")
        c["body"] = c["body"].split("\n")[0] + "\nno machine-readable verdict here"
        self.put("comments.json", [[c]])
        self.assertEqual(self.gate()[0], 20)

    def test_verdict_for_a_different_run_does_not_cover_the_red_run(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 999, H, "flaky")]])
        self.assertEqual(self.gate()[0], 20)

    def test_verdict_with_no_run_id_covers_nothing_there_is_no_head_only_fallback(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        c = verdict_comment(5, 11, H, "flaky")
        c["body"] = c["body"].replace('"run_id": "11", ', "")
        self.put("comments.json", [[c]])
        self.assertEqual(self.gate()[0], 20)

    def test_two_red_runs_one_unclassified_is_classify_first(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure"), run(12, "e2e.yml", "timed_out")])
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky")]])
        rc, out = self.gate()
        self.assertEqual(rc, 20, out)
        self.assertIn("red run(s) e2e.yml:12@1 have no ci-verdict", out)  # only the unclassified run is named as missing

    def test_two_red_runs_one_pr_caused_proceeds(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure"), run(12, "e2e.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky"), verdict_comment(6, 12, H, "pr-caused")]])
        self.assertEqual(self.gate()[0], 0)

    def test_only_the_latest_run_per_workflow_counts(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure", started="2026-11-27T09:00:00Z"), run(13, "ci.yml", "success", started="2026-11-27T10:30:00Z")])
        self.assertEqual(self.gate()[0], 0)

    def test_untracked_workflow_red_is_ignored(self):
        self.admit(H)
        self.runs(H, [run(11, "perf.yml", "failure"), run(12, "ci.yml", "success")])
        self.assertEqual(self.gate()[0], 0)

    def test_unreadable_reads_are_unavailable_not_a_guess(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky")]])
        for what in ("/events", "/actions/runs", "/comments"):
            with self.subTest(what=what):
                rc, out = self.gate(fail=what)
                self.assertEqual(rc, 11, out)
                self.assertIn("UNAVAILABLE reason=", out)

    def test_latest_scope_reports_an_older_head_red_as_diagnosis_only_never_a_packet(self):
        self.admit(OLD, H)
        self.runs(OLD, [run(21, "e2e.yml", "failure", head=OLD)])
        self.runs(H, [run(31, "ci.yml", "success")])
        self.put("comments.json", [[verdict_comment(5, 21, OLD, "inherited-main")]])
        rc, out = self.gate(scope="latest")
        self.assertEqual(rc, 0, out)
        self.assertIn("ci_verdict: info historical head=" + OLD, out)
        self.assertIn("diagnosis only", out)
        self.assertNotIn("ADMIN_ASK_PACKET", out)

    def test_latest_scope_binds_the_packet_to_the_live_head_not_an_older_one(self):
        self.admit(OLD, H)
        self.runs(OLD, [run(21, "e2e.yml", "failure", head=OLD)])
        self.runs(H, [run(31, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 21, OLD, "flaky"), verdict_comment(6, 31, H, "flaky")]])
        rc, out = self.gate(scope="latest")
        self.assertEqual(rc, 21, out)
        self.assertIn("ADMIN_ASK_PACKET (S171/S157) #14950 head=" + H, out)
        self.assertNotIn("current_pr_head", out)
        self.assertNotIn("e2e.yml:21", out)

    def test_latest_scope_live_pr_caused_verdict_proceeds_despite_an_older_flaky_red(self):
        self.admit(OLD, H)
        self.runs(OLD, [run(21, "e2e.yml", "failure", head=OLD)])
        self.runs(H, [run(31, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 21, OLD, "flaky"), verdict_comment(6, 31, H, "pr-caused")]])
        self.assertEqual(self.gate(scope="latest")[0], 0)

    def test_latest_scope_unclassified_latest_red_is_classify_first(self):
        self.admit(OLD, H)
        self.runs(H, [run(31, "ci.yml", "failure")])
        self.assertEqual(self.gate(scope="latest")[0], 20)

    def test_latest_scope_with_no_red_anywhere_proceeds_without_an_info_line(self):
        self.admit(OLD, H)
        self.runs(OLD, [run(21, "e2e.yml", "success", head=OLD)])
        self.runs(H, [run(31, "ci.yml", "success")])
        rc, out = self.gate(scope="latest")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("info historical", out)

    def test_bad_sha_is_usage(self):
        r = subprocess.run(["python3", "-I", HELPER, "gate", "--pr", "1", "--head", "abc"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 64)


class SelectionTest(Fixture):
    """Item 1: the latest SUBSTANTIVE pull_request run per workflow. Label-wrapper gate runs never hide or replace it."""

    WRAP_CI, REAL_CI = 45947144718, 45947144299   # #14950: wrapper-only red vs the real failure, identical timestamps (wrapper has the higher id)
    STAMP = "2026-11-27T05:39:56Z"

    def case_14950(self):
        self.admit(H)
        self.runs(H, [run(45947068353, "ci.yml", "failure", started="2026-11-27T05:20:00Z"),
                      run(self.WRAP_CI, "ci.yml", "failure", started=self.STAMP),
                      run(self.REAL_CI, "ci.yml", "failure", started=self.STAMP),
                      run(45947144312, "e2e.yml", "success", started=self.STAMP),
                      run(45947144689, "e2e.yml", "success", started="2026-11-27T06:01:58Z", attempt=2),
                      run(45947068311, "e2e.yml", "success", started="2026-11-27T05:20:00Z")])
        self.jobs(45947068353, "ci.yml", wrapper=True, gate_conclusion="failure")
        self.jobs(self.WRAP_CI, "ci.yml", wrapper=True, gate_conclusion="failure")
        self.jobs(self.REAL_CI, "ci.yml", wrapper=False)
        self.jobs(45947144312, "e2e.yml", wrapper=False)
        self.jobs(45947144689, "e2e.yml", wrapper=True)
        self.jobs(45947068311, "e2e.yml", wrapper=True)

    def test_14950_wrapper_red_selects_the_real_failure_and_its_existing_verdict(self):
        self.case_14950()
        self.put("comments.json", [[verdict_comment(7, self.REAL_CI, H, "inherited-main", failing_tests=["spec/x.spec.ts:9"])]])
        rc, out = self.gate()
        self.assertEqual(rc, 21, out)
        self.assertIn(f"run ci.yml:{self.REAL_CI}@1 conclusion=failure class=inherited-main", out)
        self.assertNotIn(f"run ci.yml:{self.WRAP_CI}", out)
        self.assertIn(f"selected=ci.yml:{self.REAL_CI}@1:failure,e2e.yml:45947144312@1:success", out)
        self.assertIn(f"ignored_wrapper=ci.yml:{self.WRAP_CI},e2e.yml:45947144689", out)  # newest-first, stops at the first substantive run
        self.assertIn("spec/x.spec.ts:9", out)

    def test_14950_unclassified_asks_to_classify_the_real_run_not_the_wrapper(self):
        self.case_14950()
        rc, out = self.gate()
        self.assertEqual(rc, 20, out)
        self.assertIn(f"red run(s) ci.yml:{self.REAL_CI}@1 have no ci-verdict", out)
        self.assertNotIn(f"red run(s) ci.yml:{self.WRAP_CI}", out)

    def test_a_verdict_written_for_the_wrapper_run_does_not_cover_the_real_failure(self):
        self.case_14950()
        self.put("comments.json", [[verdict_comment(7, self.WRAP_CI, H, "flaky")]])
        self.assertEqual(self.gate()[0], 20)

    def test_14950_e2e_wrapper_attempt_2_does_not_replace_the_paid_e2e_run(self):
        self.case_14950()
        self.put("comments.json", [[verdict_comment(7, self.REAL_CI, H, "pr-caused")]])
        rc, out = self.gate()
        self.assertEqual(rc, 0, out)
        self.assertIn("e2e.yml:45947144312@1:success", out)
        self.assertNotIn("e2e.yml:45947144689@", out.split("ignored_wrapper=")[0])

    def test_a_newer_green_wrapper_never_hides_a_substantive_red(self):
        self.admit(H)
        self.runs(H, [run(11, "e2e.yml", "failure", started="2026-11-27T09:00:00Z"),
                      run(12, "e2e.yml", "success", started="2026-11-27T10:30:00Z")])
        self.jobs(11, "e2e.yml", wrapper=False)
        self.jobs(12, "e2e.yml", wrapper=True)
        rc, out = self.gate()
        self.assertEqual(rc, 20, out)
        self.assertIn("e2e.yml:11@1", out)

    def test_a_newer_substantive_run_still_replaces_an_older_one(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure", started="2026-11-27T09:00:00Z"),
                      run(13, "ci.yml", "success", started="2026-11-27T10:30:00Z")])
        self.assertEqual(self.gate()[0], 0)

    def test_wrapper_whose_gate_step_failed_is_still_a_wrapper(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.jobs(11, "ci.yml", wrapper=True, gate_conclusion="failure")
        rc, out = self.gate()
        self.assertEqual(rc, 0, out)
        self.assertIn("selected=none ignored_wrapper=ci.yml:11", out)

    def test_a_skipped_or_pending_gate_step_is_not_a_wrapper(self):
        for conclusion in ("skipped", None, "cancelled"):
            with self.subTest(conclusion=conclusion):
                self.admit(H)
                self.runs(H, [run(11, "ci.yml", "failure")])
                self.jobs(11, "ci.yml", wrapper=True, gate_conclusion=conclusion)
                self.assertEqual(self.gate()[0], 20)

    def test_gate_step_in_any_job_makes_a_wrapper(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("jobs-11.json", [{"jobs": [{"name": "a", "steps": [{"name": "x", "conclusion": "success"}]}]},
                                  {"jobs": [{"name": "b", "steps": None}, {"name": GATE_STEP["ci.yml"], "steps": [
                                      {"name": GATE_STEP["ci.yml"], "conclusion": "success"}]}]}])
        self.assertEqual(self.gate()[0], 0)

    def test_non_pull_request_runs_are_not_selected(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure", event="push"), run(12, "e2e.yml", "failure", event="workflow_dispatch")])
        rc, out = self.gate()
        self.assertEqual(rc, 0, out)
        self.assertIn("selected=none", out)

    def test_other_workflow_and_other_head_runs_are_not_selected(self):
        self.admit(H)
        self.runs(H, [run(11, "perf.yml", "failure"), run(12, "ci.yml", "failure", head=OLD)])
        self.assertEqual(self.gate()[0], 0)

    def test_run_without_event_or_attempt_is_unavailable_not_a_guess(self):
        for field in ("event", "run_attempt"):
            with self.subTest(field=field):
                self.admit(H)
                bad = run(11, "ci.yml", "failure")
                del bad[field]
                self.runs(H, [bad])
                rc, out = self.gate()
                self.assertEqual(rc, 11, out)
                self.assertIn("UNAVAILABLE reason=", out)

    def test_unreadable_jobs_are_unavailable_not_a_substantive_guess(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky")]])
        rc, out = self.gate(fail="/jobs")
        self.assertEqual(rc, 11, out)
        self.assertIn("UNAVAILABLE reason=repos/" + REPO + "/actions/runs/11/jobs: gh_rc_1", out)
        self.put("jobs-11.json", [{"jobs": "nope"}])
        rc, out = self.gate()
        self.assertEqual(rc, 11, out)
        self.assertIn("bad_page", out)


class AttemptBindingTest(Fixture):
    """Item 2: verdicts bind to head + run id + the run's CURRENT attempt, in the producer shape, with no head-only fallback."""

    def red(self, attempt):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure", attempt=attempt)])

    def test_verdict_for_the_current_attempt_covers_it(self):
        self.red(2)
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky", attempt=2)]])
        rc, out = self.gate()
        self.assertEqual(rc, 21, out)
        self.assertIn("run ci.yml:11@2", out)

    def test_an_earlier_attempts_verdict_does_not_govern_a_later_attempt(self):
        self.red(2)
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky", attempt=1)]])
        rc, out = self.gate()
        self.assertEqual(rc, 20, out)
        self.assertIn("ci.yml:11@2", out)
        self.assertIn("run id + attempt", out)

    def test_a_later_attempts_verdict_does_not_cover_an_earlier_attempt(self):
        self.red(1)
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky", attempt=2)]])
        self.assertEqual(self.gate()[0], 20)

    def test_the_matching_attempt_wins_over_a_newer_comment_for_another_attempt(self):
        self.red(2)
        self.put("comments.json", [[verdict_comment(5, 11, H, "pr-caused", attempt=2, created="2026-11-27T11:00:00Z"),
                                    verdict_comment(6, 11, H, "flaky", attempt=1, created="2026-11-27T12:00:00Z")]])
        rc, out = self.gate()
        self.assertEqual(rc, 0, out)
        self.assertIn("class=pr-caused", out)

    def test_marker_must_be_the_first_non_blank_line(self):
        self.red(1)
        c = verdict_comment(5, 11, H, "flaky")
        c["body"] = "quoting an old comment:\n" + c["body"]
        self.put("comments.json", [[c]])
        self.assertEqual(self.gate()[0], 20)
        c["body"] = "\n  \n" + c["body"].split("\n", 1)[1]
        self.put("comments.json", [[c]])
        self.assertEqual(self.gate()[0], 21)

    def test_marker_and_verdict_json_must_agree_on_head_and_run(self):
        self.red(1)
        bad = {
            "json head is another head": lambda c: c["body"].replace(f'"head": "{H}"', f'"head": "{OLD}"'),
            "json head is short": lambda c: c["body"].replace(f'"head": "{H}"', f'"head": "{H[:12]}"'),
            "json run is another run": lambda c: c["body"].replace('"run_id": "11"', '"run_id": "12"'),
            "marker head is short": lambda c: c["body"].replace(f"head={H} -->", f"head={H[:12]} -->", 1),
            "marker head is another head": lambda c: c["body"].replace(f"head={H} -->", f"head={OLD} -->", 1),
        }
        for label, mutate in bad.items():
            with self.subTest(case=label):
                c = verdict_comment(5, 11, H, "flaky")
                c["body"] = mutate(c)
                self.put("comments.json", [[c]])
                self.assertEqual(self.gate()[0], 20, label)

    def test_integer_run_id_in_the_verdict_json_is_accepted(self):
        self.red(1)
        c = verdict_comment(5, 11, H, "flaky")
        c["body"] = c["body"].replace('"run_id": "11"', '"run_id": 11')
        self.put("comments.json", [[c]])
        self.assertEqual(self.gate()[0], 21)

    def test_a_stale_heading_only_comment_is_not_a_verdict(self):
        """The shape of #14950's real comment 7212435620: a heading and prose, no markers, a short head."""
        self.red(1)
        self.put("comments.json", [[{"id": 7, "created_at": "2026-11-27T11:00:00Z", "html_url": "https://example.invalid/c/7",
                                      "body": f"## CI failure investigation (head {H[:12]})\n\nClassification: IN_DIFF\n"}]])
        self.assertEqual(self.gate()[0], 20)


REAL_HEAD = "b2c191f65cad4ae3921749c7411fc347bede3760"  # PR #14917's head in comment 7212581439
REAL_RUN = 45953952237
# The exact envelope of #14917 comment 7212581439 (author = the PM account, created 2026-11-27T07:33:01Z): heading, blank line, investigation marker,
# one-line ci-verdict JSON with sorted keys and `head_sha` (NO `head` key), then free text.  Only the free-text and long evidence fields are shortened.
REAL_BODY = ("## CI failure investigation verdict\n\n"
             f"<!-- ci-failure-investigation:run={REAL_RUN} attempt=1 head={REAL_HEAD} -->\n"
             '<!-- ci-verdict: {"attempt":1,"blocking_for_merge":true,"branch":"fix/14912-fence-backup-recovery",'
             '"candidate_classes":["product-regression (main #15034/#14883 or this PR)","flake-infra"],'
             '"classification":"unconfirmed-pending-local-repro","current_for_pr":true,'
             f'"current_pr_head_sha":"{REAL_HEAD}","head_sha":"{REAL_HEAD}","pr":14917,"run_head_sha":"{REAL_HEAD}","run_id":{REAL_RUN},'
             f'"sha":"{REAL_HEAD}"}} -->\n\n'
             "**Run:** smoke-gemini-chat.spec.ts failed; local repro pending.\n")


def real_comment(login=TRUSTED, body=REAL_BODY, cid=7212581439):
    return {"id": cid, "created_at": "2026-11-27T07:33:01Z", "html_url": f"https://example.invalid/c/{cid}", "user": {"login": login}, "body": body}


class RealProducerShapeTest(Fixture):
    """Item 2 of CTO REVISE #2: the gate reads the real producer receipt (heading first, marker after it, `head_sha`) through the one shared reader,
    and still needs exact head + run id + CURRENT attempt + the trusted posting account."""

    def red(self, attempt=1, head=REAL_HEAD, run_id=REAL_RUN):
        self.admit(head)
        self.runs(head, [run(run_id, "ci.yml", "failure", head=head, attempt=attempt)])

    def test_comment_7212581439_exact_shape_parses_and_binds(self):
        self.red()
        self.put("comments.json", [[real_comment()]])
        rc, out = self.gate(head=REAL_HEAD)
        self.assertEqual(rc, 0, out)  # bound verdict, class unconfirmed-pending-local-repro is not flaky/infra/inherited -> proceeds, never a packet
        self.assertIn(f"run=ci.yml:{REAL_RUN}", out)
        self.assertIn("class=unconfirmed-pending-local-repro", out)

    def test_the_real_shape_binds_a_flaky_class_to_the_packet_with_its_head_and_run(self):
        self.red()
        self.put("comments.json", [[real_comment(body=REAL_BODY.replace("unconfirmed-pending-local-repro", "flaky"))]])
        rc, out = self.gate(head=REAL_HEAD)
        self.assertEqual(rc, 21, out)
        self.assertIn(REAL_HEAD, out)
        self.assertIn(f"ci.yml:{REAL_RUN}", out)

    def test_real_shape_for_another_head_does_not_bind(self):
        self.red()
        self.put("comments.json", [[real_comment(body=REAL_BODY.replace(REAL_HEAD, OLD))]])  # marker + every JSON head name another head
        rc, out = self.gate(head=REAL_HEAD)
        self.assertEqual(rc, 20, out)

    def test_real_shape_with_only_the_marker_head_wrong_or_the_json_head_wrong_does_not_bind(self):
        self.red()
        marker_wrong = REAL_BODY.replace(f"head={REAL_HEAD} -->", f"head={OLD} -->", 1)
        json_wrong = REAL_BODY.replace(f'"head_sha":"{REAL_HEAD}"', f'"head_sha":"{OLD}"')
        for label, body in (("marker head wrong", marker_wrong), ("json head_sha wrong", json_wrong)):
            with self.subTest(case=label):
                self.put("comments.json", [[real_comment(body=body)]])
                self.assertEqual(self.gate(head=REAL_HEAD)[0], 20, label)

    def test_real_shape_stale_attempt_does_not_bind(self):
        self.red(attempt=2)  # the run is on attempt 2; comment 7212581439 is attempt 1
        self.put("comments.json", [[real_comment()]])
        rc, out = self.gate(head=REAL_HEAD)
        self.assertEqual(rc, 20, out)
        self.assertIn(f"ci.yml:{REAL_RUN}@2", out)

    def test_payload_attempt_must_equal_the_envelope_attempt(self):
        """CTO REVISE #3 item 2: an attempt-1 payload inside an attempt-2 envelope names two attempts and is not evidence, even when the run is on attempt 2."""
        self.red(attempt=2)
        ok = real_comment(body=REAL_BODY.replace("attempt=1 head=", "attempt=2 head=", 1).replace('"attempt":1,', '"attempt":2,', 1)
                          .replace("unconfirmed-pending-local-repro", "flaky"))
        self.put("comments.json", [[ok]])
        rc, out = self.gate(head=REAL_HEAD)
        self.assertEqual(rc, 21, out)  # control: payload 2 + envelope 2 + run attempt 2 binds
        self.assertIn(f"ci.yml:{REAL_RUN}@2", out)
        mismatch = real_comment(body=ok["body"].replace('"attempt":2,', '"attempt":1,', 1))  # payload now attempt 1, marker still attempt 2
        self.assertIn('"attempt":1,', mismatch["body"])
        self.assertIn("attempt=2 head=", mismatch["body"])
        self.put("comments.json", [[mismatch]])
        rc, out = self.gate(head=REAL_HEAD)
        self.assertEqual(rc, 20, out)
        self.assertIn("run id + attempt", out)

    def test_payload_attempt_mismatch_does_not_hide_behind_a_matching_older_comment_shape(self):
        """The mismatched comment is skipped, so an older comment whose payload agrees with its envelope still governs."""
        self.red(attempt=2)
        bad = heading_comment(6, REAL_RUN, REAL_HEAD, "flaky", attempt=2, created="2026-11-27T12:00:00Z")
        bad["body"] = bad["body"].replace('"attempt":2,', '"attempt":1,', 1)
        good = heading_comment(5, REAL_RUN, REAL_HEAD, "pr-caused", attempt=2, created="2026-11-27T11:00:00Z")
        self.put("comments.json", [[bad, good]])
        rc, out = self.gate(head=REAL_HEAD)
        self.assertEqual(rc, 0, out)
        self.assertIn("class=pr-caused", out)

    def test_payload_attempt_spellings(self):
        """No attempt in the payload (legacy shape) and an equal integer or digit string bind; any other spelling or value does not."""
        self.red(attempt=1)
        legacy = verdict_comment(5, REAL_RUN, REAL_HEAD, "flaky", attempt=1)
        self.assertNotIn("attempt", json.loads(legacy["body"].split("<!-- ci-verdict: ", 1)[1].rsplit(" -->", 1)[0]))
        cases = {"legacy: no attempt in the payload": (legacy, 21)}
        for label, spelled, want in (("int equal", '1', 21), ("digit string equal", '"1"', 21), ("int other", '2', 20), ("string other", '"2"', 20),
                                     ("null", 'null', 20), ("bool", 'true', 20), ("float", '1.0', 20), ("word", '"one"', 20)):
            c = real_comment(body=REAL_BODY.replace("unconfirmed-pending-local-repro", "flaky").replace('"attempt":1,', f'"attempt":{spelled},', 1))
            cases[f"payload attempt {label}"] = (c, want)
        for label, (c, want) in cases.items():
            with self.subTest(case=label):
                self.put("comments.json", [[c]])
                self.assertEqual(self.gate(head=REAL_HEAD)[0], want, label)

    def test_real_shape_from_a_different_author_does_not_bind(self):
        self.red()
        self.put("comments.json", [[real_comment(login="someone-else")]])
        rc, out = self.gate(head=REAL_HEAD)
        self.assertEqual(rc, 20, out)
        self.assertIn("not posted by the trusted PM account", out)
        self.assertEqual(self.gate(head=REAL_HEAD, login="someone-else")[0], 0)  # the same comment IS evidence when its author is the trusted account

    def test_author_is_compared_to_the_live_gh_login_not_a_hardcoded_list(self):
        self.red()
        self.put("comments.json", [[real_comment(login="renamed-pm-account")]])
        self.assertEqual(self.gate(head=REAL_HEAD)[0], 20)
        self.assertEqual(self.gate(head=REAL_HEAD, login="renamed-pm-account")[0], 0)

    def test_comment_without_an_author_never_binds(self):
        self.red()
        c = real_comment()
        del c["user"]
        self.put("comments.json", [[c]])
        self.assertEqual(self.gate(head=REAL_HEAD)[0], 20)

    def test_unreadable_trusted_account_is_unavailable_never_a_guess(self):
        self.red()
        self.put("comments.json", [[real_comment()]])
        for label, kwargs in (("gh fails", {"fail": "^user$"}), ("empty login", {"login": ""})):
            with self.subTest(case=label):
                rc, out = self.gate(head=REAL_HEAD, **kwargs)
                self.assertEqual(rc, 11, out)
                self.assertIn("trusted_account_unreadable", out)

    def test_unreadable_account_does_not_matter_when_there_is_no_candidate_verdict(self):
        self.red()
        rc, out = self.gate(head=REAL_HEAD, fail="^user$")
        self.assertEqual(rc, 20, out)  # classify-first needs no author lookup

    def test_marker_anywhere_after_the_heading_binds_but_not_before_it_or_without_it(self):
        self.red()
        after_prose = REAL_BODY.replace("<!-- ci-failure", "Summary: red once.\n\n<!-- ci-failure", 1)
        self.put("comments.json", [[real_comment(body=after_prose)]])
        self.assertEqual(self.gate(head=REAL_HEAD)[0], 0, "marker after prose below the heading")
        no_heading = REAL_BODY.split("\n\n", 1)[1]
        self.put("comments.json", [[real_comment(body="Quoting a verdict:\n\n" + no_heading)]])
        self.assertEqual(self.gate(head=REAL_HEAD)[0], 20, "no heading and prose first: a quoted verdict is not evidence")
        self.put("comments.json", [[real_comment(body="## CI failure investigation verdict\n\nno marker line here\n" + no_heading.split("\n", 1)[1])]])
        self.assertEqual(self.gate(head=REAL_HEAD)[0], 20, "heading but the marker is not on a line of its own")

    def test_both_deployed_envelopes_and_both_head_spellings_bind(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        cases = {
            "marker-first + head": verdict_comment(5, 11, H, "flaky"),
            "heading + head_sha": heading_comment(5, 11, H, "flaky"),
            "heading + head": heading_comment(5, 11, H, "flaky", head_key="head"),
        }
        for label, comment in cases.items():
            with self.subTest(case=label):
                self.put("comments.json", [[comment]])
                self.assertEqual(self.gate()[0], 21, label)

    def test_head_and_head_sha_that_disagree_do_not_bind(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        for label, other, want in (("`head` names another head", OLD, 20), ("both spellings present and equal", H, 21)):
            with self.subTest(case=label):
                c = heading_comment(5, 11, H, "flaky")
                c["body"] = c["body"].replace('"head_sha":', f'"head":"{other}","head_sha":', 1)
                self.put("comments.json", [[c]])
                self.assertEqual(self.gate()[0], want, label)

    def test_heading_envelope_still_needs_the_run_id_to_match_the_marker(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        c = heading_comment(5, 11, H, "flaky")
        c["body"] = c["body"].replace('"run_id":11', '"run_id":12')
        self.put("comments.json", [[c]])
        self.assertEqual(self.gate()[0], 20)

    def test_legacy_marker_first_comment_from_another_author_does_not_bind_either(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky", login="someone-else")]])
        self.assertEqual(self.gate()[0], 20)

    def test_a_forged_newer_comment_from_another_author_never_overrides_the_trusted_one(self):
        self.admit(H)
        self.runs(H, [run(11, "ci.yml", "failure")])
        self.put("comments.json", [[verdict_comment(5, 11, H, "flaky", created="2026-11-27T11:00:00Z"),
                                    verdict_comment(6, 11, H, "pr-caused", created="2026-11-27T12:00:00Z", login="someone-else")]])
        rc, out = self.gate()
        self.assertEqual(rc, 21, out)  # the trusted flaky verdict stands; the newer outsider comment is ignored


class GitRepo:
    def __init__(self, root):
        self.root = root
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        return subprocess.run(["git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                              check=True, capture_output=True, text=True, env=env).stdout.strip()

    def commit(self, files, msg):
        for rel, body in files.items():
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def merge_main(self, msg="merge main"):
        self.git("merge", "-q", "--no-ff", "-m", msg, "main")
        return self.git("rev-parse", "HEAD")


class DescendantTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = GitRepo(self._tmp.name)
        self.repo.commit({"a.txt": "a\n"}, "base")
        self.repo.git("checkout", "-q", "-b", "pr")
        self.prev = self.repo.commit({"lib/pr.ts": "pr 1\n", "lib/two.ts": "two\n"}, "pr work")
        self.repo.git("checkout", "-q", "main")
        self.repo.commit({"main/other.ts": "m1\n"}, "main moves")
        self.repo.git("checkout", "-q", "pr")

    def check(self, head, prev=None):
        r = subprocess.run(["python3", "-I", HELPER, "descendant", "--repo-dir", self.repo.root, "--main-ref", "main",
                            "--head", head, "--prev", prev or self.prev], capture_output=True, text=True)
        return r.returncode, r.stdout

    def test_one_merge_only_descendant_inherits(self):
        head = self.repo.merge_main()
        rc, out = self.check(head)
        self.assertEqual(rc, 0, out)
        self.assertIn("byte-identical", out)

    def test_chain_of_merge_only_descendants_inherits(self):
        self.repo.merge_main()
        self.repo.git("checkout", "-q", "main")
        self.repo.commit({"main/more.ts": "m2\n"}, "main moves again")
        self.repo.git("checkout", "-q", "pr")
        head = self.repo.merge_main("merge main 2")
        self.assertEqual(self.check(head)[0], 0)

    def test_a_real_commit_between_the_heads_does_not_inherit(self):
        self.repo.commit({"notes.md": "x\n"}, "author commit")
        head = self.repo.merge_main()
        rc, out = self.check(head)
        self.assertEqual(rc, 1, out)
        self.assertIn("not a merge commit", out)

    def test_merge_that_edits_a_pr_file_does_not_inherit(self):
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "main")
        with open(os.path.join(self.repo.root, "lib/pr.ts"), "w") as fh:
            fh.write("pr 1 edited inside the merge\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "evil merge")
        rc, out = self.check(self.repo.git("rev-parse", "HEAD"))
        self.assertEqual(rc, 1, out)
        self.assertIn("a PR file differs", out)

    def test_merge_that_adds_a_pr_file_does_not_inherit(self):
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "main")
        with open(os.path.join(self.repo.root, "lib/new.ts"), "w") as fh:
            fh.write("sneaked in\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "evil merge adds file")
        rc, out = self.check(self.repo.git("rev-parse", "HEAD"))
        self.assertEqual(rc, 1, out)
        self.assertIn("different file set", out)

    def test_mode_change_of_a_pr_file_does_not_inherit(self):
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "main")
        os.chmod(os.path.join(self.repo.root, "lib/pr.ts"), 0o755)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "mode flip")
        self.assertEqual(self.check(self.repo.git("rev-parse", "HEAD"))[0], 1)

    def test_same_head_is_not_an_inheritance(self):
        self.assertEqual(self.check(self.prev)[0], 1)

    def test_unrelated_earlier_head_does_not_inherit(self):
        head = self.repo.merge_main()
        self.repo.git("checkout", "-q", "main")
        other = self.repo.commit({"x.txt": "x\n"}, "unrelated")
        self.assertEqual(self.check(head, prev=other)[0], 1)

    def test_unknown_object_fails_closed(self):
        head = self.repo.merge_main()
        rc, out = self.check(head, prev="c" * 40)
        self.assertNotEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
