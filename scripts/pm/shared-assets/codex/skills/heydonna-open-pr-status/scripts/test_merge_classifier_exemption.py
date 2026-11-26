#!/usr/bin/env python3
"""Focused contract tests for the classifier-exemption merge path.

Hermetic: merge.api / merge.pages / merge.command are stubbed. No network,
GitHub, subprocess or file effect.
"""

import base64
import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("merge_under_test", os.path.join(HERE, "merge.py"))
merge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge)

HEAD = "8a51d7e51ab2fe9c872af2200a401fb95553422b"
BASE = "1d917444ec054d04a2f1188133e4981b1784694f"
WORKFLOW_SHA = "7040efea9e74dd7a8d5c903e4ec4a23ba9270bf4"
NUMBER = 8101
REF = "fix/8101"
RUN_ID = 111
SCOPE_ID = 999
WORKFLOW = "ci.yml"
NAMES = ("typescript", "python", "test")
RULES = (b'{"schema_version":1,"control_plane_only":[".agents/**",".claude/**",'
         b'"docs/**","scripts/pm/**","AGENTS.md","*.md","benchmarks/pm-ops/**"],'
         b'"control_plane_legacy":["scripts/legacy-pm/**"],'
         b'"control_plane_ci":["scripts/ci/ci-fast-triage.py"],'
         b'"site":["website/**","docs-site/**"],'
         b'"app_test_only":["convex/__tests__/**","__tests__/**",'
         b'"**/*.test.ts","**/*.test.tsx"],'
         b'"app_e2e":["tests/e2e/**"]}\n')
RULES_SHA = hashlib.sha256(RULES).hexdigest()
TRUSTED = (".github/workflows/ci.yml", "scripts/ci/change_scope.py", "scripts/ci/change-scope-rules.json")
SITE_RUN_ID = 222
SITE_SCOPE_ID = 333
SITE_HEAD = "ea3d429342e6e53df344115aa651a5bb5d28cd1a"
SITE_BASE = "350ddb05" + "0" * 32
ADVANCED_MAIN = "edc414c" + "1" * 33
CONTROL_PLANE_HEAD = "d279c3fa0" + "d" * 31
CONTROL_PLANE_BASE = "037050316" + "0" * 31
CONTROL_PLANE_MAIN = "2856130a" + "1" * 32
CONTROL_PLANE_WORKFLOW_SHA = "7040efea" + "2" * 32
CONTROL_PLANE_PR = 8186
CONTROL_PLANE_RUN_ID = 35915535742
CONTROL_PLANE_SCOPE_ID = 8186001
TEST_ONLY_HEAD = "8219" + "a" * 36
TEST_ONLY_BASE = "8219" + "b" * 36
TEST_ONLY_WORKFLOW_SHA = "8219" + "c" * 36
TEST_ONLY_NUMBER = 8219
TEST_ONLY_SCOPE_ID = 8219001
# The e2e.yml sibling of the CI scope job id; its log is routed to the
# test-only classifier receipt so e2e_classifier_authenticated binds.
TEST_ONLY_E2E_SCOPE_ID = 8219002
# PR #14727 run 45334892718: BASE_SHA is the event-time base, but GitHub built the
# synthetic merge commit on main after it advanced, so the first parent is a strict
# descendant of BASE_SHA (compare status "ahead").
ADVANCED_FIRST_PARENT = "c2411b55" + "f" * 32


def pr():
    return {"number": NUMBER, "head": {"sha": HEAD, "ref": REF,
            "repo": {"full_name": "heydonna-app/heydonna-app"}},
            "state": "open", "draft": False, "base": {"ref": "main"}, "labels": []}


def run(conclusion="success"):
    # run_started_at: workflow_proof validates ISO 8601 Z ordering stamps
    # (merge.py rework 5, 8472745); fixtures must carry the live contract.
    # pull_requests: native_pr_binding_ok binds the run to exactly this PR.
    return {"id": RUN_ID, "run_attempt": 1, "head_sha": HEAD, "event": "pull_request",
            "head_branch": REF, "path": f".github/workflows/{WORKFLOW}",
            "status": "completed", "conclusion": conclusion,
            "run_started_at": "2026-09-22T10:00:00Z",
            "pull_requests": [{"number": NUMBER}]}


def site_run(conclusion="success"):
    return {"id": SITE_RUN_ID, "run_attempt": 1, "head_sha": HEAD, "event": "pull_request",
            "head_branch": REF, "path": ".github/workflows/ci-dummy.yml",
            "status": "completed", "conclusion": conclusion,
            "run_started_at": "2026-09-22T10:00:01Z",
            "pull_requests": [{"number": NUMBER}]}


def site_pr():
    # #8164 shape: website-only, no implementation issue, and main advanced
    # after the PR branch point.
    return {"number": 8164, "head": {"sha": SITE_HEAD, "ref": "site/8164",
            "repo": {"full_name": "heydonna-app/heydonna-app"}},
            "state": "open", "draft": False, "base": {"ref": "main"}, "labels": []}


def control_plane_pr():
    return {"number": CONTROL_PLANE_PR,
            "head": {"sha": CONTROL_PLANE_HEAD, "ref": "docs/8186",
                     "repo": {"full_name": "heydonna-app/heydonna-app"}},
            "state": "open", "draft": False, "base": {"ref": "main"}, "labels": []}


def test_only_pr():
    return {"number": TEST_ONLY_NUMBER,
            "head": {"sha": TEST_ONLY_HEAD, "ref": "test/8219",
                     "repo": {"full_name": "heydonna-app/heydonna-app"}},
            "state": "open", "draft": False, "base": {"ref": "main"}, "labels": []}


def test_only_run(conclusion="success", path="ci.yml"):
    return {"id": 8219002 if path == "ci.yml" else 8219004,
            "run_attempt": 1, "head_sha": TEST_ONLY_HEAD,
            "event": "pull_request", "head_branch": "test/8219",
            "path": f".github/workflows/{path}", "status": "completed",
            "conclusion": conclusion, "run_started_at": "2026-09-22T10:00:02Z",
            "pull_requests": [{"number": TEST_ONLY_NUMBER}]}


def test_only_jobs():
    rows = jobs(scope_conclusion="success", substantive="success")
    run = test_only_run()
    for row in rows:
        # Rebind to the test-only run and head (bound_attempt_jobs, rework 5).
        row["run_id"], row["run_attempt"], row["head_sha"] = (
            run["id"], run["run_attempt"], run["head_sha"])
        if row["name"] == "classify-change-scope":
            row["id"] = TEST_ONLY_SCOPE_ID
        if row["name"] == "typescript":
            row["steps"] = [{"name": "Unit tests", "status": "completed",
                             "conclusion": "success"}]
    return rows


def test_only_log(receipt=None, bindings=None):
    body = {"schema_version": 1, "scope": "app_test_only", "app_test_only": True,
            "control_plane_only": False, "paid_ci_exempt": False,
            "product_changed": False, "ci_required": True, "e2e_required": False,
            "rules_sha256": RULES_SHA,
            # e2e_classifier_authenticated (rework-5) binds the receipt's
            # changed_files to the complete PR file set; the default matches
            # the default app_test_files fixture below.
            "changed_files": ["convex/__tests__/prmHunksMigration.test.ts"]}
    body.update(receipt or {})
    values = {"BASE_SHA": TEST_ONLY_BASE, "HEAD_SHA": TEST_ONLY_HEAD,
              "WORKFLOW_SHA": TEST_ONLY_WORKFLOW_SHA,
              "EVENT_NAME": "pull_request", "PR_NUMBER": str(TEST_ONLY_NUMBER)}
    values.update(bindings or {})
    return "\n".join([*(f"{key}={value}" for key, value in values.items()),
                       json.dumps(body, sort_keys=True)]) + "\n"


def jobs(scope_conclusion="success", substantive="skipped"):
    # bound_attempt_jobs (merge.py rework 5, 8472745) binds every job natively
    # to the exact PR, head, run and attempt; fixtures must carry that contract.
    out = [{"id": SCOPE_ID, "name": "classify-change-scope", "status": "completed",
            "run_id": RUN_ID, "run_attempt": 1, "head_sha": HEAD,
            "conclusion": scope_conclusion, "steps": [{"name": "classify", "status": "completed", "conclusion": scope_conclusion}]}]
    for name in NAMES:
        out.append({"id": 500 + len(out), "name": name, "status": "completed",
                    "run_id": RUN_ID, "run_attempt": 1, "head_sha": HEAD,
                    "conclusion": substantive, "steps": []})
    return out


def site_jobs(scope_conclusion="success"):
    return [{"id": SITE_SCOPE_ID, "name": "detect-docs-only", "status": "completed",
             "conclusion": scope_conclusion, "steps": [{"name": "Check all changed files",
             "status": "completed", "conclusion": scope_conclusion}]}]


def control_plane_exemption_jobs():
    return [{"id": CONTROL_PLANE_SCOPE_ID, "name": "detect-docs-only",
             "status": "completed", "conclusion": "success",
             "steps": [{"name": "Classify all changed files", "status": "completed",
                        "conclusion": "success"}]}]


def control_plane_exemption_run():
    return {"id": CONTROL_PLANE_RUN_ID, "run_attempt": 1,
            "head_sha": CONTROL_PLANE_HEAD, "event": "pull_request",
            "head_branch": "docs/8186", "path": ".github/workflows/ci-dummy.yml",
            "status": "completed", "conclusion": "success"}


def control_plane_exemption_log(receipt=None, bindings=None):
    body = dict(schema_version=1, scope="control_plane_only", paid_ci_exempt=True,
                control_plane_only=True, product_changed=False, ci_required=False,
                e2e_required=False, rules_sha256=RULES_SHA)
    body.update(receipt or {})
    values = {"BASE_SHA": CONTROL_PLANE_BASE, "HEAD_SHA": CONTROL_PLANE_HEAD,
              "WORKFLOW_SHA": CONTROL_PLANE_WORKFLOW_SHA,
              "EVENT_NAME": "pull_request", "PR_NUMBER": str(CONTROL_PLANE_PR)}
    values.update(bindings or {})
    lines = [f"{key}={value}" for key, value in values.items()]
    lines.append(json.dumps(body, sort_keys=True))
    return "\n".join(lines) + "\n"


def log_text(receipt=None, bindings=None, extra_lines=(), sep="="):
    base = {"BASE_SHA": BASE, "HEAD_SHA": HEAD, "WORKFLOW_SHA": WORKFLOW_SHA,
            "EVENT_NAME": "pull_request", "PR_NUMBER": str(NUMBER)}
    base.update(bindings or {})
    joiner = ": " if sep == ":" else "="
    lines = [f"2026-09-22T10:00:0{i}Z {k}{joiner}{v}" for i, (k, v) in enumerate(base.items())]
    body = receipt if receipt is not None else dict(
        schema_version=1, scope="control_plane_only", paid_ci_exempt=True,
        control_plane_only=True, product_changed=False, ci_required=False,
        e2e_required=False, rules_sha256=RULES_SHA)
    lines.append("2026-09-22T10:00:09Z " + json.dumps(body, sort_keys=True))
    lines.extend(extra_lines)
    return "\x1b[36m" + "\n".join(lines) + "\x1b[0m\n"


def site_log_text(receipt=None, bindings=None, extra_lines=(), sep="="):
    body = dict(schema_version=1, scope="site", paid_ci_exempt=True,
                control_plane_only=False, product_changed=False, ci_required=False,
                e2e_required=False, rules_sha256=RULES_SHA)
    body.update(receipt or {})
    return log_text(body, bindings, extra_lines, sep)


class Stub:
    def __init__(self, world):
        self.world = world
        self.calls = []
        self.api_calls = []

    def api(self, path):
        w = self.world
        self.api_calls.append(path)
        if path.startswith("pulls/"):
            return w.get("pr", pr())
        if path == "git/ref/heads/main":
            return {"object": {"sha": w.get("main", BASE)}}
        if path.startswith("compare/"):
            if w.get("forbid_compare"):
                raise AssertionError(f"compare must not be read for an exact base parent: {path}")
            if w.get("compare_error"):
                raise merge.Refusal("COMPARE_FAILED")
            out = {"merge_base_commit": {"sha": w.get("main", BASE)},
                   "html_url": "https://example.invalid/compare", "files": []}
            if path == f"compare/{CONTROL_PLANE_BASE}...{CONTROL_PLANE_HEAD}":
                out["files"] = w.get("pinned_compare_files", [])
            if "compare_status" in w:
                out["status"] = w["compare_status"]
            # Per-path compare responses (CTO REVISE ts 1795510573.793869: the
            # advanced parent must be bound to the verified base-branch tip).
            for prefix, status in w.get("compare_statuses", {}).items():
                if path.startswith("compare/" + prefix + "..."):
                    out["status"] = status
            return out
        if path.startswith("git/commits/"):
            sha = path.split("/", 2)[2]
            if sha == TEST_ONLY_WORKFLOW_SHA:
                parents = ([{"sha": "0" * 40}, {"sha": TEST_ONLY_HEAD}]
                           if w.get("bad_parents") else
                           [{"sha": w.get("workflow_parent", TEST_ONLY_BASE)},
                            {"sha": w.get("workflow_head", TEST_ONLY_HEAD)}])
            elif sha == CONTROL_PLANE_WORKFLOW_SHA:
                parents = [{"sha": w.get("workflow_parent", CONTROL_PLANE_MAIN)},
                           {"sha": w.get("workflow_head", CONTROL_PLANE_HEAD)}]
            elif sha != w.get("workflow_sha", WORKFLOW_SHA) or w.get("bad_parents"):
                parents = [{"sha": "0" * 40}, {"sha": HEAD}]
            else:
                parents = [{"sha": w.get("workflow_parent", BASE)},
                           {"sha": w.get("workflow_head", HEAD)}]
            return {"sha": sha, "parents": parents}
        if path.startswith("contents/"):
            rel, _, ref = path[len("contents/"):].partition("?ref=")
            if rel in w.get("missing_paths", ()):
                raise merge.Refusal("CONTENTS_MISSING")
            blob = "blob-" + rel
            if w.get("blob_mismatch") and ref in (WORKFLOW_SHA, TEST_ONLY_WORKFLOW_SHA):
                blob += "-drifted"
            if ref in w.get("drifted_refs", ()):
                blob += "-drifted-at-ref"
            if ref == CONTROL_PLANE_WORKFLOW_SHA and rel in w.get("changed_workflow_paths", ()):
                blob += "-changed-by-pr"
            content = base64.b64encode(RULES if rel.endswith("rules.json") else b"x").decode()
            if (w.get("rules_bytes") is not None and rel.endswith("rules.json")
                    and ref in (WORKFLOW_SHA, TEST_ONLY_WORKFLOW_SHA)):
                content = base64.b64encode(w["rules_bytes"]).decode()
            if (w.get("ci_yaml") is not None and rel.endswith("workflows/ci.yml")
                    and ref == TEST_ONLY_WORKFLOW_SHA):
                content = base64.b64encode(w["ci_yaml"]).decode()
            return {"sha": blob, "content": content}
        raise AssertionError(path)

    def pages(self, path, key):
        self.calls.append(path)
        if "/runs?" in path:
            if "ci-dummy.yml" in path:
                return self.world.get("site_runs", [])
            if "ci.yml" in path:
                return self.world.get("ci_runs", [self.world.get("run", run())])
            if "e2e.yml" in path:
                if "e2e_runs" in self.world:
                    return self.world["e2e_runs"]
                e2e_run = copy.deepcopy(self.world.get("run", run()))
                e2e_run["path"] = ".github/workflows/e2e.yml"
                return [e2e_run]
            if "ci.yml" in path and "ci_runs" in self.world:
                return self.world["ci_runs"]
            return [self.world.get("run", run())]
        if "/jobs?" in path:
            if f"/{test_only_run(path='e2e.yml')['id']}/jobs?" in path:
                return self.world.get("e2e_jobs", [])
            if f"/{CONTROL_PLANE_RUN_ID}/jobs?" in path:
                return self.world.get("control_plane_jobs", control_plane_exemption_jobs())
            if f"/{SITE_RUN_ID}/jobs?" in path:
                return self.world.get("site_jobs", site_jobs())
            return self.world.get("jobs", jobs())
        raise AssertionError(path)

    def command(self, *args):
        if self.world.get("log_failure"):
            raise merge.Refusal("LOG_FETCH_FAILED")
        path = args[-1]
        for job_id, text in self.world.get("job_logs", {}).items():
            if path.endswith(f"/actions/jobs/{job_id}/logs"):
                return text
        if "/pulls/" in path and "/files?" in path:
            files = self.world.get("site_files", [{"filename": "website/index.html"}])
            if str(TEST_ONLY_NUMBER) in path:
                files = self.world.get("app_test_files", [
                    {"filename": "convex/__tests__/prmHunksMigration.test.ts"}])
            if str(CONTROL_PLANE_PR) in path:
                files = self.world.get("control_plane_files", [{"filename": "docs/merge-gate.md"}])
            return json.dumps([files])
        if f"/actions/jobs/{CONTROL_PLANE_SCOPE_ID}/logs" in path:
            return self.world.get("site_log", control_plane_exemption_log())
        if f"/actions/jobs/{SITE_SCOPE_ID}/logs" in path:
            return self.world.get("site_log", site_log_text())
        if f"/actions/jobs/{TEST_ONLY_SCOPE_ID}/logs" in path:
            return self.world.get("test_only_log", test_only_log())
        if f"/actions/jobs/{TEST_ONLY_E2E_SCOPE_ID}/logs" in path:
            return self.world.get("test_only_log", test_only_log())
        return self.world.get("log", log_text())


def install(world=None):
    world = {} if world is None else world
    stub = Stub(world)
    merge.api, merge.pages, merge.command = stub.api, stub.pages, stub.command
    # Ordinary fixture fallback requires E2E; never consult live canonical policy.
    merge.workflow_requirements = lambda *_: {"e2e_required": True}
    return stub


def non_exempt_log():
    """A classifier log whose receipt is NOT exempt (product_changed=true)."""
    return log_text(receipt=dict(schema_version=1, scope="product", paid_ci_exempt=False,
                                 control_plane_only=False, product_changed=True,
                                 ci_required=True, e2e_required=True, rules_sha256=RULES_SHA))


def proof():
    return merge.workflow_proof(pr(), WORKFLOW, NAMES)


class GreenTests(unittest.TestCase):
    def test_exempt_control_plane_only_run_is_accepted(self):
        install()
        result = proof()
        self.assertEqual(result["exempt"], "control_plane_only")
        self.assertEqual(result["run"], RUN_ID)
        self.assertEqual(result["scope_job_id"], SCOPE_ID)
        self.assertEqual(result["workflow_sha"], WORKFLOW_SHA)
        self.assertEqual(result["rules_sha256"], RULES_SHA)

    def test_non_exempt_all_green_still_returns_normal_proof(self):
        world = {"jobs": jobs(substantive="success"), "log": non_exempt_log()}
        for job in world["jobs"]:
            if job["name"] == "typescript":
                job["steps"] = [{"name": "Unit tests", "status": "completed", "conclusion": "success"}]
        install(world)  # green NON-exempt: receipt is product_changed, jobs green, real test step executed
        result = proof()
        self.assertEqual(result["run"], RUN_ID)
        self.assertNotIn("exempt", result)


class RedAndNegativeTests(unittest.TestCase):
    def assert_refused(self, world=None):
        install(world)
        with self.assertRaises(merge.Refusal) as ctx:
            proof()
        return str(ctx.exception)

    def test_missing_receipt_refuses(self):
        self.assert_refused({"log": log_text(receipt={})})

    def test_duplicate_receipt_refuses(self):
        self.assert_refused({"log": log_text(extra_lines=["2026-09-22T10:00:10Z " + json.dumps(
            {"schema_version": 1, "scope": "control_plane_only"})])})

    def test_malformed_receipt_line_is_ignored_not_exempt(self):
        self.assert_refused({"log": "BASE_SHA=" + BASE + "\n{not json\n"})

    def test_log_fetch_failure_refuses(self):
        self.assert_refused({"log_failure": True})

    def test_each_boolean_mismatch_refuses(self):
        for key, value in (("schema_version", 2), ("scope", "product"), ("paid_ci_exempt", False),
                           ("control_plane_only", False), ("product_changed", True),
                           ("ci_required", True), ("e2e_required", True)):
            with self.subTest(key=key):
                receipt = dict(schema_version=1, scope="control_plane_only", paid_ci_exempt=True,
                               control_plane_only=True, product_changed=False, ci_required=False,
                               e2e_required=False, rules_sha256=RULES_SHA)
                receipt[key] = value
                self.assert_refused({"log": log_text(receipt=receipt)})

    def test_bad_rules_sha_refuses(self):
        receipt = dict(schema_version=1, scope="control_plane_only", paid_ci_exempt=True,
                       control_plane_only=True, product_changed=False, ci_required=False,
                       e2e_required=False, rules_sha256="f" * 64)
        self.assert_refused({"log": log_text(receipt=receipt)})

    def test_each_binding_mismatch_refuses(self):
        cases = ({"HEAD_SHA": "0" * 40}, {"EVENT_NAME": "push"}, {"PR_NUMBER": "9999"},
                 {"BASE_SHA": "short"}, {"WORKFLOW_SHA": "short"})
        for binding in cases:
            with self.subTest(binding=binding):
                self.assert_refused({"log": log_text(bindings=binding)})

    def test_duplicate_binding_refuses(self):
        self.assert_refused({"log": log_text(extra_lines=["2026-09-22T10:00:11Z HEAD_SHA=" + "0" * 40])})

    def test_bad_merge_parents_refuse(self):
        self.assert_refused({"bad_parents": True})

    def test_trusted_blob_mismatch_refuses(self):
        self.assert_refused({"blob_mismatch": True})

    def test_missing_trusted_path_refuses(self):
        self.assert_refused({"missing_paths": ("scripts/ci/change_scope.py",)})

    def test_rules_content_digest_mismatch_refuses(self):
        self.assert_refused({"rules_bytes": b"different rules\n"})

    def test_skipped_classifier_is_not_exempt(self):
        self.assert_refused({"jobs": jobs(scope_conclusion="skipped")})

    def test_failed_classifier_is_not_exempt(self):
        self.assert_refused({"jobs": jobs(scope_conclusion="failure")})

    def test_missing_classifier_is_not_exempt(self):
        self.assert_refused({"jobs": [j for j in jobs() if j["name"] != "classify-change-scope"]})

    def test_missing_test_execution_still_refuses_on_non_exempt(self):
        # substantive jobs succeed but the required real-test step never ran; not exempt
        world = {"jobs": jobs(substantive="success"), "log": non_exempt_log()}
        install(world)
        with self.assertRaises(merge.Refusal) as ctx:
            proof()
        self.assertIn("TEST_EXECUTION_MISSING", str(ctx.exception))


class ParentRedWitnessTests(unittest.TestCase):
    @staticmethod
    def load_reviewed_parent():
        blob = subprocess.run(["git", "show", "c33c295c46c471de233e3420523bfc2e96c9e760:scripts/pm/shared-assets/codex/skills/"
                               "heydonna-open-pr-status/scripts/merge.py"],
                              capture_output=True, text=True, cwd=HERE)
        if blob.returncode != 0:
            raise AssertionError(blob.stderr)
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "merge_parent.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(blob.stdout)
        spec = importlib.util.spec_from_file_location("merge_parent_site", path)
        parent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parent)
        return tmp, parent

    def test_reviewed_parent_refuses_site_exempt_shape(self):
        tmp, parent = self.load_reviewed_parent()
        self.addCleanup(tmp.cleanup)
        stub = Stub({"site_runs": [site_run()], "site_jobs": site_jobs(),
                     "site_log": site_log_text(),
                     "run": run(conclusion="skipped"), "jobs": jobs(scope_conclusion="skipped")})
        parent.api, parent.pages, parent.command = stub.api, stub.pages, stub.command
        with self.assertRaises(parent.Refusal) as ctx:
            parent.merge(NUMBER, HEAD)
        self.assertIn("WORKFLOW_MISSING workflow=e2e.yml", str(ctx.exception))

    def test_site_fixture_returns_ready_with_the_same_proof_for_both_slots(self):
        world = {"site_runs": [site_run()], "site_jobs": site_jobs(),
                 "site_log": site_log_text(),
                 "run": run(conclusion="skipped"), "jobs": jobs(scope_conclusion="skipped")}
        stub = install(world)
        result = merge.merge(NUMBER, HEAD)
        self.assertEqual(result["status"], "READY_TO_MERGE")
        ci, e2e = result["workflows"]["ci.yml"], result["workflows"]["e2e.yml"]
        self.assertEqual(ci["exempt"], "site")
        self.assertEqual(e2e["exempt"], "site")
        self.assertEqual((ci["run"], ci["scope_job_id"]), (e2e["run"], e2e["scope_job_id"]))
        self.assertEqual(sum("ci-dummy.yml" in path for path in stub.calls), 1)


class SiteExemptionTests(unittest.TestCase):
    def site_proof(self, world=None):
        defaults = {"site_runs": [site_run()], "site_jobs": site_jobs(),
                    "site_log": site_log_text(), "run": run(conclusion="skipped"),
                    "jobs": jobs(scope_conclusion="skipped")}
        if world is not None:
            defaults.update(world)
        world = defaults
        install(world)
        return merge.site_classifier_exemption(pr())

    def test_exact_site_receipt_and_site_only_file_set_is_ready(self):
        result = self.site_proof()
        self.assertEqual(result["exempt"], "site")
        self.assertEqual(result["run"], SITE_RUN_ID)
        self.assertEqual(result["scope_job_id"], SITE_SCOPE_ID)
        self.assertEqual(result["head"], HEAD)
        self.assertEqual(result["workflow_sha"], WORKFLOW_SHA)

    def test_8164_issue_less_site_pr_passes_when_main_advanced_since_merge_base(self):
        fixture_pr = site_pr()
        self.assertNotIn("issue", fixture_pr)
        site_log = site_log_text(bindings={
            "BASE_SHA": SITE_BASE,
            "HEAD_SHA": SITE_HEAD,
            "WORKFLOW_SHA": WORKFLOW_SHA,
            "EVENT_NAME": "pull_request",
            "PR_NUMBER": "8164",
        })
        site_run_row = site_run()
        site_run_row["head_sha"] = SITE_HEAD
        site_run_row["head_branch"] = "site/8164"
        stub = install({
            "pr": fixture_pr,
            "main": ADVANCED_MAIN,
            "workflow_parent": ADVANCED_MAIN,
            "workflow_head": SITE_HEAD,
            "site_runs": [site_run_row],
            "site_jobs": site_jobs(),
            "site_log": site_log,
            "site_files": [{"filename": "website/index.html"}],
            "run": run(conclusion="skipped"),
            "jobs": jobs(scope_conclusion="skipped"),
        })

        result = merge.merge(8164, SITE_HEAD)

        self.assertEqual(result["status"], "READY_TO_MERGE")
        self.assertEqual(result["workflows"]["ci.yml"]["exempt"], "site")
        self.assertEqual(result["workflows"]["e2e.yml"]["exempt"], "site")
        self.assertEqual(result["workflows"]["ci.yml"]["head"], SITE_HEAD)
        self.assertEqual(result["workflows"]["ci.yml"]["rules_sha256"], RULES_SHA)
        self.assertTrue(any("ci-dummy.yml" in path for path in stub.calls))

    def test_site_receipt_still_requires_synthetic_second_parent_to_match_pr_head(self):
        site_log = site_log_text(bindings={
            "BASE_SHA": SITE_BASE,
            "HEAD_SHA": SITE_HEAD,
            "WORKFLOW_SHA": WORKFLOW_SHA,
            "EVENT_NAME": "pull_request",
            "PR_NUMBER": "8164",
        })
        site_run_row = site_run()
        site_run_row["head_sha"] = SITE_HEAD
        site_run_row["head_branch"] = "site/8164"
        install({
            "pr": site_pr(),
            "workflow_parent": ADVANCED_MAIN,
            "workflow_head": "0" * 40,
            "site_runs": [site_run_row],
            "site_jobs": site_jobs(),
            "site_log": site_log,
            "site_files": [{"filename": "website/index.html"}],
        })
        self.assertIsNone(merge.site_classifier_exemption(site_pr()))

    def assert_site_refused(self, world):
        base = {"site_runs": [site_run()], "site_jobs": site_jobs(),
                "site_log": site_log_text(), "run": run(conclusion="skipped"),
                "jobs": jobs(scope_conclusion="skipped")}
        base.update(world)
        install(base)
        self.assertIsNone(merge.site_classifier_exemption(pr()))

    def test_out_of_glob_files_fall_through_to_the_real_workflow_pair(self):
        stub = install({"site_runs": [site_run()], "site_jobs": site_jobs(),
                        "site_log": site_log_text(), "site_files": [
                            {"filename": "website/index.html"}, {"filename": "src/app.ts"}],
                        "ci_runs": [], "e2e_runs": []})
        with self.assertRaises(merge.Refusal) as ctx:
            merge.merge(NUMBER, HEAD)
        self.assertIn("WORKFLOW_MISSING workflow=ci.yml", str(ctx.exception))
        self.assertTrue(any("actions/workflows/ci.yml/runs?" in path for path in stub.calls))

    def test_product_scope_receipt_is_not_exempt(self):
        receipt = dict(schema_version=1, scope="product", paid_ci_exempt=False,
                       control_plane_only=False, product_changed=True, ci_required=True,
                       e2e_required=True, rules_sha256=RULES_SHA)
        self.assert_site_refused({"site_log": site_log_text(receipt=receipt)})

    def test_site_exemption_booleans_must_match_the_classifier_contract(self):
        for key, value in (("paid_ci_exempt", False), ("product_changed", True),
                           ("ci_required", True), ("e2e_required", True),
                           ("control_plane_only", True)):
            with self.subTest(key=key):
                self.assert_site_refused({"site_log": site_log_text(receipt={key: value})})

    def test_site_receipt_with_wrong_head_is_not_exempt(self):
        self.assert_site_refused({"site_log": site_log_text(bindings={"HEAD_SHA": "0" * 40})})

    def test_site_receipt_with_out_of_scope_pr_path_is_not_exempt(self):
        self.assert_site_refused({"site_files": [{"filename": "website/index.html"},
                                                   {"filename": "src/app.ts"}]})

    def test_renamed_path_must_also_match_site_globs(self):
        self.assert_site_refused({"site_files": [{"filename": "website/new.html",
                                                   "previous_filename": "src/old.ts"}]})


class ControlPlaneExemptionTests(unittest.TestCase):
    def setup_world(self, **overrides):
        world = {
            "pr": control_plane_pr(),
            "main": CONTROL_PLANE_MAIN,
            "site_runs": [control_plane_exemption_run()],
            "control_plane_jobs": control_plane_exemption_jobs(),
            "site_log": control_plane_exemption_log(),
            "control_plane_files": [{"filename": "docs/merge-gate.md"}],
            "ci_runs": [],
            "e2e_runs": [],
        }
        world.update(overrides)
        return install(world)

    def test_8186_shaped_docs_exemption_with_advanced_main_satisfies_both_slots(self):
        stub = self.setup_world()
        self.assertNotEqual(CONTROL_PLANE_BASE, CONTROL_PLANE_MAIN)
        result = merge.merge(CONTROL_PLANE_PR, CONTROL_PLANE_HEAD)
        self.assertEqual(result["status"], "READY_TO_MERGE")
        ci, e2e = result["workflows"]["ci.yml"], result["workflows"]["e2e.yml"]
        self.assertEqual(ci["exempt"], "control_plane_only")
        self.assertEqual(e2e["exempt"], "control_plane_only")
        self.assertEqual((ci["run"], ci["scope_job_id"]),
                         (CONTROL_PLANE_RUN_ID, CONTROL_PLANE_SCOPE_ID))
        self.assertEqual((ci["workflow_sha"], ci["rules_sha256"]),
                         (CONTROL_PLANE_WORKFLOW_SHA, RULES_SHA))
        self.assertTrue(any("ci-dummy.yml" in path for path in stub.calls))

    def assert_falls_through(self, **overrides):
        self.setup_world(**overrides)
        with self.assertRaises(merge.Refusal) as ctx:
            merge.merge(CONTROL_PLANE_PR, CONTROL_PLANE_HEAD)
        self.assertIn("WORKFLOW_MISSING workflow=ci.yml", str(ctx.exception))

    def test_wrong_synthetic_second_parent_refuses(self):
        self.assert_falls_through(workflow_head="0" * 40)

    def test_product_path_in_pr_file_list_refuses(self):
        self.assert_falls_through(control_plane_files=[
            {"filename": "docs/merge-gate.md"}, {"filename": "src/app.ts"}])

    # PR #14787 shape: the shared classifier's marketing-only rule (every path
    # under website/**) emits scope=control_plane_only with site_scoped=false, so
    # the paths sit in the `site` glob group, not the control-plane groups.
    # Site-only PRs have no paid ci.yml/e2e.yml run; the exact-head exemption is
    # their gate. Product PRs without ci.yml must still refuse WORKFLOW_MISSING.
    def test_site_only_paths_under_control_plane_receipt_are_exempt(self):
        self.setup_world(control_plane_files=[
            {"filename": "website/leaderboard/index.html"},
            {"filename": "website/css/donna-doc.css"}])
        result = merge.merge(CONTROL_PLANE_PR, CONTROL_PLANE_HEAD)
        self.assertEqual(result["status"], "READY_TO_MERGE")
        for slot in ("ci.yml", "e2e.yml"):
            proof = result["workflows"][slot]
            self.assertEqual(proof["exempt"], "control_plane_only")
            self.assertIs(proof["site_only_paths"], True)
            self.assertEqual((proof["run"], proof["scope_job_id"]),
                             (CONTROL_PLANE_RUN_ID, CONTROL_PLANE_SCOPE_ID))

    def test_docs_only_control_plane_exemption_carries_no_site_marker(self):
        self.setup_world()
        result = merge.merge(CONTROL_PLANE_PR, CONTROL_PLANE_HEAD)
        self.assertNotIn("site_only_paths", result["workflows"]["ci.yml"])

    def test_product_only_pr_without_ci_run_still_refuses(self):
        self.assert_falls_through(control_plane_files=[{"filename": "app/page.tsx"}])

    def test_site_plus_product_path_still_refuses(self):
        self.assert_falls_through(control_plane_files=[
            {"filename": "website/index.html"}, {"filename": "app/page.tsx"}])

    def test_site_rename_from_product_path_still_refuses(self):
        self.assert_falls_through(control_plane_files=[
            {"filename": "website/new.html", "previous_filename": "app/old.tsx"}])

    def test_site_only_paths_with_wrong_synthetic_second_parent_still_refuse(self):
        self.assert_falls_through(
            control_plane_files=[{"filename": "website/index.html"}],
            workflow_head="0" * 40)

    def test_site_only_paths_with_non_exempt_receipt_still_refuse(self):
        self.assert_falls_through(
            control_plane_files=[{"filename": "website/index.html"}],
            site_log=control_plane_exemption_log(receipt={
                "scope": "product", "paid_ci_exempt": False,
                "control_plane_only": False, "product_changed": True,
                "ci_required": True, "e2e_required": True}))

    def classifier_self_edit_world(self, extra_files=(), receipt=None, bindings=None):
        files = [{"filename": "scripts/ci/change_scope.py"},
                 {"filename": "scripts/ci/tests/test_change_scope.py"},
                 {"filename": "docs/plans/unit-selection.md"}, *extra_files]
        paths = [item[key] for item in files
                 for key in ("filename", "previous_filename") if key in item]
        run = dict(control_plane_exemption_run(), pull_requests=[
            {"number": CONTROL_PLANE_PR, "base": {"sha": CONTROL_PLANE_BASE}}])
        job = dict(control_plane_exemption_jobs()[0], run_id=CONTROL_PLANE_RUN_ID,
                   run_attempt=1, head_sha=CONTROL_PLANE_HEAD)
        # This case relies on the native association: the live receipt omits PR_NUMBER.
        return {"site_runs": [run], "control_plane_jobs": [job],
                "site_log": control_plane_exemption_log(
                    receipt={"changed_files": paths, **(receipt or {})},
                    bindings=bindings).replace(f"PR_NUMBER={CONTROL_PLANE_PR}\n", ""),
                "changed_workflow_paths": ["scripts/ci/change_scope.py"],
                "pinned_compare_files": files, "workflow_parent": CONTROL_PLANE_BASE}

    def test_classifier_self_edit_uses_green_exact_head_control_plane_receipt(self):
        stub = self.setup_world(**self.classifier_self_edit_world())
        result = merge.merge(CONTROL_PLANE_PR, CONTROL_PLANE_HEAD)
        self.assertEqual(result["status"], "READY_TO_MERGE")
        for proof in result["workflows"].values():
            self.assertEqual(proof["exempt"], "control_plane_only")
            self.assertEqual((proof["head"], proof["run"], proof["scope_job_id"]),
                             (CONTROL_PLANE_HEAD, CONTROL_PLANE_RUN_ID, CONTROL_PLANE_SCOPE_ID))
        self.assertIn(f"compare/{CONTROL_PLANE_BASE}...{CONTROL_PLANE_HEAD}", stub.api_calls)
        self.assertFalse(any("actions/workflows/ci.yml/runs?" in path for path in stub.calls))

    def test_classifier_self_edit_requires_all_exact_exemption_flags(self):
        for key, value in (("control_plane_only", False), ("product_changed", True),
                           ("ci_required", True), ("e2e_required", True),
                           ("product_changed", 0)):
            with self.subTest(key=key, value=value):
                self.assert_falls_through(**self.classifier_self_edit_world(receipt={key: value}))

    def test_classifier_self_edit_refuses_stale_logged_head(self):
        self.assert_falls_through(**self.classifier_self_edit_world(bindings={"HEAD_SHA": "0" * 40}))

    def test_classifier_self_edit_refuses_stale_or_missing_run(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                world = self.classifier_self_edit_world()
                world["site_runs"][0]["head_sha"] = "0" * 40
                if missing:
                    world["site_runs"] = []
                self.assert_falls_through(**world)

    def test_classifier_self_edit_refuses_out_of_scope_paths_even_with_exempt_receipt(self):
        for row in ({"filename": "app/page.tsx"}, {"filename": "scripts/ci/run-ci.sh"},
                    {"filename": "docs/moved.md", "previous_filename": "app/old.tsx"}):
            with self.subTest(row=row):
                self.assert_falls_through(**self.classifier_self_edit_world(extra_files=[row]))

    def test_classifier_self_edit_requires_receipt_to_cover_pinned_files(self):
        self.assert_falls_through(**self.classifier_self_edit_world(receipt={
            "changed_files": ["scripts/ci/change_scope.py"]}))

    def test_classifier_self_edit_refuses_wrong_attempt_job(self):
        world = self.classifier_self_edit_world()
        world["control_plane_jobs"][0]["run_attempt"] = 2
        self.assert_falls_through(**world)

    def test_classifier_self_edit_does_not_exempt_modified_workflow_or_rules(self):
        for path in (".github/workflows/ci-dummy.yml", "scripts/ci/change-scope-rules.json"):
            with self.subTest(path=path):
                world = self.classifier_self_edit_world()
                world["changed_workflow_paths"].append(path)
                self.assert_falls_through(**world)

    def test_parent_without_control_plane_ci_exemption_is_red(self):
        rev = "0889385c7679198a10006c086c1da9b2c95b7d41"
        blob = subprocess.run(
            ["git", "show", f"{rev}:scripts/pm/shared-assets/codex/skills/"
             "heydonna-open-pr-status/scripts/merge.py"],
            capture_output=True, text=True, cwd=HERE)
        self.assertEqual(blob.returncode, 0, blob.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "merge_parent.py")
            pathlib.Path(path).write_text(blob.stdout, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("merge_parent", path)
            parent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(parent)
            stub = Stub({
                "pr": control_plane_pr(), "main": CONTROL_PLANE_MAIN,
                "site_runs": [control_plane_exemption_run()],
                "control_plane_jobs": control_plane_exemption_jobs(),
                "site_log": control_plane_exemption_log(),
                "control_plane_files": [{"filename": "docs/merge-gate.md"}],
                "ci_runs": [], "e2e_runs": [],
            })
            parent.api, parent.pages, parent.command = stub.api, stub.pages, stub.command
            with self.assertRaises(parent.Refusal) as ctx:
                parent.merge(CONTROL_PLANE_PR, CONTROL_PLANE_HEAD)
            self.assertIn("WORKFLOW_MISSING workflow=ci.yml", str(ctx.exception))


class AppTestOnlyCompatibilityTests(unittest.TestCase):
    def world(self, **overrides):
        world = {"pr": test_only_pr(), "workflow_parent": TEST_ONLY_BASE,
                 "workflow_head": TEST_ONLY_HEAD, "ci_runs": [test_only_run()],
                 "jobs": test_only_jobs(), "test_only_log": test_only_log(),
                 "app_test_files": [
                     {"filename": "convex/__tests__/prmHunksMigration.test.ts"}],
                 "e2e_runs": []}
        world.update(overrides)
        return world

    def test_parent_is_red_for_green_ci_and_absent_e2e(self):
        rev = "a386cc9ce28b600d2eec62ff4589d5b4c08307fc"
        blob = subprocess.run(
            ["git", "show", f"{rev}:scripts/pm/shared-assets/codex/skills/"
             "heydonna-open-pr-status/scripts/merge.py"],
            capture_output=True, text=True, cwd=HERE)
        self.assertEqual(blob.returncode, 0, blob.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "merge_parent.py")
            pathlib.Path(path).write_text(blob.stdout, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("merge_parent_test_only", path)
            parent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(parent)
            stub = Stub(self.world())
            parent.api, parent.pages, parent.command = stub.api, stub.pages, stub.command
            with self.assertRaises(parent.Refusal) as ctx:
                parent.merge(TEST_ONLY_NUMBER, TEST_ONLY_HEAD)
            self.assertIn("WORKFLOW_MISSING workflow=e2e.yml", str(ctx.exception))

    def test_exact_app_test_only_receipt_without_executed_e2e_classifier_refuses(self):
        install(self.world())
        with self.assertRaisesRegex(merge.Refusal, "UNREQUIRED_E2E_NOT_SUCCESSFULLY_SKIPPED"):
            merge.merge(TEST_ONLY_NUMBER, TEST_ONLY_HEAD)

    def test_exact_app_test_only_receipt_allows_successful_explicit_e2e_skip(self):
        skipped = test_only_run(path="e2e.yml")
        skipped["pull_requests"] = [{"number": TEST_ONLY_NUMBER}]
        common = {"run_id": skipped["id"], "run_attempt": skipped["run_attempt"],
                  "head_sha": TEST_ONLY_HEAD, "status": "completed"}
        # The e2e classifier job (8219002) shares the CI classifier log id space
        # with the CI scope job; route its log by id in Stub.command so rework-5's
        # e2e_classifier_authenticated reads the test-only receipt (rework-5, ts
        # 1795444808.341169), and give the run the e2e-log-capture job the
        # e2e_exemption_shape check requires.
        install(self.world(e2e_runs=[skipped], e2e_jobs=[
            dict(common, id=TEST_ONLY_E2E_SCOPE_ID, name="classify-change-scope",
                 conclusion="success", steps=[
                     {"name": "Classify paid E2E scope", "status": "completed",
                      "conclusion": "success"}]),
            dict(common, id=8219003, name="e2e", conclusion="skipped", steps=[]),
            dict(common, id=8219004, name="e2e-log-capture",
                 conclusion="skipped", steps=[])]))
        result = merge.merge(TEST_ONLY_NUMBER, TEST_ONLY_HEAD)
        self.assertEqual(result["status"], "READY_TO_MERGE")
        self.assertTrue(result["workflows"]["e2e.yml"]["skipped"])

    def assert_blocked(self, world):
        install(self.world(**world))
        with self.assertRaises(merge.Refusal) as ctx:
            merge.merge(TEST_ONLY_NUMBER, TEST_ONLY_HEAD)
        self.assertIn("WORKFLOW_MISSING workflow=e2e.yml", str(ctx.exception))

    def test_product_mixed_and_unknown_scopes_still_require_e2e(self):
        product = test_only_log(receipt={"scope": "product", "app_test_only": False,
                                         "product_changed": True, "e2e_required": True})
        self.assert_blocked({"test_only_log": product})
        self.assert_blocked({"app_test_files": [
            {"filename": "convex/__tests__/prmHunksMigration.test.ts"},
            {"filename": "src/app.ts"}]})
        unknown = test_only_log(receipt={"scope": "unknown"})
        self.assert_blocked({"test_only_log": unknown})

    def test_stale_head_and_boolean_mismatches_still_require_e2e(self):
        self.assert_blocked({"test_only_log": test_only_log(
            bindings={"HEAD_SHA": "0" * 40})})
        for key, value in (("app_test_only", False), ("control_plane_only", True),
                           ("paid_ci_exempt", True), ("product_changed", True),
                           ("ci_required", False), ("e2e_required", True)):
            with self.subTest(key=key):
                self.assert_blocked({"test_only_log": test_only_log(
                    receipt={key: value})})

    def test_stale_workflow_and_classifier_rule_drift_require_e2e(self):
        self.assert_blocked({"bad_parents": True})
        self.assert_blocked({"blob_mismatch": True})
        self.assert_blocked({"rules_bytes": b"different trusted rules\n"})
        self.assert_blocked({"test_only_log": test_only_log(
            bindings={"WORKFLOW_SHA": "short"})})

    def test_failed_or_non_skipped_e2e_does_not_use_test_only_waiver(self):
        failed = test_only_run(path="e2e.yml", conclusion="failure")
        install(self.world(e2e_runs=[failed], e2e_jobs=[
            {"id": 8219003, "name": "e2e", "status": "completed",
             "conclusion": "failure", "steps": []}]))
        with self.assertRaises(merge.Refusal):
            merge.merge(TEST_ONLY_NUMBER, TEST_ONLY_HEAD)

    def test_red_ci_still_blocks_even_with_a_valid_test_only_receipt(self):
        failed = test_only_run(conclusion="failure")
        install(self.world(ci_runs=[failed]))
        with self.assertRaises(merge.Refusal) as ctx:
            merge.merge(TEST_ONLY_NUMBER, TEST_ONLY_HEAD)
        self.assertIn("WORKFLOW_NOT_GREEN workflow=ci.yml", str(ctx.exception))

    def test_duplicate_classifier_jobs_cannot_authorize_test_only(self):
        rows = test_only_jobs()
        rows.append({**rows[0], "id": TEST_ONLY_SCOPE_ID + 1})
        install(self.world(jobs=rows))
        with self.assertRaises(merge.Refusal) as ctx:
            merge.merge(TEST_ONLY_NUMBER, TEST_ONLY_HEAD)
        # Rework-5's native job binding refuses a duplicate classifier name
        # earlier than the old e2e-missing check; the run is still refused.
        self.assertIn("SUBSTANTIVE_RUN_NOT_BOUND workflow=ci.yml", str(ctx.exception))


def prove_test_only(world):
    install(world)
    scope = next(j for j in test_only_jobs() if j["name"] == "classify-change-scope")
    return merge.app_test_only_classifier_proof(test_only_pr(), "ci.yml", test_only_run(), scope)


def prove_control_plane(world):
    install(world)
    scope = jobs()[0]
    return merge.classifier_exemption(pr(), WORKFLOW, run(), scope, "control_plane_only")


class AdvancedMainFirstParentTests(unittest.TestCase):
    """PR #14727, run 45334892718: BASE_SHA is the event-time base, but the synthetic
    merge commit was built on main after it advanced, so its first parent is a strict
    descendant of BASE_SHA. That run is genuine and must keep its proof; anything that
    is not strictly ahead AND part of the verified base-branch history, or whose
    second parent is not the exact head, must not (CTO REVISE, C0ALZJHGE49 ts
    1795510573.793869: a descendant on a side branch must never become the trust
    reference)."""

    def scenario(self, **overrides):
        world = {"workflow_parent": ADVANCED_FIRST_PARENT, "workflow_head": TEST_ONLY_HEAD,
                 "compare_status": "ahead",
                 # The advanced parent is the GitHub-verified base-branch tip.
                 "main": ADVANCED_FIRST_PARENT,
                 "app_test_files": [{"filename": "convex/__tests__/prmHunksMigration.test.ts"}]}
        world.update(overrides)
        return world

    def test_app_test_only_proof_accepts_first_parent_strictly_ahead(self):
        result = prove_test_only(self.scenario())
        self.assertIsNotNone(result)
        self.assertEqual(result["exempt"], merge.TEST_ONLY_SCOPE)
        self.assertEqual(result["workflow_sha"], TEST_ONLY_WORKFLOW_SHA)

    def test_control_plane_proof_accepts_first_parent_strictly_ahead(self):
        result = prove_control_plane({"workflow_parent": ADVANCED_FIRST_PARENT,
                                      "compare_status": "ahead",
                                      "main": ADVANCED_FIRST_PARENT})
        self.assertIsNotNone(result)
        self.assertEqual(result["exempt"], "control_plane_only")

    def test_exact_base_first_parent_needs_no_compare(self):
        self.assertIsNotNone(prove_test_only(self.scenario(
            workflow_parent=TEST_ONLY_BASE, forbid_compare=True)))
        self.assertIsNotNone(prove_control_plane({"forbid_compare": True}))

    def test_first_parent_ahead_but_ancestor_of_the_verified_tip_is_on_main(self):
        # The merge was built on an older main tip; the verified tip moved on.
        tip = "d4e5f6a7" + "b" * 32
        self.assertIsNotNone(prove_test_only(self.scenario(
            main=tip, compare_statuses={tip: "behind"})))
        self.assertIsNotNone(prove_control_plane({"workflow_parent": ADVANCED_FIRST_PARENT,
                                                  "compare_status": "ahead",
                                                  "main": tip,
                                                  "compare_statuses": {tip: "behind"}}))

    def test_side_branch_descendant_of_the_event_base_refuses(self):
        # The CTO REVISE control: strictly ahead of BASE_SHA, but OUTSIDE the
        # base-branch history (tip compare diverged). Its workflow/classifier
        # blobs must never become the trust reference.
        tip = "9a8b7c6d" + "e" * 32
        self.assertIsNone(prove_test_only(self.scenario(
            main=tip, compare_statuses={tip: "diverged"})))
        self.assertIsNone(prove_control_plane({"workflow_parent": ADVANCED_FIRST_PARENT,
                                               "compare_status": "ahead",
                                               "main": tip,
                                               "compare_statuses": {tip: "diverged"}}))
        # An unavailable or non-conclusive tip ancestry also refuses (fail closed).
        self.assertIsNone(prove_test_only(self.scenario(
            main=tip, compare_statuses={tip: "ahead"})))
        self.assertIsNone(prove_test_only(self.scenario(main="not-a-sha")))
        self.assertIsNone(prove_control_plane({"workflow_parent": ADVANCED_FIRST_PARENT,
                                               "compare_status": "ahead",
                                               "main": tip,
                                               "compare_statuses": {tip: "identical"}}))

    def test_first_parent_not_strictly_ahead_refuses(self):
        for status in ("behind", "diverged", "identical"):
            with self.subTest(status=status):
                self.assertIsNone(prove_test_only(self.scenario(compare_status=status)))
                self.assertIsNone(prove_control_plane({"workflow_parent": ADVANCED_FIRST_PARENT,
                                                       "compare_status": status,
                                                       "main": ADVANCED_FIRST_PARENT}))

    def test_unprovable_ancestry_refuses(self):
        no_status = self.scenario()
        del no_status["compare_status"]
        self.assertIsNone(prove_test_only(no_status))
        self.assertIsNone(prove_test_only(self.scenario(compare_error=True)))
        self.assertIsNone(prove_test_only(self.scenario(workflow_parent="c2411b55")))

    def test_second_parent_must_be_the_exact_head(self):
        self.assertIsNone(prove_test_only(self.scenario(workflow_head="0" * 40)))
        self.assertIsNone(prove_control_plane({"workflow_parent": ADVANCED_FIRST_PARENT,
                                               "compare_status": "ahead",
                                               "main": ADVANCED_FIRST_PARENT,
                                               "workflow_head": "0" * 40}))

    def test_trusted_tooling_must_match_at_the_first_parent_the_merge_was_built_on(self):
        self.assertIsNone(prove_test_only(self.scenario(
            drifted_refs={ADVANCED_FIRST_PARENT})))
        self.assertIsNone(prove_control_plane({"workflow_parent": ADVANCED_FIRST_PARENT,
                                               "compare_status": "ahead",
                                               "main": ADVANCED_FIRST_PARENT,
                                               "drifted_refs": {ADVANCED_FIRST_PARENT}}))


class RealColonFormLogTests(unittest.TestCase):
    """The real GitHub Actions classifier log emits KEY: value - it must exempt."""

    # shaped from CI run 35744803503 / classify-change-scope job 106803509894
    REAL_BINDINGS = {"BASE_SHA": BASE, "HEAD_SHA": HEAD, "WORKFLOW_SHA": WORKFLOW_SHA,
                     "EVENT_NAME": "pull_request", "PR_NUMBER": str(NUMBER)}

    def test_colon_form_bindings_reach_the_exemption_path(self):
        install({"log": log_text(bindings=self.REAL_BINDINGS, sep=":")})
        result = proof()
        self.assertEqual(result["exempt"], "control_plane_only")
        self.assertEqual(result["workflow_sha"], WORKFLOW_SHA)

    def test_colon_form_bindings_tolerate_surrounding_whitespace(self):
        text = log_text(bindings=self.REAL_BINDINGS, sep=":")
        text = text.replace("BASE_SHA: ", "  BASE_SHA :  ")
        install({"log": text})
        self.assertEqual(proof()["exempt"], "control_plane_only")

    def test_colon_form_still_enforces_exactly_one_value(self):
        install({"log": log_text(bindings=self.REAL_BINDINGS, sep=":",
                                 extra_lines=["2026-09-22T10:00:20Z HEAD_SHA: " + "0" * 40])})
        with self.assertRaises(merge.Refusal):
            proof()

    def test_colon_form_mismatch_still_refuses(self):
        install({"log": log_text(bindings={"PR_NUMBER": "9999"}, sep=":")})
        with self.assertRaises(merge.Refusal):
            proof()

    def test_reviewed_parent_refuses_the_colon_form_exempt_shape(self):
        """RED: the superseded candidate only accepted KEY=value, so colon-form logs refused.

        The reviewed parent is pinned by commit, not by origin/main, so this witness
        stays valid after publication moves main."""
        rev = "5bde859adfc67fbe7bddcc7b67e22f097d3143a6"
        blob = subprocess.run(["git", "show", f"{rev}:scripts/pm/shared-assets/codex/skills/"
                               "heydonna-open-pr-status/scripts/merge.py"],
                              capture_output=True, text=True, cwd=HERE)
        self.assertEqual(blob.returncode, 0, blob.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "merge_reviewed_parent.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(blob.stdout)
            spec = importlib.util.spec_from_file_location("merge_reviewed_parent", path)
            parent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(parent)
            stub = Stub({"log": log_text(bindings=RealColonFormLogTests.REAL_BINDINGS, sep=":")})
            parent.api, parent.pages, parent.command = stub.api, stub.pages, stub.command
            with self.assertRaises(parent.Refusal) as ctx:
                parent.workflow_proof(pr(), WORKFLOW, NAMES)
            self.assertIn("WORKFLOW_NOT_GREEN", str(ctx.exception))


class NativeEventBindingTests(unittest.TestCase):
    """Replay native site/docs receipts without any live command or write."""

    @classmethod
    def setUpClass(cls):
        data = json.loads((pathlib.Path(HERE) / "fixtures/event-binding-native.json").read_text())
        cls.fixtures = data["fixtures"]
        cls.rules_content = data["rules_content"]

    def verify(self, fixture, *, direct=False):
        calls = []
        run_row, scope, pull = fixture["run"], fixture["scope"], fixture["pr"]
        expected_scope = "site" if fixture["number"] == 10522 else "control_plane_only"

        def api(path):
            calls.append(("GET", path))
            if path not in fixture["responses"]:
                raise merge.Refusal("UNRECORDED_READ")
            row = copy.deepcopy(fixture["responses"][path])
            if path.startswith("contents/"):
                row["content"] = (fixture.get("rules_content", self.rules_content)
                                  if "change-scope-rules.json?" in path else "")
            return row

        def pages(path, key):
            calls.append(("GET", path))
            if path.startswith("actions/workflows/ci-dummy.yml/runs?"):
                self.assertEqual(key, "workflow_runs")
                return [run_row]
            self.assertEqual(path, f"actions/runs/{run_row['id']}/jobs?filter=latest&per_page=100")
            self.assertEqual(key, "jobs")
            return [scope]

        def command(*args):
            calls.append(args)
            # job_log reads logs with `gh api --method GET` (60ab422 live sync):
            # the allowlist pins the live transport contract.
            allowed_log = ("gh", "api", "--method", "GET",
                           f"repos/{merge.REPO}/actions/jobs/{scope['id']}/logs")
            allowed_files = ("gh", "api", "--paginate", "--slurp",
                             f"repos/{merge.REPO}/pulls/{pull['number']}/files?per_page=100")
            self.assertIn(args, (allowed_log, allowed_files))
            return fixture["log"] if args == allowed_log else json.dumps(fixture["files"])

        with mock.patch.object(merge, "api", api), mock.patch.object(merge, "pages", pages), \
                mock.patch.object(merge, "command", command), \
                mock.patch.object(merge.subprocess, "run", side_effect=AssertionError("LIVE_COMMAND")):
            try:
                if direct:
                    result = merge.classifier_exemption(
                        pull, "ci-dummy.yml", run_row, scope, expected_scope=expected_scope)
                else:
                    result = merge.ci_exemption_proof(pull, expected_scope)
            except merge.Refusal:
                result = None
        self.assertTrue(all(call[0] in ("GET", "gh") for call in calls))
        return result

    def fixture(self):
        return copy.deepcopy(self.fixtures[0])

    @staticmethod
    def append(fixture, line):
        fixture["log"] += line + "\n"

    @staticmethod
    def change_receipt(fixture, **changes):
        lines = fixture["log"].splitlines()
        for index, line in enumerate(lines):
            body = merge.strip_log_framing(line)[0]
            if body.strip().startswith("{"):
                receipt = json.loads(body)
                receipt.update(changes)
                lines[index] = json.dumps(receipt)
        fixture["log"] = "\n".join(lines) + "\n"

    def test_both_actual_absent_event_receipts_are_accepted(self):
        for source in self.fixtures:
            with self.subTest(pr=source["number"]):
                fixture = copy.deepcopy(source)
                self.assertNotIn("EVENT_NAME", fixture["log"])
                self.assertNotIn("PR_NUMBER", fixture["log"])
                result = self.verify(fixture)
                self.assertIsNotNone(result)
                self.assertEqual(result["head"], fixture["pr"]["head"]["sha"])
                self.assertEqual(result["run"], fixture["run"]["id"])
                self.assertEqual(result["scope_job_id"], fixture["scope"]["id"])

    def test_valid_logged_event_agrees_with_native(self):
        for source in self.fixtures:
            fixture = copy.deepcopy(source)
            self.append(fixture, "EVENT_NAME: pull_request")
            self.assertIsNotNone(self.verify(fixture))

    def test_present_malformed_or_conflicting_event_never_becomes_absence(self):
        invalid = ("EVENT_NAME:", "EVENT_NAME=", "EVENT_NAME:   ",
                   "EVENT_NAME: pull_request extra", "EVENT_NAME: pull_request\textra",
                   "EVENT_NAME pull_request", "EVENT_NAME", "EVENT_NAME: push",
                   "EVENT_NAME: workflow_dispatch", "EVENT_NAME: pull_request_target",
                   "EVENT_NAME: pull_request\nEVENT_NAME: push",
                   "EVENT_NAME: pull_request\nEVENT_NAME:")
        for line in invalid:
            with self.subTest(line=line):
                fixture = self.fixture()
                self.append(fixture, line)
                self.assertIsNone(self.verify(fixture))

    def test_native_event_required_even_when_log_event_is_valid(self):
        for event in (None, "", "push", "workflow_dispatch", "pull_request_target", "unknown"):
            for logged in (False, True):
                with self.subTest(event=event, logged=logged):
                    fixture = self.fixture()
                    fixture["run"]["event"] = event
                    if logged:
                        self.append(fixture, "EVENT_NAME: pull_request")
                    self.assertIsNone(self.verify(fixture, direct=True))

    def test_absent_event_requires_only_this_native_pr_even_with_logged_pr_number(self):
        associations = (None, [], {}, [{}], ["malformed"], [{"number": 1}],
                        [{"number": 10522}, {"number": 10536}],
                        [{"number": 10522}, {"number": 10522}])
        for values in associations:
            for logged_pr in (False, True):
                with self.subTest(associations=values, logged_pr=logged_pr):
                    fixture = self.fixture()
                    fixture["run"]["pull_requests"] = values
                    if logged_pr:
                        self.append(fixture, "PR_NUMBER: 10522")
                    self.assertIsNone(self.verify(fixture))

    def test_logged_pr_number_rules_remain_unchanged(self):
        for line in ("PR_NUMBER:", "PR_NUMBER: 10522 extra", "PR_NUMBER: nope",
                     "PR_NUMBER: 10536", "PR_NUMBER: 10522\nPR_NUMBER: 10536"):
            fixture = self.fixture()
            self.append(fixture, line)
            self.assertIsNone(self.verify(fixture))
        fixture = self.fixture()
        self.append(fixture, "PR_NUMBER: 10522")
        self.assertIsNotNone(self.verify(fixture))
        # A present valid log retains the existing logged-PR-number path.
        self.append(fixture, "EVENT_NAME: pull_request")
        fixture["run"]["pull_requests"] = []
        self.assertIsNotNone(self.verify(fixture))

    def test_mandatory_bindings_and_native_workflow_identity_still_refuse_drift(self):
        for key in ("BASE_SHA", "HEAD_SHA", "WORKFLOW_SHA"):
            for operation in ("missing", "wrong", "conflict"):
                fixture = self.fixture()
                if operation == "missing":
                    fixture["log"] = "\n".join(
                        line for line in fixture["log"].splitlines() if key + ":" not in line)
                elif operation == "wrong":
                    fixture["log"] = "\n".join(
                        key + ": " + "0" * 40 if key + ":" in line else line
                        for line in fixture["log"].splitlines())
                else:
                    self.append(fixture, key + ": " + "0" * 40)
                self.assertIsNone(self.verify(fixture))
        for key, value in (("head_sha", "0" * 40), ("head_branch", "another-branch"),
                           ("path", ".github/workflows/ci.yml")):
            fixture = self.fixture()
            fixture["run"][key] = value
            self.assertIsNone(self.verify(fixture))
        fixture = self.fixture()
        commit = next(row for key, row in fixture["responses"].items() if key.startswith("git/commits/"))
        commit["parents"][1]["sha"] = "0" * 40
        self.assertIsNone(self.verify(fixture))

    def test_policy_identity_rules_digest_and_path_confinement_still_refuse(self):
        for fragment in ("ci-dummy.yml", "change_scope.py", "change-scope-rules.json"):
            fixture = self.fixture()
            key = next(key for key in fixture["responses"] if fragment + "?" in key)
            fixture["responses"][key]["sha"] = "0" * 40
            self.assertIsNone(self.verify(fixture))
        fixture = self.fixture()
        self.change_receipt(fixture, rules_sha256="0" * 64)
        self.assertIsNone(self.verify(fixture))
        fixture = self.fixture()
        fixture["rules_content"] = base64.b64encode(b"altered rules").decode()
        self.assertIsNone(self.verify(fixture))
        for item in ({"filename": "app/api/route.ts"},
                     {"filename": "website/renamed.js", "previous_filename": "app/old.ts"}):
            fixture = self.fixture()
            fixture["files"][0].append(item)
            self.assertIsNone(self.verify(fixture))

    def test_product_mixed_and_unknown_scope_never_become_exempt(self):
        for scope in ("product", "mixed", "unknown"):
            fixture = self.fixture()
            self.change_receipt(fixture, scope=scope)
            self.assertIsNone(self.verify(fixture))
        for key, value in (("product_changed", True), ("paid_ci_exempt", False),
                           ("ci_required", True), ("e2e_required", True)):
            fixture = self.fixture()
            self.change_receipt(fixture, **{key: value})
            self.assertIsNone(self.verify(fixture))

    def test_event_fallback_does_not_expand_app_test_only_policy(self):
        text = "\n".join(line for line in test_only_log().splitlines() if "EVENT_NAME" not in line)
        install({"test_only_log": text})
        native = test_only_run()
        native["pull_requests"] = [{"number": TEST_ONLY_NUMBER}]
        self.assertIsNone(merge.app_test_only_classifier_proof(
            test_only_pr(), "ci.yml", native, test_only_jobs()[0]))


GATE_STEP = "Validate unit lane and scheduled jobs"
PLACEHOLDER = "unit-tests (${{ matrix.shard }})"
GATE_ID = 8219101
SHARD_IDS = {shard: 8219110 + shard for shard in range(6)}
LANE_IDS = {"python": 8219120, "python-docx": 8219121, "python-contracts": 8219122}
LOG_STAMP = "2026-11-24T20:17:22.1174262Z"
FOOTER_0 = "= 1470 passed, 3 skipped, 10597 deselected, 678 warnings in 167.58s (0:02:47) =="
FOOTER_1 = "= 1167 passed, 3 skipped, 10900 deselected, 816 warnings in 213.04s (0:03:33) =="
FOOTER_1_SECOND = "======================== 17 passed, 1 warning in 3.31s ========================="


def zero_gate_log(receipt=None, env=None):
    """The unit gate's real log for a zero-JS schedule (run 45478680523 shape).

    Timestamped env lines, a multi-line UNIT_SCHEDULE object whose first line alone is
    timestamped, and one unit_jobs_verified event. A None override deletes that key.
    """
    run_id = str(test_only_run()["id"])
    rec = {"skip_matrix": True, "scheduled_count": 0, "mode": "zero",
           "zero_proof": "non_ts_rows_only", "reason": "zero_proof", "predicted_s": [],
           "head": TEST_ONLY_HEAD, "run_id": run_id, "run_attempt": "1"}
    facts = {"CI_EXECUTION_SHA": TEST_ONLY_HEAD, "UNIT_SELECT_RESULT": "success",
             "UNIT_PRECHECKS_RESULT": "success", "UNIT_TESTS_RESULT": "skipped",
             "UNIT_SCHEDULED_COUNT": "0", "EXPECTED_RUN_ID": run_id,
             "UNIT_JOB_HEAD_SHA": TEST_ONLY_HEAD}
    event = {"event": "unit_jobs_verified", "run_id": run_id, "head_sha": TEST_ONLY_HEAD,
             "declared_count": 0, "observed_jobs": []}
    for target, changes in ((rec, receipt), (facts, env)):
        for key, value in (changes or {}).items():
            target.pop(key, None) if value is None else target.__setitem__(key, value)
    body = json.dumps(rec, indent=2).splitlines()
    pairs = list(facts.items())
    lines = [f"{LOG_STAMP} env:"]
    lines += [f"{LOG_STAMP}   {key}: {value}" for key, value in pairs[:4]]
    lines += [f"{LOG_STAMP}   UNIT_SCHEDULE: {body[0]}"] + body[1:]
    lines += [f"{LOG_STAMP}   {key}: {value}" for key, value in pairs[4:]]
    lines += [f"{LOG_STAMP} ##[endgroup]", f"{LOG_STAMP} " + json.dumps(event, separators=(",", ":"))]
    return "\n".join(lines) + "\n"


# The `run:` text of each required test step in python_workflow() below. The Actions log echoes a
# step's script and nothing else names it, so these are how a log block is tied to its step.
SCRIPT_AUDIO = "python -m pytest modal/audio/tests"
SCRIPT_PROOFREADING = "python -m pytest scripts/proofreading"
SCRIPT_GC = "python -m pytest scripts/tests/test_modal_gc.py"
SCRIPT_DOCX = "python -m pytest modal/docx/tests"
SCRIPT_CONTRACTS = "node --test scripts/ci/tests/*.test.mjs"


def step_block(script, output):
    """One step of a job log as Actions writes it.

    `##[group]Run <first script line>` repeats the first line, the group body echoes the whole
    script, then the `shell:` / `env:` trailer, `##[endgroup]`, and finally the step's own output.
    """
    first, *rest = script.splitlines()
    lines = [f"{LOG_STAMP} ##[group]Run {first}", f"{LOG_STAMP} {first}"]
    lines += [f"{LOG_STAMP} {line}" for line in rest]
    lines += [f"{LOG_STAMP} shell: /usr/bin/bash -e {{0}}", f"{LOG_STAMP} env:",
              f"{LOG_STAMP}   CI: true", f"{LOG_STAMP} ##[endgroup]"]
    return "\n".join(lines) + "\n" + output


def pytest_log(*footers, script=SCRIPT_AUDIO):
    lines = [f"{LOG_STAMP} ============================= test session starts =============================",
             f"{LOG_STAMP} collected 1473 items"]
    lines += [f"{LOG_STAMP} {footer}" for footer in footers]
    return step_block(script, "\n".join(lines) + "\n")


def node_log(passed=35, failed=0, script=SCRIPT_CONTRACTS):
    lines = ["\u2714 contract one (1.2ms)", f"\u2139 tests {passed + failed}", "\u2139 suites 0",
             f"\u2139 pass {passed}", f"\u2139 fail {failed}", "\u2139 cancelled 0"]
    return step_block(script, "\n".join(f"{LOG_STAMP} {line}" for line in lines) + "\n")


def python_lane_log(proofreading=None, gc=None):
    """The `python` job's log: its two required steps, each with its own output, after a setup step."""
    setup = step_block("pip install -r requirements.txt", f"{LOG_STAMP} Successfully installed pytest\n")
    proofreading = pytest_log(FOOTER_0, script=SCRIPT_PROOFREADING) if proofreading is None else proofreading
    gc = pytest_log(FOOTER_1_SECOND, script=SCRIPT_GC) if gc is None else gc
    return setup + proofreading + gc


def python_workflow(matrix="shard: [0, 1, 2, 3, 4, 5]", modal_name=None, extra=""):
    """A minimal trusted ci.yml: the #14809 Python lanes, each gated on a lane_python output."""
    return f"""name: CI
jobs:
  scope:
    runs-on: ubuntu-latest
    steps:
      - name: Classify
        run: python3 scripts/ci/change_scope.py
  python-modal:
    needs: scope
    if: >
      (github.event_name == 'push' || needs.scope.outputs.python_changed != 'false') &&
      (github.event_name == 'push' || needs.scope.outputs.lane_python_modal != 'false')
    # six shards, files over 656s are item-split
    strategy:
      fail-fast: false
      matrix:
        {matrix}
{extra}{f"    name: {modal_name}" + chr(10) if modal_name else ""}    runs-on: ubuntu-latest
    steps:
      - name: Install Python test dependencies
        run: pip install -r requirements.txt
      - name: Modal audio unit tests
        run: python -m pytest modal/audio/tests
      - name: Modal scorecards unit tests
        if: matrix.shard == 0
        run: python -m pytest modal/scorecards/tests
  python:
    needs: scope
    if: needs.scope.outputs.lane_python != 'false'
    runs-on: ubuntu-latest
    steps:
      - name: Proofreading scorer unit tests
        run: python -m pytest scripts/proofreading
      - name: Modal GC unit tests
        run: python -m pytest scripts/tests/test_modal_gc.py
      - name: Optional experimental tests
        continue-on-error: true
        run: python -m pytest scripts/experimental
  python-docx:
    needs: scope
    if: needs.scope.outputs.lane_python_docx != 'false'
    runs-on: ubuntu-latest
    steps:
      - name: Modal docx unit tests
        run: python -m pytest modal/docx/tests
  python-contracts:
    needs: scope
    if: needs.scope.outputs.lane_python_contracts != 'false'
    runs-on: ubuntu-latest
    steps:
      - name: CI parallel workflow contracts
        run: node --test scripts/ci/tests/*.test.mjs
  unit-gate:
    needs: scope
    runs-on: ubuntu-latest
    steps:
      - name: Validate unit lane and scheduled jobs
        run: python3 scripts/ci/validate_unit_lane.py
"""


def zero_selection_jobs(shards=tuple(range(6)), lanes=tuple(LANE_IDS)):
    """#14809 shape: the one skipped native placeholder, an attesting gate, every Python lane."""
    run = test_only_run()
    common = {"status": "completed", "run_id": run["id"], "run_attempt": run["run_attempt"],
              "head_sha": run["head_sha"]}
    rows = [row for row in test_only_jobs() if row["name"] != "python"]
    rows.append(dict(common, id=GATE_ID - 1, name=PLACEHOLDER, conclusion="skipped", steps=[]))
    rows.append(dict(common, id=GATE_ID, name="unit-gate", conclusion="success",
                     steps=[{"name": GATE_STEP, "status": "completed", "conclusion": "success"}]))

    def done(name):
        return {"name": name, "status": "completed", "conclusion": "success"}

    for shard in shards:
        # The scorecards step is conditional: it runs on shard 0 only, as in the real workflow.
        steps = [done("Install Python test dependencies"), done("Modal audio unit tests"),
                 dict(done("Modal scorecards unit tests"),
                      conclusion="success" if shard == 0 else "skipped")]
        rows.append(dict(common, id=SHARD_IDS[shard], name=f"python-modal ({shard})",
                         conclusion="success", steps=steps))
    lane_steps = {"python": ["Proofreading scorer unit tests", "Modal GC unit tests",
                             "Optional experimental tests"],
                  "python-docx": ["Modal docx unit tests"],
                  "python-contracts": ["CI parallel workflow contracts"]}
    for lane in lanes:
        rows.append(dict(common, id=LANE_IDS[lane], name=lane, conclusion="success",
                         steps=[done(step) for step in lane_steps[lane]]))
    return rows


def lane_logs():
    """Passing logs for every Python job the fixture workflow schedules."""
    logs = {SHARD_IDS[shard]: pytest_log(FOOTER_0 if shard == 0 else FOOTER_1)
            for shard in range(6)}
    logs[SHARD_IDS[1]] = pytest_log(FOOTER_1, FOOTER_1_SECOND)
    logs[LANE_IDS["python"]] = python_lane_log()
    logs[LANE_IDS["python-docx"]] = pytest_log(FOOTER_1_SECOND, script=SCRIPT_DOCX)
    logs[LANE_IDS["python-contracts"]] = node_log()
    return logs


class PythonOnlyZeroSelectionTests(unittest.TestCase):
    """MoP 2026-11-25, #14809 (CTO C0ALZJHGE49 ts 1795553466.149459).

    A test-only PR whose Python test change schedules zero JavaScript shards leaves the
    native `unit-tests (${{ matrix.shard }})` placeholder skipped with no steps. The legacy
    "Unit tests" step check then refused a green run (TEST_EXECUTION_MISSING). That check is
    satisfied by selection only when the unit gate itself attests a zero schedule by file
    area and the Python lanes executed; everything else keeps refusing.
    """

    def world(self, **overrides):
        world = {"pr": test_only_pr(), "workflow_parent": TEST_ONLY_BASE,
                 "workflow_head": TEST_ONLY_HEAD, "ci_runs": [test_only_run()],
                 "jobs": zero_selection_jobs(), "test_only_log": test_only_log(),
                 "ci_yaml": python_workflow().encode(),
                 "app_test_files": [{"filename": "modal/audio/tests/test_x.py"}],
                 "e2e_runs": [],
                 "job_logs": {GATE_ID: zero_gate_log(), **lane_logs()}}
        world.update(overrides)
        return world

    def prove(self, **overrides):
        install(self.world(**overrides))
        return merge.workflow_proof(test_only_pr(), "ci.yml", NAMES)

    def edited(self, name, conclusion=None, step=None, base=None):
        """A copy of the run's jobs with one job (and optionally one of its steps) changed."""
        rows = copy.deepcopy(base if base is not None else zero_selection_jobs())
        for row in rows:
            if row["name"] != name:
                continue
            if conclusion is not None:
                row["conclusion"] = conclusion
            for entry in row["steps"]:
                if step is not None and entry["name"] == step[0]:
                    entry["conclusion"] = step[1]
        return rows

    def without(self, *names):
        return [row for row in zero_selection_jobs() if row["name"] not in names]

    def assert_refused(self, **overrides):
        with self.assertRaisesRegex(merge.Refusal, "TEST_EXECUTION_MISSING workflow=ci.yml"):
            self.prove(**overrides)

    def assert_not_accepted(self, **overrides):
        # The legacy `python` job is also a required name: when it is missing or red the run is
        # refused earlier as WORKFLOW_NOT_GREEN. Either way the exemption is not granted.
        with self.assertRaisesRegex(merge.Refusal,
                                    "(TEST_EXECUTION_MISSING|WORKFLOW_NOT_GREEN) workflow=ci.yml"):
            self.prove(**overrides)

    def test_zero_js_schedule_with_green_python_shards_is_accepted(self):
        result = self.prove()
        self.assertEqual(result["head"], TEST_ONLY_HEAD)
        self.assertIn("app_test_only", result)

    def test_uncertain_selection_still_refuses(self):
        # An uncertain skip is a fail-safe, not a selection by file area: its proof is empty.
        log = zero_gate_log(receipt={"mode": "uncertain", "zero_proof": None,
                                     "reason": "selector_unavailable"})
        self.assert_refused(job_logs=self.world()["job_logs"] | {GATE_ID: log})

    def test_zero_proof_outside_the_selector_set_still_refuses(self):
        log = zero_gate_log(receipt={"zero_proof": "anything_goes"})
        self.assert_refused(job_logs=self.world()["job_logs"] | {GATE_ID: log})

    def test_selected_js_shard_that_did_not_execute_still_refuses(self):
        rows = zero_selection_jobs()
        rows[:] = [row for row in rows if row["name"] != PLACEHOLDER]
        common = {"status": "completed", "run_id": test_only_run()["id"], "run_attempt": 1,
                  "head_sha": TEST_ONLY_HEAD}
        rows.append(dict(common, id=GATE_ID - 1, name="unit-tests (1)", conclusion="success",
                         steps=[{"name": "Set up job", "status": "completed",
                                 "conclusion": "success"}]))
        self.assert_refused(jobs=rows)

    def test_gate_for_another_head_still_refuses(self):
        log = zero_gate_log(receipt={"head": "0" * 40}, env={"UNIT_JOB_HEAD_SHA": "0" * 40})
        self.assert_refused(job_logs=self.world()["job_logs"] | {GATE_ID: log})

    def test_missing_gate_attestation_still_refuses(self):
        rows = [row for row in zero_selection_jobs() if row["name"] != "unit-gate"]
        self.assert_refused(jobs=rows)

    def test_python_shard_with_no_tests_executed_still_refuses(self):
        logs = self.world()["job_logs"] | {SHARD_IDS[0]: pytest_log("= no tests ran in 0.10s =")}
        self.assert_refused(job_logs=logs)

    def test_python_shard_with_a_failed_test_still_refuses(self):
        failed = "= 3 failed, 1470 passed, 3 skipped in 167.58s (0:02:47) =="
        logs = self.world()["job_logs"] | {SHARD_IDS[0]: pytest_log(failed)}
        self.assert_refused(job_logs=logs)

    def test_no_python_shard_ran_still_refuses(self):
        self.assert_refused(jobs=zero_selection_jobs(shards=()))

    def test_python_shard_unit_step_not_successful_still_refuses(self):
        self.assert_refused(jobs=self.edited("python-modal (0)", step=("Modal audio unit tests", "skipped")))

    def test_python_log_without_a_pytest_footer_still_refuses(self):
        logs = self.world()["job_logs"]
        logs.pop(SHARD_IDS[0])  # the stub falls back to a generic log with no pytest footer
        self.assert_refused(job_logs=logs)

    # --- MoP 2026-11-25, CTO REVISE on 7db0c18b (C0ALZJHGE49 ts 1795555038.168409) ---
    # The exemption must bind to the COMPLETE Python job set the authenticated workflow
    # schedules, read from the trusted ci.yml bytes, never from a hand-kept list.

    def test_all_six_shards_and_every_python_lane_are_accepted(self):
        rows = zero_selection_jobs()
        self.assertEqual([r["name"] for r in rows if r["name"].startswith("python-modal")],
                         [f"python-modal ({n})" for n in range(6)])
        # Shards 1-5 legitimately skip the shard-0-only scorecards step.
        skipped = [r["name"] for r in rows if r["name"].startswith("python-modal")
                   and any(s["conclusion"] == "skipped" for s in r["steps"])]
        self.assertEqual(len(skipped), 5)
        self.assertIn("app_test_only", self.prove())

    def test_each_required_shard_missing_refuses(self):
        for shard in range(6):
            with self.subTest(missing_shard=shard):
                self.assert_refused(jobs=self.without(f"python-modal ({shard})"))

    def test_each_required_shard_skipped_refuses(self):
        for shard in range(6):
            with self.subTest(skipped_shard=shard):
                name = f"python-modal ({shard})"
                rows = self.edited(name, conclusion="skipped")
                self.assert_refused(jobs=rows)

    def test_the_old_two_shard_subset_no_longer_clears_the_fallback(self):
        # 7db0c18b accepted shards 0 and 1 alone while the workflow schedules 0-5.
        self.assert_refused(jobs=zero_selection_jobs(shards=(0, 1)))

    def test_each_python_lane_job_missing_refuses(self):
        for lane in LANE_IDS:
            with self.subTest(missing_lane=lane):
                self.assert_not_accepted(
                    jobs=zero_selection_jobs(lanes=tuple(x for x in LANE_IDS if x != lane)))

    def test_a_python_lane_job_that_did_not_succeed_refuses(self):
        for lane in LANE_IDS:
            for conclusion in ("skipped", "failure", "cancelled"):
                with self.subTest(lane=lane, conclusion=conclusion):
                    self.assert_not_accepted(jobs=self.edited(lane, conclusion=conclusion))

    def test_a_shard_the_workflow_does_not_schedule_refuses(self):
        rows = zero_selection_jobs()
        rows.append(dict(copy.deepcopy(next(r for r in rows if r["name"] == "python-modal (1)")),
                         id=8219116, name="python-modal (6)"))
        self.assert_refused(jobs=rows)

    def test_a_duplicate_shard_row_refuses(self):
        rows = zero_selection_jobs()
        rows.append(dict(copy.deepcopy(next(r for r in rows if r["name"] == "python-modal (3)")), id=8219117))
        self.assert_refused(jobs=rows)

    def test_every_required_test_step_must_have_succeeded(self):
        cases = (("python-modal (3)", "Modal audio unit tests"),
                 ("python-modal (5)", "Modal audio unit tests"),
                 ("python", "Proofreading scorer unit tests"),
                 ("python", "Modal GC unit tests"),
                 ("python-docx", "Modal docx unit tests"),
                 ("python-contracts", "CI parallel workflow contracts"))
        for job, step in cases:
            with self.subTest(job=job, step=step):
                self.assert_refused(jobs=self.edited(job, step=(step, "skipped")))

    def test_a_missing_required_test_step_refuses(self):
        rows = self.edited("python-modal (2)")
        for row in rows:
            if row["name"] == "python-modal (2)":
                row["steps"] = [s for s in row["steps"] if s["name"] != "Modal audio unit tests"]
        self.assert_refused(jobs=rows)

    def test_conditional_and_continue_on_error_steps_are_not_required(self):
        # Scorecards (shard-0-only `if:`) and the continue-on-error step may skip or fail.
        rows = self.edited("python-modal (0)", step=("Modal scorecards unit tests", "skipped"))
        rows = self.edited("python", step=("Optional experimental tests", "failure"), base=rows)
        self.assertIn("app_test_only", self.prove(jobs=rows))

    def test_every_required_job_needs_nonzero_passing_results(self):
        none, broke = "= no tests ran in 0.10s =", "= 1 failed, 10 passed in 3.00s ="
        cases = [(SHARD_IDS[4], pytest_log(none)), (SHARD_IDS[5], pytest_log(broke)),
                 (LANE_IDS["python"], python_lane_log(proofreading=pytest_log(none, script=SCRIPT_PROOFREADING))),
                 (LANE_IDS["python"], python_lane_log(gc=pytest_log(broke, script=SCRIPT_GC))),
                 (LANE_IDS["python-docx"], pytest_log(broke, script=SCRIPT_DOCX)),
                 (LANE_IDS["python-contracts"], node_log(passed=0)),
                 (LANE_IDS["python-contracts"], node_log(passed=34, failed=1)),
                 # a node step whose block shows a pytest footer but no node summary
                 (LANE_IDS["python-contracts"], pytest_log(FOOTER_0, script=SCRIPT_CONTRACTS))]
        for job_id, text in cases:
            with self.subTest(job_id=job_id, log=text.splitlines()[-2][-40:]):
                self.assert_refused(job_logs=self.world()["job_logs"] | {job_id: text})

    # --- MoP 2026-11-25, CTO REVISE #2 (C0ALZJHGE49 ts 1795556637.302569) ---
    # A green step record is not proof the test command ran, and the job's pooled totals are not
    # per-step proof: each required step must carry its OWN nonzero passing result.

    def test_a_green_required_step_with_no_result_is_refused_while_its_sibling_passes(self):
        # NEGATIVE CONTROL. `python` has two required steps, both recorded green. The proofreading
        # step passes; the Modal GC step's block has no pytest footer at all. The old pooled count
        # saw the sibling's footer and accepted the job.
        silent_gc = step_block(SCRIPT_GC, f"{LOG_STAMP} (no test output)\n")
        logs = self.world()["job_logs"] | {LANE_IDS["python"]: python_lane_log(gc=silent_gc)}
        self.assert_refused(job_logs=logs)
        pooled = merge.pytest_totals(merge.strip_log_framing(logs[LANE_IDS["python"]]))
        self.assertGreater(pooled[0], 0, "the pooled totals alone would have vouched for the job")

    def test_a_required_step_with_no_block_in_the_log_is_refused(self):
        # The step record is green but its command never appears in the job log.
        logs = self.world()["job_logs"] | {
            LANE_IDS["python"]: python_lane_log(gc="")}
        self.assert_refused(job_logs=logs)

    def test_a_sibling_steps_footer_cannot_stand_in_for_another_step(self):
        # GC's block is empty but sits right before a passing block of ANOTHER step.
        silent_gc = step_block(SCRIPT_GC, "")
        other = pytest_log(FOOTER_1_SECOND, script=SCRIPT_DOCX)
        logs = self.world()["job_logs"] | {
            LANE_IDS["python"]: python_lane_log(gc=silent_gc + other)}
        self.assert_refused(job_logs=logs)

    def test_each_steps_own_counts_decide_not_the_jobs_total(self):
        # Passing in one step and failing in another is a failure even when passes dominate.
        failing_gc = pytest_log("= 1 failed, 2000 passed in 9.00s =", script=SCRIPT_GC)
        logs = self.world()["job_logs"] | {LANE_IDS["python"]: python_lane_log(gc=failing_gc)}
        self.assert_refused(job_logs=logs)

    def test_a_step_whose_script_appears_in_two_blocks_is_refused(self):
        twice = pytest_log(FOOTER_0, script=SCRIPT_PROOFREADING) * 2
        self.assert_refused(job_logs=self.world()["job_logs"] | {
            LANE_IDS["python"]: python_lane_log(proofreading=twice)})

    def test_a_longer_script_that_only_starts_like_the_step_is_not_the_step(self):
        # The same first line with an extra command appended is a different step.
        lookalike = pytest_log(FOOTER_0, script=SCRIPT_PROOFREADING + "\ntrue")
        self.assert_refused(job_logs=self.world()["job_logs"] | {
            LANE_IDS["python"]: python_lane_log(proofreading=lookalike)})

    def test_each_steps_output_is_split_at_the_next_group(self):
        lines = merge.strip_log_framing(self.world()["job_logs"][LANE_IDS["python"]])
        blocks = merge.log_step_blocks(lines)
        self.assertEqual([echo for echo, _ in blocks],
                         ["pip install -r requirements.txt", SCRIPT_PROOFREADING, SCRIPT_GC])
        by_script = {echo: merge.pytest_totals(output) for echo, output in blocks}
        # The last step's output runs to the end of the log, with no later group to close it.
        self.assertEqual(by_script, {"pip install -r requirements.txt": (0, 0),
                                     SCRIPT_PROOFREADING: (1470, 0), SCRIPT_GC: (17, 0)})

    def test_a_step_with_an_expression_in_its_script_matches_the_substituted_echo(self):
        # Actions echoes the script with ${{ }} already valued; the workflow text keeps the template.
        extra = """  python-newlane:
    needs: scope
    if: needs.scope.outputs.lane_python_newlane != 'false'
    runs-on: ubuntu-latest
    steps:
      - name: New lane unit tests
        run: |
          python -m pytest modal/newlane --junitxml=junit-${{ github.run_id }}.xml
          echo "attempt ${{ github.run_attempt }} done"
"""
        workflow = (python_workflow() + extra).encode()
        rows = zero_selection_jobs()
        rows.append(dict(copy.deepcopy(next(r for r in rows if r["name"] == "python-docx")),
                         id=8219130, name="python-newlane",
                         steps=[{"name": "New lane unit tests", "status": "completed",
                                 "conclusion": "success"}]))
        echoed = ("python -m pytest modal/newlane --junitxml=junit-45478680523.xml\n"
                  'echo "attempt 1 done"')
        logs = self.world()["job_logs"] | {8219130: pytest_log(FOOTER_0, script=echoed)}
        self.assertIn("app_test_only", self.prove(ci_yaml=workflow, jobs=rows, job_logs=logs))
        wrong = self.world()["job_logs"] | {
            8219130: pytest_log(FOOTER_0, script=echoed.replace("modal/newlane", "modal/other"))}
        self.assert_refused(ci_yaml=workflow, jobs=rows, job_logs=wrong)

    # --- MoP 2026-11-25, CTO REVISE #3 (C0ALZJHGE49 ts 1795557717.070049) ---
    # One logged execution cannot prove two required steps: each step needs its OWN block, so the
    # step-to-block assignment is one-to-one. Checking each step independently let two green step
    # records with the same command pass on a single passing block.

    def doubled_step_world(self, first, second):
        """The `python` lane with a second required step; `first` and `second` are the run scripts
        of its two GC-style steps (the plain fixture has one). Returns (ci.yml bytes, job rows)."""
        plain = "      - name: Modal GC unit tests\n        run: " + SCRIPT_GC + "\n"
        workflow = python_workflow().replace(
            plain, "      - name: Modal GC unit tests\n        run: " + first + "\n"
                   "      - name: Modal GC unit tests again\n        run: " + second + "\n")
        self.assertNotEqual(workflow, python_workflow())
        rows = zero_selection_jobs()
        for row in rows:
            if row["name"] == "python":
                row["steps"].append({"name": "Modal GC unit tests again", "status": "completed",
                                     "conclusion": "success"})
        return workflow.encode(), rows

    def doubled_logs(self, *gc_blocks):
        logs = self.world()["job_logs"]
        return logs | {LANE_IDS["python"]: python_lane_log(gc="".join(gc_blocks))}

    def test_two_green_steps_with_the_same_command_and_one_passing_block_are_refused(self):
        # NEGATIVE CONTROL. Both step records are green and both name the same command, but the
        # log holds one execution of it. Per-step matching found that one block for each step.
        workflow, rows = self.doubled_step_world(SCRIPT_GC, SCRIPT_GC)
        logs = self.doubled_logs(pytest_log(FOOTER_1_SECOND, script=SCRIPT_GC))
        blocks = merge.log_step_blocks(merge.strip_log_framing(logs[LANE_IDS["python"]]))
        self.assertIsNotNone(merge.step_block(blocks, SCRIPT_GC),
                             "each step alone would have found the same one block")
        self.assert_refused(ci_yaml=workflow, jobs=rows, job_logs=logs)

    def test_two_steps_with_the_same_command_and_two_blocks_are_still_refused(self):
        # Two executions, but a block cannot be told apart by script: neither is assignable.
        workflow, rows = self.doubled_step_world(SCRIPT_GC, SCRIPT_GC)
        twice = pytest_log(FOOTER_1_SECOND, script=SCRIPT_GC) * 2
        self.assert_refused(ci_yaml=workflow, jobs=rows, job_logs=self.doubled_logs(twice))

    def test_two_expression_templates_that_match_the_same_echo_are_refused(self):
        # NEGATIVE CONTROL. The two scripts differ in the template but both reduce to this echo.
        first = "python -m pytest scripts/tests/test_modal_gc.py --junitxml=gc-${{ github.run_id }}.xml"
        second = "python -m pytest scripts/tests/test_modal_gc.py --junitxml=gc-${{ github.run_attempt }}.xml"
        echoed = "python -m pytest scripts/tests/test_modal_gc.py --junitxml=gc-45478680523.xml"
        workflow, rows = self.doubled_step_world(first, second)
        logs = self.doubled_logs(pytest_log(FOOTER_1_SECOND, script=echoed))
        self.assert_refused(ci_yaml=workflow, jobs=rows, job_logs=logs)

    def test_two_expression_steps_each_with_their_own_block_are_accepted(self):
        # The one-to-one assignment still accepts the real shape: two templated steps, two blocks.
        first = "python -m pytest scripts/tests/test_modal_gc.py --junitxml=a-${{ github.run_id }}.xml"
        second = "python -m pytest scripts/tests/test_modal_gc.py --junitxml=b-${{ github.run_id }}.xml"
        workflow, rows = self.doubled_step_world(first, second)
        own = (pytest_log(FOOTER_1_SECOND, script=first.replace("${{ github.run_id }}", "45478680523"))
               + pytest_log(FOOTER_1_SECOND, script=second.replace("${{ github.run_id }}", "45478680523")))
        self.assertIn("app_test_only", self.prove(ci_yaml=workflow, jobs=rows, job_logs=self.doubled_logs(own)))
        # ...and the second step's block alone cannot vouch for the first step.
        only_second = pytest_log(FOOTER_1_SECOND, script=second.replace("${{ github.run_id }}", "45478680523"))
        self.assert_refused(ci_yaml=workflow, jobs=rows, job_logs=self.doubled_logs(only_second))

    def test_an_unreadable_job_log_refuses(self):
        # A shard whose log is absent falls back to the generic log, which has no pytest footer.
        logs = self.world()["job_logs"]
        logs.pop(SHARD_IDS[3])
        self.assert_refused(job_logs=logs)

    def test_workflow_bytes_come_from_the_classifier_workflow_sha(self):
        stub = install(self.world())
        merge.workflow_proof(test_only_pr(), "ci.yml", NAMES)
        self.assertIn(f"contents/.github/workflows/ci.yml?ref={TEST_ONLY_WORKFLOW_SHA}", stub.api_calls)

    def test_an_unreadable_or_unparseable_workflow_refuses(self):
        for bad in (b"x", b"", b"jobs:\n  python:\n    if: needs.scope.outputs.lane_python != 'false'\n"):
            with self.subTest(workflow=bad[:20]):
                self.assert_refused(ci_yaml=bad)

    def test_a_new_python_lane_in_the_workflow_is_required_without_editing_the_helper(self):
        # Rule 60: the required set is discovered from the workflow, not listed in merge.py.
        extra = """  python-newlane:
    needs: scope
    if: needs.scope.outputs.lane_python_newlane != 'false'
    runs-on: ubuntu-latest
    steps:
      - name: New lane unit tests
        run: python -m pytest modal/newlane
"""
        workflow = (python_workflow() + extra).encode()
        self.assert_refused(ci_yaml=workflow)  # the run has no such job
        rows = zero_selection_jobs()
        rows.append(dict(copy.deepcopy(next(r for r in rows if r["name"] == "python-docx")),
                         id=8219130, name="python-newlane",
                         steps=[{"name": "New lane unit tests", "status": "completed",
                                 "conclusion": "success"}]))
        logs = self.world()["job_logs"] | {8219130: pytest_log(FOOTER_0, script="python -m pytest modal/newlane")}
        self.assertIn("app_test_only", self.prove(ci_yaml=workflow, jobs=rows, job_logs=logs))

    def test_a_matrix_that_cannot_be_expanded_from_the_workflow_text_refuses(self):
        cases = {"expression": "shard: ${{ fromJSON(needs.scope.outputs.shards) }}",
                 "second axis": "shard: [0, 1]\n        os: [a, b]",
                 "repeated shard": "shard: [0, 1, 1]",
                 "non numeric": "shard: [a, b]"}
        for label, matrix in cases.items():
            with self.subTest(matrix=label):
                self.assert_refused(ci_yaml=python_workflow(matrix=matrix).encode(),
                                    jobs=zero_selection_jobs(shards=(0, 1)))
        with self.subTest(matrix="include"):
            workflow = python_workflow(matrix="shard: [0, 1]", extra="        include:\n          - shard: 2\n")
            self.assert_refused(ci_yaml=workflow.encode(), jobs=zero_selection_jobs(shards=(0, 1, 2)))
        with self.subTest(matrix="renamed job"):
            self.assert_refused(ci_yaml=python_workflow(modal_name="Audio ${{ matrix.shard }}").encode())


class PythonLaneRequirementTests(unittest.TestCase):
    """python_lane_requirements: the required job/step set read from workflow text alone."""

    def test_expands_the_literal_shards_and_names_the_unconditional_steps(self):
        required = merge.python_lane_requirements(python_workflow())
        self.assertEqual(sorted(required), sorted(
            [f"python-modal ({n})" for n in range(6)] + list(LANE_IDS)))
        self.assertEqual(required["python-modal (4)"], {"Modal audio unit tests": ({"pytest"}, SCRIPT_AUDIO)})
        self.assertEqual(required["python"], {
            "Proofreading scorer unit tests": ({"pytest"}, SCRIPT_PROOFREADING),
            "Modal GC unit tests": ({"pytest"}, SCRIPT_GC)})
        self.assertEqual(required["python-contracts"],
                         {"CI parallel workflow contracts": ({"node"}, SCRIPT_CONTRACTS)})

    def test_jobs_without_a_lane_python_gate_are_not_python_lanes(self):
        required = merge.python_lane_requirements(python_workflow())
        self.assertNotIn("unit-gate", required)
        self.assertNotIn("scope", required)

    def test_a_lane_with_no_unconditional_test_step_gives_no_answer(self):
        workflow = """jobs:
  python:
    if: needs.scope.outputs.lane_python != 'false'
    steps:
      - name: Only conditional
        if: always()
        run: python -m pytest x
"""
        self.assertIsNone(merge.python_lane_requirements(workflow))

    def test_a_workflow_with_no_python_lane_gives_no_answer(self):
        self.assertIsNone(merge.python_lane_requirements("jobs:\n  scope:\n    steps:\n      - run: true\n"))

    def test_a_job_defined_twice_gives_no_answer(self):
        self.assertIsNone(merge.python_lane_requirements(python_workflow() + python_workflow().split("jobs:\n", 1)[1]))

    @staticmethod
    def lane(step_lines):
        return ("jobs:\n  python:\n    if: needs.scope.outputs.lane_python != 'false'\n    steps:\n"
                + step_lines)

    def test_a_literal_block_script_keeps_comments_and_drops_trailing_blanks(self):
        # Actions echoes comment lines too, so they stay part of the step's identity.
        required = merge.python_lane_requirements(self.lane(
            "      - name: Tests\n        run: |\n          # warm the cache\n"
            "          python -m pytest x \\\n            -q\n\n"))
        self.assertEqual(required["python"]["Tests"][1], "# warm the cache\npython -m pytest x \\\n  -q")

    def test_a_test_step_whose_run_text_cannot_be_read_gives_no_answer(self):
        # A folded, quoted or multi-line plain value has no echoed form to match in the log.
        for label, run in (("folded", "run: >\n          python -m pytest x\n"),
                           ("quoted", 'run: "python -m pytest x"\n'),
                           ("plain continued", "run: python -m pytest x\n          -q\n"),
                           ("inline comment", "run: python -m pytest x # keep\n")):
            with self.subTest(form=label):
                self.assertIsNone(merge.python_lane_requirements(
                    self.lane(f"      - name: Tests\n        {run}")))

    def test_two_required_steps_with_the_same_name_give_no_answer(self):
        self.assertIsNone(merge.python_lane_requirements(self.lane(
            "      - name: Tests\n        run: python -m pytest a\n"
            "      - name: Tests\n        run: python -m pytest b\n")))


class ScriptMatchTests(unittest.TestCase):
    """script_matches / log_step_blocks: tying a log block to the step that produced it."""

    def test_a_literal_script_must_equal_the_echo(self):
        self.assertTrue(merge.script_matches("python -m pytest x", "python -m pytest x"))
        self.assertFalse(merge.script_matches("python -m pytest x", "python -m pytest x -q"))
        self.assertFalse(merge.script_matches("python -m pytest x", "echo hi\npython -m pytest x"))

    def test_expressions_match_any_value_but_the_literal_parts_are_fixed(self):
        template = "run --shard ${{ matrix.shard }} --of 6"
        self.assertTrue(merge.script_matches(template, "run --shard 3 --of 6"))
        self.assertTrue(merge.script_matches(template, "run --shard  --of 6"))
        self.assertFalse(merge.script_matches(template, "run --shard 3 --of 7"))
        self.assertFalse(merge.script_matches(template, "x run --shard 3 --of 6"))
        self.assertFalse(merge.script_matches(template, "run --shard 3"))

    def test_expression_parts_cannot_overlap(self):
        # The pieces must occur in order and without reusing text: "ab" needs both a and b.
        self.assertFalse(merge.script_matches("a${{ x }}a", "a"))
        self.assertTrue(merge.script_matches("a${{ x }}a", "aa"))

    def test_a_template_with_no_literal_text_matches_nothing(self):
        self.assertFalse(merge.script_matches("${{ matrix.cmd }}", "python -m pytest x"))
        self.assertFalse(merge.script_matches("", ""))

    def test_blocks_carry_their_own_output_up_to_the_next_group(self):
        log = (step_block("echo one", f"{LOG_STAMP} out one\n") + step_block("echo two\necho three", f"{LOG_STAMP} out two\n"))
        blocks = merge.log_step_blocks(merge.strip_log_framing(log))
        self.assertEqual([(echo, [line.strip() for line in out]) for echo, out in blocks],
                         [("echo one", ["out one"]), ("echo two\necho three", ["out two"])])


class StepAssignmentTests(unittest.TestCase):
    """steps_executed: required steps are assigned to log blocks one-to-one (REVISE #3)."""

    OK = [FOOTER_0]

    @staticmethod
    def step(script, kinds=("pytest",)):
        return (set(kinds), script)

    def test_each_step_with_its_own_passing_block_is_accepted(self):
        blocks = [("a", self.OK), ("b", self.OK), ("setup", [])]
        steps = {"A": self.step("a"), "B": self.step("b")}
        self.assertTrue(merge.steps_executed(blocks, steps))

    def test_two_steps_with_the_same_script_cannot_share_one_block(self):
        steps = {"A": self.step("a"), "B": self.step("a")}
        self.assertEqual(merge.step_block([("a", self.OK)], "a"), 0)
        self.assertFalse(merge.steps_executed([("a", self.OK)], steps))

    def test_two_templates_that_match_one_echo_cannot_share_it(self):
        steps = {"A": self.step("x ${{ a }}"), "B": self.step("x ${{ b }}")}
        self.assertFalse(merge.steps_executed([("x 1", self.OK)], steps))

    def test_a_block_claimed_twice_refuses_even_when_a_third_step_is_satisfied(self):
        blocks = [("a 1", self.OK), ("b", self.OK)]
        steps = {"A": self.step("a ${{ x }}"), "B": self.step("a ${{ y }}"), "C": self.step("b")}
        self.assertFalse(merge.steps_executed(blocks, steps))

    def test_a_step_with_no_block_refuses_even_when_its_sibling_has_one(self):
        steps = {"A": self.step("a"), "B": self.step("b")}
        self.assertFalse(merge.steps_executed([("a", self.OK)], steps))

    def test_a_step_with_two_candidate_blocks_refuses(self):
        self.assertFalse(merge.steps_executed([("a", self.OK), ("a", self.OK)], {"A": self.step("a")}))

    def test_distinct_blocks_still_need_their_own_passing_result(self):
        blocks = [("a", self.OK), ("b", ["= 1 failed, 5 passed in 1.00s ="])]
        steps = {"A": self.step("a"), "B": self.step("b")}
        self.assertFalse(merge.steps_executed(blocks, steps))


class NodeSummaryTests(unittest.TestCase):
    def test_node_summary_lines_are_matched_after_log_framing(self):
        lines = merge.strip_log_framing(node_log(passed=35))
        found = {m.group(1): int(m.group(2)) for m in map(merge.NODE_SUMMARY.match, map(str.strip, lines)) if m}
        self.assertEqual(found, {"pass": 35, "fail": 0})

    def test_a_line_that_merely_mentions_pass_is_not_a_summary(self):
        self.assertIsNone(merge.NODE_SUMMARY.match("\u2139 pass 35 of them"))
        self.assertIsNone(merge.NODE_SUMMARY.match("tests pass 35"))


class PytestFooterTests(unittest.TestCase):
    def test_footers_sum_across_framed_and_quiet_forms(self):
        lines = merge.strip_log_framing(pytest_log(FOOTER_1, FOOTER_1_SECOND))
        self.assertEqual(merge.pytest_totals(lines), (1184, 0))

    def test_failed_and_errored_counts_are_reported(self):
        lines = ["= 2 failed, 5 passed, 1 error in 3.00s =", "5 passed, 1 xfailed in 1.00s"]
        self.assertEqual(merge.pytest_totals(lines), (10, 3))

    def test_a_line_that_merely_mentions_passed_is_not_a_footer(self):
        self.assertEqual(merge.pytest_totals(["tests/test_x.py::t 5 passed in the log"]), (0, 0))


class ManifestMappingTests(unittest.TestCase):
    """The repaired merge executable must be install-activatable at the Codex path."""

    ROOT = pathlib.Path(__file__).resolve().parents[7]
    SOURCE_PATH = "codex/skills/heydonna-open-pr-status/scripts/merge.py"
    TARGET = "/Users/rajiv/.codex/skills/heydonna-open-pr-status/scripts/merge.py"

    def manifest_row(self):
        manifest = json.loads((self.ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json").read_text())
        rows = [e for e in manifest["entries"] if e["source_path"] == self.SOURCE_PATH]
        self.assertEqual(len(rows), 1, "exactly one mapping row for the merge executable")
        return rows[0]

    def test_source_exists_and_digest_matches_candidate_bytes(self):
        source = self.ROOT / "scripts" / "pm" / "shared-assets" / self.SOURCE_PATH
        self.assertTrue(source.is_file())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), self.manifest_row()["sha256"])

    def test_canonical_target_is_the_live_codex_executable_never_claude(self):
        target = self.manifest_row()["canonical_target"]
        self.assertEqual(target, self.TARGET)
        self.assertNotIn("/.claude/", target)
        self.assertTrue(target.startswith("/Users/rajiv/.codex/skills/"))

    def test_row_metadata_matches_adjacent_codex_skill_conventions(self):
        row = self.manifest_row()
        self.assertEqual(row["mode"], 420)                      # live file is 0644
        self.assertEqual(row["ownership_class"], "shared-codex-skill-executable")
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["dependencies"], [])

    def test_the_test_file_is_not_mapped(self):
        manifest = json.loads((self.ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json").read_text())
        paths = {e["source_path"] for e in manifest["entries"]}
        self.assertNotIn("codex/skills/heydonna-open-pr-status/scripts/test_merge_classifier_exemption.py", paths)


if __name__ == "__main__":
    unittest.main()
