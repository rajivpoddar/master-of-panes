"""Hourly '*Burn:*' section: one line per CLIProxyAPI account, then a POOL verdict per family.

Rajiv C0ALZJHGE49 thread 1794746097.575959 (P0-ONLY gate) and thread 1794851199.275299, 2026-11-16 (pace to a
per-pool horizon; list per-account headroom; P0-ONLY only when the pool burn exceeds the pool budget).
Uses the same pool rule as the launcher burn gate (proxy-burn-shadow.py). Not executed by the MoP repair run.
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


NOW = datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc)
TS = NOW.timestamp()
CLAUDE_HORIZON = datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc).timestamp()  # Wed 09:30 IST
CODEX_HORIZON = datetime(2026, 11, 20, 7, 36, tzinfo=timezone.utc).timestamp()  # Fri 13:06 IST


def detail(decision, pct=10, burn=0.1, budget=1.0, **extra):
    live = decision in ("allow", "over_budget")
    d = {"decision": decision, "pct": pct, "reset": None, "ts": TS - 600, "burn": burn if live else None,
         "budget": budget if decision != "unknown" else None,
         "headroom": (budget - burn) if live else None, "after": None, "target": "horizon"}
    d.update(extra)
    return d


def pool(decision, accounts):
    live = [d for _, d in accounts if d["decision"] in ("allow", "over_budget")]
    burn, budget = sum(d["burn"] for d in live), sum(d["budget"] for d in live)
    return {"decision": decision, "burn": burn, "budget": budget, "headroom": budget - burn, "accounts": accounts}


OK_CLAUDE = pool("allow", [("a@example.test", detail("allow"))])
OK_CODEX = pool("allow", [("acct-1", detail("allow"))])


class FakeShadow:
    LOG = ""

    def __init__(self, claude=OK_CLAUDE, codex=OK_CODEX, rows=None, horizons=None, labels=None):
        self.pools = {"claude": claude, "codex": codex}
        self.rows = rows or {}
        self.horizons = {"claude": CLAUDE_HORIZON, "codex": CODEX_HORIZON} if horizons is None else horizons
        self.labels = labels or {"acct-1": "codex-user@example.test"}

    def read_latest(self, path):
        return self.rows

    def read_config(self, path):
        return {}

    def load_horizons(self):
        return self.horizons

    def active_horizon(self, pool_name, horizons, now):
        horizon = horizons.get(pool_name)
        return horizon if horizon and horizon > now else None

    def bindings(self, model, config):
        return ("claude", ["a@example.test"]) if model.startswith("claude") else ("codex", ["acct-1"])

    def pool_status(self, pool_name, accounts, rows, now, horizons=None):
        status = dict(self.pools[pool_name])
        status["pool"] = pool_name
        status["horizon"] = self.active_horizon(pool_name, self.horizons, now)
        return status

    def account_labels(self, pool_name, config):
        return self.labels


class BurnSectionTests(unittest.TestCase):
    def setUp(self):
        self.hc = _composer()

    def _status(self, claude=OK_CLAUDE, codex=OK_CODEX, **kw):
        shadow = FakeShadow(claude, codex, **kw)
        return self.hc.burn_status(NOW, snapshot=Path("/nonexistent"), config=Path("/nonexistent"), shadow=shadow)

    # ---- pool decision (unchanged contract: P0-ONLY is scoped to the family that is over/exhausted/unknown) ----
    def test_all_ok_is_normal_for_both_families(self):
        line, modes = self._status()
        self.assertEqual(modes, {"Claude": "NORMAL", "Codex": "NORMAL"})
        self.assertIn("→ OK → Claude routes: NORMAL", line)
        self.assertIn("→ OK → Codex routes: NORMAL", line)
        self.assertIn("Claude and Codex only; Grok not metered", line)

    def test_claude_pool_over_budget_is_scoped_to_claude(self):
        over = pool("over_budget", [("a@example.test", detail("over_budget", burn=2.0, budget=1.0))])
        line, modes = self._status(claude=over)
        self.assertEqual(modes, {"Claude": "P0-ONLY", "Codex": "NORMAL"})
        self.assertIn("→ OVER → Claude routes: P0-ONLY", line)
        self.assertIn("→ OK → Codex routes: NORMAL", line)

    def test_unknown_pool_is_p0_only_for_its_family(self):
        unknown = pool("unknown", [("a@example.test", detail("unknown", burn=None))])
        line, modes = self._status(claude=unknown)
        self.assertEqual(modes, {"Claude": "P0-ONLY", "Codex": "NORMAL"})
        self.assertIn("UNKNOWN (deny)", line)

    def test_exhausted_codex_pool_is_scoped_to_codex(self):
        dead = pool("exhausted", [("acct-1", detail("exhausted", pct=100, budget=0.0))])
        _, modes = self._status(codex=dead)
        self.assertEqual(modes, {"Claude": "NORMAL", "Codex": "P0-ONLY"})

    def test_unbound_pool_is_unknown_and_p0_only(self):
        class NoAccounts(FakeShadow):
            def bindings(self, model, config):
                raise ValueError("no_accounts")

        line, modes = self.hc.burn_status(NOW, snapshot=Path("/x"), config=Path("/x"), shadow=NoAccounts())
        self.assertEqual(modes, {"Claude": "P0-ONLY", "Codex": "P0-ONLY"})
        self.assertIn("Claude pool: accounts UNKNOWN (deny) → Claude routes: P0-ONLY", line)

    def test_both_families_denied_names_both(self):
        over = pool("over_budget", [("a@example.test", detail("over_budget", burn=2.0, budget=1.0))])
        unknown = pool("unknown", [("acct-1", detail("unknown", burn=None))])
        line, modes = self._status(claude=over, codex=unknown)
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
        self.assertEqual(len(line.splitlines()), 1)

    def test_mixed_pool_action_names_only_denied_route(self):
        over = pool("over_budget", [("a@example.test", detail("over_budget", burn=2.0, budget=1.0))])
        line, modes = self._status(claude=over)
        out = self.hc.compose("1h", NOW, None, {}, [], failures=[], burn=(line, modes))
        actions = [l for l in out.splitlines() if l.startswith("*ACTIONS (PM):*")][0]
        self.assertIn("burn P0-only for Claude routes", actions)
        self.assertNotIn("Codex routes", actions)
        self.assertIn("P0 and scoped rajiv:<ts> overrides and CI/admin exemptions continue", actions)
        for row in line.splitlines():
            self.assertIn(row, out)

    def test_all_normal_adds_no_burn_action(self):
        line, modes = self._status()
        out = self.hc.compose("1h", NOW, None, {}, [], failures=[], burn=(line, modes))
        self.assertNotIn("burn P0-only", out)

    def test_decision_uses_unrounded_pool_value_not_display(self):
        # Display rounds to 2 dp; the decision the helper returned must stand as-is.
        over = pool("over_budget", [("a@example.test", detail("over_budget", burn=1.00001, budget=1.0))])
        line, modes = self._status(claude=over)
        self.assertEqual(modes["Claude"], "P0-ONLY")
        self.assertIn("burn 1.00 vs budget 1.00", line)

    def test_pool_over_budget_stays_p0_only_even_when_one_account_has_free_headroom(self):
        # The launcher cannot pin a NORMAL launch to the account with headroom, so the POOL decides.
        accounts = [("free@example.test", detail("allow", burn=1.41, budget=2.23)),
                    ("hot@example.test", detail("over_budget", burn=3.53, budget=1.10))]
        line, modes = self._status(claude=pool("over_budget", accounts))
        self.assertEqual(modes["Claude"], "P0-ONLY")
        self.assertIn("Claude free@example.test: 10% used · burn 1.41 · budget 2.23 · headroom +0.82 %/h", line)
        self.assertIn("Claude hot@example.test: 10% used · burn 3.53 · budget 1.10 · headroom -2.43 %/h", line)

    # ---- layout: per-account rows, headroom, session cap, horizon header ----
    def test_one_line_per_account_then_pool_total(self):
        accounts = [("a@example.test", detail("allow")), ("b@example.test", detail("allow", burn=0.2, budget=0.5))]
        line, _ = self._status(claude=pool("allow", accounts))
        rows = line.splitlines()
        # header + Claude (pool + 2 accounts) + Codex (pool + 1 account)
        self.assertEqual(len(rows), 1 + 3 + 2)
        self.assertTrue(rows[1].startswith("Claude pool: burn 0.30 vs budget 1.50 %/h → headroom +1.20 → OK"))
        self.assertTrue(rows[2].startswith("Claude a@example.test:") and rows[3].startswith("Claude b@example.test:"))
        self.assertTrue(rows[4].startswith("Codex pool:"))
        # Codex accounts show the auth-dir email, not the bare account id.
        self.assertTrue(rows[5].startswith("Codex codex-user@example.test:"))

    def test_account_row_reports_session_cap_state(self):
        rows = {
            "claude:a@example.test:session": {"pct": 100, "reset": TS + 3600, "ts": TS - 60},
        }
        line, _ = self._status(rows=rows)
        self.assertIn("session CAPPED (resets", line)
        stale = {"claude:a@example.test:session": {"pct": 20, "reset": TS + 3600, "ts": TS - 9000}}
        line, _ = self._status(rows=stale)
        self.assertIn("session stale", line)
        line, _ = self._status(rows={})
        self.assertIn("session n/a", line)
        live = {"claude:a@example.test:session": {"pct": 54, "reset": TS + 3600, "ts": TS - 60}}
        line, _ = self._status(rows=live)
        self.assertIn("session 54% (resets", line)

    def test_header_states_each_pool_horizon_in_ist(self):
        line, _ = self._status()
        head = line.splitlines()[0]
        self.assertIn("pace to Claude Wed 09:30 IST (62.0h)", head)
        self.assertIn("Codex Fri 13:06 IST (", head)
        self.assertIn("headroom = budget - burn, %/h", head)

    def test_passed_or_missing_horizon_falls_back_to_own_weekly_reset(self):
        line, _ = self._status(horizons={"claude": TS - 1})
        head = line.splitlines()[0]
        self.assertIn("Claude own weekly reset", head)
        self.assertIn("Codex own weekly reset", head)

    def test_exhausted_account_wording(self):
        reset_before = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc).timestamp()  # Tue 08:30 IST
        reset_after = datetime(2026, 11, 18, 14, 59, tzinfo=timezone.utc).timestamp()  # Wed 20:29 IST
        accounts = [
            ("qa@example.test", detail("exhausted", pct=100, budget=0.0, reset=reset_before, after=4.0, target="reset")),
            ("donna@example.test", detail("exhausted", pct=100, budget=0.0, reset=reset_after)),
            ("a@example.test", detail("allow")),
        ]
        line, _ = self._status(claude=pool("allow", accounts))
        self.assertIn("Claude qa@example.test: 100% used · EXHAUSTED · budget 0 until reset Tue 08:30 IST, then 4.00%/h", line)
        self.assertIn("Claude donna@example.test: 100% used · EXHAUSTED · budget 0, resets Wed 20:29 IST "
                      "(after the Wed 09:30 IST horizon)", line)

    def test_reset_at_the_horizon_is_labelled_not_after_it(self):
        accounts = [("acct-1", detail("exhausted", pct=100, budget=0.0, reset=CODEX_HORIZON + 60))]
        line, _ = self._status(codex=pool("exhausted", accounts))
        self.assertIn("EXHAUSTED · budget 0, resets Fri 13:07 IST (at the horizon)", line)
        self.assertNotIn("after the Fri 13:06 IST horizon", line)

    def test_unknown_account_row_says_how_stale_the_sample_is(self):
        accounts = [("a@example.test", detail("unknown", burn=None, ts=TS - 7800)),
                    ("b@example.test", detail("unknown", burn=None, ts=None, pct=None))]
        line, _ = self._status(claude=pool("unknown", accounts))
        self.assertIn("Claude a@example.test: 10% used · burn n/a, sampled 130m ago · UNKNOWN (deny)", line)
        self.assertIn("Claude b@example.test: UNKNOWN used · burn n/a, sample missing · UNKNOWN (deny)", line)

    def test_no_row_is_folded_by_the_compactor_and_extra_rows_widen_the_cap(self):
        accounts = [(f"acct{i}@example.test", detail("allow")) for i in range(12)]
        line, modes = self._status(claude=pool("allow", accounts))
        for row in line.splitlines()[1:]:
            self.assertFalse(row.startswith(("•", "- ", "  ")), row)
        out = self.hc.compose("1h", NOW, None, {}, [], failures=[], burn=(line, modes)).splitlines()
        for row in line.splitlines():
            self.assertIn(row, out)  # 20-line cap widened by the extra burn rows, nothing dropped


if __name__ == "__main__":
    unittest.main()
