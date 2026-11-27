#!/usr/bin/env python3
"""S173 merge-only re-admit contract (Rajiv D0AMF0XE6TS ts 1795791089.117779).

A PR head produced ONLY by a rule-62 main integration is READY_TO_MERGE for the
S167 admin path when (a) an earlier head had exact-head CI success and executed
green E2E, (b) the delta is exactly a main merge (tree == merge-tree(parents)),
and (c) a trusted PR comment carries a green remote-box receipt for the full
current head. GitHub is stubbed; (b) runs real git in a throwaway temp repo.

CTO REVISE (C0ALZJHGE49 thread 1795798240.220189, ts 1795798363.328589): absent CI
never turns a red or pending current-head E2E into S167-ready, and a receipt with
e2e_specs=none needs the canonical main classifier's exact-head no-area result.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("merge_under_test_s173", os.path.join(HERE, "merge.py"))
merge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge)

NUMBER = 14999
REF = "fix/14999"


def git(repo, *args):
    return subprocess.run(("git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", *args),
                          check=True, text=True, capture_output=True).stdout.strip()


def write(repo, name, text):
    with open(os.path.join(repo, name), "w") as handle:
        handle.write(text)


class MergeOnlyReadmit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = self.repo = self.tmp.name
        git(repo, "init", "-q", "-b", "main")
        write(repo, "a.txt", "a\n")
        write(repo, "b.txt", "b\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "base")
        git(repo, "checkout", "-qb", REF)
        write(repo, "a.txt", "a changed by PR\n")
        git(repo, "commit", "-qam", "pr change")
        self.green = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "main")
        write(repo, "b.txt", "b changed on main\n")
        git(repo, "commit", "-qam", "main moves")
        self.main = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", REF)
        git(repo, "merge", "-q", "--no-ff", "-m", "Merge main", "main")
        self.head = git(repo, "rev-parse", "HEAD")
        # Same parents, but the merge commit also edits a PR-side file.
        git(repo, "checkout", "-q", self.green)
        git(repo, "merge", "-q", "--no-ff", "--no-commit", self.main)
        write(repo, "a.txt", "a changed by PR\nsneaky hunk\n")
        git(repo, "commit", "-qam", "Merge main plus hunk")
        self.hunk_head = git(repo, "rev-parse", "HEAD")
        self.green_heads = {self.green}
        self.comments = []
        self.current_e2e_refusal = None  # None: no E2E run at the current head
        self.selector = None  # canonical main classifier result for the current head

    def tearDown(self):
        self.tmp.cleanup()

    def receipt(self, head, author="MEMBER", **fields):
        values = {"overlap_tests": "green", "owning_tests": "green", "typecheck": "green",
                  "e2e_specs": "green", **fields}
        body = "Box proof\nS173_BOX_RECEIPT head=%s %s\n" % (
            head, " ".join(f"{k}={v}" for k, v in values.items()))
        self.comments.append({"id": len(self.comments) + 1, "author_association": author,
                              "html_url": "https://example.invalid/c", "body": body})

    def run_merge(self, head, apply=False):
        pr = {"number": NUMBER, "merged": False, "head": {"sha": head, "ref": REF}}

        def workflow_proof(pr_arg, workflow, names):
            sha = pr_arg["head"]["sha"]
            if workflow == "e2e.yml" and sha == head and self.current_e2e_refusal:
                raise merge.Refusal(self.current_e2e_refusal)
            if sha in self.green_heads:
                return {"head": sha, "run": 7, "conclusion": "success", "workflow": workflow}
            raise merge.Refusal(f"WORKFLOW_MISSING workflow={workflow}")

        def api(path):
            if path == "git/ref/heads/main":
                return {"object": {"sha": self.main}}
            if path.startswith("compare/"):
                return {"merge_base_commit": {"sha": self.main}, "html_url": "u", "files": []}
            raise AssertionError(path)

        def command(*args):
            assert args[:2] == ("gh", "api") and f"issues/{NUMBER}/comments" in args[-1], args
            return json.dumps([self.comments])

        real_git = merge.local_git

        def local_git(*args, check=True):
            if args and args[0] == "fetch":
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_git(*args, check=check)

        with mock.patch.object(merge, "LOCAL_APP_REPO", self.repo), \
                mock.patch.object(merge, "local_git", local_git), \
                mock.patch.object(merge, "pr_at", lambda number, sha: pr), \
                mock.patch.object(merge, "site_classifier_exemption", lambda p: None), \
                mock.patch.object(merge, "control_plane_classifier_exemption", lambda p: None), \
                mock.patch.object(merge, "workflow_proof", workflow_proof), \
                mock.patch.object(merge, "api", api), \
                mock.patch.object(merge, "command", command), \
                mock.patch.object(merge, "workflow_requirements", self.workflow_requirements):
            return merge.merge(NUMBER, head, apply=apply)

    def workflow_requirements(self, number, sha):
        if self.selector is None:
            raise AssertionError("classifier consulted without e2e_specs=none")
        return self.selector

    def refused(self, head, prefix):
        with self.assertRaises(merge.Refusal) as caught:
            self.run_merge(head)
        self.assertTrue(str(caught.exception).startswith(prefix), str(caught.exception))

    def test_accepts_main_merge_only_head_with_green_earlier_head_and_box_receipt(self):
        self.receipt(self.head)
        result = self.run_merge(self.head)
        self.assertEqual(result["status"], "READY_TO_MERGE")
        self.assertEqual(result["merge_path"], "S167_admin")
        proof = result["workflows"]["ci.yml"]
        self.assertEqual(proof["green_head"]["head"], self.green)
        self.assertEqual(proof["main_integrations"], [self.head])
        self.assertEqual(proof["box_receipt"]["head"], self.head)
        # This helper never merges it: the CTO runs the S167 head-pinned admin merge.
        with self.assertRaises(merge.Refusal) as caught:
            self.run_merge(self.head, apply=True)
        self.assertIn("S173_USE_S167_ADMIN_MERGE", str(caught.exception))

    def test_refuses_without_an_earlier_dual_green_head(self):
        self.receipt(self.head)
        self.green_heads = set()
        self.refused(self.head, "S173_NOT_MAIN_MERGE_ONLY")  # walks to the PR commit, which is no merge

    def test_refuses_merge_commit_carrying_a_pr_side_hunk(self):
        self.receipt(self.hunk_head)
        self.refused(self.hunk_head, "S173_NOT_MAIN_MERGE_ONLY")

    def test_refuses_wrong_sha_red_or_untrusted_receipt(self):
        self.receipt(self.green)  # bound to the earlier head, not the current one
        self.refused(self.head, "S173_BOX_RECEIPT_MISSING")
        self.receipt(self.head, author="NONE")
        self.refused(self.head, "S173_BOX_RECEIPT_MISSING")
        self.receipt(self.head, typecheck="red")
        self.refused(self.head, "S173_BOX_RECEIPT_NOT_GREEN")
        self.receipt(self.head, e2e_specs="failed")
        self.refused(self.head, "S173_BOX_RECEIPT_NOT_GREEN")

    def test_refuses_red_or_pending_current_head_e2e_even_with_green_receipt(self):
        self.receipt(self.head)
        # workflow_proof selects an in-progress run as substantive and refuses it NOT_GREEN, so
        # pending never reads as WORKFLOW_MISSING; verifier reds refuse VERIFIER_NOT_GREEN.
        for refusal in ("WORKFLOW_NOT_GREEN workflow=e2e.yml run=9",
                        "WORKFLOW_NOT_GREEN workflow=e2e.yml run=10",
                        "VERIFIER_NOT_GREEN workflow=e2e.yml run=11"):
            self.current_e2e_refusal = refusal
            self.refused(self.head, "S173_CURRENT_HEAD_E2E_NOT_GREEN")

    def test_refuses_e2e_specs_none_without_verified_selector_no_area(self):
        self.receipt(self.head, e2e_specs="none")
        for selector in ({"head": self.head, "ci_required": True, "e2e_required": True},
                         {"head": self.green, "ci_required": True, "e2e_required": False}):
            self.selector = selector
            self.refused(self.head, "S173_E2E_NONE_UNVERIFIED")

    def test_accepts_e2e_specs_none_with_exact_head_selector_no_area(self):
        self.receipt(self.head, e2e_specs="none")
        self.selector = {"head": self.head, "ci_required": True, "e2e_required": False}
        result = self.run_merge(self.head)
        self.assertEqual(result["status"], "READY_TO_MERGE")
        self.assertEqual(result["workflows"]["e2e.yml"]["box_receipt"]["e2e_selector"], self.selector)

    def test_other_ci_refusals_are_unchanged(self):
        self.receipt(self.head)

        def red(pr_arg, workflow, names):
            raise merge.Refusal("VERIFIER_NOT_GREEN workflow=ci.yml run=1")

        with mock.patch.object(merge, "workflow_proof", red), \
                mock.patch.object(merge, "pr_at", lambda n, s: {"number": NUMBER, "head": {"sha": s, "ref": REF}}), \
                mock.patch.object(merge, "site_classifier_exemption", lambda p: None), \
                mock.patch.object(merge, "control_plane_classifier_exemption", lambda p: None):
            with self.assertRaises(merge.Refusal) as caught:
                merge.merge(NUMBER, self.head)
        self.assertTrue(str(caught.exception).startswith("VERIFIER_NOT_GREEN"))


if __name__ == "__main__":
    unittest.main()
