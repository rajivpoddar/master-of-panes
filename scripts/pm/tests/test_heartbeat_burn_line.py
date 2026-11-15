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

    def test_all_ok_is_normal_for_both_families(self):
        line, modes = self._status(("allow", 0.1, 1.0), ("allow", 0.1, 1.0))
        self.assertEqual(modes, {"Claude": "NORMAL", "Codex": "NORMAL"})
        self.assertIn("Claude OK, Codex OK", line)
        self.assertIn("Claude and Codex only; Grok not metered", line)

    def test_claude_over_budget_is_scoped_to_claude(self):
        line, modes = self._status(("over_budget", 2.0, 1.0), ("allow", 0.1, 1.0))
        self.assertEqual(modes, {"Claude": "P0-ONLY", "Codex": "NORMAL"})
        self.assertIn("Claude OVER", line)
        self.assertIn("Claude routes: P0-ONLY | Codex routes: NORMAL", line)

    def test_unknown_counts_as_p0_only_for_its_family(self):
        _, modes = self._status(("unknown", None, None), ("allow", 0.1, 1.0))
        self.assertEqual(modes, {"Claude": "P0-ONLY", "Codex": "NORMAL"})

    def test_exhausted_codex_is_scoped_to_codex(self):
        _, modes = self._status(("allow", 0.1, 1.0), ("exhausted", None, 0.0))
        self.assertEqual(modes, {"Claude": "NORMAL", "Codex": "P0-ONLY"})

    def test_both_families_denied_names_both(self):
        line, modes = self._status(("over_budget", 2.0, 1.0), ("unknown", None, None))
        self.assertEqual(modes, {"Claude": "P0-ONLY", "Codex": "P0-ONLY"})
        out = self.hc.compose("1h", NOW, None, {}, [], failures=[], burn=(line, modes))
        self.assertIn("*ACTIONS (PM):* burn P0-only for Claude and Codex routes", out)

    def test_check_failure_is_p0_only_for_every_family(self):
        class Broken:
            LOG = ""

            def read_latest(self, path):
                raise RuntimeError("boom")

        line, modes = self.hc.burn_status(NOW, shadow=Broken())
        self.assertEqual(modes, {"Claude": "P0-ONLY", "Codex": "P0-ONLY"})
        self.assertIn("UNKNOWN", line)

    def test_mixed_pool_action_names_only_denied_route(self):
        line, modes = self._status(("over_budget", 2.0, 1.0), ("allow", 0.1, 1.0))
        out = self.hc.compose("1h", NOW, None, {}, [], failures=[], burn=(line, modes))
        actions = [l for l in out.splitlines() if l.startswith("*ACTIONS (PM):*")][0]
        self.assertIn("burn P0-only for Claude routes", actions)
        self.assertNotIn("Codex routes", actions)
        self.assertIn("P0 and scoped rajiv:<ts> overrides and CI/admin exemptions continue", actions)
        self.assertIn(line, out)

    def test_all_normal_adds_no_burn_action(self):
        line, modes = self._status(("allow", 0.1, 1.0), ("allow", 0.1, 1.0))
        out = self.hc.compose("1h", NOW, None, {}, [], failures=[], burn=(line, modes))
        self.assertNotIn("burn P0-only", out)

    def test_decision_uses_unrounded_value_not_display(self):
        # Display rounds to 2 dp; the decision the helper returned must stand as-is.
        line, modes = self._status(("over_budget", 1.00001, 1.0), ("allow", 0.1, 1.0))
        self.assertEqual(modes["Claude"], "P0-ONLY")
        self.assertIn("1.00%/h", line)


if __name__ == "__main__":
    unittest.main()
