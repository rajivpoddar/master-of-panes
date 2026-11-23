"""Hermetic log-read and false-exemption regressions; no GitHub/merge effects.

MERGE_UNDER_TEST can point to the retained preimage for the RED witness.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path.home() / ".codex/scripts"))
TARGET = Path(os.environ.get("MERGE_UNDER_TEST", Path(__file__).with_name("merge.py")))
HEAD, BASE, WORKFLOW, OTHER = (letter * 40 for letter in "abcf")
NUMBER, RUN_ID, JOB_ID = 8122, 4242, 17
RULES = b'{"control_plane_only":["docs/**"]}'
RULES_SHA = hashlib.sha256(RULES).hexdigest()
RECEIPT = {
    "schema_version": 1, "scope": "control_plane_only",
    "control_plane_only": True, "paid_ci_exempt": True,
    "product_changed": False, "ci_required": False, "e2e_required": False,
    "rules_sha256": RULES_SHA, "changed_files": ["docs/example.md"],
}


def framed(*lines):
    return "\r\n".join("2026-11-14T00:00:00.123456789Z \x1b[36;1m" + line
                       + "\x1b[0m" for line in lines)


def log(receipt=RECEIPT, head=HEAD, event="pull_request", number=NUMBER):
    return framed(f"  BASE_SHA: {BASE}", f"  HEAD_SHA: {head}",
                  f"  WORKFLOW_SHA: {WORKFLOW}", f"  EVENT_NAME: {event}",
                  f"  PR_NUMBER: {number}", json.dumps(receipt))


class JobLogTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("merge_job_logs_under_test", TARGET)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def world(self, raw=None, files=("docs/example.md",)):
        m = self.m
        raw = log() if raw is None else raw
        pr = {"number": NUMBER, "state": "open", "draft": False,
              "head": {"sha": HEAD, "ref": "fix/example", "repo": {
                  "full_name": "Scribie/heydonna-app", "id": 1}},
              "base": {"ref": "main", "repo": {"id": 1}}}
        run = {"id": RUN_ID, "run_attempt": 1, "head_sha": HEAD,
               "head_branch": "fix/example", "event": "pull_request",
               "path": ".github/workflows/ci-dummy.yml",
               "pull_requests": [{"number": NUMBER}],
               "status": "completed", "conclusion": "success"}
        scope = {"id": JOB_ID, "run_id": RUN_ID, "run_attempt": 1,
                 "head_sha": HEAD, "name": "detect-docs-only",
                 "status": "completed", "conclusion": "success"}
        calls = []

        def command(*args):
            calls.append(args)
            if args[-1].endswith(f"/actions/jobs/{JOB_ID}/logs"):
                # Reproduce gh 2.101.0's refusal on real Actions ANSI framing.
                if "--allow-escape-sequences" not in args:
                    raise m.Refusal("the response contains terminal escape sequences; "
                                    "pass --allow-escape-sequences to output it anyway")
                return raw
            if args[-1].endswith(f"/pulls/{NUMBER}/files?per_page=100"):
                return json.dumps([[{"filename": name} for name in files]])
            raise AssertionError(f"unexpected command (no external effects): {args}")

        def api(path):
            if path == f"git/commits/{WORKFLOW}":
                return {"parents": [{"sha": BASE}, {"sha": HEAD}]}
            if path == f"pulls/{NUMBER}":
                return pr
            raise AssertionError(f"unexpected API (no external effects): {path}")

        def pages(path, key):
            if path.startswith("actions/workflows/ci-dummy.yml/runs?"):
                return [run]
            if path.startswith(f"actions/runs/{RUN_ID}/jobs?"):
                return [scope]
            if path.startswith("actions/workflows/"):
                return []
            raise AssertionError(f"unexpected page (no external effects): {path}")

        m.command, m.api, m.pages = command, api, pages
        m.contents_identity = lambda path, ref: (
            "blob-" + path, RULES if path.endswith("change-scope-rules.json") else b"trusted")
        return pr, run, scope, calls

    def test_old_gh_raw_read_is_one_explicit_get_without_unsupported_flag(self):
        calls = []
        self.m.command = lambda *args: calls.append(args) or "\x1b[36mraw log\x1b[0m"
        self.assertEqual(self.m.job_log(JOB_ID), "\x1b[36mraw log\x1b[0m")
        self.assertEqual(calls, [("gh", "api", "--method", "GET",
                                 f"repos/{self.m.REPO}/actions/jobs/{JOB_ID}/logs")])

    def test_real_framing_reaches_bound_exemption_without_paid_workflow(self):
        pr, _, _, calls = self.world()
        proof = self.m.control_plane_classifier_exemption(pr)
        self.assertIsNotNone(proof)
        self.assertEqual((proof["head"], proof["run"], proof["rules_sha256"]),
                         (HEAD, RUN_ID, RULES_SHA))
        log_reads = [args for args in calls if args[-1].endswith("/logs")]
        self.assertEqual(log_reads, [
            ("gh", "api", "--method", "GET", f"repos/{self.m.REPO}/actions/jobs/{JOB_ID}/logs"),
            ("gh", "api", "--method", "GET", "--allow-escape-sequences",
             f"repos/{self.m.REPO}/actions/jobs/{JOB_ID}/logs"),
        ])
        self.assertFalse(any("merge" in args for args in calls))

    def test_unrelated_diagnostic_mentioning_escape_flag_has_no_retry(self):
        calls = []

        def command(*args):
            calls.append(args)
            raise self.m.Refusal("proxy error: --allow-escape-sequences was rejected")

        self.m.command = command
        with self.assertRaisesRegex(self.m.Refusal, "proxy error"):
            self.m.job_log(JOB_ID)
        self.assertEqual(len(calls), 1)

    def test_escape_refusal_fallback_failure_is_terminal(self):
        calls = []

        def command(*args):
            calls.append(args)
            if len(calls) == 1:
                raise self.m.Refusal("the response contains terminal escape sequences; "
                                     "pass --allow-escape-sequences to output it anyway")
            raise self.m.Refusal("gh: HTTP 403")

        self.m.command = command
        with self.assertRaisesRegex(self.m.Refusal, "HTTP 403"):
            self.m.job_log(JOB_ID)
        self.assertEqual(len(calls), 2)

    def test_transport_failure_never_parses_success_looking_stdout(self):
        with patch.object(self.m.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, stdout=log(), stderr="gh: HTTP 403")) as transport:
            with self.assertRaisesRegex(self.m.Refusal, "HTTP 403"):
                self.m.job_log(JOB_ID)
            self.assertEqual(transport.call_count, 1)

    def test_transport_timeout_has_no_retry(self):
        with patch.object(self.m.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                "gh", 120)) as transport:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.m.job_log(JOB_ID)
            self.assertEqual(transport.call_count, 1)

    def test_diagnostic_json_and_shell_echo_are_not_receipts(self):
        for raw in (framed('{"message":"API rate limit exceeded","status":"403"}'),
                    framed("gh: error: " + json.dumps(RECEIPT)),
                    framed("echo '" + json.dumps(RECEIPT) + "'"),
                    framed('{"unrelated":"json"}')):
            with self.subTest(raw=raw):
                pr, _, _, _ = self.world(raw)
                self.assertIsNone(self.m.control_plane_classifier_exemption(pr))

    def test_missing_or_malformed_receipt_cannot_exempt(self):
        for raw in (framed("no receipt"), framed('{"schema_version":1,')):
            pr, _, _, _ = self.world(raw)
            self.assertIsNone(self.m.control_plane_classifier_exemption(pr))

    def test_duplicate_and_conflicting_receipts_cannot_exempt(self):
        for second in (RECEIPT, dict(RECEIPT, e2e_required=True)):
            pr, _, _, _ = self.world(log() + "\n" + framed(json.dumps(second)))
            self.assertIsNone(self.m.control_plane_classifier_exemption(pr))

    def test_head_event_and_pr_bindings_still_refuse_drift(self):
        for kwargs in ({"head": OTHER}, {"event": "push"}, {"number": NUMBER + 1}):
            pr, _, _, _ = self.world(log(**kwargs))
            self.assertIsNone(self.m.control_plane_classifier_exemption(pr))

    def test_conflicting_head_bindings_still_refuse(self):
        pr, _, _, _ = self.world(log() + "\n" + framed(f"HEAD_SHA: {OTHER}"))
        self.assertIsNone(self.m.control_plane_classifier_exemption(pr))

    def test_native_workflow_parent_binding_still_refuses(self):
        pr, _, _, _ = self.world()
        self.m.api = lambda path: {"parents": [{"sha": BASE}, {"sha": OTHER}]}
        self.assertIsNone(self.m.control_plane_classifier_exemption(pr))

    def test_trusted_classifier_blob_drift_still_refuses(self):
        pr, _, _, _ = self.world()
        self.m.contents_identity = lambda path, ref: (
            "base" if ref == BASE else "drifted", RULES)
        self.assertIsNone(self.m.control_plane_classifier_exemption(pr))

    def test_rules_digest_and_exemption_fields_still_refuse(self):
        for changes in ({"rules_sha256": "f" * 64}, {"product_changed": True},
                        {"ci_required": True}, {"e2e_required": True},
                        {"paid_ci_exempt": False}, {"scope": "product"}):
            pr, _, _, _ = self.world(log(dict(RECEIPT, **changes)))
            self.assertIsNone(self.m.control_plane_classifier_exemption(pr))

    def test_product_path_cannot_borrow_a_docs_receipt(self):
        pr, _, _, _ = self.world(files=("docs/example.md", "app/page.tsx"))
        self.assertIsNone(self.m.control_plane_classifier_exemption(pr))

    def test_native_red_does_not_become_exempt(self):
        pr, run, _, calls = self.world()
        run["conclusion"] = "failure"
        self.assertIsNone(self.m.control_plane_classifier_exemption(pr))
        self.assertEqual(calls, [])

    def test_absent_producer_receipt_still_refuses_without_merge_effect(self):
        self.world(framed("classifier JSON was not emitted"))
        with self.assertRaisesRegex(self.m.Refusal, "WORKFLOW_MISSING"):
            self.m.merge(NUMBER, HEAD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
