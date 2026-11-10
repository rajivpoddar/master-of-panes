"""Hourly collector regressions; no live SSH/GitHub/production calls."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import io
from contextlib import redirect_stdout, redirect_stderr
import threading
from unittest.mock import patch
from datetime import datetime, timezone

SOURCE = Path(__file__).resolve().parents[1] / "pm/heartbeat/heartbeat-compose.py"
spec = importlib.util.spec_from_file_location("hourly_compose", SOURCE)
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)


class HourlyCollectors(unittest.TestCase):
    def test_timeout_uses_cached_value_and_original_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            at = time.time() - 125
            (root / "training.json").write_text(json.dumps({"at": at, "value": "RUNNING GPU 40%"}))
            def timeout():
                raise subprocess.TimeoutExpired("ssh", 30)
            values, stale = compose.parallel_collect({"training": timeout}, cache_dir=root)
            self.assertEqual(values["training"], "RUNNING GPU 40%")
            self.assertIn("as of", stale["training"])
            self.assertIn("live check timed out", stale["training"])
            self.assertIn("age 2m", stale["training"])
            self.assertEqual(json.loads((root / "training.json").read_text())["at"], at)

    def test_expired_cache_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.json").write_text(json.dumps({"at": time.time() - 21601, "value": "old"}))
            values, stale = compose.parallel_collect({"a": lambda: None}, cache_dir=root)
            self.assertIsNone(values["a"])
            self.assertFalse(stale)

    def test_parallel_jobs_finish_inside_wall_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            def slow():
                time.sleep(.15)
                return "ok"
            start = time.monotonic()
            values, _ = compose.parallel_collect({str(i): slow for i in range(5)}, wall_cap=.5, cache_dir=Path(tmp))
            self.assertLess(time.monotonic() - start, .5)
            self.assertEqual(list(values.values()), ["ok"] * 5)

    def test_stuck_worker_cannot_extend_wall_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            def stuck():
                time.sleep(1)
                return "late"
            start = time.monotonic()
            values, _ = compose.parallel_collect({"stuck": stuck}, wall_cap=.05, cache_dir=Path(tmp))
            self.assertLess(time.monotonic() - start, .3)
            self.assertIsNone(values["stuck"])
            self.assertFalse((Path(tmp) / "stuck.json").exists())

    @staticmethod
    def probe_output(gpu="72 %, 29123 MiB", containers="sindhu-train Up 3 hours", processes="python -u /work/train.py"):
        return f"{gpu}\n__CONTAINERS__\n{containers}\n__PROCESSES__\n{processes}\n__END__\n"

    def test_running_idle_between_steps(self):
        for gpu in ("72 %, 29123 MiB", "0 %, 1000 MiB"):
            doc = compose.parse_training_output(self.probe_output(gpu=gpu))
            line, actions = compose.training_line(probe=(doc, ""))
            self.assertIn("Training:* RUNNING", line)
            self.assertEqual(actions, [])

    def test_expected_recorded_container_is_live_without_gpu_activity(self):
        doc = compose.parse_training_output(self.probe_output(gpu="0 %, 1000 MiB", containers="run-123 Up 1 hour", processes="/sbin/init"), "run-123")
        self.assertTrue(doc["running"])

    def test_foreign_container_and_busy_gpu_are_not_training(self):
        doc = compose.parse_training_output(self.probe_output(containers="foreign Up 3 hours", processes="/sbin/init"))
        self.assertFalse(doc["running"])

    def test_empty_or_incomplete_probe_is_unknown(self):
        for output in ("", "0 %, 123 MiB\n", self.probe_output(gpu=""), self.probe_output(processes="")):
            line, _ = compose.training_line(probe=(compose.parse_training_output(output), "incomplete"))
            self.assertIn("UNKNOWN", line)
            self.assertNotIn("NOT RUNNING", line)

    def test_nvidia_smi_failure_is_unknown(self):
        with patch.object(compose.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "nvidia-smi failed")):
            line, _ = compose.training_line()
        self.assertIn("UNKNOWN", line)
        self.assertNotIn("NOT RUNNING", line)
        self.assertIn(" && ", compose._SPARK_REMOTE)
        self.assertNotIn(";", compose._SPARK_REMOTE)

    def test_cached_slots_cannot_regenerate_posted_actions(self):
        with patch.object(compose, "slot_block", return_value=(["historical idle slot"], "idle", ["SPLIT cached idle slot"])), patch.object(compose, "compact_report", side_effect=lambda lines, limit: "\n".join(lines)):
            report = compose.compose("1h", datetime.now(timezone.utc), None, {}, [],
                include_slots=True, slots={}, priority_flags=["cached priority"],
                slots_authoritative=False, training=("training unavailable", []))
        self.assertIn("historical idle slot", report)
        self.assertIn("cached priority", report)
        self.assertNotIn("*ACTIONS (PM):*", report)
        self.assertNotIn("SPLIT cached idle slot", report)

    def test_cached_slot_actions_suppressed_in_stderr_and_post(self):
        for stale_source in ("slots", "priority"):
            with self.subTest(source=stale_source), tempfile.TemporaryDirectory() as tmp:
                stdout, stderr = io.StringIO(), io.StringIO()
                data = {"slots": {}, "priority": ["cached priority"], "prs": [], "decisions": {}, "agents": {}}
                with patch.object(compose.sys, "argv", ["heartbeat-compose.py", "--mode", "1h", "--no-active-users"]), patch.object(compose, "collect_hourly", return_value=(data, {stale_source: " (as of yesterday)"})), patch.object(compose, "slot_transitions", return_value=({}, {})), patch.object(compose, "SLOT_STATE", Path(tmp) / "slots.json"), patch.object(compose, "slot_block", return_value=(["historical idle slot"], "idle", ["SPLIT cached idle slot"])), patch.object(compose, "pending_obligations", return_value={}), patch.object(compose, "cleanup_status_alert", return_value=None), patch.object(compose, "compact_report", side_effect=lambda lines, limit: "\n".join(lines)), redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(compose.main(), 0)
                self.assertNotIn("ACTIONS:", stderr.getvalue())
                self.assertNotIn("*ACTIONS (PM):*", stdout.getvalue())
                self.assertIn("historical idle slot", stdout.getvalue())

    def test_overlapping_invocations_older_finishes_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_observed = threading.Event()
            release_old = threading.Event()
            def old_source():
                old_observed.set()
                return "old"
            def blocker():
                release_old.wait(2)
                return "done"
            thread = threading.Thread(target=lambda: compose.parallel_collect(
                {"source": old_source, "blocker": blocker}, cache_dir=root))
            thread.start()
            self.assertTrue(old_observed.wait(1))
            # Ensure the newer source has a strictly newer observation timestamp.
            time.sleep(.02)
            compose.parallel_collect({"source": lambda: "new"}, cache_dir=root)
            newer = json.loads((root / "source.json").read_text())
            release_old.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(json.loads((root / "source.json").read_text()), newer)
            self.assertEqual(newer["value"], "new")

    def test_cached_or_incomplete_results_never_refresh_last_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.json"
            original = {"at": time.time() - 10, "value": "good"}
            path.write_text(json.dumps(original))
            for value in ({"cached": True}, {"cache_age_min": 2}, {"incomplete": True}, "UNKNOWN"):
                compose.store_observation(path, value, time.time())
                self.assertEqual(json.loads(path.read_text()), original)


if __name__ == "__main__":
    unittest.main()
