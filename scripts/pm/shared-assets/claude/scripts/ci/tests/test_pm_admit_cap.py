#!/usr/bin/env python3
"""Tests for the admission round breaker and the reporting run classifier."""
import contextlib
import importlib.util
import io
import json
import os
import shlex
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
SPEC.loader.exec_module(pac)


def label(event_id, name, event="labeled"):
    return {"id": event_id, "event": event, "label": {"name": name}}


def job(name, conclusion, seconds):
    started = "2026-11-20T10:00:00Z"
    completed = f"2026-11-20T10:{seconds // 60:02}:{seconds % 60:02}Z"
    return {"name": name, "conclusion": conclusion, "started_at": started, "completed_at": completed}


GATE_ONLY = [job("classify-change-scope", "success", 12), job("test", "failure", 10), job("typescript", "skipped", 0)]


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
        self.assertIsNone(car.workflow_kind({"path": ".github/workflows/perf.yml", "name": "CI"}))
        self.assertIsNone(car.workflow_kind({}))


class Breaker(unittest.TestCase):
    def cli(self, body):
        with tempfile.TemporaryDirectory() as d:
            gh = Path(d, "gh")
            gh.write_text('#!/usr/bin/env bash\n[[ "$*" = "api repos/o/r/issues/5/events?per_page=100 --paginate --slurp" ]] || exit 99\n' + body)
            gh.chmod(0o700)
            env = {"PATH": d + os.pathsep + os.environ["PATH"], "PM_ADMIT_RETRY_SLEEP": "0"}
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
                rc = pac.main(["--repo", "o/r", "--pr", "5"])
            return rc, out.getvalue().strip()

    def pages(self, pages):
        return self.cli(f"printf '%s\\n' {shlex.quote(json.dumps(pages))}")

    def test_first_and_second_admission_are_allowed_then_breaker_refuses(self):
        name = f"ci-head:{'a' * 40}"
        one, two = label(101, name), label(102, name)
        for pages, count, rc, status in (([[]], 0, 0, "ok"), ([[one]], 1, 0, "ok"),
                                         ([[one], [one, two]], 2, 10, "cap_reached")):
            with self.subTest(count=count):
                self.assertEqual(self.pages(pages), (rc, f"ADMISSION_ROUND_BREAKER: {status} pr=5 rounds={count} cap=2"))

    def test_only_exact_label_additions_count(self):
        name = f"ci-head:{'a' * 40}"
        events = [label(1, name), label(2, name, "unlabeled"), label(3, "P2"), label(4, "ci-head:"),
                  label(5, name + "\n"), label(6, f"ci-head:{'A' * 40}")]
        self.assertEqual(self.pages([events]), (0, "ADMISSION_ROUND_BREAKER: ok pr=5 rounds=1 cap=2"))

    def test_unreadable_history_refuses_instead_of_reporting_zero(self):
        for pages, reason in (([], "bad_response_shape"), ([{}], "bad_response_shape"), ([[None]], "invalid_event"),
                              ([[{"event": "labeled"}]], "invalid_label_event"),
                              ([[label(None, f"ci-head:{'a' * 40}")]], "invalid_admission_event_id")):
            with self.subTest(pages=pages):
                self.assertEqual(self.pages(pages), (11, f"ADMISSION_ROUND_BREAKER: UNAVAILABLE reason=issues/5/events: {reason}"))
        for body, reason in (("printf ''", "bad_json"), ("exit 4", "gh_rc_4")):
            self.assertEqual(self.cli(body), (11, f"ADMISSION_ROUND_BREAKER: UNAVAILABLE reason=issues/5/events: {reason}"))

    def test_retries_once_then_succeeds(self):
        self.assertEqual(self.cli('stamp="$(dirname "$0")/seen"; [[ -e "$stamp" ]] || { : > "$stamp"; exit 1; }; printf \'[[]]\\n\''),
                         (0, "ADMISSION_ROUND_BREAKER: ok pr=5 rounds=0 cap=2"))


if __name__ == "__main__":
    unittest.main()
