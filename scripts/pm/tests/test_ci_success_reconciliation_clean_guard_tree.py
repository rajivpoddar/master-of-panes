from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
RECONCILER = (
    ROOT
    / "scripts/pm/shared-assets/claude/scripts/ci-success-reconciliation.py"
)
SPEC = importlib.util.spec_from_file_location("ci_success_reconciliation", RECONCILER)
assert SPEC and SPEC.loader
RECONCILER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECONCILER_MODULE)

HEAD = "e" * 40
GREEN = (
    f"MERGE_GUARD: PASS pr=8156 head={HEAD} "
    "[CI] PASS success_run=35858112396 required_job=test; "
    "[E2E Smoke Tests] PASS success_run=35858112464 required_job=e2e;"
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_checkout(tmp: Path) -> tuple[Path, Path]:
    origin = tmp / "origin.git"
    checkout = tmp / "checkout"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "clone", str(origin), str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(checkout, "config", "user.name", "fixture")
    git(checkout, "config", "user.email", "fixture@example.invalid")
    (checkout / "scripts/ci").mkdir(parents=True)
    rules = checkout / "scripts/ci/change-scope-rules.json"
    rules.write_text('{"site":["website/**"]}\n', encoding="utf-8")
    guard = checkout / "scripts/ci/pre-merge-current-head-ci-guard.sh"
    guard.write_text(
        "#!/bin/sh\n"
        "if ! git diff --cached --quiet -- scripts/ci/change-scope-rules.json; then\n"
        "  echo 'MERGE_GUARD: BLOCKED reason=change_scope rules digest mismatch'\n"
        "  exit 1\n"
        "fi\n"
        f"echo '{GREEN}'\n",
        encoding="utf-8",
    )
    guard.chmod(0o755)
    git(checkout, "add", "scripts/ci/change-scope-rules.json", "scripts/ci/pre-merge-current-head-ci-guard.sh")
    git(checkout, "commit", "-m", "fixture baseline")
    git(checkout, "push", "origin", "main")
    return checkout, guard


class CleanGuardTreeTests(unittest.TestCase):
    def test_staged_rule_drift_is_red_in_operator_tree_and_green_from_origin_main(self):
        with tempfile.TemporaryDirectory(prefix="ci-success-clean-guard-test-") as temp:
            checkout, guard = make_checkout(Path(temp))
            rules = checkout / "scripts/ci/change-scope-rules.json"
            rules.write_text('{"site":["website/**","docs-site/**"]}\n', encoding="utf-8")
            git(checkout, "add", "scripts/ci/change-scope-rules.json")

            red = subprocess.run(
                [str(guard), "--mode", "promotion", "8156"],
                cwd=checkout,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(red.returncode, 0)
            self.assertIn("change_scope rules digest mismatch", red.stdout)

            proof = RECONCILER_MODULE.exact_green(guard, 8156, cwd=checkout)
            self.assertEqual(proof[:3], (HEAD, 35858112396, 35858112464))
            self.assertIn("success_run=35858112464", proof[3])
            self.assertIn("M  scripts/ci/change-scope-rules.json", git(checkout, "status", "--short"))

    def test_unavailable_origin_main_fails_with_a_typed_error(self):
        with tempfile.TemporaryDirectory(prefix="ci-success-no-origin-test-") as temp:
            checkout = Path(temp)
            subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
            guard = checkout / "guard.sh"
            guard.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            guard.chmod(0o755)
            with self.assertRaisesRegex(OSError, "CI_GUARD_CLEAN_TREE_UNAVAILABLE"):
                RECONCILER_MODULE.exact_green(guard, 8156, cwd=checkout)


if __name__ == "__main__":
    unittest.main()
