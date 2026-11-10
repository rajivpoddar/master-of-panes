"""Hourly collector regressions; no live SSH/GitHub/production calls."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

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

    def test_running_parser(self):
        doc = compose.parse_training_output("72 %, 29123 MiB\nsindhu-train Up 3 hours\n")
        line, actions = compose.training_line(probe=(doc, ""))
        self.assertIn("RUNNING", line)
        self.assertIn("GPU 72%", line)
        self.assertIn("sindhu-train", line)
        self.assertEqual(actions, [])

    def test_not_running_parser_idle_and_no_container(self):
        for output in ("0 %, 123 MiB\n", "0 %, 1000 MiB\nsindhu-train Up 3 hours\n", "", "64 %, 1000 MiB\n"):
            with self.subTest(output=output):
                line, _ = compose.training_line(probe=(compose.parse_training_output(output), ""))
                self.assertIn("NOT RUNNING", line)
                self.assertIn("GPU", line)
                self.assertIn("container", line)
                self.assertNotIn("UNAVAILABLE", line)


if __name__ == "__main__":
    unittest.main()
