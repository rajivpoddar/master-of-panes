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
        for kind in ("p0_escalation", "customer_incident", "backlog"):
            with self.subTest(kind=kind):
                self.assertIsNone(call(kind=kind, evidence={}, owner=None, required_action=None))

    def test_durable_continuation_is_typed_refused(self):
        self.assertEqual(call(kind="durable_continuation", evidence={"head": HEAD}),
                         "continuation_kind_not_reader_recognized")

    def test_owner_shape_is_mirrored_from_the_reader(self):
        for bad in ("a", "ab cd"):  # OPEN_PR_CONCRETE_TOKEN = ^[^\s]{2,}$
            with self.subTest(owner=bad):
                self.assertEqual(call(owner=bad), "continuation_owner_shape_invalid")

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
        # the reader checks fullmatch(OPEN_PR_CONCRETE_TOKEN) BEFORE the placeholder set, so an
        # empty/whitespace owner fails the shape rule and a placeholder WORD fails the placeholder rule.
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                self.assertEqual(call(owner=bad), "continuation_owner_shape_invalid")
        for bad in ("unknown", "NONE", "n/a", "cto-owned", "relay-only", "not-actionable"):
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

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile


def _run_writer(db, *args):
    env = dict(os.environ)
    env["PM_OPS_DB"] = db
    return subprocess.run([sys.executable, str(WRITER), *args], env=env,
                          capture_output=True, text=True)


def _rows(db):
    con = sqlite3.connect(db)
    try:
        return int(con.execute("SELECT COUNT(*) FROM obligations").fetchone()[0])
    finally:
        con.close()


class WriterRefusalEndToEnd(unittest.TestCase):
    """Disposable temp-DB proof: every refusal exits 2, writes zero rows, leaves no partial state."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pmops-contract-")
        self.db = os.path.join(self.dir, "pm-ops.db")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _case(self, kind, evidence, owner, action):
        before = _rows(self.db) if os.path.exists(self.db) else 0
        r = _run_writer(self.db, "obligation-upsert", "--kind", kind, "--pr", "7721",
                        "--owner", owner, "--title", "t", "--action", action,
                        "--evidence-json", evidence)
        after = _rows(self.db) if os.path.exists(self.db) else 0
        self.assertEqual(r.returncode, 2, f"{kind}/{evidence}: expected exit 2, got {r.returncode}: {r.stderr}")
        self.assertIn("REFUSED:", r.stderr, "refusal must be typed on stderr")
        self.assertEqual(after, before, "a refusal must write zero rows")

    def test_all_refusals_are_zero_write(self):
        good = ("pm", "Continue from the exact head and verify CI/E2E.")
        cases = [
            ("durable_continuation", '{"head": "%s"}' % HEAD, *good),          # 1
            ("capture_recovery", "{}", *good),                                  # absent head
            ("capture_recovery", '{"head": "%s"}' % ("a" * 39), *good),         # partial head
            ("capture_recovery", '{"head": "%s", "head_sha": "%s"}' % (HEAD, "b" * 40), *good),  # conflict
            ("capture_recovery", '{"head": "%s"}' % HEAD, "a", good[1]),        # owner shape
            ("capture_recovery", '{"head": "%s"}' % HEAD, "ab cd", good[1]),    # owner shape
            ("capture_recovery", '{"head": "%s"}' % HEAD, "unknown", good[1]),  # owner placeholder
            ("capture_recovery", '{"head": "%s"}' % HEAD, good[0], "unknown"),  # action placeholder
        ]
        for kind, evidence, owner, action in cases:
            with self.subTest(kind=kind, evidence=evidence, owner=owner):
                self._case(kind, evidence, owner, action)

    def test_valid_continuation_writes_one_row(self):
        r = _run_writer(self.db, "obligation-upsert", "--kind", "capture_recovery", "--pr", "7721",
                        "--owner", "pm", "--title", "t",
                        "--action", "Continue from the exact head and verify CI/E2E.",
                        "--evidence-json", '{"head": "%s"}' % HEAD, "--print-id")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_rows(self.db), 1)

    def test_ordinary_non_continuation_kind_still_writes(self):
        r = _run_writer(self.db, "obligation-upsert", "--kind", "cleanup_pr", "--pr", "7721",
                        "--owner", "pm", "--title", "t", "--action", "seed", "--evidence-json", "{}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_rows(self.db), 1)
