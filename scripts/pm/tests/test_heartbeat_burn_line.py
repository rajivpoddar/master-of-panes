"""Hourly '*Burn:*' section: one Markdown pipe table, a summary row per pool, then one row per account.

Rajiv C0ALZJHGE49 thread 1794746097.575959 (P0-ONLY gate) and thread 1794851199.275299, 2026-11-16 (pace to a
per-pool horizon; list per-account headroom; P0-ONLY only when the pool burn exceeds the pool budget).
Rajiv C0ALZJHGE49 ts 1795323839.696369, 2026-11-22: "change the claude team and opencode burn section to tables" (the
section is now a pipe table; the pool decision and the per-account figures are unchanged).
Uses the same pool rule as the launcher burn gate (proxy-burn-shadow.py). Not executed by the MoP repair run.
"""
import importlib.util
import json
import tempfile
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

    def __init__(self, claude=OK_CLAUDE, codex=OK_CODEX, rows=None, horizons=None, labels=None, config=None):
        self.pools = {"claude": claude, "codex": codex}
        self.rows = rows or {}
        self.horizons = {"claude": CLAUDE_HORIZON, "codex": CODEX_HORIZON} if horizons is None else horizons
        self.labels = labels or {"acct-1": "codex-user@example.test"}
        self.config = config or {}

    def read_latest(self, path):
        return self.rows

    def read_config(self, path):
        return self.config

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


def parse(section):
    """(head line, header cells, separator row, ordered row names, {row name: {header: cell}}) of a Burn section."""
    rows = section.splitlines()
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows[1:]]
    header, body = cells[0], cells[2:]
    return rows[0], header, rows[2], [r[0] for r in body], {r[0]: dict(zip(header, r)) for r in body}


class BurnSectionTests(unittest.TestCase):
    def setUp(self):
        self.hc = _composer()
        # Point the pin and reserve files at an empty temp dir so the host's real files never leak into a test.
        self.tmp = Path(tempfile.mkdtemp(prefix="burn-table-test-"))
        self.hc.CLAUDE_PIN_FILE = self.tmp / "pin"
        self.hc.CLAUDE_RESERVE_FILE = self.tmp / "reserve"

    def _status(self, claude=OK_CLAUDE, codex=OK_CODEX, **kw):
        shadow = FakeShadow(claude, codex, **kw)
        return self.hc.burn_status(NOW, snapshot=Path("/nonexistent"), config=Path("/nonexistent"), shadow=shadow)

    def _table(self, **kw):
        line, modes = self._status(**kw)
        return parse(line), modes

    # ---- pool decision (unchanged contract: P0-ONLY is scoped to the family that is over/exhausted/unknown) ----
    def test_all_ok_is_normal_for_both_families(self):
        ((head, _, _, _, by), modes) = self._table()
        self.assertEqual(modes, {"Claude": "NORMAL", "Codex": "NORMAL"})
        self.assertEqual(by["Claude pool"]["State"], "OK · routes NORMAL")
        self.assertEqual(by["Codex pool"]["State"], "OK · routes NORMAL")
        self.assertIn("Claude and Codex only; Grok not metered", head)

    def test_claude_pool_over_budget_is_scoped_to_claude(self):
        over = pool("over_budget", [("a@example.test", detail("over_budget", burn=2.0, budget=1.0))])
        ((_, _, _, _, by), modes) = self._table(claude=over)
        self.assertEqual(modes, {"Claude": "P0-ONLY", "Codex": "NORMAL"})
        self.assertEqual(by["Claude pool"]["State"], "OVER · routes P0-ONLY")
        self.assertEqual(by["Codex pool"]["State"], "OK · routes NORMAL")

    def test_unknown_pool_is_p0_only_for_its_family(self):
        unknown = pool("unknown", [("a@example.test", detail("unknown", burn=None))])
        ((_, _, _, _, by), modes) = self._table(claude=unknown)
        self.assertEqual(modes, {"Claude": "P0-ONLY", "Codex": "NORMAL"})
        self.assertEqual(by["Claude pool"]["State"], "UNKNOWN (deny) · routes P0-ONLY")
        self.assertIn("UNKNOWN (deny)", by["Claude a@example.test"]["Note"])

    def test_pool_with_no_measured_account_shows_n_a_never_zero(self):
        # pool_status sums an empty set to 0 burn / 0 budget; that is "not measured", not a measurement.
        unknown = pool("unknown", [("a@example.test", detail("unknown", burn=None))])
        ((_, _, _, _, by), _) = self._table(claude=unknown)
        pool_row = by["Claude pool"]
        self.assertEqual([pool_row["Burn %/h"], pool_row["Budget %/h"], pool_row["Headroom %/h"]], ["n/a"] * 3)

    def test_pool_totals_say_when_an_unmeasured_account_is_left_out(self):
        accounts = [("a@example.test", detail("allow")), ("b@example.test", detail("unknown", burn=None))]
        ((_, _, _, _, by), _) = self._table(claude=pool("unknown", accounts))
        pool_row = by["Claude pool"]
        self.assertEqual([pool_row["Burn %/h"], pool_row["Budget %/h"], pool_row["Headroom %/h"]],
                         ["0.10", "1.00", "+0.90"])
        self.assertEqual(pool_row["Note"], "totals exclude 1 unmeasured account(s)")

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
        _, _, _, names, by = parse(line)
        self.assertEqual(names, ["Claude pool", "Codex pool"])
        self.assertEqual(by["Claude pool"]["State"], "UNKNOWN (deny) · routes P0-ONLY")
        self.assertEqual(by["Claude pool"]["Note"], "accounts UNKNOWN")
        self.assertEqual(by["Claude pool"]["Burn %/h"], "n/a")

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
        # The failure path stays ONE plain line: a half-rendered table would mislead more than it helps.
        self.assertEqual(len(line.splitlines()), 1)
        self.assertNotIn("|---", line)

    def test_mixed_pool_action_names_only_denied_route(self):
        over = pool("over_budget", [("a@example.test", detail("over_budget", burn=2.0, budget=1.0))])
        line, modes = self._status(claude=over)
        out = self.hc.compose("1h", NOW, None, {}, [], failures=[], burn=(line, modes))
        actions = [l for l in out.splitlines() if l.startswith("*ACTIONS (PM):*")][0]
        self.assertIn("burn P0-only for Claude routes", actions)
        self.assertNotIn("Codex routes", actions)
        self.assertIn("P0 and scoped rajiv:<ts> overrides and CI/admin exemptions continue", actions)
        # With no Pace table above it, Burn is the post's first (native) table and every row survives unchanged.
        for row in line.splitlines():
            self.assertIn(row, out)

    def test_all_normal_adds_no_burn_action(self):
        line, modes = self._status()
        out = self.hc.compose("1h", NOW, None, {}, [], failures=[], burn=(line, modes))
        self.assertNotIn("burn P0-only", out)

    def test_decision_uses_unrounded_pool_value_not_display(self):
        # Display rounds to 2 dp; the decision the helper returned must stand as-is.
        over = pool("over_budget", [("a@example.test", detail("over_budget", burn=1.00001, budget=1.0))])
        ((_, _, _, _, by), modes) = self._table(claude=over)
        self.assertEqual(modes["Claude"], "P0-ONLY")
        self.assertEqual((by["Claude pool"]["Burn %/h"], by["Claude pool"]["Budget %/h"]), ("1.00", "1.00"))

    def test_pool_over_budget_stays_p0_only_even_when_one_account_has_free_headroom(self):
        # The launcher cannot pin a NORMAL launch to the account with headroom, so the POOL decides.
        accounts = [("free@example.test", detail("allow", burn=1.41, budget=2.23)),
                    ("hot@example.test", detail("over_budget", burn=3.53, budget=1.10))]
        ((_, _, _, _, by), modes) = self._table(claude=pool("over_budget", accounts))
        self.assertEqual(modes["Claude"], "P0-ONLY")
        free, hot = by["Claude free@example.test"], by["Claude hot@example.test"]
        self.assertEqual((free["Weekly used"], free["Burn %/h"], free["Budget %/h"], free["Headroom %/h"]),
                         ("10%", "1.41", "2.23", "+0.82"))
        self.assertEqual((hot["Weekly used"], hot["Burn %/h"], hot["Budget %/h"], hot["Headroom %/h"]),
                         ("10%", "3.53", "1.10", "-2.43"))

    # ---- layout: pipe table, per-account rows, headroom, session cap, horizon header ----
    def test_section_is_a_pipe_table_with_the_burn_header(self):
        line, _ = self._status()
        rows = line.splitlines()
        self.assertTrue(rows[0].startswith("*Burn:*"))
        self.assertEqual([c.strip() for c in rows[1].strip().strip("|").split("|")], self.hc.BURN_TABLE_HEADER)
        self.assertEqual(rows[2], "|" + "|".join("---" for _ in self.hc.BURN_TABLE_HEADER) + "|")
        self.assertTrue(all(r.startswith("| ") and r.endswith(" |") for r in rows[1:2] + rows[3:]))
        # Every row has exactly one cell per header column, or slack-send.sh would shift the columns.
        for r in rows[1:2] + rows[3:]:
            self.assertEqual(len(r.strip().strip("|").split("|")), len(self.hc.BURN_TABLE_HEADER), r)

    def test_pool_row_then_one_row_per_account(self):
        accounts = [("a@example.test", detail("allow")), ("b@example.test", detail("allow", burn=0.2, budget=0.5))]
        ((_, _, _, names, by), _) = self._table(claude=pool("allow", accounts))
        # Claude (pool + 2 accounts) + Codex (pool + 1 account); Codex accounts show the auth-dir email, not the bare id.
        self.assertEqual(names, ["Claude pool", "Claude a@example.test", "Claude b@example.test", "Codex pool",
                                 "Codex codex-user@example.test"])
        pool_row = by["Claude pool"]
        self.assertEqual((pool_row["Burn %/h"], pool_row["Budget %/h"], pool_row["Headroom %/h"]),
                         ("0.30", "1.50", "+1.20"))

    def test_account_row_reports_session_cap_state(self):
        def session(rows):
            ((_, _, _, _, by), _) = self._table(rows=rows)
            return by["Claude a@example.test"]["Session"]

        self.assertTrue(session({"claude:a@example.test:session": {"pct": 100, "reset": TS + 3600, "ts": TS - 60}})
                        .startswith("CAPPED (resets"))
        self.assertEqual(session({"claude:a@example.test:session": {"pct": 20, "reset": TS + 3600, "ts": TS - 9000}}),
                         "stale")
        self.assertEqual(session({}), "n/a")
        self.assertTrue(session({"claude:a@example.test:session": {"pct": 54, "reset": TS + 3600, "ts": TS - 60}})
                        .startswith("54% (resets"))

    def test_codex_rows_have_no_session_meter(self):
        ((_, _, _, _, by), _) = self._table()
        self.assertEqual(by["Codex codex-user@example.test"]["Session"], "n/a")

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
        ((_, _, _, _, by), _) = self._table(claude=pool("allow", accounts))
        qa = by["Claude qa@example.test"]
        self.assertEqual((qa["Weekly used"], qa["Burn %/h"], qa["Budget %/h"], qa["Weekly resets"]),
                         ("100%", "n/a", "0", "Tue 08:30 IST"))
        self.assertEqual(qa["Note"], "EXHAUSTED · budget 0 until reset Tue 08:30 IST, then 4.00%/h")
        self.assertEqual(by["Claude donna@example.test"]["Note"],
                         "EXHAUSTED · budget 0, resets Wed 20:29 IST (after the Wed 09:30 IST horizon)")

    def test_reset_at_the_horizon_is_labelled_not_after_it(self):
        accounts = [("acct-1", detail("exhausted", pct=100, budget=0.0, reset=CODEX_HORIZON + 60))]
        line, _ = self._status(codex=pool("exhausted", accounts))
        self.assertIn("EXHAUSTED · budget 0, resets Fri 13:07 IST (at the horizon)", line)
        self.assertNotIn("after the Fri 13:06 IST horizon", line)

    def test_unknown_account_row_says_how_stale_the_sample_is(self):
        accounts = [("a@example.test", detail("unknown", burn=None, ts=TS - 7800)),
                    ("b@example.test", detail("unknown", burn=None, ts=None, pct=None))]
        ((_, _, _, _, by), _) = self._table(claude=pool("unknown", accounts))
        a, b = by["Claude a@example.test"], by["Claude b@example.test"]
        self.assertEqual((a["Weekly used"], a["Burn %/h"], a["Note"]), ("10%", "n/a", "sampled 130m ago · UNKNOWN (deny)"))
        self.assertEqual((b["Weekly used"], b["Burn %/h"], b["Note"]), ("UNKNOWN", "n/a", "sample missing · UNKNOWN (deny)"))

    def test_no_row_is_folded_by_the_compactor_and_extra_rows_widen_the_cap(self):
        accounts = [(f"acct{i}@example.test", detail("allow")) for i in range(12)]
        line, modes = self._status(claude=pool("allow", accounts))
        for row in line.splitlines()[1:]:
            self.assertTrue(row.startswith("|"), row)  # a pipe row is never a fold anchor, so none can gain a cell
        out = self.hc.compose("1h", NOW, None, {}, [], failures=[], burn=(line, modes)).splitlines()
        for row in line.splitlines():
            self.assertIn(row, out)  # 20-line cap widened by the extra burn rows, nothing dropped

    # ---- Claude team roster: pin, reserve, disabled and not-in-pool accounts ----
    def _roster_config(self):
        auth = self.tmp / "auth"
        auth.mkdir(exist_ok=True)
        files = {
            "a.json": {"type": "claude", "email": "A@example.test", "access_token": "SECRET-TOKEN-A"},
            "off.json": {"type": "claude", "email": "off@example.test", "disabled": True, "access_token": "SECRET-OFF"},
            "spare.json": {"type": "claude", "email": "spare@example.test"},
            "codex.json": {"type": "codex", "email": "codex-user@example.test"},
            "old.json.bak": {"type": "claude", "email": "bak@example.test"},
        }
        for name, body in files.items():
            (auth / name).write_text(json.dumps(body), encoding="utf-8")
        (auth / ".hidden.json").write_text(json.dumps({"type": "claude", "email": "hidden@example.test"}), encoding="utf-8")
        return {"auth-dir": str(auth)}

    def test_team_rows_show_pin_reserve_disabled_and_not_in_pool(self):
        (self.tmp / "pin").write_text("A@example.test\n", encoding="utf-8")
        (self.tmp / "reserve").write_text("# reserve accounts\n\nSpare@example.test\n", encoding="utf-8")
        line, _ = self._status(config=self._roster_config())
        _, _, _, names, by = parse(line)
        self.assertEqual(names, ["Claude pool", "Claude a@example.test", "Claude off@example.test",
                                 "Claude spare@example.test", "Codex pool", "Codex codex-user@example.test"])
        self.assertEqual(by["Claude a@example.test"]["State"], "enabled · pinned")
        off = by["Claude off@example.test"]
        self.assertEqual((off["State"], off["Weekly used"], off["Burn %/h"], off["Note"]),
                         ("disabled", "n/a", "n/a", "not in the pool"))
        spare = by["Claude spare@example.test"]
        self.assertEqual((spare["State"], spare["Note"]), ("enabled · reserve", "enabled, not bound to the pool"))
        # Only type, e-mail and the disabled flag are read from an auth file; tokens never reach the report.
        self.assertNotIn("SECRET", line)

    def test_unreadable_pin_file_leaves_the_pin_unmarked_not_guessed(self):
        (self.tmp / "pin").mkdir()  # reading a directory fails with an OSError
        self.assertIsNone(self.hc._read_email_set(self.tmp / "pin"))
        ((_, _, _, _, by), _) = self._table()
        self.assertEqual(by["Claude a@example.test"]["State"], "enabled")

    def test_email_set_reader_skips_comments_and_blanks_and_lowercases(self):
        path = self.tmp / "emails"
        path.write_text("# note\n\n  Mixed@Example.Test  \nsecond@example.test\n", encoding="utf-8")
        self.assertEqual(self.hc._read_email_set(path), {"mixed@example.test", "second@example.test"})
        self.assertEqual(self.hc._read_email_set(self.tmp / "missing"), set())

    # ---- pipe-table helper ----
    def test_pipe_table_pads_short_rows_cuts_long_rows_and_neutralises_pipes(self):
        rows = self.hc._pipe_table(["A", "B", "C"], [["x"], ["1", "2", "3", "4"], ["a|b", "multi\nline", "ok"]])
        self.assertEqual(rows[0], "| A | B | C |")
        self.assertEqual(rows[1], "|---|---|---|")
        self.assertEqual(rows[2], "| x | n/a | n/a |")
        self.assertEqual(rows[3], "| 1 | 2 | 3 |")
        self.assertEqual(rows[4], "| a/b | multi line | ok |")


if __name__ == "__main__":
    unittest.main()
