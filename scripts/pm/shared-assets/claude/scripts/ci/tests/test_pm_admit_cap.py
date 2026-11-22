#!/usr/bin/env python3
"""Unit tests for ci/pm-admit-cap.py and ci/ci_admitted_runs.py (no network: fake api and a stub gh on PATH).

Structural by design (rule 60 spirit): decisions are asserted from generated fixtures and from the constants the
code itself defines, never from hand-kept lists of production PRs, runs or sites.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CI_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CI_DIR))
import ci_admitted_runs as car  # noqa: E402

SPEC = importlib.util.spec_from_file_location("pac", CI_DIR / "pm-admit-cap.py")
pac = importlib.util.module_from_spec(SPEC)
sys.modules["pac"] = pac
SPEC.loader.exec_module(pac)

SKILL = Path.home() / ".claude/skills/ci-failure-investigation/SKILL.md"
T0 = dt.datetime(2026, 11, 20, 10, 0, 0, tzinfo=dt.timezone.utc)


def sha(n):
    return f"{n:040x}"


def iso(seconds):
    return (T0 + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def label(head, at, name=None, event="labeled"):
    return {"event": event, "created_at": iso(at), "label": {"name": name if name is not None else f"ci-head:{head}"}}


def run(rid, head, workflow="ci.yml", conclusion="failure", wall=1800, attempt=1, pr=None, event="pull_request"):
    row = {"id": rid, "head_sha": head, "event": event, "path": f".github/workflows/{workflow}@refs/pull/1/merge",
           "name": "CI", "conclusion": conclusion, "run_attempt": attempt, "run_started_at": iso(0), "updated_at": iso(wall),
           "pull_requests": [{"number": pr}] if pr else []}
    return row


def job(name, conclusion, seconds):
    return {"name": name, "conclusion": conclusion, "started_at": iso(0), "completed_at": iso(seconds)}


GATE_ONLY = [job("classify-change-scope", "success", 12), job("test", "failure", 10), job("typescript", "skipped", 0)]
REAL = [job("classify-change-scope", "success", 12), job("typescript", "failure", car.QUICK_JOB_S * 20)]


def verdict(rid, head, classification, attempt=1, at=0, cid=None, run_id_in_json=None):
    payload = {"classification": classification, "run_id": run_id_in_json if run_id_in_json is not None else rid}
    body = (f"## CI failure investigation verdict\n<!-- ci-failure-investigation:run={rid} attempt={attempt} head={head} -->\n"
            f"<!-- ci-verdict: {json.dumps(payload)} -->\nbody")
    return {"id": cid if cid is not None else at, "created_at": iso(at), "body": body}


class FakeApi:
    """Routes the exact paths evaluate() may request; anything else fails the test."""

    def __init__(self, comments=(), runs=None, jobs=None):
        self.comments, self.runs, self.jobs, self.calls = list(comments), runs or {}, jobs or {}, []

    def __call__(self, path, key=None):
        self.calls.append(path)
        if re.match(r"issues/\d+/comments", path):
            return self.comments
        m = re.match(r"actions/runs\?head_sha=([0-9a-f]{40})&event=pull_request", path)
        if m:
            return self.runs.get(m.group(1), [])
        m = re.match(r"actions/runs/(\d+)/jobs", path)
        if m:
            return self.jobs.get(int(m.group(1)), REAL)
        raise AssertionError(f"unexpected api path {path}")


def heads_events(*heads):
    return [label(h, 100 * (i + 1)) for i, h in enumerate(heads)]


class SharedRunDefinition(unittest.TestCase):
    def test_noop_needs_short_run_and_only_quick_executed_jobs(self):
        for verifier in ("success", "failure"):  # a gate-only run can end red
            jobs = [job("classify-change-scope", "success", 12), job("test", verifier, 10), job("lane", "skipped", 0)]
            self.assertTrue(car.is_noop(25, jobs), verifier)
        quick, slow = car.QUICK_JOB_S - 1, car.QUICK_JOB_S
        self.assertTrue(car.is_noop(25, [job("a", "success", quick)]))
        self.assertFalse(car.is_noop(25, [job("a", "success", slow)]))
        self.assertFalse(car.is_noop(25, [job("a", "success", 5), job("lane", "failure", slow)]))

    def test_wall_time_gates_the_jobs_lookup(self):
        self.assertTrue(car.needs_jobs_lookup(car.NOOP_WALL_S - 1))
        self.assertFalse(car.needs_jobs_lookup(car.NOOP_WALL_S))
        self.assertFalse(car.needs_jobs_lookup(None))
        self.assertFalse(car.is_noop(car.NOOP_WALL_S, GATE_ONLY))  # long wall: never a no-op, even with quick jobs

    def test_unknown_is_never_assumed_noop(self):
        self.assertFalse(car.is_noop(None, GATE_ONLY))
        self.assertFalse(car.is_noop(25, None))
        self.assertFalse(car.is_noop(25, []))
        self.assertFalse(car.is_noop(25, [job("a", "skipped", 0)]))                     # nothing executed, e.g. startup_failure
        self.assertFalse(car.is_noop(25, [{"name": "a", "conclusion": "success"}]))   # executed job with no timing

    def test_workflow_kind(self):
        self.assertEqual(car.workflow_kind({"path": ".github/workflows/ci.yml"}), "ci")
        self.assertEqual(car.workflow_kind({"path": ".github/workflows/e2e.yml@refs/pull/9/merge"}), "e2e")
        self.assertEqual(car.workflow_kind({"name": "E2E Smoke Tests"}), "e2e")
        self.assertIsNone(car.workflow_kind({"path": ".github/workflows/perf.yml", "name": "Perf Tests"}))
        self.assertIsNone(car.workflow_kind({}))


class Classification(unittest.TestCase):
    def test_exempt_and_pr_caused_values(self):
        for text in ("infra", "INFRA", "infra_setup_occ", "flaky", "FLAKY", "test-flake", "out-of-diff", "OUT_OF_DIFF",
                     "inherited-main", "INHERITED_MAIN", "e2e: infra transient"):
            self.assertTrue(pac.classification_is_exempt(text), text)
        for text in ("unknown", "unclassified-pending-repro", "IN_DIFF", "pr-introduced-test-failure", "code-regression",
                     "unit-test-fail", "wall-budget-cancel", "fixture", "", None, "not-flaky", "non-infra", "never infra",
                     "infra but pr-caused", "flaky candidate-owned", "not out-of-diff"):
            self.assertFalse(pac.classification_is_exempt(text), text)

    def test_matches_the_skill_vocabulary(self):
        """Every class the ci-failure-investigation skill defines is exempt except IN_DIFF (read from the skill, no copy)."""
        if not SKILL.exists():
            self.skipTest("skill not installed")
        line = next((l for l in SKILL.read_text(encoding="utf-8").splitlines() if "Classify each one:" in l), None)
        self.assertIsNotNone(line, "skill no longer has its classification sentence; update pm-admit-cap.py vocabulary")
        names = [m.group(1) for piece in line.split("Classify each one:", 1)[1].split(", ")
                 if (m := re.match(r"\s*(?:or )?([A-Z][A-Z_]+)", piece))]
        self.assertIn("IN_DIFF", names)
        self.assertGreaterEqual(len(names), 4)
        for name in names:
            self.assertEqual(pac.classification_is_exempt(name), name != "IN_DIFF", name)


class Records(unittest.TestCase):
    def test_admitted_heads_only_exact_labels_distinct_oldest_first(self):
        a, b = sha(0xa1), sha(0xb2)
        events = [label(b, 50), label(a, 10), label(a, 90), label(a, 20, event="unlabeled"),
                  label(a, 5, name="ci-head:"), label(a, 5, name="ci-head:abc"), label(a, 5, name="P2"),
                  label(a, 5, name=f"ci-head:{a.upper()}"), {"event": "closed", "created_at": iso(1)}]
        self.assertEqual(pac.admitted_heads(events), [(a, iso(10)), (b, iso(50))])

    def test_newest_verdict_wins_and_malformed_never_masks(self):
        h = sha(1)
        old, new = verdict(7, h, "unknown", at=10), verdict(7, h, "flaky", at=20)
        broken = {"id": 99, "created_at": iso(30), "body": f"<!-- ci-failure-investigation:run=7 attempt=1 head={h} -->\n<!-- ci-verdict: {{nope -->"}
        other_attempt = verdict(7, h, "infra", attempt=2, at=40)
        self.assertEqual(pac.verdict_classifications([new, old, broken, other_attempt]),
                         {(7, 1, h): "flaky", (7, 2, h): "infra"})

    def test_contradicting_or_unmarked_comments_are_ignored(self):
        h = sha(1)
        self.assertEqual(pac.verdict_classifications([verdict(7, h, "infra", run_id_in_json=8),
                                                      {"id": 1, "created_at": iso(1), "body": "PM 12b: PASS at abc"}]), {})


class Evaluate(unittest.TestCase):
    def two_heads(self, **kw):
        h1, h2 = sha(1), sha(2)
        return h1, h2, FakeApi(runs={h1: [run(10, h1, pr=5)], h2: [run(20, h2, "e2e.yml", pr=5)]}, **kw)

    def test_under_the_cap_makes_no_lookup(self):
        for events in ([], heads_events(sha(1))):
            api = FakeApi()
            out = pac.evaluate(5, events, api)
            self.assertEqual((out["status"], len(out["red_heads"]), api.calls), ("ok", 0, []))

    def test_two_pr_caused_red_heads_hit_the_cap(self):
        h1, h2, api = self.two_heads()
        out = pac.evaluate(5, heads_events(h1, h2), api)
        self.assertEqual(out["status"], "cap")
        self.assertEqual([(e["head"], e["runs"][0]["workflow"], e["runs"][0]["classification"]) for e in out["red_heads"]],
                         [(h1, "ci", "unrecorded"), (h2, "e2e", "unrecorded")])
        self.assertIn("ADMISSION_CAP_2", pac.render(out))

    def test_cap_is_a_parameter_not_a_literal(self):
        h1, h2, api = self.two_heads()
        self.assertEqual(pac.evaluate(5, heads_events(h1, h2), api, cap=3)["status"], "ok")
        self.assertEqual(pac.evaluate(5, heads_events(h1), api, cap=1)["status"], "cap")

    def test_exempt_classifications_do_not_count(self):
        for text in ("infra", "flaky", "out-of-diff", "inherited-main"):
            h1, h2, api = self.two_heads(comments=[verdict(10, sha(1), text)])
            out = pac.evaluate(5, heads_events(h1, h2), api)
            self.assertEqual((out["status"], len(out["red_heads"])), ("ok", 1), text)       # one exempt, one PR-caused
            h1, h2, api = self.two_heads(comments=[verdict(10, sha(1), text), verdict(20, sha(2), text)])
            out = pac.evaluate(5, heads_events(h1, h2), api)
            self.assertEqual((out["status"], len(out["red_heads"])), ("ok", 0), text)       # both exempt

    def test_pending_or_unknown_verdicts_still_count(self):
        h1, h2, api = self.two_heads(comments=[verdict(10, sha(1), "unclassified-pending-repro"), verdict(20, sha(2), "unknown")])
        self.assertEqual(pac.evaluate(5, heads_events(h1, h2), api)["status"], "cap")

    def test_a_verdict_for_another_attempt_or_head_does_not_exempt(self):
        h1, h2, api = self.two_heads(comments=[verdict(10, sha(1), "infra", attempt=2), verdict(20, sha(9), "infra")])
        self.assertEqual(pac.evaluate(5, heads_events(h1, h2), api)["status"], "cap")

    def test_noop_red_runs_never_count_but_a_real_red_on_the_same_head_does(self):
        h1, h2 = sha(1), sha(2)
        runs = {h1: [run(10, h1, wall=25, pr=5)], h2: [run(20, h2, wall=25), run(21, h2, wall=3000)]}
        api = FakeApi(runs=runs, jobs={10: GATE_ONLY, 20: GATE_ONLY})
        out = pac.evaluate(5, heads_events(h1, h2), api)
        self.assertEqual((out["status"], [e["head"] for e in out["red_heads"]]), ("ok", [h2]))
        self.assertNotIn("actions/runs/21/jobs?per_page=100", api.calls)  # long run: no jobs lookup needed

    def test_one_head_counts_once_even_with_several_red_runs(self):
        h1, h2 = sha(1), sha(2)
        api = FakeApi(runs={h1: [run(10, h1), run(11, h1, "e2e.yml"), run(12, h1, attempt=2)], h2: [run(20, h2, conclusion="success")]})
        out = pac.evaluate(5, heads_events(h1, h2), api)
        self.assertEqual((out["status"], len(out["red_heads"]), len(out["red_heads"][0]["runs"])), ("ok", 1, 3))

    def test_cancelled_counts_only_on_the_latest_admitted_head(self):
        h1, h2 = sha(1), sha(2)
        api = FakeApi(runs={h1: [run(10, h1, conclusion="cancelled")], h2: [run(20, h2, conclusion="cancelled")]})
        out = pac.evaluate(5, heads_events(h1, h2), api)
        self.assertEqual([e["head"] for e in out["red_heads"]], [h2])  # superseded head's cancel is a concurrency cancel

    def test_only_this_prs_ci_and_e2e_pull_request_runs_count(self):
        h1, h2 = sha(1), sha(2)
        noise = [run(1, h1, "perf.yml"), run(2, h1, event="push"), run(3, h1, pr=99), run(4, h1, conclusion="success"),
                 run(5, h1, conclusion="skipped"), run(6, h1, conclusion="neutral")]
        api = FakeApi(runs={h1: noise, h2: [run(20, h2)]})
        out = pac.evaluate(5, heads_events(h1, h2), api)
        self.assertEqual((out["status"], [e["head"] for e in out["red_heads"]]), ("ok", [h2]))

    def test_stops_looking_once_the_cap_is_reached(self):
        heads = [sha(i) for i in range(1, 6)]
        api = FakeApi(runs={h: [run(100 + i, h)] for i, h in enumerate(heads)})
        out = pac.evaluate(5, heads_events(*heads), api)
        self.assertEqual((out["status"], len(out["red_heads"])), ("cap", pac.CAP))
        self.assertEqual(sum(c.startswith("actions/runs?head_sha=") for c in api.calls), pac.CAP)


class Cli(unittest.TestCase):
    def call(self, events, api, *extra):
        def fake(path, key=None):
            return events if path.startswith("issues/") and "/events" in path else api(path, key)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pac.main(["--pr", "5", *extra], api=fake)
        return rc, out.getvalue()

    def test_exit_codes_and_lines(self):
        h1, h2 = sha(1), sha(2)
        api = FakeApi(runs={h1: [run(10, h1)], h2: [run(20, h2)]})
        rc, text = self.call(heads_events(h1), api)
        self.assertEqual((rc, text.startswith("ADMISSION_CAP: ok pr=5 ")), (0, True))
        rc, text = self.call(heads_events(h1, h2), api)
        self.assertEqual((rc, text.splitlines()[0].startswith("ADMISSION_CAP_2: PR #5 has 2 admitted heads")), (10, True))
        self.assertIn(f"head {h1[:10]} ci run 10 failure classification=unrecorded", text)
        self.assertEqual(self.call([], api, "--pr", "0")[0], 64)

    def test_unavailable_is_reported_never_guessed(self):
        def boom(path, key=None):
            raise pac.Unavailable("issues/5/events: timeout")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pac.main(["--pr", "5"], api=boom)
        self.assertEqual((rc, out.getvalue().strip()), (11, "ADMISSION_CAP: UNAVAILABLE reason=issues/5/events: timeout"))


class GhWrapper(unittest.TestCase):
    """make_api against a stub gh on PATH: pagination flatten, one retry, Unavailable with a reason."""

    def stub(self, body):
        d = tempfile.mkdtemp(prefix="pac-gh-")
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and os.rmdir(d))
        path = os.path.join(d, "gh")
        Path(path).write_text("#!/usr/bin/env bash\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return d

    def api(self, body):
        d = self.stub(body)
        patcher = mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ["PATH"], "PM_ADMIT_RETRY_SLEEP": "0", "STUB_DIR": d})
        patcher.start()
        self.addCleanup(patcher.stop)
        return pac.make_api("o/r"), d

    def test_flattens_pages_for_lists_and_keyed_objects(self):
        api, _ = self.api('case "$2" in *runs*) echo \'[{"workflow_runs":[{"id":1}]},{"workflow_runs":[{"id":2}]}]\';; *) echo \'[[{"a":1}],[{"a":2}]]\';; esac')
        self.assertEqual(api("issues/1/events"), [{"a": 1}, {"a": 2}])
        self.assertEqual([r["id"] for r in api("actions/runs?head_sha=x", key="workflow_runs")], [1, 2])

    def test_retries_once_then_succeeds(self):
        api, d = self.api('n=$(cat "$STUB_DIR/n" 2>/dev/null || echo 0); echo $((n+1)) > "$STUB_DIR/n"; [ "$n" = 0 ] && exit 1; echo \'[[]]\'')
        self.assertEqual(api("issues/1/events"), [])
        self.assertEqual(Path(d, "n").read_text().strip(), "2")

    def test_two_failures_raise_unavailable_with_the_reason(self):
        api, _ = self.api("exit 4")
        with self.assertRaises(pac.Unavailable) as ctx:
            api("issues/1/events?per_page=100")
        self.assertEqual(str(ctx.exception), "issues/1/events: gh_rc_4")

    def test_bad_json_is_unavailable(self):
        api, _ = self.api("echo not-json")
        with self.assertRaises(pac.Unavailable) as ctx:
            api("issues/1/events")
        self.assertIn("bad_json", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
