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


def pr():
    return {"number": NUMBER, "head": {"sha": HEAD, "ref": REF,
            "repo": {"full_name": "heydonna-app/heydonna-app"}},
            "state": "open", "draft": False, "base": {"ref": "main"}, "labels": []}


def run(conclusion="success"):
    return {"id": RUN_ID, "run_attempt": 1, "head_sha": HEAD, "event": "pull_request",
            "head_branch": REF, "path": f".github/workflows/{WORKFLOW}",
            "status": "completed", "conclusion": conclusion}


def site_run(conclusion="success"):
    return {"id": SITE_RUN_ID, "run_attempt": 1, "head_sha": HEAD, "event": "pull_request",
            "head_branch": REF, "path": ".github/workflows/ci-dummy.yml",
            "status": "completed", "conclusion": conclusion}


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
            "conclusion": conclusion}


def test_only_jobs():
    rows = jobs(scope_conclusion="success", substantive="success")
    for row in rows:
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
            "rules_sha256": RULES_SHA}
    body.update(receipt or {})
    values = {"BASE_SHA": TEST_ONLY_BASE, "HEAD_SHA": TEST_ONLY_HEAD,
              "WORKFLOW_SHA": TEST_ONLY_WORKFLOW_SHA,
              "EVENT_NAME": "pull_request", "PR_NUMBER": str(TEST_ONLY_NUMBER)}
    values.update(bindings or {})
    return "\n".join([*(f"{key}={value}" for key, value in values.items()),
                       json.dumps(body, sort_keys=True)]) + "\n"


def jobs(scope_conclusion="success", substantive="skipped"):
    out = [{"id": SCOPE_ID, "name": "classify-change-scope", "status": "completed",
            "conclusion": scope_conclusion, "steps": [{"name": "classify", "status": "completed", "conclusion": scope_conclusion}]}]
    for name in NAMES:
        out.append({"id": 500 + len(out), "name": name, "status": "completed",
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

    def api(self, path):
        w = self.world
        if path.startswith("pulls/"):
            return w.get("pr", pr())
        if path == "git/ref/heads/main":
            return {"object": {"sha": w.get("main", BASE)}}
        if path.startswith("compare/"):
            return {"merge_base_commit": {"sha": w.get("main", BASE)},
                    "html_url": "https://example.invalid/compare", "files": []}
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
            content = base64.b64encode(RULES if rel.endswith("rules.json") else b"x").decode()
            if (w.get("rules_bytes") is not None and rel.endswith("rules.json")
                    and ref in (WORKFLOW_SHA, TEST_ONLY_WORKFLOW_SHA)):
                content = base64.b64encode(w["rules_bytes"]).decode()
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
        return self.world.get("log", log_text())


def install(world=None):
    world = {} if world is None else world
    stub = Stub(world)
    merge.api, merge.pages, merge.command = stub.api, stub.pages, stub.command
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

    def test_exact_app_test_only_receipt_allows_absent_e2e_with_green_ci(self):
        install(self.world())
        result = merge.merge(TEST_ONLY_NUMBER, TEST_ONLY_HEAD)
        self.assertEqual(result["status"], "READY_TO_MERGE")
        self.assertTrue(result["workflows"]["ci.yml"]["app_test_only"])
        self.assertEqual(result["workflows"]["e2e.yml"]["exempt"], "app_test_only")
        self.assertIsNone(result["workflows"]["e2e.yml"]["run"])

    def test_exact_app_test_only_receipt_allows_successful_explicit_e2e_skip(self):
        skipped = test_only_run(path="e2e.yml")
        install(self.world(e2e_runs=[skipped], e2e_jobs=[
            {"id": 8219003, "name": "e2e", "status": "completed",
             "conclusion": "skipped", "steps": []}]))
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
        self.assertIn("WORKFLOW_MISSING workflow=e2e.yml", str(ctx.exception))


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
