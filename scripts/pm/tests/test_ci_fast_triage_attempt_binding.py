from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "shared-assets" / "claude" / "scripts" / "ci" / "ci-fast-triage.py"
SPEC = importlib.util.spec_from_file_location("ci_fast_triage_candidate", SOURCE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


HEAD = "f109414c02cc296510103fe2c090ce964e9b9dfb"
RUN_ID = 33397393224
PR = 7591


def run(*, attempt: int, conclusion: str = "failure") -> dict:
    return {
        "id": RUN_ID,
        "name": "E2E Smoke Tests",
        "conclusion": conclusion,
        "status": "completed",
        "event": "pull_request",
        "head_branch": "fix/7591",
        "head_sha": HEAD,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:10:00Z",
        "run_attempt": attempt,
        "html_url": "https://example.test/runs/33397393224",
        "pull_requests": [{"number": PR}],
    }


def job(job_id: int, attempt: int, *, message: str) -> dict:
    return {
        "id": job_id,
        "name": "e2e",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": attempt,
        "steps": [],
        "message": message,
    }


class AttemptBindingTests(unittest.TestCase):
    def test_two_attempts_selects_current_jobs_and_named_assertion(self) -> None:
        calls: list[str] = []
        detail = run(attempt=2)
        jobs = [
            job(99505379453, 1, message="timeout-or-wall-budget"),
            job(99594816424, 2, message="auto-process-regression.spec.ts:1675 save-admission assertion"),
        ]

        class API:
            def get(self, path: str):
                calls.append(path)
                if path == f"actions/runs/{RUN_ID}/jobs?filter=all&per_page=100":
                    return {"jobs": jobs}
                if path == f"pulls/{PR}":
                    return {"number": PR, "state": "open", "head": {"sha": HEAD}}
                if path == f"check-runs/{99594816424}/annotations?per_page=100":
                    return [{
                        "path": "tests/e2e/specs/auto-process-regression.spec.ts",
                        "start_line": 1675,
                        "annotation_level": "failure",
                        "title": "save admission",
                        "message": "save-admission assertion failed",
                    }]
                if path == f"check-runs/{99505379453}/annotations?per_page=100":
                    raise AssertionError("stale attempt annotation was consumed")
                if path.startswith(f"issues/{PR}/comments?"):
                    return []
                raise AssertionError(path)

            def get_text(self, path: str):
                raise AssertionError(f"unexpected log fallback: {path}")

        result = MODULE.triage_run(API(), RUN_ID, run_detail=detail)
        self.assertEqual(result["run_attempt"], 2)
        self.assertEqual(result["attempt_provenance"]["job_ids"], ["99594816424"])
        self.assertEqual(
            result["fingerprint"]["paths"],
            ["tests/e2e/specs/auto-process-regression.spec.ts"],
        )
        self.assertTrue(any(call.startswith(f"check-runs/{99594816424}/annotations") for call in calls))
        self.assertNotIn(f"check-runs/{99505379453}/annotations?per_page=100", calls)

    def test_single_attempt_remains_actionable(self) -> None:
        detail = run(attempt=1)
        current_job = job(200, 1, message="named assertion")

        class API:
            def get(self, path: str):
                if path == f"actions/runs/{RUN_ID}/jobs?filter=all&per_page=100":
                    return {"jobs": [current_job]}
                if path == f"pulls/{PR}":
                    return {"number": PR, "state": "open", "head": {"sha": HEAD}}
                if path == "check-runs/200/annotations?per_page=100":
                    return [{"annotation_level": "failure", "message": "named assertion"}]
                if path.startswith(f"issues/{PR}/comments?"):
                    return []
                raise AssertionError(path)

            def get_text(self, path: str):
                raise AssertionError(path)

        result = MODULE.triage_run(API(), RUN_ID, run_detail=detail)
        self.assertTrue(result["actionable"])
        self.assertEqual(result["run_attempt"], 1)

    def test_missing_run_attempt_is_a_typed_fail_closed_error(self) -> None:
        detail = run(attempt=1)
        detail.pop("run_attempt")

        class API:
            def get(self, path: str):
                raise AssertionError(f"no downstream read after missing attempt: {path}")

        with self.assertRaisesRegex(RuntimeError, "run attempt provenance"):
            MODULE.triage_run(API(), RUN_ID, run_detail=detail)

    def test_missing_job_attempt_is_a_typed_fail_closed_error(self) -> None:
        detail = run(attempt=2)

        class API:
            def get(self, path: str):
                if path == f"actions/runs/{RUN_ID}/jobs?filter=all&per_page=100":
                    return {"jobs": [job(1, 2, message="x") | {"run_attempt": None}]}
                raise AssertionError(path)

        with self.assertRaisesRegex(RuntimeError, "workflow job attempt provenance"):
            MODULE.triage_run(API(), RUN_ID, run_detail=detail)

    def test_watchdog_uses_detail_attempt_for_output_and_annotation_binding(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        old = (now - dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        listed = run(attempt=1)
        listed["created_at"] = old
        listed["updated_at"] = old
        detail = run(attempt=2)
        detail["created_at"] = old
        detail["updated_at"] = old
        current_job = job(200, 2, message="auto-process-regression.spec.ts:1675")

        class API:
            def get(self, path: str):
                if path == "actions/workflows?per_page=100":
                    return {"workflows": [
                        {"id": 101, "path": ".github/workflows/ci.yml"},
                        {"id": 102, "path": ".github/workflows/e2e.yml"},
                    ]}
                if path.startswith("actions/workflows/101/runs?"):
                    return {"workflow_runs": []}
                if path.startswith("actions/workflows/102/runs?"):
                    return {"workflow_runs": [listed]}
                if path == f"actions/runs/{RUN_ID}":
                    return detail
                if path == f"pulls/{PR}":
                    return {"number": PR, "state": "open", "head": {"sha": HEAD}}
                if path.startswith(f"issues/{PR}/comments?"):
                    return []
                if path == f"actions/runs/{RUN_ID}/jobs?filter=all&per_page=100":
                    return {"jobs": [
                        job(100, 1, message="timeout-or-wall-budget"),
                        current_job,
                    ]}
                if path == "check-runs/200/annotations?per_page=100":
                    return [{
                        "path": "tests/e2e/specs/auto-process-regression.spec.ts",
                        "start_line": 1675,
                        "annotation_level": "failure",
                        "message": "save-admission assertion failed",
                    }]
                if path == "check-runs/100/annotations?per_page=100":
                    raise AssertionError("watchdog consumed stale attempt annotation")
                raise AssertionError(path)

            def get_text(self, path: str):
                raise AssertionError(path)

        result = MODULE.watchdog(API(), older_than_minutes=5, lookback_hours=6)
        self.assertFalse(result["degraded"])
        self.assertEqual(result["missing_verdicts"][0]["attempt"], 2)
        self.assertEqual(result["missing_verdicts"][0]["category"], "test-failure")


if __name__ == "__main__":
    unittest.main()
