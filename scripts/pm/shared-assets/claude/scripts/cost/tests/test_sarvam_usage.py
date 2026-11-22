"""Sarvam TF measured estimate: attribution by REGISTERED ROUTE ONLY (bare upstream ids from foreign-provider lanes
never count), per-completion usage summed once across continuations, verified lane usage x published rates (separate
cached rate), UNAVAILABLE with a reason instead of an invented zero, balance kept separate.

Structural, not inventory-pinned (rule 60): route tables are discovered from `su.ROUTES` and the launcher, never copied.

Run by path (the launcher tests do the same): python3 -I ~/.claude/scripts/cost/tests/test_sarvam_usage.py
"""
import importlib.util
import json
import os
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
GLM = "sarvam-glm-5.3"
BANNED_WORDING = ("lower bound", "upper bound", "≤", "floor")


def ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def init(model):
    return json.dumps({"type": "system", "subtype": "init", "model": model, "session_id": "s-1"}, separators=(",", ":"))


def asst(msg_id, model, minutes_ago=5, inp=0, out=0, kind="assistant", text=None):
    """An assistant event. Real Sarvam lanes print the UPSTREAM id here and a placeholder usage (output_tokens 0)."""
    message = {"id": msg_id, "model": model, "usage": {"input_tokens": inp, "output_tokens": out}}
    if text:
        message["content"] = [{"type": "text", "text": text}]
    return json.dumps({"type": kind, "message": message, "timestamp": ts(minutes_ago)}, separators=(",", ":"))


def result(uuid=None, since_last=None, **by_model):
    """A result event. by_model: model-key -> (inputTokens, cacheRead, outputTokens[, cacheCreation]).

    `since_last` (dict of the same shape) writes `model_usage_since_last_result`, the per-completion delta the real
    harness emits; without it the event carries only the cumulative `modelUsage` (the older shape).
    """
    usage = {}
    for key, tokens in by_model.items():
        tokens = tuple(tokens) + (0,) * (4 - len(tokens))
        usage[key.replace("_", "-") if key.startswith("sarvam") else key] = {
            "inputTokens": tokens[0], "cacheReadInputTokens": tokens[1], "outputTokens": tokens[2],
            "cacheCreationInputTokens": tokens[3]}
    event = {"type": "result", "subtype": "success", "modelUsage": usage}
    if uuid is not None:
        event["uuid"] = uuid
        event["result_index"] = 0
    if since_last is not None:
        deltas = {}
        for key, tokens in since_last.items():
            tokens = tuple(tokens) + (0,) * (4 - len(tokens))
            deltas[key.replace("_", "-") if key.startswith("sarvam") else key] = {
                "inputTokens": tokens[0], "cacheReadInputTokens": tokens[1], "outputTokens": tokens[2],
                "cacheCreationInputTokens": tokens[3]}
        event["model_usage_since_last_result"] = deltas
    return json.dumps(event, separators=(",", ":"))


def priced(alias, in_tok=0, cached=0, out_tok=0):
    rate_in, rate_cached, rate_out = su.ROUTES[alias]["price"]
    return (in_tok * rate_in + cached * rate_cached + out_tok * rate_out) / 1e6


class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "lanes"
        self.root.mkdir()
        self.ledger = Path(self._tmp.name) / "state" / "ledger.jsonl"
        self.caps = Path(self._tmp.name) / "proxy-caps.env"

    def lane(self, stem, *events, launch_alias=None, minutes_ago=5):
        """A lane transcript (+ optional LAUNCH manifest) whose mtime is the lane's end time."""
        path = self.root / f"{stem}.jsonl"
        path.write_text("\n".join(events) + "\n")
        stamp = NOW.timestamp() - minutes_ago * 60
        os.utime(path, (stamp, stamp))
        if launch_alias is not None:
            (self.root / f"{stem}.log").write_text(
                f"[2026-11-23T11:00:00Z] LAUNCH name=proxy-{stem} ISSUE:1 PR:2 model_alias={launch_alias} "
                f"model_id={launch_alias} MODE:dev\nother noise\n")
        return path

    def sarvam_lane(self, stem, alias=GLM, usage=(1_000_000, 0, 0), minutes_ago=5, with_result=True,
                    completions=None):
        """A lane shaped like the real ones: init prints the alias, messages print the upstream id.

        `completions` (list of (uuid, delta) pairs) shapes a multi-completion continuation lane; each completion
        carries `modelUsage` = the running cumulative and `model_usage_since_last_result` = its own delta, exactly
        as the launcher appends them.
        """
        upstream = su.ROUTES[alias]["upstream"]
        events = [init(alias), asst(f"{stem}-1", upstream, minutes_ago, inp=123456), asst(f"{stem}-2", upstream, minutes_ago)]
        if with_result:
            if completions is None:
                events.append(result(uuid=f"{stem}-r0", since_last={alias.replace("-", "_"): usage},
                                     **{alias.replace("-", "_"): usage}))
            else:
                cumulative = [0, 0, 0]
                for i, (uuid, delta) in enumerate(completions):
                    cumulative = [c + d for c, d in zip(cumulative, tuple(delta) + (0,) * (3 - len(delta)))]
                    key = alias.replace("-", "_")
                    events.append(result(uuid=uuid, since_last={key: delta}, **{key: tuple(cumulative)}))
        return self.lane(stem, *events, launch_alias=alias, minutes_ago=minutes_ago)

    def run_collect(self, environ=None, record=False, now=NOW, **kw):
        return su.collect(now, self.root, self.ledger, self.caps, environ if environ is not None else {},
                          record=record, **kw)


class Attribution(Sandbox):
    def test_upstream_id_lane_is_attributed_through_its_registered_route(self):
        # A REGISTERED Sarvam lane whose events print the upstream id: attributed through the manifest route.
        self.sarvam_lane("a", GLM, usage=(1_000_000, 0, 0))
        out = self.run_collect()
        self.assertEqual(out["summary"]["priced_lanes"], 1)
        self.assertAlmostEqual(out["summary"]["used_inr"], priced(GLM, 1_000_000), places=9)

    def test_foreign_provider_glm_lane_counts_zero_for_sarvam(self):
        # The CTO's control: a GLM lane routed via OpenCode. Its manifest alias is `glm-5.3` (NOT the registered
        # alias), its events print `glm-5.3`, and even a result whose modelUsage key is Sarvam's own upstream id
        # must count ZERO for Sarvam: no registered route, no attribution.
        upstream = su.ROUTES[GLM]["upstream"]  # "glm5.3": the id Sarvam's route maps, which must not help OpenCode
        foreign = upstream.replace("glm", "glm-")  # "glm-5.3": a lookalike foreign alias
        self.assertNotIn(foreign, su.ROUTES)
        events = [init(foreign), asst("m1", foreign, inp=9_000_000),
                  result(**{foreign: (9_000_000, 0, 9_000_000)}),
                  result(**{upstream: (9_000_000, 0, 9_000_000)})]  # bare upstream id in usage: still refused
        self.lane("a", *events, launch_alias=foreign)
        out = self.run_collect()
        self.assertEqual(out["summary"]["priced_lanes"], 0)
        self.assertEqual(out["summary"]["used_inr"], 0.0)
        self.assertEqual(out["foreign_usage_lanes"], 1)
        self.assertIn("no registered Sarvam route", out["line"])

    def test_foreign_lane_manifest_glmm53_style_bare_upstream_id_is_not_a_route(self):
        # The CTO's named failure: a lane manifest that logs the BARE upstream id (`glm5.3`) is a lane routed to
        # another provider as far as attribution can prove; its usage must not be counted as Sarvam.
        upstream = su.ROUTES[GLM]["upstream"]
        self.lane("a", init(upstream), asst("m1", upstream, inp=5_000_000),
                  result(**{upstream: (5_000_000, 0, 5_000_000)}), launch_alias=upstream)
        out = self.run_collect()
        self.assertEqual(out["summary"]["priced_lanes"], 0)
        self.assertEqual(out["summary"]["used_inr"], 0.0)
        self.assertEqual(out["unattributed_lanes"], 0)
        self.assertEqual(out["foreign_usage_lanes"], 1)  # loudly refused, never a silent zero

    def test_manifest_route_attributes_even_when_the_init_alias_is_missing(self):
        upstream = su.ROUTES[GLM]["upstream"]
        self.lane("a", asst("m1", upstream), result(**{upstream: (2_000_000, 0, 0)}), launch_alias=GLM)
        self.assertAlmostEqual(self.run_collect()["summary"]["used_inr"], priced(GLM, 2_000_000), places=9)

    def test_alias_lookalike_that_is_not_a_registered_route_is_not_counted(self):
        # `glm-5.3` (a different, non-Sarvam gateway alias) must never match upstream `glm5.3`.
        lookalike = su.ROUTES[GLM]["upstream"].replace("glm", "glm-")
        self.assertNotIn(lookalike, su.ROUTES)
        self.assertNotIn(lookalike, su.UPSTREAM_ALIAS)
        self.lane("a", init(lookalike), asst("m1", lookalike, inp=9_000_000), result(**{lookalike: (9_000_000, 0, 9_000_000)}),
                  launch_alias=lookalike)
        out = self.run_collect()
        self.assertEqual(out["summary"]["priced_lanes"], 0)
        self.assertEqual(out["unattributed_lanes"], 0)  # not a Sarvam lane at all, so nothing to flag either

    def test_other_models_and_text_mentions_are_not_counted(self):
        self.lane("a", init("claude-sonnet-5-5"),
                  asst("m1", "claude-sonnet-5-5", text=f'the "model":"{GLM}" alias', inp=9_000_000),
                  asst("m2", su.ROUTES[GLM]["upstream"], kind="user"),
                  result(**{"claude-sonnet-5-5": (9_000_000, 0, 9_000_000)}), launch_alias="claude-sonnet-5-5")
        self.assertEqual(self.run_collect()["summary"]["priced_lanes"], 0)

    def test_every_registered_route_resolves_by_alias_and_by_upstream_id(self):
        for alias, route in su.ROUTES.items():
            self.assertEqual(su.resolve_route(alias), alias)
            self.assertEqual(su.resolve_route(route["upstream"]), alias)
        self.assertIsNone(su.resolve_route("claude-sonnet-5-5"))
        self.assertIsNone(su.resolve_route(None))

    def test_scan_lane_route_comes_from_registered_aliases_only(self):
        # Structural: upstream ids never make a lane Sarvam; only a registered alias in the manifest or init does.
        # A self-named upstream (sarvam-105b) is the one legal overlap: it is itself a registered alias.
        for alias, route in su.ROUTES.items():
            self.assertEqual(su.scan_lane(alias, b"")["route"], alias)
            if route["upstream"] == alias:
                continue
            self.assertIsNone(su.scan_lane(route["upstream"], b"")["route"])

    def test_unregistered_sarvam_alias_is_flagged_unattributed_never_zero(self):
        self.lane("a", init("sarvam-newmodel-9"), asst("m1", "newmodel9", inp=1000),
                  result(**{"sarvam-newmodel-9": (5_000_000, 0, 5_000_000)}), launch_alias="sarvam-newmodel-9")
        out = self.run_collect()
        self.assertEqual(out["unattributed_lanes"], 1)
        self.assertTrue(out["line"].startswith("Sarvam TF: measured estimate UNAVAILABLE"), out["line"])
        self.assertIn("sarvam-newmodel-9", out["line"])
        self.assertNotIn("₹0.00", out["line"])


class Continuations(Sandbox):
    """The launcher appends continuation results to the same lane transcript; each result is one completion."""

    def test_three_completions_are_summed_not_maxed(self):
        # The CTO's live case: one lane, three result records. modelUsage is the running cumulative; the deltas are
        # each completion's own usage and must SUM to the final cumulative (per-field maxima would drop two of them).
        deltas = [(321395, 6850560, 19381), (2892, 415744, 375), (13098, 1404928, 949)]
        self.sarvam_lane("lane", completions=[(f"r{i}", d) for i, d in enumerate(deltas)])
        out = self.run_collect()
        want_in = sum(d[0] for d in deltas)
        want_cached = sum(d[1] for d in deltas)
        want_out = sum(d[2] for d in deltas)
        self.assertEqual(out["summary"]["priced_lanes"], 3)
        self.assertAlmostEqual(out["summary"]["used_inr"], priced(GLM, want_in, want_cached, want_out), places=6)

    def test_a_reinvoked_lane_whose_counter_resets_is_never_dropped_by_a_max(self):
        # A re-invocation RESETS the cumulative counter; per-field maxima would silently drop the second run's
        # smaller numbers. Per-completion deltas keep both invocations.
        first = (3_000_000, 0, 0)
        second = (1_000_000, 0, 0)  # smaller than first: a max-based merge would lose it
        events = [init(GLM), asst("m1", su.ROUTES[GLM]["upstream"]),
                  result(uuid="r0", since_last={GLM.replace("-", "_"): first}, **{GLM.replace("-", "_"): first}),
                  asst("m2", su.ROUTES[GLM]["upstream"]),
                  result(uuid="r1", since_last={GLM.replace("-", "_"): second}, **{GLM.replace("-", "_"): second})]
        self.lane("a", *events, launch_alias=GLM)
        self.assertAlmostEqual(self.run_collect()["summary"]["used_inr"], priced(GLM, 4_000_000), places=6)

    def test_duplicate_result_uuid_across_rescans_is_counted_once(self):
        # The same result uuid seen again (a re-scan past the watermark, or a repeated event) is the same row id:
        # merged by max, never summed twice.
        deltas = [(1_000_000, 0, 0), (2_000_000, 0, 0)]
        self.sarvam_lane("a", completions=[(f"r{i}", d) for i, d in enumerate(deltas)])
        first = self.run_collect(record=True)
        self.assertAlmostEqual(first["summary"]["used_inr"], priced(GLM, 3_000_000), places=6)
        again = self.run_collect()  # unchanged file, still inside the overlap window: re-read, same uuids
        self.assertAlmostEqual(again["summary"]["used_inr"], priced(GLM, 3_000_000), places=6)
        self.assertEqual(again["summary"]["priced_lanes"], 2)

    def test_two_lanes_with_the_same_result_uuid_stay_separate_rows(self):
        # Row ids are lane-stem + route + result uuid, so a uuid colliding across lanes does not merge them.
        for stem in ("laneA", "laneB"):
            self.sarvam_lane(stem, completions=[("same-uuid", (1_000_000, 0, 0))])
        out = self.run_collect()
        self.assertEqual(out["summary"]["priced_lanes"], 2)
        self.assertAlmostEqual(out["summary"]["used_inr"], priced(GLM, 2_000_000), places=6)

    def test_old_cumulative_only_results_are_merged_by_max_per_invocation(self):
        # Backward shape: results that carry ONLY cumulative modelUsage (no per-result delta). Successive results
        # of one invocation keep the max; a new invocation (new assistant id) starts a fresh row.
        self.lane("a", init(GLM),
                  asst("base-1", su.ROUTES[GLM]["upstream"]),
                  result(**{GLM.replace("-", "_"): (1_000_000, 0, 0)}),
                  result(**{GLM.replace("-", "_"): (2_000_000, 0, 0)}),  # same invocation, cumulative grew
                  asst("base-2", su.ROUTES[GLM]["upstream"]),
                  result(**{GLM.replace("-", "_"): (500_000, 0, 0)}),  # counter reset: a new invocation
                  launch_alias=GLM)
        out = self.run_collect()
        self.assertAlmostEqual(out["summary"]["used_inr"], priced(GLM, 2_500_000), places=6)
        self.assertEqual(out["summary"]["priced_lanes"], 2)

    def test_result_usage_from_a_foreign_lane_without_since_last_is_refused_too(self):
        upstream = su.ROUTES[GLM]["upstream"]
        foreign = upstream.replace("glm", "glm-")
        self.lane("a", init(foreign), asst("m1", foreign), result(**{upstream: (1_000_000, 0, 0)}),
                  launch_alias=foreign)
        out = self.run_collect()
        self.assertEqual(out["summary"]["used_inr"], 0.0)
        self.assertEqual(out["foreign_usage_lanes"], 1)


class CostMath(Sandbox):
    def test_separate_cached_input_rate_and_cache_writes_billed_as_input(self):
        self.lane("a", init(GLM), asst("m1", su.ROUTES[GLM]["upstream"]),
                  result(**{GLM.replace("-", "_"): (1_000_000, 2_000_000, 100_000, 500_000)}), launch_alias=GLM)
        got = self.run_collect()["summary"]["used_inr"]
        rate_in, rate_cached, rate_out = su.ROUTES[GLM]["price"]
        self.assertLess(rate_cached, rate_in)  # cached input really is priced separately
        want = (1_500_000 * rate_in + 2_000_000 * rate_cached + 100_000 * rate_out) / 1e6
        self.assertAlmostEqual(got, want, places=9)

    def test_every_priced_route_uses_its_own_row_and_unpriced_routes_are_never_costed(self):
        for i, alias in enumerate(sorted(su.ROUTES)):
            self.sarvam_lane(f"lane{i}", alias, usage=(1_000_000, 1_000_000, 1_000_000))
        out = self.run_collect()
        want = sum(sum(route["price"]) for route in su.ROUTES.values() if route["price"])
        self.assertAlmostEqual(out["summary"]["used_inr"], want, places=6)
        self.assertEqual(set(out["summary"]["unpriced"]), {a for a, r in su.ROUTES.items() if r["price"] is None})

    def test_assistant_message_usage_is_never_priced(self):
        # Per-message usage on Sarvam routes is a placeholder (prompt size, output 0): only result usage is trusted.
        self.sarvam_lane("a", with_result=False)
        out = self.run_collect()
        self.assertEqual(out["summary"]["used_inr"], 0.0)
        self.assertEqual(out["summary"]["priced_lanes"], 0)

    def test_result_usage_dedupes_on_result_uuid_not_summed_across_repeats(self):
        # The same completion (same result uuid, same delta) written twice in one file is one row: max merge.
        self.lane("a", init(GLM), asst("m1", su.ROUTES[GLM]["upstream"]),
                  result(uuid="dup", since_last={GLM.replace("-", "_"): (1_000_000, 0, 0)},
                         **{GLM.replace("-", "_"): (1_000_000, 0, 0)}),
                  result(uuid="dup", since_last={GLM.replace("-", "_"): (1_000_000, 0, 0)},
                         **{GLM.replace("-", "_"): (1_000_000, 0, 0)}),
                  launch_alias=GLM)
        self.assertAlmostEqual(self.run_collect()["summary"]["used_inr"], priced(GLM, 1_000_000), places=9)

    def test_previous_month_excluded(self):
        self.sarvam_lane("old", usage=(1_000_000, 0, 0), minutes_ago=60 * 24 * 30)  # 30 days back: October
        self.sarvam_lane("new", usage=(1_000_000, 0, 0), minutes_ago=10)
        self.assertAlmostEqual(self.run_collect()["summary"]["used_inr"], priced(GLM, 1_000_000), places=6)

    def test_burn_is_trailing_window_average(self):
        cheap = "sarvam-105b"
        self.sarvam_lane("in", cheap, usage=(3_000_000, 0, 0), minutes_ago=60)       # inside 3h
        self.sarvam_lane("out", cheap, usage=(9_000_000, 0, 0), minutes_ago=60 * 5)  # 5h ago: month only
        out = self.run_collect()
        self.assertAlmostEqual(out["summary"]["window_inr"], priced(cheap, 3_000_000), places=6)
        self.assertIn(f"burn ₹{priced(cheap, 3_000_000) / 3:.2f}/h (3h avg)", out["line"])


class Unavailable(Sandbox):
    def test_pending_lane_only_is_unavailable_not_zero(self):
        self.sarvam_lane("a", with_result=False)  # in flight, or killed before it wrote a result
        out = self.run_collect()
        self.assertFalse(out["available"])
        self.assertTrue(out["line"].startswith("Sarvam TF: measured estimate UNAVAILABLE"), out["line"])
        self.assertIn("without verified usage yet", out["line"])
        self.assertNotRegex(out["line"], r"₹\d+\.\d\d this month")

    def test_pending_lane_beside_a_verified_one_is_noted_and_never_costed(self):
        self.sarvam_lane("done", usage=(1_000_000, 0, 0))
        self.sarvam_lane("running", with_result=False)
        out = self.run_collect()
        self.assertAlmostEqual(out["summary"]["used_inr"], priced(GLM, 1_000_000), places=6)
        self.assertIn("1 lane on " + GLM + " without verified usage yet", out["line"])
        self.assertIn("burn n/a (in-flight lanes report usage when they end)", out["line"])

    def test_unpriced_route_alone_is_unavailable_and_named(self):
        unpriced = next(a for a, r in su.ROUTES.items() if r["price"] is None)
        self.sarvam_lane("k", unpriced, usage=(5_000_000, 0, 5_000_000))
        out = self.run_collect()
        self.assertFalse(out["available"])
        self.assertIn("measured estimate UNAVAILABLE", out["line"])
        self.assertIn(f"{unpriced} x1 lane", out["line"])
        self.assertEqual(out["summary"]["used_inr"], 0.0)

    def test_zero_sarvam_lanes_is_an_explicit_measured_zero_not_a_silent_one(self):
        self.lane("c", init("claude-sonnet-5-5"), asst("m1", "claude-sonnet-5-5"),
                  result(**{"claude-sonnet-5-5": (1000, 0, 100)}), launch_alias="claude-sonnet-5-5")
        out = self.run_collect()
        self.assertIn("₹0.00 this month (ex-GST; no Sarvam lane usage found in 1 transcripts scanned)", out["line"])

    def test_no_transcripts_and_no_ledger_is_unavailable_with_reason(self):
        self.root = self.root / "missing"
        out = self.run_collect()
        self.assertFalse(out["available"])
        self.assertTrue(out["line"].startswith("Sarvam TF: UNAVAILABLE (lane transcripts unreadable"))
        self.assertNotRegex(out["line"], r"₹\d")

    def test_empty_transcript_dir_and_no_ledger_is_unavailable_not_zero(self):
        out = self.run_collect()
        self.assertIn("measured estimate UNAVAILABLE (no lane transcripts in range and no ledger", out["line"])

    def test_failed_scan_on_stale_ledger_keeps_month_total_but_burn_is_unavailable(self):
        self.ledger.parent.mkdir(parents=True)
        old = NOW - timedelta(hours=10)
        row = {"v": 2, "id": "a:" + GLM, "ts": ts(60 * 11), "route": GLM, "in": 1_000_000, "cin": 0, "out": 0}
        self.ledger.write_text(json.dumps(row) + "\n" + json.dumps({"v": 2, "scan_at": old.timestamp()}) + "\n")
        self.root = self.root / "missing"
        out = self.run_collect()
        self.assertTrue(out["available"])
        self.assertIn("burn n/a (no lane-transcript scan in the last 3h)", out["line"])
        self.assertIn(f"₹{priced(GLM, 1_000_000):,.2f} this month", out["line"])

    def test_deadline_hit_marks_partial_and_burn_unavailable(self):
        self.sarvam_lane("a")
        out = self.run_collect(deadline_s=-1.0)
        self.assertIn("partial scan (deadline)", out["line"])

    def test_no_output_ever_claims_a_bound_or_floor(self):
        outputs = []
        self.sarvam_lane("a", usage=(1_000_000, 500_000, 1000))
        outputs.append(self.run_collect({su.ALLOWANCE_KEY: "500"})["line"])
        outputs.append(self.run_collect()["line"])
        self.sarvam_lane("p", with_result=False)
        outputs.append(self.run_collect({su.ALLOWANCE_KEY: "500"})["line"])
        for line in outputs:
            self.assertTrue(line.startswith("Sarvam TF: measured estimate"), line)
            for word in BANNED_WORDING:
                self.assertNotIn(word, line.lower(), line)
        self.assertNotIn("lower bound", su.__doc__.lower())

    def test_line_keeps_the_prefix_the_heartbeat_consumer_requires(self):
        consumer = HERE.parents[1] / "pm" / "heartbeat" / "heartbeat-compose.py"
        if not consumer.exists():
            self.skipTest("consumer not deployed beside the collector")
        self.assertIn('startswith("Sarvam TF:")', consumer.read_text())
        self.sarvam_lane("a")
        for line in (self.run_collect()["line"], self.run_collect(deadline_s=-1.0)["line"]):
            self.assertTrue(line.startswith("Sarvam TF:"), line)
            self.assertNotIn("\n", line)


class Allowance(Sandbox):
    """The provider balance is a separate figure: UNAVAILABLE until the operator sets an allowance."""

    def test_unset_allowance_leaves_provider_balance_unavailable_and_separate(self):
        self.sarvam_lane("a", usage=(1_000_000, 0, 0))
        line = self.run_collect()["line"]
        self.assertIn("provider balance UNAVAILABLE (no operator allowance set)", line)
        self.assertNotIn("₹500", line)
        self.assertIn(f"₹{priced(GLM, 1_000_000):,.2f} this month", line)  # the estimate itself is unaffected

    def test_env_allowance_is_labelled_an_allowance_not_a_provider_reading(self):
        self.sarvam_lane("a", usage=(1_000_000, 0, 0))
        line = self.run_collect({su.ALLOWANCE_KEY: "500"})["line"]
        left = 500 - priced(GLM, 1_000_000)
        self.assertIn(f"balance vs operator allowance ₹{left:,.2f} of ₹500.00", line)
        self.assertIn("not a provider reading", line)
        self.assertNotIn("provider balance UNAVAILABLE", line)

    def test_allowance_balance_is_unavailable_when_the_estimate_is(self):
        self.sarvam_lane("a", with_result=False)
        line = self.run_collect({su.ALLOWANCE_KEY: "500"})["line"]
        self.assertIn("balance vs operator allowance ₹500.00 UNAVAILABLE (measured estimate unavailable)", line)

    def test_caps_file_value_used_and_unterminated_last_line_ignored(self):
        self.sarvam_lane("a")  # the allowance balance is only computed against an available estimate
        self.caps.write_text(f"OTHER=1\n{su.ALLOWANCE_KEY}=250\n")
        self.assertIn("of ₹250.00", self.run_collect()["line"])
        self.caps.write_text(f"{su.ALLOWANCE_KEY}=250")  # no trailing newline: the launcher's read loop skips it too
        self.assertIn("provider balance UNAVAILABLE (no operator allowance set)", self.run_collect()["line"])

    def test_env_wins_over_caps_file(self):
        self.sarvam_lane("a")
        self.caps.write_text(f"{su.ALLOWANCE_KEY}=250\n")
        self.assertIn("of ₹900.00", self.run_collect({su.ALLOWANCE_KEY: "900"})["line"])

    def test_invalid_allowance_is_unavailable_with_reason(self):
        for bad in ("abc", "0", "-5", "1e3"):
            line = self.run_collect({su.ALLOWANCE_KEY: bad})["line"]
            self.assertIn("provider balance UNAVAILABLE (allowance invalid in env SARVAM_TF_ALLOWANCE_INR)", line, bad)

    def test_overspend_clamps_allowance_balance_at_zero(self):
        self.sarvam_lane("a", usage=(1_000_000, 0, 0))
        self.assertIn("balance vs operator allowance ₹0.00 of ₹100.00", self.run_collect({su.ALLOWANCE_KEY: "100"})["line"])


class Ledger(Sandbox):
    def ledger_rows(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]

    def test_read_only_run_writes_nothing(self):
        self.sarvam_lane("a")
        self.run_collect()
        self.assertFalse(self.ledger.exists())

    def test_record_makes_month_total_survive_transcript_loss_without_double_count(self):
        self.sarvam_lane("a", usage=(1_000_000, 0, 0))
        first = self.run_collect(record=True)
        self.assertEqual(first["recorded"], 1)
        for path in self.root.iterdir():
            path.unlink()
        gone = self.run_collect()
        self.assertAlmostEqual(gone["summary"]["used_inr"], priced(GLM, 1_000_000), places=6)
        # The same lane reappearing, recent enough to be re-read past the watermark, is the same row id.
        self.sarvam_lane("a", usage=(1_000_000, 0, 0), minutes_ago=1)
        again = self.run_collect(record=True)
        self.assertAlmostEqual(again["summary"]["used_inr"], priced(GLM, 1_000_000), places=6)

    def test_rows_and_watermark_carry_the_v2_schema(self):
        self.sarvam_lane("a")
        self.run_collect(record=True)
        rows = self.ledger_rows()
        self.assertTrue(rows and all(row["v"] == su.SCHEMA for row in rows))
        self.assertTrue(any("scan_at" in row for row in rows))
        usage_rows = [row for row in rows if "id" in row]
        self.assertEqual({row["route"] for row in usage_rows}, {GLM})
        self.assertTrue(all({"in", "cin", "out", "ts"} <= set(row) for row in usage_rows))

    def test_pending_lane_is_never_recorded(self):
        self.sarvam_lane("running", with_result=False)
        out = self.run_collect(record=True)
        self.assertEqual(out["recorded"], 0)
        self.assertEqual([row for row in self.ledger_rows() if "id" in row], [])

    def test_a_lane_that_gains_a_new_completion_between_scans_adds_only_the_new_row(self):
        # Scan 1 sees one completion; scan 2 re-reads the grown file: the first row dedupes by its result uuid and
        # only the second completion's row is new usage.
        self.sarvam_lane("a", completions=[("r0", (1_000_000, 0, 0))])
        self.run_collect(record=True)  # watermark = NOW; later scans only re-read files changed within its overlap
        self.sarvam_lane("a", completions=[("r0", (1_000_000, 0, 0)), ("r1", (2_000_000, 0, 0))], minutes_ago=1)
        out = self.run_collect(record=True)
        self.assertAlmostEqual(out["summary"]["used_inr"], priced(GLM, 3_000_000), places=6)
        self.assertEqual(out["new_rows"], 1)

    def test_unversioned_torn_or_foreign_ledger_lines_are_ignored(self):
        self.ledger.parent.mkdir(parents=True)
        legacy = {"id": "gw-1", "ts": ts(5), "model": GLM, "in": 1_000_000, "out": 0}  # old per-message row, no "v"
        self.ledger.write_text(json.dumps(legacy) + '\n{"id":"gw-2","ts"\n[1]\n')
        out = self.run_collect()
        self.assertEqual(out["summary"]["used_inr"], 0.0)
        self.assertEqual(out["summary"]["priced_lanes"], 0)

    def test_v2_watermark_limits_rescans_but_an_unversioned_one_forces_a_full_month_rescan(self):
        self.sarvam_lane("old", usage=(1_000_000, 0, 0), minutes_ago=60)
        self.ledger.parent.mkdir(parents=True)
        mark = NOW.timestamp() - 60  # scanned a minute ago
        self.ledger.write_text(json.dumps({"v": 2, "scan_at": mark}) + "\n")
        self.assertEqual(self.run_collect()["summary"]["priced_lanes"], 0)  # predates the watermark: not reread
        self.ledger.write_text(json.dumps({"scan_at": mark}) + "\n")
        self.assertEqual(self.run_collect()["summary"]["priced_lanes"], 1)  # unversioned watermark: rescan


class LauncherWiring(unittest.TestCase):
    """Structural, no hand-kept list (rule 60): every Sarvam alias the launcher can route to has a registered route."""

    @staticmethod
    def launcher_text():
        return (HERE.parents[1] / "proxy-agent.sh").read_text()

    def test_every_launcher_ring_alias_has_a_registered_priced_route(self):
        ring = re.search(r'^PROXY_AGENT_SARVAM_RING="([^"]+)"', self.launcher_text(), re.M)
        self.assertIsNotNone(ring, "launcher no longer defines PROXY_AGENT_SARVAM_RING")
        aliases = ring.group(1).split()
        self.assertTrue(aliases)
        for alias in aliases:
            self.assertIn(alias, su.ROUTES, f"{alias} is routable but unregistered: its usage would be unattributed")
            self.assertIsNotNone(su.ROUTES[alias]["price"], f"{alias} is routable but has no published price")

    def test_every_sarvam_alias_in_launcher_tiers_has_a_registered_route(self):
        ids = set()
        for tier in re.findall(r'\bmodels="([^"]+)"', self.launcher_text()):  # discovered from the tier lists
            ids |= {m for m in tier.split() if m.startswith(su.ALIAS_PREFIX)}
        self.assertTrue(ids, "no sarvam-* ids found in launcher tier lists")
        self.assertEqual({i for i in ids if i not in su.ROUTES}, set())
        # Provisioned Throughput (billed by capacity, not tokens) is the one allowed unpriced route, with that reason.
        self.assertLessEqual({a for a in ids if su.ROUTES[a]["price"] is None}, {"sarvam-kimi-k3"})

    def test_route_table_is_self_consistent(self):
        self.assertEqual(len(su.UPSTREAM_ALIAS), len(su.ROUTES), "two aliases share one upstream id")
        for alias, route in su.ROUTES.items():
            self.assertTrue(alias.startswith(su.ALIAS_PREFIX), alias)
            # An upstream id must not collide with a DIFFERENT alias (a self-named upstream like sarvam-105b is fine:
            # it still cannot route a foreign lane, because ROUTES keys are the only route source).
            self.assertNotIn(route["upstream"], {a for a in su.ROUTES if a != alias},
                            "an upstream id must not collide with another alias")
            price = route["price"]
            if price is not None:
                rate_in, rate_cached, rate_out = price
                self.assertTrue(0 <= rate_cached <= rate_in, f"{alias}: cached input must not cost more than input")
                self.assertGreater(rate_out, 0, alias)


if __name__ == "__main__":
    unittest.main(verbosity=1)
