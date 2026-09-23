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
RULES = b'{"version":1,"control_plane":["scripts/pm/**"],"site":["website/**","docs-site/**"]}\n'
RULES_SHA = hashlib.sha256(RULES).hexdigest()
TRUSTED = (".github/workflows/ci.yml", "scripts/ci/change_scope.py", "scripts/ci/change-scope-rules.json")
SITE_RUN_ID = 222
SITE_SCOPE_ID = 333
SITE_HEAD = "ea3d429342e6e53df344115aa651a5bb5d28cd1a"
SITE_BASE = "350ddb05" + "0" * 32
ADVANCED_MAIN = "edc414c" + "1" * 33


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
            if sha != w.get("workflow_sha", WORKFLOW_SHA) or w.get("bad_parents"):
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
            if w.get("blob_mismatch") and ref == WORKFLOW_SHA:
                blob += "-drifted"
            content = base64.b64encode(RULES if rel.endswith("rules.json") else b"x").decode()
            if w.get("rules_bytes") is not None and rel.endswith("rules.json") and ref == WORKFLOW_SHA:
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
                e2e_run = copy.deepcopy(self.world.get("run", run()))
                e2e_run["path"] = ".github/workflows/e2e.yml"
                return self.world.get("e2e_runs", [e2e_run])
            return [self.world.get("run", run())]
        if "/jobs?" in path:
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
            return json.dumps([files])
        if f"/actions/jobs/{SITE_SCOPE_ID}/logs" in path:
            return self.world.get("site_log", site_log_text())
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
