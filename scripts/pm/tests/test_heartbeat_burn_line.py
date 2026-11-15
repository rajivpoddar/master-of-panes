"""Hourly '*Burn:*' line: P0-ONLY when any billable account is over/exhausted/unknown.

Rajiv C0ALZJHGE49 thread 1794746097.575959. Uses the same per-account rule as the
launcher burn gate (proxy-burn-shadow.py). Not executed by the MoP repair run.
"""
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

COMPOSER = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py"


def _composer():
    spec = importlib.util.spec_from_file_location("heartbeat_compose_under_test", COMPOSER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeShadow:
    LOG = ""

    def __init__(self, decisions):
        self.decisions = decisions  # key -> (decision, burn, budget)

    def read_latest(self, path):
        return {k: {"pct": 10, "ts": 1, "reset": 2} for k in self.decisions}

    def read_config(self, path):
        return {}

    def bindings(self, model, config):
        if model.startswith("claude"):
            return "claude", ["a@example.test"]
        return "codex", ["acct-1"]

    def account_decision(self, key, rows, pool, now):
        decision, burn, budget = self.decisions[key]
        return decision, burn, budget


NOW = datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc)


class BurnLineTests(unittest.TestCase):
    def setUp(self):
        self.hc = _composer()

    def _status(self, claude, codex):
        shadow = FakeShadow({
            "claude:a@example.test:weekly": claude,
            "codex:acct-1:weekly": codex,
        })
        return self.hc.burn_status(NOW, snapshot=Path("/nonexistent"), config=Path("/nonexistent"), shadow=shadow)

    def test_all_ok_is_normal(self):
        line, mode = self._status(("allow", 0.1, 1.0), ("allow", 0.1, 1.0))
        self.assertEqual(mode, "NORMAL")
        self.assertIn("Claude OK, Codex OK → mode NORMAL", line)

    def test_over_budget_is_p0_only(self):
        line, mode = self._status(("over_budget", 2.0, 1.0), ("allow", 0.1, 1.0))
        self.assertEqual(mode, "P0-ONLY")
        self.assertIn("Claude OVER", line)

    def test_unknown_counts_as_p0_only(self):
        _, mode = self._status(("unknown", None, None), ("allow", 0.1, 1.0))
        self.assertEqual(mode, "P0-ONLY")

    def test_exhausted_counts_as_p0_only(self):
        _, mode = self._status(("allow", 0.1, 1.0), ("exhausted", None, 0.0))
        self.assertEqual(mode, "P0-ONLY")

    def test_check_failure_is_p0_only(self):
        class Broken:
            LOG = ""

            def read_latest(self, path):
                raise RuntimeError("boom")

        line, mode = self.hc.burn_status(NOW, shadow=Broken())
        self.assertEqual(mode, "P0-ONLY")
        self.assertIn("UNKNOWN", line)

    def test_p0_only_adds_pm_action(self):
        line, mode = self._status(("over_budget", 2.0, 1.0), ("allow", 0.1, 1.0))
        out = self.hc.compose("1h", NOW, None, {}, [], failures=[], burn=(line, mode))
        self.assertIn("*ACTIONS (PM):* " + self.hc.BURN_ACTION, out)
        self.assertIn(line, out)


if __name__ == "__main__":
    unittest.main()
