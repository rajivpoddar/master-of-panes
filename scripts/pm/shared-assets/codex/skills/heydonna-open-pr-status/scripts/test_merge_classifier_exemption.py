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
RULES = b'{"version":1,"control_plane":["scripts/pm/**"]}\n'
RULES_SHA = hashlib.sha256(RULES).hexdigest()
TRUSTED = (".github/workflows/ci.yml", "scripts/ci/change_scope.py", "scripts/ci/change-scope-rules.json")


def pr():
    return {"number": NUMBER, "head": {"sha": HEAD, "ref": REF,
            "repo": {"full_name": "heydonna-app/heydonna-app"}},
            "state": "open", "draft": False, "base": {"ref": "main"}, "labels": []}


def run(conclusion="success"):
    return {"id": RUN_ID, "run_attempt": 1, "head_sha": HEAD, "event": "pull_request",
            "head_branch": REF, "path": f".github/workflows/{WORKFLOW}",
            "status": "completed", "conclusion": conclusion}


def jobs(scope_conclusion="success", substantive="skipped"):
    out = [{"id": SCOPE_ID, "name": "classify-change-scope", "status": "completed",
            "conclusion": scope_conclusion, "steps": [{"name": "classify", "status": "completed", "conclusion": scope_conclusion}]}]
    for name in NAMES:
        out.append({"id": 500 + len(out), "name": name, "status": "completed",
                    "conclusion": substantive, "steps": []})
    return out


def log_text(receipt=None, bindings=None, extra_lines=()):
    base = {"BASE_SHA": BASE, "HEAD_SHA": HEAD, "WORKFLOW_SHA": WORKFLOW_SHA,
            "EVENT_NAME": "pull_request", "PR_NUMBER": str(NUMBER)}
    base.update(bindings or {})
    lines = [f"2026-09-22T10:00:0{i}Z {k}={v}" for i, (k, v) in enumerate(base.items())]
    body = receipt if receipt is not None else dict(
        schema_version=1, scope="control_plane_only", paid_ci_exempt=True,
        control_plane_only=True, product_changed=False, ci_required=False,
        e2e_required=False, rules_sha256=RULES_SHA)
    lines.append("2026-09-22T10:00:09Z " + json.dumps(body, sort_keys=True))
    lines.extend(extra_lines)
    return "\x1b[36m" + "\n".join(lines) + "\x1b[0m\n"


class Stub:
    def __init__(self, world):
        self.world = world

    def api(self, path):
        w = self.world
        if path.startswith("git/commits/"):
            sha = path.split("/", 2)[2]
            if sha != w.get("workflow_sha", WORKFLOW_SHA) or w.get("bad_parents"):
                parents = [{"sha": "0" * 40}, {"sha": HEAD}]
            else:
                parents = [{"sha": BASE}, {"sha": HEAD}]
            return {"sha": sha, "parents": parents}
        if path.startswith("contents/"):
            rel, _, ref = path[len("contents/"):].partition("?ref=")
            if rel in w.get("missing_paths", ()):
                raise merge.Refusal("CONTENTS_MISSING")
            blob = "blob-" + rel
            if w.get("blob_mismatch") and ref == WORKFLOW_SHA:
                blob += "-drifted"
            content = base64.b64encode(RULES if rel.endswith("rules.json") else b"x").decode()
            if w.get("rules_bytes") is not None and rel.endswith("rules.json") and ref == WORKFLOW_SHA:
                content = base64.b64encode(w["rules_bytes"]).decode()
            return {"sha": blob, "content": content}
        raise AssertionError(path)

    def pages(self, path, key):
        if "/runs?" in path:
            return [self.world.get("run", run())]
        if "/jobs?" in path:
            return self.world.get("jobs", jobs())
        raise AssertionError(path)

    def command(self, *args):
        if self.world.get("log_failure"):
            raise merge.Refusal("LOG_FETCH_FAILED")
        return self.world.get("log", log_text())


def install(world=None):
    world = {} if world is None else world
    stub = Stub(world)
    merge.api, merge.pages, merge.command = stub.api, stub.pages, stub.command
    return world


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
    def test_reviewed_parent_refuses_the_exempt_shape(self):
        blob = subprocess.run(["git", "show", "origin/main:scripts/pm/shared-assets/codex/skills/"
                               "heydonna-open-pr-status/scripts/merge.py"],
                              capture_output=True, text=True, cwd=HERE)
        self.assertEqual(blob.returncode, 0, blob.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "merge_parent.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(blob.stdout)
            spec = importlib.util.spec_from_file_location("merge_parent", path)
            parent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(parent)
            stub = Stub({})
            parent.api, parent.pages = stub.api, stub.pages
            with self.assertRaises(parent.Refusal) as ctx:
                parent.workflow_proof(pr(), WORKFLOW, NAMES)
            self.assertIn("WORKFLOW_NOT_GREEN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
