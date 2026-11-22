"""Sarvam TF burn line: token x published price from lane transcripts, UNAVAILABLE with a reason, no invented numbers.

Run by path (the launcher tests do the same): python3 -I ~/.claude/scripts/cost/tests/test_sarvam_usage.py
"""
import importlib.util
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("sarvam_usage", HERE.parent / "sarvam-usage.py")
su = importlib.util.module_from_spec(spec)
spec.loader.exec_module(su)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)  # 17:30 IST, month 2026-11


def ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ev(msg_id, model, inp, out, minutes_ago, cache_read=0, cache_create=0, kind="assistant", text=None):
    usage = {"input_tokens": inp, "output_tokens": out}
    if cache_read:
        usage["cache_read_input_tokens"] = cache_read
    if cache_create:
        usage["cache_creation_input_tokens"] = cache_create
    message = {"id": msg_id, "model": model, "usage": usage}
    if text:
        message["content"] = [{"type": "text", "text": text}]
    return json.dumps({"type": kind, "message": message, "timestamp": ts(minutes_ago)}, separators=(",", ":"))


class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "lanes"
        self.root.mkdir()
        self.ledger = Path(self._tmp.name) / "state" / "ledger.jsonl"
        self.caps = Path(self._tmp.name) / "proxy-caps.env"

    def lane(self, name, *events):
        (self.root / name).write_text("\n".join(events) + "\n")

    def run_collect(self, environ=None, record=False, now=NOW, **kw):
        return su.collect(now, self.root, self.ledger, self.caps, environ if environ is not None else {},
                          record=record, **kw)


class CostMath(Sandbox):
    def test_priced_from_published_table_with_streaming_dedupe_and_cache_as_input(self):
        # Same message id streamed three times with growing usage: the largest counts win, never a sum.
        self.lane("a.jsonl",
                  ev("gw-1", "sarvam-glm-5.3", 1000, 0, 30, cache_read=500, cache_create=500),
                  ev("gw-1", "sarvam-glm-5.3", 1000, 40, 30, cache_read=500, cache_create=500),
                  ev("gw-1", "sarvam-glm-5.3", 1000, 200, 30, cache_read=500, cache_create=500))
        got = self.run_collect()["summary"]
        in_tok, out_tok = 2000, 200
        want = (in_tok * 126 + out_tok * 390) / 1e6
        self.assertAlmostEqual(got["used_inr"], want, places=9)
        self.assertEqual(got["priced_msgs"], 1)

    def test_every_priced_alias_uses_its_own_row(self):
        for i, (alias, (pin, pout)) in enumerate(sorted(su.PRICES.items())):
            self.lane(f"{i}.jsonl", ev(f"gw-{i}", alias, 1_000_000, 1_000_000, 10))
        got = self.run_collect()["summary"]["used_inr"]
        self.assertAlmostEqual(got, sum(a + b for a, b in su.PRICES.values()), places=6)

    def test_text_mention_or_other_model_is_not_counted(self):
        self.lane("a.jsonl",
                  ev("gw-1", "claude-sonnet-5-5", 9_000_000, 9_000_000, 5, text='the "model":"sarvam-glm-5.3" alias'),
                  ev("gw-2", "sarvam-glm-5.3", 5_000_000, 5_000_000, 5, kind="user"))
        self.assertEqual(self.run_collect()["summary"]["used_inr"], 0.0)

    def test_previous_month_excluded(self):
        self.lane("a.jsonl", ev("gw-old", "sarvam-glm-5.3", 1_000_000, 0, 60 * 24 * 30),  # 30 days back: October
                  ev("gw-new", "sarvam-glm-5.3", 1_000_000, 0, 10))
        self.assertAlmostEqual(self.run_collect()["summary"]["used_inr"], 126.0, places=6)

    def test_burn_is_trailing_window_average(self):
        self.lane("a.jsonl",
                  ev("gw-in", "sarvam-105b", 3_000_000, 0, 60),            # inside 3h: 30 rupees
                  ev("gw-out", "sarvam-105b", 9_000_000, 0, 60 * 5))       # 5h ago: counted in month, not in burn
        out = self.run_collect()
        self.assertAlmostEqual(out["summary"]["window_inr"], 30.0, places=6)
        self.assertIn("burn ₹10.00/h (3h avg)", out["line"])

    def test_kimi_is_unpriced_not_costed(self):
        self.lane("a.jsonl", ev("gw-k", "sarvam-kimi-k3", 1_000_000, 1_000_000, 5))
        out = self.run_collect()
        self.assertEqual(out["summary"]["used_inr"], 0.0)
        self.assertEqual(out["summary"]["unpriced"], {"sarvam-kimi-k3": 1})
        self.assertIn("unpriced msgs: sarvam-kimi-k3 x1", out["line"])


class Allowance(Sandbox):
    def test_unset_allowance_is_unavailable_never_a_default(self):
        line = self.run_collect()["line"]
        self.assertIn("/UNAVAILABLE (no allowance configured) this month", line)
        self.assertIn("balance UNAVAILABLE (no allowance configured)", line)
        self.assertNotRegex(line, r"/₹\d")

    def test_env_allowance_gives_balance_upper_bound(self):
        self.lane("a.jsonl", ev("gw-1", "sarvam-glm-5.3", 1_000_000, 0, 10))
        line = self.run_collect({su.ALLOWANCE_KEY: "500"})["line"]
        self.assertIn("₹126.00/₹500 this month", line)
        self.assertIn("balance ≤₹374.00", line)

    def test_caps_file_value_used_and_unterminated_last_line_ignored(self):
        self.caps.write_text(f"OTHER=1\n{su.ALLOWANCE_KEY}=250\n")
        self.assertIn("/₹250 this month", self.run_collect()["line"])
        self.caps.write_text(f"{su.ALLOWANCE_KEY}=250")  # no trailing newline: the launcher's read loop skips it too
        self.assertIn("/UNAVAILABLE (no allowance configured)", self.run_collect()["line"])

    def test_env_wins_over_caps_file(self):
        self.caps.write_text(f"{su.ALLOWANCE_KEY}=250\n")
        self.assertIn("/₹900 this month", self.run_collect({su.ALLOWANCE_KEY: "900"})["line"])

    def test_invalid_allowance_is_unavailable_with_reason(self):
        for bad in ("abc", "0", "-5", "1e3"):
            line = self.run_collect({su.ALLOWANCE_KEY: bad})["line"]
            self.assertIn("UNAVAILABLE (allowance invalid in env SARVAM_TF_ALLOWANCE_INR)", line, bad)

    def test_overspend_clamps_balance_at_zero(self):
        self.lane("a.jsonl", ev("gw-1", "sarvam-glm-5.3", 1_000_000, 0, 10))
        self.assertIn("balance ≤₹0.00", self.run_collect({su.ALLOWANCE_KEY: "100"})["line"])


class Unavailable(Sandbox):
    def test_no_transcripts_and_no_ledger_is_unavailable_with_reason(self):
        self.root = self.root / "missing"
        out = self.run_collect()
        self.assertFalse(out["available"])
        self.assertTrue(out["line"].startswith("Sarvam TF: UNAVAILABLE (lane transcripts unreadable"))
        self.assertNotRegex(out["line"], r"₹\d")

    def test_failed_scan_on_stale_ledger_reports_burn_unavailable_not_zero(self):
        self.ledger.parent.mkdir(parents=True)
        old = NOW - timedelta(hours=10)
        self.ledger.write_text(
            json.dumps({"id": "gw-1", "ts": ts(60 * 11), "model": "sarvam-glm-5.3", "in": 1_000_000, "out": 0}) + "\n"
            + json.dumps({"scan_at": old.timestamp()}) + "\n")
        self.root = self.root / "missing"
        out = self.run_collect()
        self.assertTrue(out["available"])
        self.assertIn("burn n/a (no lane-transcript scan in the last 3h)", out["line"])
        self.assertIn("₹126.00/", out["line"])  # the ledgered month total still shows

    def test_deadline_hit_marks_partial_and_burn_unavailable(self):
        self.lane("a.jsonl", ev("gw-1", "sarvam-105b", 1_000_000, 0, 10))
        out = self.run_collect(deadline_s=-1.0)
        self.assertIn("partial scan (deadline)", out["line"])
        self.assertIn("burn n/a (scan hit its deadline)", out["line"])


class Ledger(Sandbox):
    def test_read_only_run_writes_nothing(self):
        self.lane("a.jsonl", ev("gw-1", "sarvam-glm-5.3", 1000, 100, 10))
        self.run_collect()
        self.assertFalse(self.ledger.exists())

    def test_record_makes_month_total_survive_transcript_loss_without_double_count(self):
        self.lane("a.jsonl", ev("gw-1", "sarvam-glm-5.3", 1_000_000, 0, 10))
        first = self.run_collect(record=True)
        self.assertEqual(first["recorded"], 1)
        (self.root / "a.jsonl").unlink()
        gone = self.run_collect()
        self.assertAlmostEqual(gone["summary"]["used_inr"], 126.0, places=6)
        # The same transcript reappearing is the same message id: not counted twice, not re-recorded as new.
        self.lane("a.jsonl", ev("gw-1", "sarvam-glm-5.3", 1_000_000, 0, 10))
        again = self.run_collect(record=True)
        self.assertAlmostEqual(again["summary"]["used_inr"], 126.0, places=6)

    def test_torn_or_foreign_ledger_lines_are_skipped(self):
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text('{"id":"gw-1","ts":"%s","model":"sarvam-glm-5.3","in":1000000,"out":0}\n{"id":"gw-2","ts"\n[1]\n'
                               % ts(5))
        self.assertAlmostEqual(self.run_collect()["summary"]["used_inr"], 126.0, places=6)

    def test_watermark_limits_which_transcripts_are_rescanned(self):
        old = self.root / "old.jsonl"
        old.write_text(ev("gw-old", "sarvam-105b", 1_000_000, 0, 10) + "\n")
        import os
        os.utime(old, (NOW.timestamp() - 3600, NOW.timestamp() - 3600))
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text(json.dumps({"scan_at": NOW.timestamp() - 60}) + "\n")  # scanned a minute ago
        got = self.run_collect()
        self.assertEqual(got["summary"]["priced_msgs"], 0)  # old.jsonl predates the watermark: not reread


class LauncherWiring(unittest.TestCase):
    """Structural, no hand-kept list (rule 60): every serverless Sarvam alias the launcher can route to has a price."""

    def test_every_launcher_ring_alias_is_priced(self):
        launcher = (HERE.parents[1] / "proxy-agent.sh").read_text()
        ring = re.search(r'^PROXY_AGENT_SARVAM_RING="([^"]+)"', launcher, re.M)
        self.assertIsNotNone(ring, "launcher no longer defines PROXY_AGENT_SARVAM_RING")
        aliases = ring.group(1).split()
        self.assertGreaterEqual(len(aliases), 2)
        for alias in aliases:
            self.assertIn(alias, su.PRICES, f"{alias} is routable but has no published price: burn would undercount")

    def test_kimi_is_the_only_unpriced_launcher_alias(self):
        launcher = (HERE.parents[1] / "proxy-agent.sh").read_text()
        ids = set()
        for tier in re.findall(r'\bmodels="([^"]+)"', launcher):  # discovered from the LOW/MID/TOP tier lists
            ids |= {m for m in tier.split() if m.startswith("sarvam-")}
        self.assertGreaterEqual(len(ids), 5)
        # sarvam-kimi-k3 is Provisioned Throughput (billed by capacity, not tokens): the one allowed exception.
        self.assertEqual({i for i in ids if i not in su.PRICES}, {"sarvam-kimi-k3"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
