#!/usr/bin/env python3
"""Decision tests for the ONE-paid-round-per-PR admission gate (pm-admit-paid-round.py). No network: GitHub reads are stubbed, git is a temp repo."""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CI_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CI_DIR))

SPEC = importlib.util.spec_from_file_location("ppr", CI_DIR / "pm-admit-paid-round.py")
ppr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ppr)

PR = 15174
A, B = "a" * 40, "b" * 40
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def job(name, conclusion, start, end, status="completed"):
    return {"name": name, "conclusion": conclusion, "status": status, "started_at": f"2026-11-27T10:{start // 60:02}:{start % 60:02}Z",
            "completed_at": f"2026-11-27T10:{end // 60:02}:{end % 60:02}Z" if end is not None else None}


PAID_JOBS = [job("classify-change-scope", "success", 0, 10), job("test", "failure", 10, 1500)]
GATE_ONLY_JOBS = [job("classify-change-scope", "success", 0, 12), job("test", "failure", 12, 20), job("lane", "skipped", 0, 0)]
RUN = {"workflow": "ci", "id": 7, "attempt": 1, "conclusion": "failure", "url": ""}


class Stubs:
    """Stub the GitHub reads: admitted heads, the substantive run per head, its jobs and the ci-verdict comments."""

    def __init__(self, admitted, jobs_by_head, verdict_tests=None):
        self.admitted, self.jobs_by_head, self.verdict_tests = admitted, jobs_by_head, verdict_tests or {}

    def __enter__(self):
        run_for = lambda repo, head: ([dict(RUN, id=int(head[0], 16) + 100)], []) if head in self.jobs_by_head else ([], [])
        jobs_of = lambda repo, rid: next(j for h, j in self.jobs_by_head.items() if int(h[0], 16) + 100 == rid)
        verdicts = lambda repo, pr, head: ([{"raw": {"failing_tests": self.verdict_tests[head]}}] if head in self.verdict_tests else [], 0)
        self.patches = [mock.patch.object(ppr.pav, "admitted_heads", lambda repo, pr: [(h, i) for i, h in enumerate(self.admitted, 1)]),
                        mock.patch.object(ppr.pav, "substantive_runs", run_for),
                        mock.patch.object(ppr, "jobs_of", jobs_of),
                        mock.patch.object(ppr.pav, "verdicts_for_head", verdicts)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


def sh(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV}).stdout.strip()


def commit(repo, files, msg):
    for rel, body in files.items():
        p = Path(repo, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", msg)
    return sh(repo, "rev-parse", "HEAD")


def fixture(tmp):
    """main M; PR branch P (src/widget.ts) = the round-1 head; returns (repo, M, P)."""
    repo = Path(tmp)
    sh(repo, "init", "-q", "-b", "main")
    m = commit(repo, {"README.md": "x", "src/widget.ts": "a0", "tests/widget.test.ts": "t0"}, "main")
    sh(repo, "update-ref", "refs/remotes/origin/main", m)
    sh(repo, "checkout", "-q", "-b", "pr")
    p = commit(repo, {"src/widget.ts": "a1"}, "pr round 1")
    return repo, m, p


class Decision(unittest.TestCase):
    def evaluate(self, head, repo_dir=None, env=None, dry=False):
        sel = lambda *a: ["tests/e2e/specs/qa-tests/x.spec.ts"]
        return ppr.evaluate("o/r", PR, head, repo_dir, env=env if env is not None else {}, dry=dry, selector=sel)

    def test_first_round_dispatches(self):
        with Stubs([], {}):
            rc, lines = self.evaluate(A)
        self.assertEqual(rc, 0)
        self.assertIn("no earlier paid round", lines[0])

    def test_same_head_readmission_is_not_a_second_round(self):
        with Stubs([A], {A: PAID_JOBS}):
            rc, _ = self.evaluate(A)
        self.assertEqual(rc, 0)

    def test_gate_only_earlier_head_is_not_a_paid_round(self):
        with Stubs([A], {A: GATE_ONLY_JOBS}):
            rc, _ = self.evaluate(B)
        self.assertEqual(rc, 0)

    def test_second_head_after_a_paid_round_requires_the_box(self):
        with Stubs([A], {A: PAID_JOBS}, {A: ["test/foo.test.ts > bar"]}):
            rc, lines = self.evaluate(B)
        self.assertEqual(rc, ppr.EXIT_BOX)
        self.assertEqual(lines[0], f"BOX_ROUND_REQUIRED pr={PR} head={B}")
        text = "\n".join(lines)
        for needle in ("test/foo.test.ts > bar", "failed_jobs: test", "typecheck: app tsc AND tests tsc", "MP4 paths", "remote-qa-runner",
                       "SELECT_ON_BOX", "n/a (main not merged"):
            self.assertIn(needle, text)

    def test_in_flight_job_counts_as_paid(self):
        running = [job("classify-change-scope", "success", 0, 10), job("test", None, 10, None, status="in_progress")]
        with Stubs([A], {A: running}):
            rc, _ = self.evaluate(B)
        self.assertEqual(rc, ppr.EXIT_BOX)

    def test_override_dispatches_and_is_logged(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ppr, "LOG", Path(d, "skip.log")), Stubs([A], {A: PAID_JOBS}):
            rc, lines = self.evaluate(B, env={ppr.OVERRIDE_ENV: f"cto:1795850000.000100@{PR}:{B}"})
            self.assertEqual(rc, 0)
            self.assertIn("OVERRIDE cto:1795850000.000100", lines[0])
            self.assertIn(f"override=cto:1795850000.000100@{PR}:{B}", Path(d, "skip.log").read_text())
            rc, _ = self.evaluate(B, env={ppr.OVERRIDE_ENV: f"rajiv:1795850000.000100@{PR}:{B}"})
            self.assertEqual(rc, 0)

    def test_override_for_another_pr_head_or_shape_is_ignored(self):
        with Stubs([A], {A: PAID_JOBS}):
            for bad in (f"cto:1795850000.000100@{PR}:{A}", f"cto:1795850000.000100@999:{B}", "cto:1795850000.000100", f"bot:1.1@{PR}:{B}", ""):
                with self.subTest(bad=bad):
                    rc, _ = self.evaluate(B, env={ppr.OVERRIDE_ENV: bad})
                    self.assertEqual(rc, ppr.EXIT_BOX)

    def test_cap_override_does_not_lift_the_gate(self):
        with Stubs([A], {A: PAID_JOBS}):
            rc, _ = self.evaluate(B, env={"PM_ADMIT_CAP_OVERRIDE": f"cto:1795850000.000100@{PR}:{B}"})
        self.assertEqual(rc, ppr.EXIT_BOX)

    def test_dry_run_override_writes_no_log(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ppr, "LOG", Path(d, "skip.log")), Stubs([A], {A: PAID_JOBS}):
            rc, lines = self.evaluate(B, env={ppr.OVERRIDE_ENV: f"cto:1.1@{PR}:{B}"}, dry=True)
            self.assertEqual(rc, 0)
            self.assertTrue(lines[0].startswith("[dry-run]"))
            self.assertFalse(Path(d, "skip.log").exists())


class GitDelta(unittest.TestCase):
    def run_gate(self, repo, prev, head, **kw):
        with Stubs([prev], {prev: PAID_JOBS}):
            return ppr.evaluate("o/r", PR, head, str(repo), env={}, selector=lambda *a: ["s.spec.ts"], **kw)

    def test_ci_input_delta_dispatches_a_fresh_round(self):
        for rel in (".github/workflows/ci.yml", "scripts/ci/change_scope.py"):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as logdir, \
                    mock.patch.object(ppr, "LOG", Path(logdir, "skip.log")):
                repo, _, p = fixture(tmp)
                head = commit(repo, {rel: "changed"}, "touch ci input")
                rc, lines = self.run_gate(repo, p, head)
                self.assertEqual(rc, 0)
                self.assertIn("CI-input delta", lines[0])
                self.assertIn("ci_input_delta", Path(logdir, "skip.log").read_text())

    def test_ci_test_files_are_not_ci_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, p = fixture(tmp)
            head = commit(repo, {"scripts/ci/tests/test_x.py": "t", "src/widget.ts": "a2"}, "test-only ci delta")
            rc, lines = self.run_gate(repo, p, head)
        self.assertEqual(rc, ppr.EXIT_BOX)
        self.assertIn("specs: s.spec.ts", "\n".join(lines))

    def test_main_merge_ci_input_changes_do_not_count_and_overlap_tests_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, m, p = fixture(tmp)
            sh(repo, "checkout", "-q", "main")
            m2 = commit(repo, {"scripts/ci/new_guard.py": "g", "src/widget.ts": "main-edit"}, "main moves: CI input + overlap with the PR file")
            sh(repo, "update-ref", "refs/remotes/origin/main", m2)
            sh(repo, "checkout", "-q", "pr")
            subprocess.run(["git", "-C", str(repo), "merge", "-q", "--no-ff", "-X", "ours", "-m", "merge main", "main"], check=True,
                           capture_output=True, env={**os.environ, **GIT_ENV})
            head = sh(repo, "rev-parse", "HEAD")
            rc, lines = self.run_gate(repo, p, head)
        self.assertEqual(rc, ppr.EXIT_BOX)
        text = "\n".join(lines)
        self.assertIn("rule62_overlap_nearest_owner_tests: tests/widget.test.ts", text)

    def test_unreadable_git_is_unavailable_not_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, p = fixture(tmp)
            with self.assertRaises(ppr.Unavailable):
                self.run_gate(repo, p, "c" * 40)


class Cli(unittest.TestCase):
    def main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ppr.main(argv)
        return rc, out.getvalue()

    def test_usage_errors(self):
        self.assertEqual(self.main(["gate", "--pr", "5", "--head", "short"])[0], 64)
        self.assertEqual(self.main(["gate", "--pr", "0", "--head", A])[0], 64)
        self.assertEqual(self.main(["bogus"])[0], 64)

    def test_exit_codes_and_unavailable(self):
        with Stubs([A], {A: PAID_JOBS}), mock.patch.dict(os.environ, {ppr.OVERRIDE_ENV: ""}):
            with mock.patch.object(ppr, "select_e2e_specs", lambda *a: None):
                rc, out = self.main(["gate", "--repo", "o/r", "--pr", str(PR), "--head", B])
        self.assertEqual(rc, 21)
        self.assertTrue(out.startswith(f"BOX_ROUND_REQUIRED pr={PR} head={B}"))
        with mock.patch.object(ppr.pav, "admitted_heads", side_effect=ppr.Unavailable("gh_rc_4")):
            rc, out = self.main(["gate", "--repo", "o/r", "--pr", str(PR), "--head", B])
        self.assertEqual((rc, out.strip()), (11, "paid_round: UNAVAILABLE reason=gh_rc_4"))


if __name__ == "__main__":
    unittest.main()
