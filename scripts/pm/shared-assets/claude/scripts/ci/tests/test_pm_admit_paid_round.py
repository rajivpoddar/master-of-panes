#!/usr/bin/env python3
"""Decision tests for the ONE-paid-round-per-PR admission gate (pm-admit-paid-round.py). No network: GitHub reads are stubbed, git is a temp repo."""
import contextlib
import importlib.util
import io
import json
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


def job(name, conclusion, status="completed", started=True, steps=None):
    j = {"name": name, "conclusion": conclusion, "status": status, "started_at": "2026-11-27T10:00:00Z" if started else None}
    if steps is not None:
        j["steps"] = steps
    return j


# A lane job that FAILS in two seconds still executed: there is no elapsed-time proxy.
PAID_JOBS = [job("classify-change-scope", "success"), job("unit-shard-1", "failure"), job("test", "failure")]
GATE_ONLY_JOBS = [job("classify-change-scope", "success"), job("test", "failure"), job("unit-shard-1", "skipped", started=False)]
QUEUED_JOBS = [job("classify-change-scope", "success"), job("unit-shard-1", None, status="queued", started=False)]
RUNNING_JOBS = [job("classify-change-scope", "success"), job("unit-shard-1", None, status="in_progress")]
WRAPPER_STEPS = [{"name": "Require exemption or exact-head real CI/E2E success", "conclusion": "success"}]
WRAPPER_JOBS = [job("gate", "success", steps=WRAPPER_STEPS)]


def run(rid, conclusion="failure", status="completed", ts="10:00"):
    return {"id": rid, "status": status, "conclusion": conclusion, "event": "pull_request", "name": "CI",
            "path": ".github/workflows/ci.yml", "run_attempt": 1, "run_started_at": f"2026-11-27T{ts}:00Z"}


class Stubs:
    """Stub the GitHub reads: admitted heads, EVERY run per head with its jobs, and the ci-verdict comments.

    runs_by_head: head -> list of (run, jobs).  A head in `bad_jobs` raises Unavailable on its jobs read."""

    def __init__(self, admitted, runs_by_head, verdict_tests=None, bad_jobs=()):
        self.admitted, self.runs_by_head, self.verdict_tests, self.bad_jobs = admitted, runs_by_head, verdict_tests or {}, set(bad_jobs)

    def __enter__(self):
        runs_of = lambda repo, head: [r for r, _ in self.runs_by_head.get(head, [])]

        def jobs_of(repo, rid):
            for head, items in self.runs_by_head.items():
                for r, jobs in items:
                    if r["id"] == rid:
                        if head in self.bad_jobs:
                            raise ppr.Unavailable(f"jobs {rid}: gh_rc_1")
                        return jobs
            raise AssertionError(rid)

        verdicts = lambda repo, pr, head: ([{"raw": {"failing_tests": self.verdict_tests[head]}}] if head in self.verdict_tests else [], 0)
        self.patches = [mock.patch.object(ppr.pav, "admitted_heads", lambda repo, pr: [(h, i) for i, h in enumerate(self.admitted, 1)]),
                        mock.patch.object(ppr, "runs_of", runs_of),
                        mock.patch.object(ppr, "jobs_of", jobs_of),
                        mock.patch.object(ppr.pav, "verdicts_for_head", verdicts)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


def one(jobs, rid=7, **kw):
    return [(run(rid, **kw), jobs)]


SELECTED = lambda *a: ppr._selection("selected", "selected", "area", ["tests/e2e/specs/qa-tests/x.spec.ts"])


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
    def evaluate(self, head, repo_dir=None, env=None, dry=False, selector=SELECTED):
        return ppr.evaluate("o/r", PR, head, repo_dir, env=env if env is not None else {}, dry=dry, selector=selector)

    def test_first_round_dispatches(self):
        with Stubs([], {}):
            rc, lines = self.evaluate(A)
        self.assertEqual(rc, 0)
        self.assertIn("no earlier paid", lines[0])
        self.assertEqual(lines[-1], "paid_round_same_head=none")

    def test_same_head_rearm_after_a_completed_paid_pair_is_blocked(self):
        """pm-admit-ci.sh could re-arm an already completed head: that would be a second paid pair."""
        with Stubs([A], {A: one(PAID_JOBS)}):
            rc, lines = self.evaluate(A)
        self.assertEqual(rc, ppr.EXIT_BOX)
        self.assertEqual(lines[0], f"BOX_ROUND_REQUIRED pr={PR} head={A}")
        self.assertEqual(lines[-1], "paid_round_same_head=completed_paid")

    def test_same_head_activation_still_in_flight_keeps_already_admitted(self):
        for jobs in (RUNNING_JOBS, QUEUED_JOBS):
            with self.subTest(jobs=jobs[-1]["status"]), Stubs([A], {A: one(jobs, conclusion=None, status="in_progress")}):
                rc, lines = self.evaluate(A)
            self.assertEqual(rc, 0)
            self.assertEqual(lines[-1], "paid_round_same_head=in_flight")

    def test_admitted_same_head_with_no_visible_run_is_unresolved_not_free(self):
        with Stubs([A], {}):
            rc, lines = self.evaluate(A)
        self.assertEqual(rc, ppr.EXIT_BOX)
        self.assertEqual(lines[0], f"PAID_ROUND_UNRESOLVED pr={PR} head={A}")
        self.assertEqual(lines[-1], "paid_round_same_head=unresolved")

    def test_gate_only_earlier_head_is_not_a_paid_round(self):
        with Stubs([A], {A: one(GATE_ONLY_JOBS, conclusion="success")}):
            rc, _ = self.evaluate(B)
        self.assertEqual(rc, 0)

    def test_second_head_after_a_paid_round_requires_the_box(self):
        with Stubs([A], {A: one(PAID_JOBS)}, {A: ["test/foo.test.ts > bar"]}):
            rc, lines = self.evaluate(B)
        self.assertEqual(rc, ppr.EXIT_BOX)
        self.assertEqual(lines[0], f"BOX_ROUND_REQUIRED pr={PR} head={B}")
        text = "\n".join(lines)
        for needle in ("test/foo.test.ts > bar", "failed_jobs: unit-shard-1; test", "typecheck: app tsc AND tests tsc", "MP4", "remote-qa-runner",
                       "specs (1): tests/e2e/specs/qa-tests/x.spec.ts", "n/a (main not merged"):
            self.assertIn(needle, text)

    def test_prior_queued_head_blocks_a_new_paid_write(self):
        """An admitted head whose jobs are still queued (or whose run is not visible yet) is not free."""
        with Stubs([A], {A: one(QUEUED_JOBS, conclusion=None, status="queued")}):
            rc, lines = self.evaluate(B)
        self.assertEqual(rc, ppr.EXIT_BOX)
        self.assertEqual(lines[0], f"PAID_ROUND_UNRESOLVED pr={PR} head={B}")
        self.assertIn(f"{A[:12]}[queued", "\n".join(lines))
        with Stubs([A], {}):
            rc, lines = self.evaluate(B)
        self.assertEqual(rc, ppr.EXIT_BOX)
        self.assertIn(f"{A[:12]}[unresolved", "\n".join(lines))

    def test_older_executed_attempt_is_not_hidden_by_a_later_noop_or_wrapper(self):
        later_noop = run(9, conclusion="success", ts="11:00")
        later_wrapper = run(10, conclusion="success", ts="12:00")
        runs = [(later_wrapper, WRAPPER_JOBS), (later_noop, GATE_ONLY_JOBS), (run(7, ts="10:00"), PAID_JOBS)]
        with Stubs([A], {A: runs}):
            rc, lines = self.evaluate(B)
        self.assertEqual(rc, ppr.EXIT_BOX)
        self.assertIn(f"{A[:12]}[executed", "\n".join(lines))

    def test_a_two_second_failing_lane_job_still_counts_as_executed(self):
        with Stubs([A], {A: one([job("classify-change-scope", "success"), job("e2e-shard-1", "failure")])}):
            rc, _ = self.evaluate(B)
        self.assertEqual(rc, ppr.EXIT_BOX)

    def test_unreadable_jobs_are_unavailable_not_free(self):
        with Stubs([A], {A: one(PAID_JOBS)}, bad_jobs=[A]):
            with self.assertRaises(ppr.Unavailable):
                self.evaluate(B)

    def test_override_dispatches_and_is_logged(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ppr, "LOG", Path(d, "skip.log")), Stubs([A], {A: one(PAID_JOBS)}):
            rc, lines = self.evaluate(B, env={ppr.OVERRIDE_ENV: f"cto:1795850000.000100@{PR}:{B}"})
            self.assertEqual(rc, 0)
            self.assertIn("OVERRIDE cto:1795850000.000100", lines[0])
            self.assertIn(f"override=cto:1795850000.000100@{PR}:{B}", Path(d, "skip.log").read_text())
            rc, _ = self.evaluate(B, env={ppr.OVERRIDE_ENV: f"rajiv:1795850000.000100@{PR}:{B}"})
            self.assertEqual(rc, 0)

    def test_override_lifts_a_same_head_rearm_block_too(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ppr, "LOG", Path(d, "skip.log")), Stubs([A], {A: one(PAID_JOBS)}):
            rc, lines = self.evaluate(A, env={ppr.OVERRIDE_ENV: f"cto:1795850000.000100@{PR}:{A}"})
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-1], "paid_round_same_head=completed_paid")

    def test_override_for_another_pr_head_or_shape_is_ignored(self):
        with Stubs([A], {A: one(PAID_JOBS)}):
            for bad in (f"cto:1795850000.000100@{PR}:{A}", f"cto:1795850000.000100@999:{B}", "cto:1795850000.000100", f"bot:1.1@{PR}:{B}", ""):
                with self.subTest(bad=bad):
                    rc, _ = self.evaluate(B, env={ppr.OVERRIDE_ENV: bad})
                    self.assertEqual(rc, ppr.EXIT_BOX)

    def test_cap_override_does_not_lift_the_gate(self):
        with Stubs([A], {A: one(PAID_JOBS)}):
            rc, _ = self.evaluate(B, env={"PM_ADMIT_CAP_OVERRIDE": f"cto:1795850000.000100@{PR}:{B}"})
        self.assertEqual(rc, ppr.EXIT_BOX)

    def test_dry_run_override_writes_no_log(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ppr, "LOG", Path(d, "skip.log")), Stubs([A], {A: one(PAID_JOBS)}):
            rc, lines = self.evaluate(B, env={ppr.OVERRIDE_ENV: f"cto:1.1@{PR}:{B}"}, dry=True)
            self.assertEqual(rc, 0)
            self.assertTrue(lines[0].startswith("[dry-run]"))
            self.assertFalse(Path(d, "skip.log").exists())


class Selector(unittest.TestCase):
    """A selector failure is UNRESOLVED coverage, never 'none selected'; only mode=none reason=no_area is a verified none."""

    def packet(self, sel):
        with Stubs([A], {A: one(PAID_JOBS)}):
            rc, lines = ppr.evaluate("o/r", PR, B, None, env={}, selector=lambda *a: sel)
        self.assertEqual(rc, ppr.EXIT_BOX)
        return "\n".join(lines)

    def test_failure_with_empty_specs_is_unresolved(self):
        text = self.packet(ppr._selection("unresolved", "failed", "selector_failed", [], "rc=1"))
        self.assertIn("specs: UNRESOLVED", text)
        self.assertIn("SELECT_ON_BOX", text)
        self.assertIn("mode=failed reason=selector_failed", text)
        self.assertNotIn("specs: none", text)

    def test_verified_no_area_is_a_verified_none(self):
        text = self.packet(ppr._selection("verified_none", "none", "no_area"))
        self.assertIn("specs: none (verified: mode=none reason=no_area)", text)
        self.assertNotIn("UNRESOLVED", text)

    def test_other_none_reason_is_unresolved(self):
        with mock.patch.object(ppr.subprocess, "run") as run_, mock.patch.object(ppr.Path, "exists", return_value=True):
            def fake(cmd, **kw):
                out = {"clone": "", "checkout": "", "rev-parse": B + "\n", "status": ""}
                key = next((k for k in ("clone", "checkout", "rev-parse", "status") if k in cmd), None)
                if "show" in cmd:  # trusted classifier files, read from the pinned origin/main commit (bytes, like the real capture)
                    return subprocess.CompletedProcess(cmd, 0, b"{}", b"")
                if "--select-e2e-specs" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({"mode": "none", "reason": "unmapped_path", "specs": []}), "")
                return subprocess.CompletedProcess(cmd, 0, out.get(key, ""), "")
            run_.side_effect = fake
            sel = ppr.select_e2e_specs("/repo", A, B, PR)
        self.assertEqual(sel["status"], "unresolved")
        self.assertEqual((sel["mode"], sel["reason"]), ("none", "unmapped_path"))

    def test_missing_repo_dir_or_unverified_checkout_is_unresolved(self):
        self.assertEqual(ppr.select_e2e_specs(None, A, B)["status"], "unresolved")
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, p = fixture(tmp)
            sel = ppr.select_e2e_specs(str(repo), p, "d" * 40)  # head not in the repo: checkout fails
        self.assertEqual((sel["status"], sel["reason"]), ("unresolved", "checkout_failed"))

    def test_selected_specs_list_the_videos_for_slack(self):
        text = self.packet(ppr._selection("selected", "selected", "area", ["a.spec.ts", "b.spec.ts"]))
        self.assertIn("specs (2): a.spec.ts; b.spec.ts", text)
        self.assertIn("a.spec.ts -> <mp4 path or artifact link>", text)


class GitDelta(unittest.TestCase):
    def run_gate(self, repo, prev, head, **kw):
        with Stubs([prev], {prev: one(PAID_JOBS)}):
            return ppr.evaluate("o/r", PR, head, str(repo), env={}, selector=lambda *a: ppr._selection("selected", "selected", "area", ["s.spec.ts"]), **kw)

    def test_ci_input_delta_gets_no_automatic_second_paid_pair(self):
        for rel in (".github/workflows/ci.yml", "scripts/ci/change_scope.py"):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as tmp:
                repo, _, p = fixture(tmp)
                head = commit(repo, {rel: "changed"}, "touch ci input")
                rc, lines = self.run_gate(repo, p, head)
                self.assertEqual(rc, ppr.EXIT_BOX)
                self.assertIn("ci_input_delta:", "\n".join(lines))
                self.assertIn("pinned authority", "\n".join(lines))

    def test_ci_input_delta_needs_the_pinned_override(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as logdir, mock.patch.object(ppr, "LOG", Path(logdir, "skip.log")):
            repo, _, p = fixture(tmp)
            head = commit(repo, {".github/workflows/ci.yml": "changed"}, "touch ci input")
            with Stubs([p], {p: one(PAID_JOBS)}):
                rc, lines = ppr.evaluate("o/r", PR, head, str(repo), env={ppr.OVERRIDE_ENV: f"cto:1795850000.000100@{PR}:{head}"}, selector=SELECTED)
            self.assertEqual(rc, 0)
            self.assertIn("OVERRIDE cto:1795850000.000100", lines[0])

    def test_ci_test_files_are_not_ci_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, p = fixture(tmp)
            head = commit(repo, {"scripts/ci/tests/test_x.py": "t", "src/widget.ts": "a2"}, "test-only ci delta")
            rc, lines = self.run_gate(repo, p, head)
        self.assertEqual(rc, ppr.EXIT_BOX)
        text = "\n".join(lines)
        self.assertIn("specs (1): s.spec.ts", text)
        self.assertNotIn("ci_input_delta: scripts/ci/tests", text)

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
        self.assertNotIn("ci_input_delta: scripts/ci/new_guard.py", text)

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
        with Stubs([A], {A: one(PAID_JOBS)}), mock.patch.dict(os.environ, {ppr.OVERRIDE_ENV: ""}):
            rc, out = self.main(["gate", "--repo", "o/r", "--pr", str(PR), "--head", B])
        self.assertEqual(rc, 21)
        self.assertTrue(out.startswith(f"BOX_ROUND_REQUIRED pr={PR} head={B}"))
        self.assertIn("specs: UNRESOLVED", out)  # no --repo-dir: coverage unresolved, never "none selected"
        self.assertTrue(out.rstrip().endswith("paid_round_same_head=none"))
        with mock.patch.object(ppr.pav, "admitted_heads", side_effect=ppr.Unavailable("gh_rc_4")):
            rc, out = self.main(["gate", "--repo", "o/r", "--pr", str(PR), "--head", B])
        self.assertEqual((rc, out.strip()), (11, "paid_round: UNAVAILABLE reason=gh_rc_4"))
        with Stubs([A], {A: one(PAID_JOBS)}, bad_jobs=[A]):
            rc, out = self.main(["gate", "--repo", "o/r", "--pr", str(PR), "--head", B])
        self.assertEqual(rc, 11)


class TrustedClassifier(unittest.TestCase):
    """CTO REVISE on MoP 6a6f9f1e (fix 2): PR-authored classifier code and rules must never execute on the PM host.
    The selector runs the origin/main copy (git show into a temp dir, isolated, scrubbed env) against the head checkout as data only."""

    TRUSTED = (
        "import json, os, sys\n"
        "root = sys.argv[sys.argv.index('--repo-root') + 1]\n"
        "print(json.dumps({'mode': 'selected', 'reason': 'area', 'specs': [\n"
        "    'ran_from=trusted-copy',\n"
        "    'secret=' + ('LEAK' if os.environ.get('MOP_TEST_SECRET') else 'scrubbed'),\n"
        "    'rules_beside_script=' + str(os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'change-scope-rules.json'))),\n"
        "    'root_is_checkout_with_pr_file=' + str(os.path.exists(os.path.join(root, 'src/widget.ts'))),\n"
        "]}))\n"
    )

    def repo_with_malicious_pr(self, tmp, marker):
        repo = Path(tmp, "repo")
        repo.mkdir()
        sh(repo, "init", "-q", "-b", "main")
        files = {"README.md": "x", "src/widget.ts": "a0", "scripts/ci/change_scope.py": self.TRUSTED,
                 "scripts/ci/e2e-functional-areas.json": '{"trusted": true}', "scripts/ci/change-scope-rules.json": '{"trusted": true}'}
        m = commit(repo, files, "main")
        sh(repo, "update-ref", "refs/remotes/origin/main", m)
        sh(repo, "checkout", "-q", "-b", "pr")
        evil = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('executed from the PR checkout')\nprint('{{\"mode\": \"none\", \"reason\": \"no_area\", \"specs\": []}}')\n"
        p = commit(repo, {"src/widget.ts": "a1", "scripts/ci/change_scope.py": evil, "scripts/ci/change-scope-rules.json": '{"trusted": false}'}, "pr: evil classifier + rules")
        return repo, m, p

    def test_pr_authored_classifier_never_runs_and_trusted_main_copy_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp, "pr-code-ran")
            repo, m, p = self.repo_with_malicious_pr(tmp, marker)
            with mock.patch.dict(os.environ, {"MOP_TEST_SECRET": "s3cret"}):
                sel = ppr.select_e2e_specs(str(repo), m, p)
            self.assertFalse(marker.exists(), "the PR checkout's change_scope.py executed on the host")
        self.assertEqual(sel["status"], "selected")
        self.assertEqual(sel["specs"], ["ran_from=trusted-copy", "secret=scrubbed", "rules_beside_script=True", "root_is_checkout_with_pr_file=True"])

    def test_selection_reports_trusted_main_sha_and_classifier_hashes(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            repo, m, p = self.repo_with_malicious_pr(tmp, Path(tmp, "never"))
            sel = ppr.select_e2e_specs(str(repo), m, p)
        tr = sel["trusted"]
        self.assertEqual(tr["main_sha"], m)
        self.assertEqual(tr["classifier"], "origin/main:scripts/ci/change_scope.py")
        self.assertEqual(tr["sha256"]["change_scope.py"], hashlib.sha256(self.TRUSTED.encode()).hexdigest())
        self.assertEqual(tr["sha256"]["change-scope-rules.json"], hashlib.sha256(b'{"trusted": true}').hexdigest())  # main's rules, not the PR's
        text = "\n".join(ppr.e2e_lines(sel, m, p))
        self.assertIn(f"classifier: trusted origin/main:scripts/ci/change_scope.py @ {m[:12]}", text)
        self.assertIn("change_scope.py=" + tr["sha256"]["change_scope.py"][:12], text)

    def test_unreadable_trusted_main_is_unresolved_never_falls_back_to_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp, "pr-code-ran")
            repo, m, p = self.repo_with_malicious_pr(tmp, marker)
            sh(repo, "update-ref", "-d", "refs/remotes/origin/main")
            sel = ppr.select_e2e_specs(str(repo), m, p)
            self.assertFalse(marker.exists())
        self.assertEqual((sel["status"], sel["reason"]), ("unresolved", "trusted_classifier_unavailable"))
        self.assertIn("SELECT_ON_BOX", "\n".join(ppr.e2e_lines(sel, m, p)))

    def test_gh_token_is_forwarded_only_when_already_set_and_nothing_else_inherits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"GH_TOKEN": "t0ken", "AWS_SECRET_ACCESS_KEY": "x", "PYTHONPATH": "/evil"}, clear=False):
                env = ppr._trusted_env(tmp)
            self.assertEqual(env["GH_TOKEN"], "t0ken")
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
            self.assertNotIn("PYTHONPATH", env)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GH_TOKEN", None)
                self.assertNotIn("GH_TOKEN", ppr._trusted_env(tmp))


if __name__ == "__main__":
    unittest.main()
