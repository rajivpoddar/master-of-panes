"""PM-ops writer contract: malformed durable continuations are refused at the writer.

The reader (sakshi-heartbeat.py) only accepts an exact-head continuation when the kind is in
CONTINUATION_KIND_LANES, evidence carries exactly one accepted head key with a lowercase 40-hex
value, and owner/required_action are concrete. The writer must refuse anything else BEFORE any DB
write, so a malformed row cannot be created in the first place.
"""
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WRITER = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "pm-ops-legacy.py"
READER = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "pm" / "control-plane" / "sakshi-heartbeat.py"

spec = importlib.util.spec_from_file_location("pm_ops_legacy_under_test", WRITER)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

HEAD = "a" * 40
GOOD_OWNER = "pm"
GOOD_ACTION = "Continue from the exact head and verify CI/E2E."


def call(**kw):
    base = dict(kind="capture_recovery", evidence={"head": HEAD}, owner=GOOD_OWNER,
                required_action=GOOD_ACTION)
    base.update(kw)
    return w.continuation_upsert_refusal(**base)


class WriterContract(unittest.TestCase):
    # ---- happy path and non-continuation control ----
    def test_valid_continuation_succeeds(self):
        self.assertIsNone(call())

    def test_non_continuation_kind_is_untouched(self):
        for kind in ("p0_escalation", "customer_incident", "backlog", "durable_continuation"):
            with self.subTest(kind=kind):
                self.assertIsNone(call(kind=kind, evidence={}, owner=None, required_action=None))

    # ---- head rules ----
    def test_absent_head_refuses(self):
        self.assertEqual(call(evidence={}), "continuation_head_missing")

    def test_partial_head_refuses(self):
        for bad in ("a" * 39, "a" * 41, HEAD.upper(), "g" * 40, "", None, 123):
            with self.subTest(bad=bad):
                self.assertEqual(call(evidence={"head": bad}), "continuation_head_malformed")

    def test_conflicting_accepted_keys_refuse(self):
        self.assertEqual(call(evidence={"head": HEAD, "head_sha": "b" * 40}),
                         "continuation_head_conflicting")

    def test_duplicate_identical_accepted_keys_are_allowed(self):
        self.assertIsNone(call(evidence={"head": HEAD, "current_head": HEAD}))

    # ---- owner / action rules ----
    def test_placeholder_owner_refuses(self):
        for bad in ("", "   ", "unknown", "NONE", "n/a", "cto-owned", "relay-only", "not-actionable"):
            with self.subTest(bad=bad):
                self.assertEqual(call(owner=bad), "continuation_owner_placeholder")

    def test_placeholder_action_refuses(self):
        for bad in ("", "  ", "unknown", "None", "N/A", "cto-owned", "relay-only", "not-actionable"):
            with self.subTest(bad=bad):
                self.assertEqual(call(required_action=bad), "continuation_action_placeholder")

    # ---- writer/reader synchronization (contract check against the reader source) ----
    def test_constants_match_the_reader_source(self):
        src = READER.read_text()
        kinds = set(re.findall(r'"([a-z_]+)": "[^"]+"', src.split("CONTINUATION_KIND_LANES")[1].split("}")[0]))
        self.assertEqual(kinds, set(w.CONTINUATION_KIND_LANES), "kind predicate drifted from the reader")
        keys = tuple(re.findall(r'"([A-Za-z_]+)"', src.split("CONTINUATION_HEAD_KEYS")[1].split(")")[0]))
        self.assertEqual(keys, w.CONTINUATION_HEAD_KEYS, "head-key list drifted from the reader")
        self.assertIn("OPEN_PR_HEAD = re.compile(r\"^[0-9a-f]{40}$\")", src)
        for token in ("unknown", "none", "n/a", "cto-owned", "relay-only", "not-actionable"):
            self.assertIn(f'"{token}"', src, f"placeholder {token} missing from the reader")
        self.assertEqual(sorted(w.CONTINUATION_PLACEHOLDERS),
                         sorted(["unknown", "none", "n/a", "cto-owned", "relay-only", "not-actionable"]))
        self.assertNotIn("durable_continuation", w.CONTINUATION_KIND_LANES,
                         "durable_continuation must NOT be blessed")

    def test_validation_precedes_any_db_write(self):
        src = WRITER.read_text()
        body = src.split("def upsert_obligation(")[1].split("\ndef ")[0]
        # locate the real STATEMENT, not the comment that mentions it
        db_stmt = re.search(r"^\s+init_db\(\)\s*$", body, re.M).start()
        validate_call = re.search(r"^\s*_refusal = continuation_upsert_refusal", body, re.M).start()
        self.assertLess(validate_call, db_stmt,
                        "validation must run before init_db()/connect()")


if __name__ == "__main__":
    unittest.main()
